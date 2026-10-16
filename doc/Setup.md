# AI Digest 環境設定ガイド

## 1. 概要

AI Digest は、Claude Code と LLM に関する記事を毎朝集めて、日本語で要約し、1 通のメールで届ける Python アプリです。

### 処理の流れ

```
cron（毎朝 6:30 JST）
  → 取得        Zenn・Qiita・Hacker News・Simon Willison のブログから新着記事を集める
  → 既読判定    一度見た記事は除く（同じ記事を 2 日続けて配信しない）
  → 事前絞込    古い記事・除外キーワードを含む記事を外す
  → ローカルスコア  キーワードとソースの優先度で採点し、似たタイトルの重複を除く
  → LLM 再ランク   上位 30 件を Claude Haiku で関連度判定し、並べ直す
  → 枠配分      Claude Code 4 件・LLM 動向 3 件・その他 2 件を上限に、最大 8 件を選ぶ
  → 要約        選んだ記事の本文を取得し、Claude Haiku で日本語の要約と「AI活用」を作る
  → 配信        HTML メールで送り、同じ内容を output/YYYY-MM-DD.html にも書き出す
```

### 構成

| 要素 | 内容 |
|---|---|
| 言語・実行環境 | Python 3.12（uv で管理） |
| データ | SQLite（`data/aidigest.db`） |
| LLM | Anthropic API の Claude Haiku 5.5（`claude-haiku-5-5`） |
| メール | Gmail の SMTP（アプリパスワード）。設定だけで、さくらインターネットなど別の SMTP に切り替えられる |
| 本番の実行場所 | さくらの VPS（Ubuntu 22.04）の `~/ws/ai-digest`。cron で毎朝 6:30 に実行 |

### 費用の目安

- LLM の費用は 1 日あたり約 $0.008（約 1.3 円）、1 か月で約 40 円です。
- アプリの予算ガード（1 日 $0.15、1 か月 $2.0）を超えると LLM を呼ぶのをやめ、タイトルだけのダイジェストに切り替えます。
- Anthropic Console のクレジット残高（自動リロードはオフ）が、最後の歯止めになります。

### 関連する文書

| 文書 | 内容 |
|---|---|
| [SPEC.md](../SPEC.md) | 全体の仕様 |
| [specs/](../specs/) | 機能ごとの詳細な仕様と受け入れ基準（001〜014） |
| [CLAUDE.md](../CLAUDE.md) | 開発のルール（仕様駆動・テスト駆動、型、秘密情報の扱いなど） |

---

## 2. 必要なもの

