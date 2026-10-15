# 007: LLM ラッパーと予算ガード

- 状態: 実装済み
- 関連: SPEC.md §6（LLM 利用方針とコスト設計、予算ガード、LLM が使えないときの縮退動作）、§7（`llm_calls`）、CLAUDE.md（LLM 呼び出しは 1 つのラッパー経由、トークン数と概算コストの記録、上限チェック）、002

## 目的
すべての LLM 呼び出しを 1 つのラッパーに通し、呼び出しの前に予算を確認し、呼び出しの後にトークン数と概算の費用を `llm_calls` に記録する。
予算の上限に達したら LLM を呼ばず、呼び出し側（008 の再ランク、010 の要約）が縮退動作に切り替えられるようにする。

## 範囲
- 含む:
  - Anthropic API の呼び出しの入口（`LlmClient`）
  - 単価表（`pricing.yaml`）の読み込みと、usage からの費用の計算
  - 日次・月次の予算ガード
  - `llm_calls` への記録と、当日・当月の累計の取得（011 のフッター表示でも使う）
  - SDK の自動再試行・タイムアウトの設定
  - 設定値の追加（001 の `Settings`）
- 含まない:
  - プロンプト・tool の定義・応答の検証（`stop_reason` の扱いを含む）：008、010
  - 縮退動作そのもの（タイトルのみで配信する等）：008、010、013
  - `--no-llm` の扱い：013（`LlmClient` を作らない）
  - prompt caching と Batch API の採用：008、010 で検討する（費用の計算はキャッシュの読み書きにも対応しておく）

## 使用するモデルと料金（2026-10-16 に公式の料金ページで確認）
- モデル：`claude-haiku-5-5`（Claude Haiku 5.5。現行の Haiku）
- 料金（100 万トークンあたり、プロンプトが 10 万トークン以下）：入力 $0.10、キャッシュ書き込み 5 分 $0.125 / 1 時間 $0.20、キャッシュ読み取り $0.01、出力 $0.50
- プロンプトが 10 万トークンを超えるリクエストは、そのリクエスト全体が高い単価：入力 $0.50、キャッシュ書き込み 5 分 $0.625 / 1 時間 $1.00、キャッシュ読み取り $0.05、出力 $2.50
- Batch API は入力・出力とも 50% 引き

### pricing.yaml
```yaml
# 出典: https://platform.claude.com/docs/en/about-claude/pricing（2026-10-16 確認）
models:
  claude-haiku-5-5:
    input: 0.10            # USD / 100 万トークン
    output: 0.50
    cache_write_5m: 0.125
    cache_write_1h: 0.20
    cache_read: 0.01
    long_context:          # プロンプトが threshold トークンを超えるリクエストの単価（任意）
      threshold: 100000
      input: 0.50
      output: 2.50
      cache_write_5m: 0.625
      cache_write_1h: 1.00
      cache_read: 0.05
```
- 単価は 0 以上の数、`threshold` は 1 以上の整数。不正なら `ConfigError`。
- パスは設定値 `PRICING_PATH`（既定 `pricing.yaml`）。

## 追加する設定値（`.env` / 環境変数）
| 変数 | 型 | 既定値 | 用途 |
|---|---|---|---|
| `LLM_MODEL` | 文字列 | `claude-haiku-5-5` | 使用するモデル。`pricing.yaml` に無いモデルなら `ConfigError` |
| `LLM_TIMEOUT_SECONDS` | 正の数 | `60` | 1 回のリクエストのタイムアウト（SDK の `timeout`） |
| `LLM_MAX_RETRIES` | 0 以上の整数 | `2` | SDK の自動再試行の回数（SDK の `max_retries`。接続エラー・408・409・429・5xx が対象） |
| `BUDGET_DAILY_USD` | 0 以上の数 | `0.15` | 日次の上限 |
| `BUDGET_MONTHLY_USD` | 0 以上の数 | `2.0` | 月次の上限 |
| `USD_JPY_RATE` | 正の数 | `160` | 円換算の為替レート（011 のフッターで使う） |
| `PRICING_PATH` | パス | `pricing.yaml` | 単価表 |

