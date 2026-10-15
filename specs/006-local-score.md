# 006: ローカルスコアと重複話題の除去

- 状態: 承認済み
- 関連: SPEC.md §3（関心テーマと優先度）、§4 の 4（ローカルスコア）、§7（重複排除の方針 3、`articles` の列）、005

## 目的
LLM を使わずに、事前の絞り込み（005）を通った記事を採点して並べ、タイトルが似た別ソースの同一話題を除く。
ここで並べた上位の記事だけが、LLM の再ランク（008）に進む。

## 範囲
- 含む:
  - `topics.yaml`（テーマ別の重みとキーワード、採点のパラメータ）の形式・読み込み・検証
  - ローカルスコアの計算と、どのテーマに当てはまったか（`matched_topics`）
  - スコアの下限による足切り
  - タイトルの文字 n-gram の類似度による重複話題の除去
  - `articles` への記録（`local_score`、`matched_topics`、`status`）
- 含まない:
  - LLM の再ランクとスコアの混合：008
  - セクションへの振り分けと枠配分：009（`matched_topics` を使う）
  - 人気の指標（HN のポイント、Qiita のストック、Zenn のいいね）による加点：取得時の足切りに使っており、採点には使わない（MVP の範囲外）

## topics.yaml の形式
```yaml
scoring:
  priority_bonus: {1: 2.0, 2: 1.0, 3: 0.0}   # ソースの priority ごとの加点
  high_points: 2.0           # high キーワード 1 語あたり
  medium_points: 1.0         # medium キーワード 1 語あたり
  negative_points: 3.0       # negative キーワード 1 語あたりの減点
  highlight_points: 1.0      # ソースの highlight_keywords 1 語あたり
  content_factor: 0.25       # 本文だけに現れた語の点数の倍率（タイトルに現れた語は 1.0）
  max_hits: 3                # 1 つのテーマ・種類で数える語数の上限
  min_score: 1.0             # これ未満の記事は外す
  similarity_threshold: 0.45 # タイトルの類似度がこれ以上なら同一話題とみなす
  ngram: 2                   # 類似度に使う文字 n-gram の n

topics:
  - id: claude_code
    weight: 3.0
    high: [...]
    medium: [...]
  - id: llm
    weight: 2.0
    high: [...]
    medium: [...]

negative: [...]               # 全テーマ共通の減点キーワード
```
- `topics` の `id` は空でなく一意。`weight` は 0 より大きい数。キーワードは空でない文字列のリスト（`medium` と `negative` は省略可、省略時は空）。
- `scoring` の制約：`priority_bonus` は priority 1・2・3 のすべてについて数値を持つ。`high_points`・`medium_points`・`negative_points`・`highlight_points` は 0 以上。`content_factor` は 0 以上 1 以下。`max_hits` と `ngram` は 1 以上の整数。`min_score` は任意の数。`similarity_threshold` は 0 より大きく 1 以下。すべて必須。
- 不正なら `ConfigError` を送出する（採点できないため、続行しない）。
- ファイルのパスは設定値 `TOPICS_PATH`（既定 `topics.yaml`）。

## 振る舞い

### キーワードの照合
- 005 と同じく、NFKC 正規化と `casefold` をしてから部分一致で比べる。
- タイトルに現れた語は倍率 1.0、タイトルには無く本文（HTML タグを除いた先頭 1,000 文字）だけに現れた語は倍率 `content_factor`。
- 同じ語は 1 回だけ数える。テーマ・種類（high / medium）ごとに、倍率の大きい順に最大 `max_hits` 語まで数える。

### ローカルスコア
```
テーマの点  = weight × (high_points × high の語の倍率の合計 + medium_points × medium の語の倍率の合計)
ローカルスコア = Σ テーマの点
              + priority_bonus[ソースの priority]
              + highlight_points × highlight_keywords の語の倍率の合計（最大 max_hits 語）
              − negative_points × negative の語の倍率の合計（最大 max_hits 語）
```
- `matched_topics` は、テーマの点が 0 より大きいテーマの `id` を、点の大きい順に並べたリスト。