| もの | 用途 |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Python と依存パッケージの管理。Python 3.12 は uv が自動で用意する |
| git、GitHub への ssh 接続 | リポジトリ（`git@github.com:dreammind/ai-digest.git`）の取得 |
| Anthropic API キー | 再ランクと要約。[Anthropic Console](https://console.anthropic.com/) で発行し、クレジットを購入しておく |
| Gmail アカウントとアプリパスワード | メールの送信。2 段階認証が有効なアカウントが必要 |

---

## 3. ディレクトリ構成

```
src/aidigest/
  config.py        # 設定の読み込み（.env・sources.yaml・topics.yaml・pricing.yaml）
  db.py            # SQLite とマイグレーション
  migrations/      # スキーマ（番号付きの SQL）
  fetchers/        # 取得（RSS・HN・Qiita・Zenn）
  pipeline/        # 既読判定・絞り込み・採点・再ランク・枠配分・要約
  prompts/         # LLM に渡すプロンプト
  llm.py           # LLM の呼び出しの入口（費用の記録と予算ガード）
  render.py        # メールの HTML とテキストの生成
  mailer.py        # SMTP でのメール送信
  cli.py           # コマンド（python -m aidigest.cli run）
templates/         # メールのテンプレート（HTML とテキスト）
sources.yaml       # 取得するソース
topics.yaml        # 採点のキーワードと重み、配信の件数と上限
pricing.yaml       # LLM の単価表
.env.example       # .env の見本
tests/             # テスト
specs/             # 機能ごとの仕様
data/              # DB とロックファイル（Git の管理外）
output/            # 静的 HTML（Git の管理外）
logs/              # ログ（Git の管理外）
```

---

## 4. 手元の開発環境

```bash
git clone git@github.com:dreammind/ai-digest.git
cd ai-digest
uv sync                         # Python 3.12 と依存パッケージ（開発用を含む）を入れる
uv run pre-commit install       # コミット前の検査を有効にする
cp .env.example .env            # 設定ファイルを作り、5 章のとおりに書く
```

### コミット前の検査（pre-commit）

コミットのたびに、次の検査が自動で走ります。どれかが失敗すると、コミットは拒否されます。

| 検査 | 内容 |
|---|---|
| API キーらしい文字列 | `sk-ant-...` のような長いキーを含むファイルがあれば止める（ファイル名だけを表示する） |
| `ruff check` / `ruff format --check` | lint と書式 |
| `pyright` | 型検査（strict） |
| `pytest` | テストとカバレッジ（80% 以上） |

### Claude Code の hooks

`.claude/settings.json` で、次の hooks が設定されています。

- ファイルを編集した後：ruff と pyright を実行し、エラーがあれば差し戻す
- `src/aidigest/` にモジュールを作ったとき：対応するテストが無ければ警告する
- 作業を終えるとき：pytest を実行し、失敗していれば差し戻す

---

## 5. 設定

### 5.1 `.env`（秘密情報と実行時の設定）

`.env` はリポジトリの直下に置きます。**Git の管理外**で、`.env.example` が見本です。値が空の行は、設定していないものとして扱われます。

#### 必須の設定

| 変数 | 内容 | 例 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API キー。無い場合は LLM を使わず、タイトルだけのダイジェストになる | `sk-ant-...` |
| `SMTP_HOST` | SMTP サーバー | `smtp.gmail.com` |
| `SMTP_PORT` | ポート | `587` |
| `SMTP_USER` | SMTP の認証ユーザー | Gmail のアドレス |
| `SMTP_PASSWORD` | SMTP のパスワード（Gmail はアプリパスワード） | 16 文字 |
| `MAIL_FROM` | 送信元。Gmail では `SMTP_USER` と同じアドレスにする | `AI digest <you@gmail.com>` |
| `MAIL_TO` | 宛先。カンマ区切りで複数書ける。失敗通知もここに送る | `you@gmail.com` |

#### 任意の設定（省略すると既定値）

| 変数 | 既定値 | 内容 |
|---|---|---|
| `SMTP_SECURITY` | `starttls` | 暗号化の方式。`starttls`（587 番）か `ssl`（465 番） |
| `SMTP_TIMEOUT_SECONDS` | `30` | SMTP のタイムアウト（秒） |
| `SMTP_RETRIES` | `1` | 一時的なエラーのときの再試行の回数 |
| `SMTP_RETRY_WAIT_SECONDS` | `10` | 再試行の前に待つ時間（秒） |
| `LLM_MODEL` | `claude-haiku-5-5` | 使うモデル（`pricing.yaml` にあるもの） |
| `LLM_TIMEOUT_SECONDS` | `60` | LLM のタイムアウト（秒） |
| `LLM_MAX_RETRIES` | `2` | SDK の自動再試行の回数 |
| `BUDGET_DAILY_USD` | `0.15` | 1 日の予算（USD） |
| `BUDGET_MONTHLY_USD` | `2.0` | 1 か月の予算（USD） |
| `USD_JPY_RATE` | `160` | フッターで円に換算するときのレート |
| `RERANK_TOP_N` | `30` | LLM で再ランクする件数 |
| `RERANK_EXCERPT_CHARS` | `200` | 再ランクに渡す抜粋の文字数 |
| `RERANK_LLM_WEIGHT` | `0.5` | 最終スコアでの LLM の関連度の重み（0〜1） |
| `RERANK_MAX_TOKENS` | `4000` | 再ランクの `max_tokens` |
| `SUMMARY_BODY_CHARS` | `4000` | 要約に渡す本文の最大文字数 |
| `SUMMARY_MAX_TOKENS` | `4000` | 要約の `max_tokens` |
| `TIME_WINDOW_HOURS` | `36` | 対象にする記事の時間窓（時間） |
| `USER_AGENT` | `aidigest/0.1 (+https://github.com/dreammind/ai-digest)` | 外部アクセスのユーザーエージェント |
| `HTTP_TIMEOUT_SECONDS` | `20` | 取得のタイムアウト（秒） |
| `HTTP_RETRIES` | `2` | 取得の再試行の回数 |
| `HTTP_BACKOFF_SECONDS` | `1.0` | 再試行の待ち時間（倍々に延びる） |
| `HOST_INTERVAL_SECONDS` | `1.0` | 同じホストへのアクセスの間隔（秒） |
| `SOURCES_PATH` / `TOPICS_PATH` / `PRICING_PATH` | `sources.yaml` / `topics.yaml` / `pricing.yaml` | 設定ファイルの場所 |
| `TEMPLATES_DIR` / `OUTPUT_DIR` | `templates` / `output` | テンプレートと静的 HTML の場所 |
| `DB_PATH` | `data/aidigest.db` | DB ファイル |
| `LOG_DIR` / `LOG_BACKUP_DAYS` | `logs` / `30` | ログの場所と保存日数 |
| `LOCK_PATH` | `data/aidigest.lock` | 二重実行を防ぐロックファイル |

### 5.2 Anthropic API キー

1. [Anthropic Console](https://console.anthropic.com/) で API キーを発行し、`ANTHROPIC_API_KEY` に書く。
2. クレジットを購入しておく。自動リロードをオフにしておくと、残高が使える額の上限になる。
3. 同じクレジットをほかの API の利用と共有している場合は、そちらの分も残高から減る。

### 5.3 Gmail のアプリパスワード

1. Google アカウントで 2 段階認証を有効にする（https://myaccount.google.com/signinoptions/twosv ）。
2. https://myaccount.google.com/apppasswords でアプリパスワード（16 文字）を作る。表示は一度だけ。
3. `.env` に次のように書く。

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<アプリパスワード>
MAIL_FROM="AI digest <you@gmail.com>"
MAIL_TO=you@gmail.com
```

さくらインターネットの SMTP に切り替えるときは、`SMTP_HOST`・`SMTP_PORT`・`SMTP_SECURITY`・認証情報を書き換えるだけで済みます（コードの変更は不要）。

### 5.4 `sources.yaml`（取得するソース）

| type | 取得方法 | 主な項目 |
|---|---|---|
| `rss` | RSS / Atom | `url` |
| `hn_search` | Hacker News の検索 API（タイトル検索） | `query`（文字列かリスト）、`query_operator`（`and`/`or`）、`min_score`（ポイントの下限） |
| `qiita_search` | Qiita API（投稿から `lookback_days` 日以内で、ストックが `min_stocks` 以上の記事） | `query`（例：`tag:ClaudeCode`）、`min_stocks`（既定 3）、`lookback_days`（既定 7） |
| `zenn_topic` | Zenn の非公式 API（過去 1 週間の人気順から、いいねが `min_likes` 以上の記事） | `topic`、`min_likes`（既定 5）、`lookback_days`（既定 7）、`pages`（既定 2） |

すべての type に共通の項目：`id`、`category`（`official`/`community`/`paper`/`japanese`）、`priority`（1〜3）、`max_items`、`require_keywords`、`hide_keywords`、`highlight_keywords`。トップレベルの `exclude_keywords` は、全ソースに共通の除外キーワードです。

ソースを足すときは、URL を実際に取得して存在を確かめてから書いてください（CLAUDE.md）。

### 5.5 `topics.yaml`（採点と配信の件数）

- `scoring`：キーワードの点数、ソースの優先度による加点、足切りのスコア、重複とみなすタイトルの類似度など
- `topics`：テーマごとの重みとキーワード（`claude_code`、`llm`）
- `negative`：減点するキーワード（ゴシップや求人など）
- `digest`：配信する件数（`size`）と、グループごとの上限（`limits`）、「Claude Code 更新情報」に入れるキーワード

### 5.6 `pricing.yaml`（LLM の単価表）

モデルごとの単価（100 万トークンあたりの USD）です。費用の記録と予算ガードに使います。料金が変わったら、[公式の料金ページ](https://platform.claude.com/docs/en/about-claude/pricing) を確認して更新してください。

---

## 6. 動かし方

```bash
uv run python -m aidigest.cli run              # 取得からメール送信までを 1 回実行する
uv run python -m aidigest.cli run --dry-run    # メールを送らず、記事の状態も残さない（試し用）
uv run python -m aidigest.cli run --no-llm     # LLM を使わない（タイトルだけのダイジェスト）
```

| オプション | 動き |
|---|---|
| なし | メールを送り、送った記事を既読（`sent`）にする |
| `--dry-run` | メールは送らない。静的 HTML は書き出す。記事の状態は残さないので、次の通常の実行で同じ記事が配信される。LLM の費用の記録は残る |
| `--no-llm` | LLM を呼ばない。記事はタイトルと原文リンクだけになり、メールにお知らせが付く |

| 終了コード | 意味 |
|---|---|
| `0` | 成功（記事が 0 件でメールを送らなかった日を含む） |
| `1` | 失敗（設定の誤り、途中のエラー、メールの送信の失敗など）。失敗通知メールを送る（`--dry-run` を除く） |
| `2` | ほかの実行が動いているため中止、または引数の誤り |

### 出力されるもの

| 場所 | 内容 |
|---|---|
| メール | 件名 `[AI Digest] YYYY-MM-DD 注目 N 件` |
| `output/YYYY-MM-DD.html` | メールと同じ内容の HTML |
| `logs/aidigest.log` | アプリのログ（日ごとにローテーション、30 日分） |
| `data/aidigest.db` | 記事・実行の記録・LLM の費用・ソースの取得結果・送ったダイジェスト |

---

## 7. 品質の確認

変更したら、次がすべて通ることを確かめます（CLAUDE.md）。

```bash
uv run pyright && uv run ruff check . && uv run pytest
uv run ruff format .        # 書式を整える
```

テストでは、外部のサイト・Anthropic API・SMTP はすべて偽物に差し替えていて、実際にはアクセスしません。

---

## 8. VPS への配置と cron の登録

本番は、さくらの VPS（Ubuntu 22.04、タイムゾーン Asia/Tokyo）で動かします。詳しい手順と記録は [specs/014-deploy.md](../specs/014-deploy.md) にあります。

### 8.1 初めて配置するとき

```bash
# 手元：履歴に秘密情報が無いことを確かめてから push する（pre-commit でも検査している）
git push origin main

# VPS：clone して環境を作り、テストを通す
ssh my-vps
cd ~/ws && git clone git@github.com:dreammind/ai-digest.git
cd ~/ws/ai-digest
~/.local/bin/uv sync --frozen
~/.local/bin/uv run --frozen pytest -q
exit

# 手元：.env を送る（作られた瞬間から所有者だけが読める）
ssh my-vps 'umask 077; cat > ~/ws/ai-digest/.env' < .env

# VPS：dry-run で確かめる（メールは送らない）
ssh my-vps 'cd ~/ws/ai-digest && ~/.local/bin/uv run --frozen python -m aidigest.cli run --dry-run'
```

### 8.2 cron の登録

既存の crontab をバックアップしてから、末尾に 1 行だけ足します。

```bash
crontab -l > ~/ws/ai-digest/data/crontab.backup-$(date +%Y%m%d)
crontab -e
```

足す行：

```
30 6 * * * cd /home/<ユーザー名>/ws/ai-digest && /home/<ユーザー名>/.local/bin/uv run --frozen python -m aidigest.cli run >> /home/<ユーザー名>/ws/ai-digest/logs/cron.log 2>&1
```

- cron の PATH は最小限なので、uv は絶対パスで呼びます。
- `logs/cron.log` には、uv の起動の失敗など、アプリより前の段階のエラーが残ります。

---

## 9. 運用

| やりたいこと | 方法 |
|---|---|
| コードを更新する | 手元で push → VPS で `cd ~/ws/ai-digest && git pull && ~/.local/bin/uv sync --frozen` |
| 配信を止める | VPS の crontab の該当の行の先頭に `#` を付ける |
| 今すぐ 1 回送る | VPS で `cd ~/ws/ai-digest && ~/.local/bin/uv run --frozen python -m aidigest.cli run` |
| ログを見る | VPS の `~/ws/ai-digest/logs/aidigest.log`（`logs/cron.log` も） |
| 費用を見る | メールのフッター（本日・今月）、または `data/aidigest.db` の `llm_calls` テーブル |
| 取得に失敗したソースを見る | メールのフッター、または `source_health` テーブル |
| crontab を元に戻す | `crontab ~/ws/ai-digest/data/crontab.backup-YYYYMMDD` |
| ソースやキーワードを調整する | `sources.yaml`・`topics.yaml` を直して push → VPS で `git pull`（コードの変更は不要） |

---

## 10. 困ったとき

| 症状 | 確かめること |
|---|---|
| メールが届かない | 迷惑メールのフォルダ。`logs/aidigest.log` の最後の「実行を終了しました」の行。記事が 0 件の日はメールを送らない（`digests.status = empty`） |
| 「実行に失敗しました」のメールが届いた | 本文の「段階」と「エラー」、`logs/aidigest.log` の該当時刻 |
| メールに「LLM が使えないため…」と出る | `.env` の `ANTHROPIC_API_KEY` |
| メールに「予算の上限…」と出る | `BUDGET_DAILY_USD` / `BUDGET_MONTHLY_USD`、`llm_calls` の合計 |
| 要約が無く、タイトルだけの記事が多い | Anthropic Console のクレジット残高（尽きると API がエラーになり、タイトルだけになる）。`logs/aidigest.log` の WARNING |
| 特定のソースから記事が来ない | フッターの「取得に失敗したソース」、`source_health` テーブル。Zenn の非公式 API は予告なく変わることがある |
| SMTP の認証に失敗する | アプリパスワードが有効か、`SMTP_USER` と `MAIL_FROM` が同じアドレスか |
| 終了コード 2（ほかの実行が動いている） | 前の実行が終わっているか。残っていれば `ps` で確かめる |

---

## 11. セキュリティの注意

- **秘密情報は `.env` にだけ書く。** コード・ログ・コミット・チャットに書かない。
- `.env`、`.env.*`（`.env.example` を除く）、エディタの一時ファイル（vim の `.swp` など）は Git の管理外にしてある。
- コミットするときは、`git add -A` / `git add .` を使わず、ファイル名を指定して追加する（未追跡の無関係なファイルを巻き込まないため）。
- pre-commit が、API キーらしい文字列を含むコミットを止める。
- VPS の `.env` は権限を `600`（所有者だけが読み書きできる）にする。
- API キーやアプリパスワードが漏れたら、すぐに失効させて作り直す（Anthropic Console の API Keys、Google のアプリパスワードの画面）。
