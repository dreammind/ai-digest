# 009: セクションへの振り分けと枠配分

- 状態: 承認済み
- 関連: SPEC.md §4 の 6（枠配分）、§8（配信の構成、枠配分）、§13（セクション別の上限を超えない）、006（`matched_topics`）、008（`final_score`）

## 目的
再ランク（008）の結果から、配信する記事を最大 N 件（初期値 8）選ぶ。
各記事をメールのセクションに振り分け、セクションのグループごとの上限を超えないようにする。Claude Code を最優先にするため、均等ではなく上限方式にする（SPEC §8）。

## 範囲
- 含む:
  - 記事のセクションへの振り分け
  - 上限を守りながら、`final_score` の順に N 件を選ぶ
  - 選んだ記事の `status` を `selected` にする
  - 設定（`topics.yaml` の `digest`）の追加（006 の `TopicsConfig` に足す）
- 含まない:
  - メールの組み立て・見出しの文言：011
  - 要約：010（選んだ記事だけを要約する）

## セクション
SPEC §8 の構成に合わせて、4 つのセクションを、この順に並べる。

| セクション | 見出し（011 で使う） | グループ |
|---|---|---|
| `claude_code` | 今日の最重要（Claude Code 手法） | `claude_code` |
| `claude_code_update` | Claude Code 更新情報 | `claude_code` |
| `llm` | LLM 動向 | `llm` |
| `other` | その他 | `other` |

### 振り分けのルール
1. 記事のグループは、`matched_topics` の先頭（点のいちばん大きいテーマ）の `id` とする。ただし、その `id` が `digest.limits` に無い、または `matched_topics` が空なら `other` とする。
2. グループが `claude_code` の記事は、タイトルに `digest.update_keywords` のいずれかを含めば `claude_code_update`、含まなければ `claude_code` セクションにする（照合は 005 と同じく、NFKC 正規化と `casefold` をしてからの部分一致）。
3. それ以外は、グループ名をそのままセクション名にする。

## topics.yaml の追加（006 の変更）
```yaml
digest:
  size: 8                       # 配信する最大件数 N
  limits:                       # グループごとの上限（other は必須）
    claude_code: 4              # 手法と更新情報の合計
    llm: 3
    other: 2
  update_keywords: [リリース, 新機能, アップデート, release, changelog]
```
- `size` と各上限は 1 以上の整数。`limits` のキーは `claude_code`・`llm`・`other` のいずれか（セクションが固定のため）で、`other` は必須。満たさなければ `ConfigError`。`update_keywords` は空でない文字列のリスト（省略時は空）。
- `digest` を省略したら、上の値を既定値とする。

## 振る舞い
1. 入力（`final_score` の高い順）を先頭から見ていく。
2. 選んだ件数が `size` 未満で、その記事のグループの件数が上限未満なら選ぶ。そうでなければ飛ばす。
3. 最後まで見たら終わる（候補が足りなければ `size` 件未満になる）。
   - これで「上限を超えない範囲で、空いた枠は全体のスコアの順に埋める」（SPEC §8）を満たす
4. 選んだ記事の `status` を `selected` にする。選ばなかった記事の `status` は変えない（`scored` のまま）。コミットはしない。
5. 出力は、セクションの順（上の表の順）、同じセクションの中は `final_score` の高い順に並べる。
6. 各グループの選んだ件数と上限を INFO ログに出す。

## 出力
- 選んだ記事のリスト。各記事は 008 の `RankedArticle` と、セクション名を持つ。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| `digest` の値が不正（`other` の上限が無い、上限が 0 など） | `ConfigError` |
| 入力が 0 件 | 空のリストを返す |
| DB の操作で例外 | 例外をそのまま呼び出し側に渡す |

## 受け入れ基準
### 設定
- **AC-009-1**: Given `digest` を書いた `topics.yaml` / When 読み込む / Then `size`・`limits`・`update_keywords` が読み込まれる。`digest` を省略すると既定値（8、`claude_code` 4・`llm` 3・`other` 2、更新キーワードは上の 5 語）になる
- **AC-009-2**: Given `limits` に `other` が無い、`limits` に未知のキー（例：`paper`）がある、`size` が 0、上限が 0、空の文字列の更新キーワード / When 読み込む / Then `ConfigError` が送出される
- **AC-009-3**: Given リポジトリの `topics.yaml` / When 読み込む / Then `digest` が上の値で読み込まれる

### 振り分け
- **AC-009-4**: Given `matched_topics` が `["claude_code", "llm"]`・`["llm"]`・`[]`・`["paper"]`（`limits` に無いテーマ）の記事 / When 振り分ける / Then セクションはそれぞれ `claude_code`・`llm`・`other`・`other` になる
- **AC-009-5**: Given グループが `claude_code` で、タイトルに「新機能」を含む記事、`RELEASE` を含む記事、どちらも含まない記事 / When 振り分ける / Then 前の 2 件は `claude_code_update`、最後の 1 件は `claude_code` になる

### 枠配分
- **AC-009-6**: Given `final_score` の高い順に、`claude_code` グループ 6 件、`llm` 4 件、`other` 3 件が交ざった入力と、既定の設定 / When 選ぶ / Then 選ばれるのは最大 8 件で、`claude_code` グループ（手法 + 更新情報）は 4 件以下、`llm` は 3 件以下、`other` は 2 件以下になる
- **AC-009-7**: Given 上限に達したグループの記事より `final_score` の低い、別グループの記事 / When 選ぶ / Then 上限に達したグループの記事は飛ばされ、別グループの記事で枠が埋まる（全体のスコアの順に埋まる）
- **AC-009-8**: Given 候補が 5 件だけ / When 選ぶ / Then 上限の範囲で選べるものだけが選ばれ、8 件未満になる
- **AC-009-9**: Given 入力が 0 件 / When 選ぶ / Then 空のリストを返す

### 記録と出力
- **AC-009-10**: Given 選ばれる記事と選ばれない記事 / When 選ぶ / Then 選ばれた記事は `status = selected`、選ばれない記事は `scored` のまま。呼び出し側がロールバックすると元に戻る
- **AC-009-11**: Given 選ばれた記事 / When 出力を見る / Then セクションの順（`claude_code`・`claude_code_update`・`llm`・`other`）、同じセクションの中は `final_score` の高い順に並び、各グループの件数が INFO ログに出る

## 実装方針（参考）
- モジュール：`src/aidigest/pipeline/allocation.py`、設定は `config.py` の `TopicsConfig` に `digest` を追加
- 更新キーワードの照合は `pipeline/text.py` の `normalize` を使う

## 決定事項
1. **グループの決め方**：`matched_topics` の先頭（いちばん点の高いテーマ）にした。Claude Code の記事は「Claude」で `llm` テーマにも点が付くが、`claude_code` テーマは重みが大きい（3 対 2）ので、通常は `claude_code` が先頭になる。
2. **手法と更新情報の分け方**：タイトルのキーワード（リリース、新機能、アップデート、release、changelog）で分ける。GitHub Releases を使わないことにしたため、更新情報は Zenn や HN の「新機能を試した」記事などになる。手法と更新情報は同じ上限（合わせて 4 件）を共有するので、分け方は見出しにだけ影響する。
3. **スコアの下限は設けない**：候補が少ない日は、`final_score` の低い記事も選ばれうる。再ランクで明らかに関係の無い記事は低くなり、上位 30 件には入りにくいので、まずは下限なしで運用して様子を見る。
4. **設定の置き場所**：`topics.yaml` に `digest` として置く。テーマとセクションの対応が `limits` のキー（テーマの `id`）でつながるため、同じファイルにある方が分かりやすい。