- `ANTHROPIC_API_KEY` は 001 で定義済み。

## 振る舞い

### 呼び出し
`LlmClient.create(purpose, **params)` は次の順に処理する（`purpose` は `rerank` / `summarize`）。
1. 当日と当月の費用の累計を `llm_calls` から求める。日と月の区切りは日本時間。
2. 当日の累計が `BUDGET_DAILY_USD` 以上、または当月の累計が `BUDGET_MONTHLY_USD` 以上なら、API を呼ばずに `BudgetExceeded` を送出する。
3. 設定のモデル・タイムアウト・再試行回数で Messages API を呼ぶ（`params` はそのまま渡す。`model` は渡せない）。
4. 応答の usage から費用を計算し、`llm_calls` に 1 行記録する（`run_id`、`purpose`、`model`、`input_tokens`、`output_tokens`、`cost_usd`、`created_at`）。
   - `input_tokens` には、キャッシュの読み書きの分も含めた入力の合計を記録する
5. 応答（SDK の `Message`）をそのまま返す。`stop_reason` の判定は呼び出し側が行う。

### 費用の計算
```
cost = (input_tokens × input
        + 5 分キャッシュの書き込み × cache_write_5m
        + 1 時間キャッシュの書き込み × cache_write_1h
        + cache_read_input_tokens × cache_read
        + output_tokens × output) / 1,000,000
```
- usage にキャッシュ書き込みの内訳（5 分 / 1 時間）が無ければ、`cache_creation_input_tokens` をすべて 5 分として扱う。
- プロンプトの長さ（`input_tokens` + キャッシュ書き込み + キャッシュ読み取り）が `long_context.threshold` を超えたら、そのリクエストは `long_context` の単価で計算する。
- 予算の確認は呼び出しの前に行うので、最後の 1 回の分だけ上限を超えることがある（1 回あたりの費用は上限に比べて十分小さいため許容する）。

### 累計の取得
- `spent_usd(conn, now)` は当日と当月の累計（USD）を返す。011 のフッターと予算ガードで使う。

### エラー
- API キーが無い：`LlmClient` を作るときに `LlmUnavailable` を送出する（呼び出し側は SPEC §6 の「LLM 全体が使えない」として扱う）。
- SDK の再試行の後も失敗した（`anthropic.APIError` 系）：`LlmError` にして送出する。`llm_calls` には記録しない（usage が無いため）。
- いずれも API キーなどの秘密情報をメッセージ・ログに含めない。

### 記録とコミット
- `llm_calls` への登録はコミットしない（003〜006 と同じ）。ただし、使ったお金の記録が失われると予算ガードが効かなくなるため、013 は、ほかの処理が失敗したときや `--dry-run` のときも `llm_calls` だけはコミットされるようにする（決定事項 2）。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| `pricing.yaml` が無い・不正、`LLM_MODEL` が単価表に無い | `ConfigError` |
| API キーが無い | `LlmUnavailable` |
| 予算の上限に達している | API を呼ばず `BudgetExceeded` |
| API の一時エラー | SDK が `LLM_MAX_RETRIES` 回まで再試行。尽きたら `LlmError` |
| API の恒久エラー（400・401・404 など） | 再試行せず `LlmError` |

## 受け入れ基準
### 設定と単価表
- **AC-007-1**: Given 追加した設定値を設定していない / When `Settings` を作る / Then 表の既定値になる。`.env.example` にすべて載っている（AC-001-13 のテストで確認する）
- **AC-007-2**: Given 正しい `pricing.yaml` / When 読み込む / Then 各単価と `long_context` が読み込まれる。`long_context` は省略できる
- **AC-007-3**: Given 単価が負、`threshold` が 0、必須の単価が無い、ファイルが無い `pricing.yaml` / When 読み込む / Then `ConfigError` が送出される
- **AC-007-4**: Given `LLM_MODEL` が `pricing.yaml` に無い / When `LlmClient` を作る / Then `ConfigError` が送出される
- **AC-007-5**: Given リポジトリの `pricing.yaml` / When 読み込む / Then エラーにならず、`claude-haiku-5-5` の単価が上の表のとおりになる

