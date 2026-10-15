# 001: 設定の読み込み（sources.yaml / .env）

- 状態: 実装済み
- 関連: SPEC.md §5（ソース設定）、§6（予算・単価）、§9（実行環境）、§10（外部境界は pydantic で検証）

## 目的
アプリの設定を読み込み、pydantic で検証して、型の付いたオブジェクトとして他の機能に渡す。
設定の誤りでアプリ全体が止まらないようにする。ただし、誤りはログに残す。

## 範囲
- 含む:
  - `sources.yaml` の読み込みと検証（SPEC §5「ソース設定」の全項目）
  - `.env` / 環境変数の読み込みと検証（秘密情報、`sources.yaml` のパス）
- 含まない:
  - `topics.yaml`：構造がスコア式と一体なので、006（ローカルスコア）の仕様で定める
  - 予算・単価表・枠配分・時間窓などの設定値：使う機能の仕様（007、009 など）で、設定クラスに項目を追加する
  - キーワードの照合方法（大文字小文字の区別など）：005（事前絞込）で定める
  - 秘密情報が無いときの縮退動作：LLM は 007、SMTP は 012 で扱う
  - 設定エラーの通知（メールやフッター）：013 で扱う

## 入出力
- 入力:
  - `sources.yaml`（パスは設定値 `SOURCES_PATH`。既定値は `sources.yaml`）
  - `.env` と環境変数
- 出力:
  - `Settings`：`.env` / 環境変数から作る設定オブジェクト
  - `LoadedSources`：検証を通ったソースの一覧（`sources`）と、除外したエントリの一覧（`errors`）。除外したエントリは、位置・id（分かれば）・理由を持つ

### sources.yaml の形式
```yaml
sources:
  - id: claude-code-releases
    type: github_release
    repo: anthropics/claude-code
    category: official
    priority: 1
    max_items: 20
    recheck_changed: true
  - id: hn-claude-code
    type: hn_search
    query: ["Claude Code", "Claude Agent SDK"]
    query_operator: or
    category: community
    priority: 2
    max_items: 10
    min_score: 50
    highlight_keywords: ["hooks", "MCP"]
```

| 項目 | 型・制約 | 必須 | 既定値 |
|---|---|---|---|
| `id` | 空でない文字列。ファイル内で一意 | 必須 | — |
| `type` | `rss` / `github_release` / `hn_search` / `arxiv` / `qiita_search` / `zenn_topic` | 必須 | — |
| `url` | http / https の URL | `rss` のとき必須 | — |
| `repo` | `owner/name` 形式 | `github_release` のとき必須 | — |
| `query` | 空でない文字列、または空でない文字列のリスト（1 件以上）。リストの各要素を 1 つの検索語（フレーズ）として扱う。文字列 1 つは、要素 1 つのリストと同じ意味 | `hn_search` / `arxiv` のとき必須 | — |
| `query`（`qiita_search`） | 空でない文字列。Qiita の検索構文をそのまま書く（例：`tag:ClaudeCode`）。リストにはしない | `qiita_search` のとき必須 | — |
| `topic` | 空でない文字列（Zenn のトピック名。例：`claudecode`） | `zenn_topic` のとき必須 | — |
| `query_operator` | `and`（すべての検索語を含む）/ `or`（いずれかの検索語を含む）。`hn_search` / `arxiv` 以外では指定できない | 任意 | `and` |
| `category` | `official` / `community` / `paper` / `japanese` | 必須 | — |
| `priority` | 1〜3 の整数 | 必須 | — |
| `max_items` | 1 以上の整数 | 必須 | — |
| `min_score` | 0 以上の整数。`hn_search` 以外では指定できない | 任意 | なし（フィルタしない） |
| `min_stocks` | 0 以上の整数。`qiita_search` 以外では指定できない | 任意 | `3` |
| `lookback_days` | 1 以上の整数。`qiita_search` / `zenn_topic` 以外では指定できない | 任意 | `7` |
| `min_likes` | 0 以上の整数。`zenn_topic` 以外では指定できない | 任意 | `5` |
| `pages` | 1 以上の整数。`zenn_topic` 以外では指定できない | 任意 | `2` |
| `require_keywords` / `hide_keywords` / `highlight_keywords` | 文字列のリスト | 任意 | 空のリスト |
| `recheck_changed` | 真偽値 | 任意 | `false` |

