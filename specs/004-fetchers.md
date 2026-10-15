# 004: フェッチャー（RSS / HN 検索 / Qiita 検索）

- 状態: 実装済み
- 関連: SPEC.md §4 の 1（取得）、§5（情報源・ソース設定・健全性記録）、CLAUDE.md（外部アクセスのルール）、001、002、003

## 目的
`sources.yaml` の各ソースから新着記事を取得し、`FetchedItem`（003）のリストにする。
1 つのソースが失敗しても全体を止めず、ソースごとの成否を `source_health` に記録する。

## 範囲
- 含む:
  - 共通の HTTP アクセス（ユーザーエージェント、タイムアウト、再試行、同じホストへの間隔）
  - `rss`（RSS 2.0 / Atom）、`hn_search`（Algolia HN Search API）、`qiita_search`（Qiita API v2）のフェッチャー
  - 全ソースの取得と、ソース単位の例外の隔離
  - `source_health` への記録
  - 設定値の追加（001 の `Settings`）
  - リポジトリの `sources.yaml`（SPEC §5 の 10 ソース）
- 含まない:
  - `github_release` と `arxiv` のフェッチャー（MVP で使うソースに無いため。設定にあれば「未対応」のエラーとして記録する）
  - 既読判定：003
  - 時間窓・キーワードによる絞り込み：005（ただし HN は取得時にも時間窓で絞る。後述）
  - 本文の取得（trafilatura）：010
  - 失敗の通知・フッターへの表示：011、013

## 入出力
- 入力: ソース設定のリスト（001）、`Settings`、現在日時
- 出力: `FetchedItem` のリスト（ソースの順、各ソース内は後述の順）と、ソースごとの取得結果（`SourceHealth`：`source_id`、`status`（`ok` / `error`）、`error`、`item_count`）
- `source_health` への記録: `run_id` と `SourceHealth` のリストを受け取って登録する。コミットはしない（003 と同じく呼び出し側が決める）

### 追加する設定値（`.env` / 環境変数）
| 変数 | 型 | 既定値 | 用途 |
|---|---|---|---|
| `USER_AGENT` | 文字列 | `aidigest/0.1 (+https://github.com/dreammind/ai-digest)` | すべての HTTP リクエストに付ける |
| `HTTP_TIMEOUT_SECONDS` | 正の数 | `20` | 1 回のリクエストのタイムアウト |
| `HTTP_RETRIES` | 0 以上の整数 | `2` | 再試行の回数（最初の 1 回は含まない） |
| `HTTP_BACKOFF_SECONDS` | 0 以上の数 | `1.0` | 再試行の待ち時間。n 回目の再試行の前に `HTTP_BACKOFF_SECONDS × 2^(n-1)` 秒待つ |
| `HOST_INTERVAL_SECONDS` | 0 以上の数 | `1.0` | 同じホストへの連続リクエストの最小間隔 |
| `TIME_WINDOW_HOURS` | 正の整数 | `36` | 時間窓（SPEC §4）。HN の取得と 005 で使う |

## 振る舞い

### 共通の HTTP アクセス
1. すべてのリクエストに `User-Agent: <USER_AGENT>` を付ける。
2. 同じホストへのリクエストは、前回のリクエストの開始から `HOST_INTERVAL_SECONDS` 以上空ける。違うホストへは待たない。
3. 次の場合は `HTTP_RETRIES` 回まで再試行する：接続エラー・タイムアウト、HTTP 429、HTTP 5xx。
4. それ以外の 4xx は再試行しない。再試行しても成功しなければ、そのソースの取得失敗とする。

### rss
1. `url` を取得し、RSS 2.0 / Atom として解析する。
2. フィードに書かれた順に、先頭から `max_items` 件までのエントリを `FetchedItem` にする。
   - `url`：エントリのリンク
   - `title`：エントリのタイトル
   - `published_at`：公開日時。無ければ更新日時。どちらも無ければなし。UTC のタイムゾーン付きにする
   - `content`：本文（`content`）。無ければ要約（`summary`）。どちらも無ければ空
3. リンクかタイトルが無いエントリはスキップし、WARNING ログを出す。
4. 応答がフィードとして解析できず、エントリが 1 件も取れなければ、そのソースの取得失敗とする。

### hn_search
1. Algolia HN Search API の `search_by_date` を、次の条件で呼ぶ。
   - 検索対象はタイトルだけ（`restrictSearchableAttributes=title`）、引用符をフレーズとして扱う（`advancedSyntax=true`）、記事のみ（`tags=story`）
   - 作成日時が `現在 − TIME_WINDOW_HOURS` より後（`created_at_i`）
   - `min_score` があれば、ポイントがそれ以上（`points>=`）
   - 1 ページ、`hitsPerPage = max_items`