### 足切りと重複話題の除去
1. ローカルスコアが `min_score` 未満の記事を外す。
2. 残りをローカルスコアの高い順に並べる（同点なら公開日時の新しい順、それも同じなら入力の順。公開日時の無い記事は最も古いものとして扱う）。
3. 先頭から順に、すでに残した記事のどれかとタイトルの類似度が `similarity_threshold` 以上なら外す。
   - 類似度は、タイトルを NFKC 正規化・`casefold` し、空白と記号を除いた文字列の、文字 `ngram`-gram の集合の Jaccard 係数
   - スコアの高い記事が残る（SPEC §7 の方針 3）

### 記録
- 採点したすべての記事の `local_score` と `matched_topics`（JSON）を記録する。
- 残した記事は `status = scored`、外した記事（足切り・重複）は `status = skipped` にする。
- コミットはしない（003、005 と同じ）。
- 足切りと重複で外した件数を INFO ログに、重複で外した記事と残した記事の組を DEBUG ログに出す。

### 出力
- 残した記事を、ローカルスコアの高い順に返す。各記事は `NewArticle`、ローカルスコア、`matched_topics` を持つ。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| `topics.yaml` が無い、YAML として不正、形式が不正 | `ConfigError` |
| 記事のソースが設定に無い | `priority_bonus` と `highlight_keywords` は 0 として採点し、WARNING ログ |
| タイトルが記号だけで n-gram が作れない | 類似度は 0 とする（重複とはみなさない） |
| DB の操作で例外が起きた | 例外をそのまま呼び出し側に渡す |

## 受け入れ基準
### topics.yaml
- **AC-006-1**: Given 正しい `topics.yaml` / When 読み込む / Then `scoring` の値、テーマ（`id`・`weight`・`high`・`medium`）、共通の `negative` がそのまま読み込まれる。`medium` と `negative` を省略すると空のリストになる
- **AC-006-2**: Given ファイルが無い、YAML として不正、または制約を満たさない `topics.yaml`（テーマの `id` の重複、`weight` が 0、空の文字列のキーワード、`priority_bonus` に priority 3 が無い、`similarity_threshold` が 0 や 1.5、`ngram` が 0、`content_factor` が 1.5、`scoring` の項目が無い） / When 読み込む / Then `ConfigError` が送出される
- **AC-006-3**: Given `TOPICS_PATH` を設定していない / When `Settings` を作る / Then `topics_path` は `topics.yaml` になる。`.env.example` に載っている（AC-001-13 のテストで確認する）
- **AC-006-4**: Given リポジトリの `topics.yaml` / When 読み込む / Then エラーにならない

### 採点
- **AC-006-5**: Given high キーワード `hooks` がタイトルにある記事、本文にだけある記事、タイトルと本文の両方にある記事 / When 採点する / Then 語の倍率はそれぞれ 1.0、`content_factor`、1.0（同じ語は 1 回だけ数える）になる
- **AC-006-6**: Given 1 つのテーマの high キーワードを、タイトルに 2 語・本文だけに 3 語含む記事と `max_hits: 3` / When 採点する / Then 倍率の大きい順に 3 語（1.0 + 1.0 + `content_factor`）だけが数えられる
- **AC-006-7**: Given 次の設定と記事 / When 採点する / Then ローカルスコアは計算式どおりの値になる
  - 設定：テーマ A（`weight` 3、high `[hooks]`、medium `[MCP]`）、テーマ B（`weight` 2、high `[LLM]`）、`negative` `[hiring]`、`high_points` 2、`medium_points` 1、`negative_points` 3、`highlight_points` 1、`content_factor` 0.25、`priority_bonus` `{1: 2, 2: 1, 3: 0}`
  - 記事：priority 1 のソース（`highlight_keywords` `[tips]`）、タイトル「hooks and MCP tips」、本文「LLM hiring」
  - 期待値：A = 3 × (2 × 1.0 + 1 × 1.0) = 9、B = 2 × (2 × 0.25) = 1、priority 2、highlight 1 × 1.0 = 1、negative 3 × 0.25 = 0.75 → 合計 12.25