- `query` は読み込み時にリストへそろえる（文字列 1 つなら要素 1 つのリストにする）。
- AND / OR を実際の検索でどう実現するか（API の検索構文にするか、検索語ごとに呼んで結果を合わせるか）は、004（フェッチャー）で定める。
- `type` で使わない取得先の項目（例：`rss` に `repo`）や、上の表にない項目（タイプミスなど）があるエントリは誤りとする。

### .env / 環境変数
| 変数 | 型 | 必須 | 既定値 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | 秘密の文字列 | 任意 | なし |
| `SMTP_HOST` | 文字列 | 任意 | なし |
| `SMTP_PORT` | 1〜65535 の整数 | 任意 | なし |
| `SMTP_USER` | 文字列 | 任意 | なし |
| `SMTP_PASSWORD` | 秘密の文字列 | 任意 | なし |
| `MAIL_FROM` / `MAIL_TO` | 文字列 | 任意 | なし |
| `SOURCES_PATH` | パス | 任意 | `sources.yaml` |

- 値が空の変数（例：`SMTP_HOST=`）は、設定されていないものとして扱う（`.env.example` をそのままコピーしても動くようにするため）。
- 値が無くても読み込みは失敗させない。無いときにどう動くかは、使う側の機能で決める（SPEC §6 の縮退動作：API キーに不備があってもタイトルのみで配信する）。
- 環境変数と `.env` の両方に同じ値があれば、環境変数を優先する。

## 振る舞い
1. `.env` と環境変数から `Settings` を作る。
2. `SOURCES_PATH` の YAML を読み、`sources` の各エントリを検証する。
3. 検証を通ったエントリは `sources` に入れ、ファイルに書かれた順を保つ。
4. 検証に失敗したエントリは除外し、`errors` に記録したうえで ERROR ログに出す。ほかのエントリの読み込みは続ける。
5. 同じ `id` が複数あれば、最初のエントリを残し、後のエントリを誤りとして除外する。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| 1 エントリの検証に失敗 | そのエントリだけを除外し、`errors` と ERROR ログに残す。全体は止めない |
| `sources.yaml` が無い、YAML として読めない、トップレベルに `sources` のリストが無い | `ConfigError` を送出する（続行できないため） |
| `.env` の値の型が不正（例：`SMTP_PORT=abc`） | `ConfigError` を送出する |
| 秘密情報の値 | ログ・例外メッセージ・`repr` に出さない |