2. `query_operator` が `and` なら、各検索語を引用符で囲んで空白でつないだ 1 つのクエリで、1 回だけ呼ぶ（例：`"Claude Code" "MCP"`）。
3. `or` なら、検索語ごとに 1 回ずつ呼び、結果を合わせる。同じ記事（`objectID` が同じ）は 1 件にし、作成日時の新しい順に並べて先頭 `max_items` 件にする。
4. 各ヒットを `FetchedItem` にする。
   - `url`：記事の URL。無ければ（Ask HN など）`https://news.ycombinator.com/item?id=<objectID>`
   - `title`、`published_at`（`created_at`）、`content`（`story_text`。無ければ空）
5. 応答の JSON は pydantic で検証する。個々のヒットが不正ならスキップして WARNING ログ。応答全体が不正なら、そのソースの取得失敗とする。

### qiita_search
1. Qiita API v2 の `GET /api/v2/items` を、次のクエリで 1 回呼ぶ。
   - `query`：`<query> created:>=<日付> stocks:>=<min_stocks>`。日付は `現在 − lookback_days 日` の日本時間の日付（`YYYY-MM-DD`）
   - `per_page = min(max_items, 100)`、`page = 1`
2. 各記事を `FetchedItem` にする：`url`、`title`、`published_at`（`created_at`）、`content`（`body`）。
3. 応答の検証と不正時の扱いは hn_search と同じ。

### 全ソースの取得
1. ソースを設定の順に 1 つずつ取得する（並列にはしない）。
2. 取得中に例外が起きたソースは、`status = error`、`error` に例外の種類とメッセージ（先頭 500 文字）を入れ、次のソースに進む。
3. 成功したソースは、`status = ok`、`item_count` に `FetchedItem` の件数を入れる。
4. `github_release` / `arxiv` のソースは、リクエストを送らずに `status = error`、`error = "未対応の type: <type>"` とする。

### sources.yaml
SPEC §5 の 10 ソースを、次の値で書く。`category` は日本語のソースを `japanese`、それ以外を `community` とする。

| id | type | 取得先 | priority | max_items | その他 |
|---|---|---|---|---|---|
| `zenn-claudecode` | rss | `https://zenn.dev/topics/claudecode/feed` | 1 | 20 | |
| `zenn-anthropic` | rss | `https://zenn.dev/topics/anthropic/feed` | 1 | 20 | |
| `zenn-llm` | rss | `https://zenn.dev/topics/llm/feed` | 2 | 20 | |
| `qiita-claudecode` | qiita_search | `tag:ClaudeCode` | 1 | 20 | 既定値（ストック 3、7 日） |
| `qiita-llm` | qiita_search | `tag:LLM` | 2 | 20 | 同上 |
| `qiita-aiagent` | qiita_search | `tag:AIエージェント` | 2 | 20 | 同上 |
| `hn-claude-code` | hn_search | `Claude Code` | 2 | 20 | `min_score: 20` |
| `hn-llm` | hn_search | `LLM` | 2 | 20 | `min_score: 100` |
| `hn-models` | hn_search | `[Gemini, OpenAI, Anthropic, GPT]`（`or`） | 2 | 20 | `min_score: 300` |
| `simonw` | rss | `https://simonwillison.net/atom/everything/` | 2 | 30 | |

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| 接続エラー・タイムアウト・429・5xx | 再試行する。尽きたらそのソースを `error` にして次へ |
| 429・5xx 以外の 4xx | 再試行せず、そのソースを `error` にして次へ |
| 応答がフィード / 期待した JSON でない | そのソースを `error` にして次へ |
| 個々のエントリ・ヒットが不正 | その 1 件だけスキップし、WARNING ログ |
| 未対応の type | リクエストせず `error` |
| 想定外の例外 | そのソースを `error` にして次へ（全体は止めない） |

## 受け入れ基準
### 共通の HTTP アクセス
- **AC-004-1**: Given `USER_AGENT` の設定 / When 任意のフェッチャーがリクエストする / Then リクエストにその `User-Agent` が付いている
- **AC-004-2**: Given 1 回目が 503（またはタイムアウト、429）、2 回目が 200 を返すサーバー / When 取得する / Then 再試行して成功し、再試行の前に `HTTP_BACKOFF_SECONDS` 秒待つ
- **AC-004-3**: Given 常に 503 を返すサーバー / When 取得する / Then 最初の 1 回 + `HTTP_RETRIES` 回で諦め、失敗になる
- **AC-004-4**: Given 404 を返すサーバー / When 取得する / Then 再試行せず失敗になる
- **AC-004-5**: Given 同じホストへの 2 回のリクエスト / When 続けて送る / Then 2 回目は 1 回目の開始から `HOST_INTERVAL_SECONDS` 以上あとに送られる。違うホストへのリクエストは待たない