- **AC-006-8**: Given AC-006-7 の記事 / When 採点する / Then `matched_topics` は `["A", "B"]`（点の大きい順）。どのテーマのキーワードも含まない記事では空のリストになる
- **AC-006-9**: Given キーワード `claude` と、タイトルが全角の `Ｃｌａｕｄｅ` の記事、本文の HTML タグの中にだけキーワードがある記事、本文の 1,000 文字目より後ろにだけキーワードがある記事 / When 採点する / Then 1 つ目だけが一致する（照合の方法は 005 と同じ）
- **AC-006-10**: Given 設定に無いソースの記事 / When 採点する / Then `priority_bonus` と `highlight_keywords` を 0 として採点され、WARNING ログが出る

### 足切りと重複話題の除去
- **AC-006-11**: Given `min_score` と、ローカルスコアがそれ未満・ちょうど同じ・それより上の記事 / When 採点する / Then 未満の記事だけが外れる
- **AC-006-12**: Given ローカルスコアが同じで公開日時の違う記事、公開日時の無い記事、スコアも公開日時も同じ記事 / When 採点する / Then スコアの高い順、同点は公開日時の新しい順（公開日時の無い記事は最後）、それも同じなら入力の順に並ぶ
- **AC-006-13**: Given 同じタイトルの記事は類似度 1.0、共通する文字 n-gram の無いタイトルは 0.0、`ab` と `abc`（2-gram）は 0.5 / When 類似度を計算する / Then それぞれその値になる。記号と空白は無視され、全角と半角、大文字と小文字は同一視される
- **AC-006-14**: Given 類似度がしきい値以上の 2 件（スコアの違う別ソースの記事）と、しきい値未満の 2 件 / When 採点する / Then しきい値以上の組はスコアの高い記事だけが残り、しきい値未満の組は両方残る
- **AC-006-15**: Given タイトルが記号だけの記事 2 件 / When 採点する / Then 類似度は 0 として扱われ、両方残る

### 記録と出力
- **AC-006-16**: Given 残る記事・足切りされる記事・重複で外れる記事 / When 採点する / Then 3 件とも `local_score` と `matched_topics`（JSON の配列）が記録され、残った記事は `status = scored`、外れた記事は `status = skipped` になる。呼び出し側がロールバックすると元に戻る（コミットしない）
- **AC-006-17**: Given 複数の記事 / When 採点する / Then 残った記事がローカルスコアの高い順に、ローカルスコアと `matched_topics` 付きで返り、残った件数・足切りの件数・重複の件数が INFO ログに出る

## 実装方針（参考）
- `topics.yaml` の読み込み：`src/aidigest/config.py` に `load_topics()` を追加（pydantic で検証）
- 採点：`src/aidigest/pipeline/scoring.py`。キーワードの正規化・照合の部品は 005（`prefilter.py`）と共通にする
- 実データでの調整には、scratchpad の使い捨てスクリプトを使った（本番コードではない）

## 決定事項
1. **キーワードとパラメータの初期値**：リポジトリの `topics.yaml` のとおり。2026-10-16 の実データ（事前の絞り込み後 54 件）で並び順を確認して調整した（本文だけの語の倍率を 0.5 → 0.25 に下げ、`Skill`・`ハーネス`・`コードレビュー`・`AI` などを追加）。
2. **重複除去のしきい値は 0.45**：同じ話題の言い換えは 0.35〜0.57、同じ製品の別の話題は 0.14〜0.44 で重なる。別の記事を誤って消すほうが損失が大きいため、誤って消さない値にする。見逃した重複は LLM の再ランクと枠配分で目立たなくなる。
3. **減点キーワードは全テーマ共通の `negative` とする**：ゴシップ系の語（逮捕、売上など）はテーマに関係なく減点したいため。SPEC §4 の「テーマ別の negative」をこの形で実現する。
4. **HN の記事が低めになることは許容する**：英語の短いタイトルは点が付きにくく、2026-10-16 の実データでは HN の 3 件が 37〜40 位だった。優先度を上げる対策はしない。