### 費用の計算
- **AC-007-6**: Given 入力 1,000、出力 500、キャッシュなしの usage / When 費用を計算する / Then (1,000 × 0.10 + 500 × 0.50) / 1,000,000 = 0.00035 USD
- **AC-007-7**: Given キャッシュ書き込み 5 分 2,000・1 時間 1,000、キャッシュ読み取り 3,000 を含む usage / When 費用を計算する / Then それぞれの単価で計算される。内訳の無い usage では書き込みをすべて 5 分として計算する
- **AC-007-8**: Given プロンプトの長さが `threshold` を超える usage と、ちょうど `threshold` の usage / When 費用を計算する / Then 超える方だけが `long_context` の単価で計算される

### 呼び出しと記録
- **AC-007-9**: Given 予算に余裕がある / When `create("summarize", messages=..., max_tokens=...)` を呼ぶ / Then 設定のモデルで API が 1 回呼ばれ、渡した引数がそのまま渡り、応答がそのまま返り、`llm_calls` に `purpose`・`model`・トークン数・費用・`created_at`（UTC）が 1 行記録される（コミットはされない）
- **AC-007-10**: Given 当日の累計が日次の上限ちょうど / When `create` を呼ぶ / Then API は呼ばれず `BudgetExceeded` が送出される。上限より少しでも少なければ呼ばれる
- **AC-007-11**: Given 当月の累計が月次の上限以上（当日はゼロ） / When `create` を呼ぶ / Then API は呼ばれず `BudgetExceeded` が送出される
- **AC-007-12**: Given 日本時間の前日と前月の `llm_calls` / When 累計を求める / Then 当日・当月には含まれない。日本時間で当日 0:00（UTC では前日 15:00）以降の記録は当日に含まれる
- **AC-007-13**: Given API キーが無い設定 / When `LlmClient` を作る / Then `LlmUnavailable` が送出される
- **AC-007-14**: Given API が再試行の後もエラー（429・500・接続エラー）、または恒久エラー（400） / When `create` を呼ぶ / Then `LlmError` が送出され、`llm_calls` には記録されず、メッセージに API キーが含まれない
- **AC-007-15**: Given `LLM_TIMEOUT_SECONDS` と `LLM_MAX_RETRIES` の設定 / When `LlmClient` を作る / Then SDK のクライアントにその値が渡る
- **AC-007-16**: Given `params` に `model` を含む / When `create` を呼ぶ / Then `ValueError` が送出され、API は呼ばれない（モデルは設定でだけ決める）

## 実装方針（参考）
- 依存の追加：`anthropic`（公式 SDK）
- テストでは、Messages API を呼ぶ部分を差し替えられるようにし（`messages.create` を持つオブジェクトを渡す）、実際の API には出ない。応答は SDK の `Message` 型で作る
- モジュール：`src/aidigest/llm.py`（`LlmClient`、費用の計算、累計の取得）、単価表の読み込みは `config.py`

## 決定事項
1. **為替レート `USD_JPY_RATE` の既定値は 160**。フッターに円で表示するためだけに使う。
2. **`llm_calls` を必ず残す方法**：このラッパーはコミットしない。013（CLI）で、失敗時や `--dry-run` でも `llm_calls` だけはコミットする方法を決める（例：LLM を呼ぶ段階の後に必ずコミットする）。
3. **日と月の区切りは日本時間**とした。配信も日本時間の朝なので、予算の「1 日」と配信の「1 日」がそろう。
4. **予算の上限**：SPEC の初期値（日 $0.15、月 $2.0）のまま。Haiku 5.5 の単価では、日次の上限は入力約 150 万トークン、出力約 30 万トークンに相当し、想定の使い方（再ランク 1 回 + 要約 8 回）より十分大きい。