### rss
- **AC-004-6**: Given Atom と RSS 2.0 のフィード / When 取得する / Then 各エントリが振る舞いのとおり `FetchedItem` になり、`published_at` は UTC のタイムゾーン付きになる
- **AC-004-7**: Given `max_items` より多いエントリのフィード / When 取得する / Then フィードの先頭から `max_items` 件だけが返る
- **AC-004-8**: Given 本文が無く要約だけのエントリ、日時が無いエントリ / When 取得する / Then `content` は要約になり、`published_at` はなしになる
- **AC-004-9**: Given リンクかタイトルが無いエントリを含むフィード / When 取得する / Then その 1 件だけスキップされ、WARNING ログが出る
- **AC-004-10**: Given フィードではない応答（HTML など） / When 取得する / Then そのソースの取得失敗になる

### hn_search
- **AC-004-11**: Given `query_operator: and` で検索語 2 つ、`min_score` ありのソース / When 取得する / Then API は 1 回だけ呼ばれ、クエリは引用符で囲んだ語を空白でつないだもの、条件にタイトル限定・`advancedSyntax`・`tags=story`・`created_at_i`（現在 − `TIME_WINDOW_HOURS`）・`points>=min_score`・`hitsPerPage=max_items` が含まれる
- **AC-004-12**: Given `min_score` なしのソース / When 取得する / Then 条件に `points` が含まれない
- **AC-004-13**: Given `query_operator: or` で検索語 3 つ、結果に重複のある応答 / When 取得する / Then API は語ごとに 3 回呼ばれ、結果は重複なしで作成日時の新しい順、`max_items` 件以内になる
- **AC-004-14**: Given URL のあるヒットと無いヒット / When 取得する / Then URL が無いヒットは HN の記事ページの URL になり、`title`・`published_at`・`content` が振る舞いのとおりになる
- **AC-004-15**: Given 一部のヒットが不正な応答、または全体が不正な応答 / When 取得する / Then 不正なヒットだけがスキップされる。全体が不正ならそのソースの取得失敗になる

### qiita_search
- **AC-004-16**: Given `query: tag:LLM`、`min_stocks: 3`、`lookback_days: 7`、`max_items: 20` のソースと、現在日時 2026-10-15T21:30Z（日本時間 10/16） / When 取得する / Then クエリは `tag:LLM created:>=2026-10-09 stocks:>=3`、`per_page=20` になる。`max_items` が 100 を超えていれば `per_page=100` になる
- **AC-004-17**: Given 記事の一覧の応答 / When 取得する / Then 各記事が振る舞いのとおり `FetchedItem` になる。不正な記事・応答の扱いは AC-004-15 と同じ

### 全ソースの取得と記録
- **AC-004-18**: Given 3 つのソースのうち 2 つ目が例外を出す / When 全ソースを取得する / Then 1 つ目と 3 つ目の記事が返り、2 つ目は `status = error` で、例外の種類とメッセージ（500 文字以内）が残る。ほかは `status = ok` で `item_count` が件数になる
- **AC-004-19**: Given `github_release` または `arxiv` のソース / When 全ソースを取得する / Then リクエストは送られず、`status = error`、`error` に「未対応の type」が入る
- **AC-004-20**: Given `SourceHealth` のリストと `run_id` / When `source_health` に記録する / Then ソースごとに 1 行登録され、コミットはされない

### 設定とソース
- **AC-004-21**: Given 追加した設定値を設定していない / When `Settings` を作る / Then 表の既定値になる。`.env.example` にすべて載っている（AC-001-13 のテストで確認する）
- **AC-004-22**: Given リポジトリの `sources.yaml` / When 読み込む（001） / Then エラーなしで、上の表の 10 ソースがその値で読み込まれる

## 実装方針（参考）
- 依存の追加：`httpx`、`feedparser`
- テストは `httpx.MockTransport` で応答を差し替え、実ネットワークに出ない。待ち時間は時計と sleep を差し替えて確かめる（実際には待たない）
- モジュール：`fetchers/http.py`（共通の HTTP）、`fetchers/rss.py`、`fetchers/hn.py`、`fetchers/qiita.py`、`fetchers/runner.py`（全ソースの取得と記録）
- `feedparser` は型情報が無いため、結果は境界で明示的に型を確かめてから `FetchedItem` にする（`Any` を内部に広げない）

## 決定事項
1. **HN の OR と「候補の確認数は `max_items` の 3 倍まで」（SPEC §5）**：語ごとに `max_items` 件ずつ取るので、4 語の `hn-models` では最大 80 件を確認することになる。一方、ポイントでの絞り込みは API 側で行っており、SPEC のこの制限が想定していた「取ってから手元で捨てる」処理は無い。この制限は「1 回の取得で呼ぶ API は検索語の数まで（ページ送りしない）」と読み替えたい。
2. **HN も取得時に時間窓（36 時間）で絞る**：API の段階で古い記事を取らないため（005 でも同じ時間窓で絞る）。
3. **Qiita の日付は日本時間**：Qiita の `created:>=` は日付だけを受け付けるため、日本時間の日付にする。
4. **設定値の既定値**：上の表のとおり（タイムアウト 20 秒、再試行 2 回、間隔 1 秒など）。