## 受け入れ基準
- **AC-001-1**: Given 4 種類の `type` の正しいエントリを含む `sources.yaml` / When 読み込む / Then すべてが記載順に `sources` に入り、省略した項目には既定値（キーワードは空のリスト、`recheck_changed` は `false`、`min_score` はなし）が入り、`errors` は空になる
- **AC-001-2**: Given `type` ごとの必須の取得先（`rss` の `url`、`github_release` の `repo`、`hn_search` / `arxiv` の `query`）が無いエントリ / When 読み込む / Then そのエントリだけが除外され、`errors` に id と理由が残り、ほかのエントリは読み込まれる
- **AC-001-3**: Given 値が制約を満たさないエントリ（`priority` が 0 や 4、`max_items` が 0、`url` が http(s) でない、`repo` が `owner/name` 形式でない、`category` が許可されていない値） / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-4**: Given 表にない項目、または `type` で使わない取得先の項目を含むエントリ / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-5**: Given `hn_search` 以外のエントリに `min_score` がある / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-14**: Given `query` が文字列 1 つのエントリと、文字列のリストのエントリ / When 読み込む / Then どちらも `query` は文字列のリストになり（文字列 1 つなら要素 1 つ）、`query_operator` を省略していれば `and` になる
- **AC-001-15**: Given `query_operator` が `and` / `or` 以外、`query` が空のリスト、または空の文字列を含むリスト / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-16**: Given `hn_search` / `arxiv` 以外のエントリに `query_operator` がある / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-18**: Given `query` を書いた `qiita_search` のエントリ / When 読み込む / Then `query` は書いたとおりの文字列で読み込まれ、`min_stocks` と `lookback_days` を省略していれば `3` と `7` になる
- **AC-001-19**: Given `query` が無い、または空の文字列の `qiita_search` のエントリ / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-20**: Given `min_stocks` が負、または `lookback_days` が 0 の `qiita_search` のエントリ / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-21**: Given `qiita_search` 以外のエントリに `min_stocks` か `lookback_days` がある、または `qiita_search` のエントリに `query_operator` か `min_score` がある / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-22**: Given `topic` を書いた `zenn_topic` のエントリ / When 読み込む / Then `topic` はそのまま読み込まれ、`min_likes`・`lookback_days`・`pages` を省略していれば `5`・`7`・`2` になる
- **AC-001-23**: Given `topic` が無い、または空の `zenn_topic` のエントリ / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-24**: Given `min_likes` が負、`lookback_days` が 0、または `pages` が 0 の `zenn_topic` のエントリ / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-25**: Given `zenn_topic` 以外のエントリに `topic`・`min_likes`・`pages` がある、または `zenn_topic` のエントリに `query`・`min_stocks`・`min_score` がある / When 読み込む / Then そのエントリが除外され、`errors` に残る
- **AC-001-6**: Given 同じ `id` のエントリが 2 つある / When 読み込む / Then 最初のエントリだけが残り、2 つ目は `errors` に残る
- **AC-001-7**: Given 検証に失敗したエントリがある / When 読み込む / Then 除外したエントリの位置と id が ERROR ログに出る
- **AC-001-8**: Given `sources.yaml` が存在しない、YAML として不正、またはトップレベルに `sources` のリストが無い / When 読み込む / Then `ConfigError` が送出される
- **AC-001-9**: Given `.env` に値がある / When `Settings` を作る / Then 値が読み込まれる。同じ変数が環境変数にもあれば、環境変数の値が使われる
- **AC-001-10**: Given 秘密情報が設定されていない / When `Settings` を作る / Then エラーにならず、該当の項目は「なし」になる
- **AC-001-17**: Given `.env` に値が空の変数がある（例：`SMTP_HOST=`、`SMTP_PORT=`） / When `Settings` を作る / Then その項目は「なし」（`SOURCES_PATH` は既定値）になり、エラーにならない
- **AC-001-11**: Given `SMTP_PORT` が整数でない / When `Settings` を作る / Then `ConfigError` が送出され、メッセージに秘密情報の値が含まれない
- **AC-001-12**: Given `ANTHROPIC_API_KEY` と `SMTP_PASSWORD` が設定されている / When `Settings` を文字列化またはログ出力する / Then 秘密の値が含まれない
- **AC-001-13**: Given `Settings` の環境変数の一覧 / When `.env.example` と比べる / Then `.env.example` にすべての変数名が載っている

## 実装方針（参考）
- 依存の追加：`pyyaml`（実行時）、`types-pyyaml`（開発用。pyright strict のため）、`pydantic`、`pydantic-settings`
- `.env` の読み込みには `pydantic-settings` を使う。秘密情報は `SecretStr` で持つ
- ソースは `type` を判別子にした pydantic の判別共用体（discriminated union）で表し、すべてのモデルで `extra="forbid"` にする
- テストでは一時ディレクトリに YAML と `.env` を作る。外部通信はしない

## 決定事項
- **`zenn_topic` の追加**（SPEC §5「Zenn の取得方法」に合わせる）：既定値は `min_likes` 5、`lookback_days` 7、`pages` 2。
0. **`qiita_search` の追加**（SPEC §5 の変更に合わせる）：`query` は Qiita の検索構文の文字列 1 つ。`min_stocks` と `lookback_days` の既定値は SPEC §5 の初期値（3 と 7 日）とする。
1. **`category` の値**：SPEC §5 の「区分」の列から `official` / `community` / `paper` / `japanese` とする。メールのセクション（Claude Code / LLM 動向 / その他）への振り分けは 009 で決める。
2. **`max_items`**：SPEC に既定値が無いので必須とする。
3. **`query`**：検索語は 1 つでも複数でもよく、複数のときは `query_operator`（`and` / `or`）で組み合わせる。
4. **ライブラリ**：`pydantic-settings` を使う（SPEC §10 で最終判断を任されている範囲）。

## 未決事項
- 文字列 1 つの `query` に空白が含まれる場合（例：`Claude Code`）は、1 つのフレーズとして扱う。空白で語に分けて AND にはしない。
- `query_operator` を `hn_search` と `arxiv` の両方で使えるようにした（`query` を共通の項目にしているため）。
