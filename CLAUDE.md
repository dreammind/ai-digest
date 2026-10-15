# CLAUDE.md

## プロジェクト概要
Claude Code と LLM に関する情報を RSS/API から収集し、日本語で要約して毎朝メール配信する Python アプリ。
詳細な仕様は `SPEC.md` を参照すること。仕様と実装が食い違う場合は、勝手に変えずに確認する。

## 技術スタック
- Python 3.12+、uv（パッケージ・仮想環境管理）、SQLite、cron（さくらの VPS / Ubuntu）
- LLM: Anthropic API（Haiku クラスを基本）
- 主要ライブラリ: feedparser, httpx, trafilatura, anthropic, jinja2, pydantic, pytest
- 開発ツール: pyright（strict）, ruff, pytest, pytest-cov, pre-commit

## ディレクトリ構成（予定）
```
src/aidigest/
  fetchers/     # ソース別の取得処理
  pipeline/     # 正規化・重複排除・スコアリング・要約
  db.py
  mailer.py
  render.py     # HTML/メール生成（jinja2）
  cli.py
templates/
sources.yaml
topics.yaml     # テーマ別の重みとキーワード（high / medium / negative）
tests/
specs/          # 機能単位の詳細仕様（SDD）
pyproject.toml  # 依存・ツール設定（uv.lock もコミットする）
output/         # 静的HTML出力（Git管理外）
logs/           # ログ（Git管理外）
```

## コマンド
- 環境構築: `uv sync`
- 依存の追加: `uv add <package>`（開発用は `uv add --dev <package>`）。`pip install` や `python -m venv` は使わない
- 実行: `uv run python -m aidigest.cli run`
- オプション: `--dry-run`（送信・DB更新なし）、`--no-llm`（LLM を呼ばない）
- 型チェック: `uv run pyright`
- テスト: `uv run pytest --cov --cov-fail-under=80`
- Lint/整形: `uv run ruff check . && uv run ruff format .`
- **変更後は必ず `uv run pyright && uv run ruff check . && uv run pytest` をすべて通してから完了とする**

## 開発プロセス（SDD / TDD：必須・例外なし）

### SDD（仕様駆動）
- `SPEC.md` と `specs/` が唯一の正。仕様に書かれていない挙動を実装しない。
- 作業は必ず **仕様 → テスト → 実装** の順。仕様にない変更が必要になったら、実装を止めて仕様の修正案を提示し、承認を得てから進める。
- 機能に着手する前に、`specs/NNN-<name>.md` に受け入れ基準（Given / When / Then、ID 付き）を書いて承認を得る。
- テスト名またはコメントに対応する受け入れ基準 ID を入れる。
- 完了前に、仕様・テスト・実装が一致していることを確認する。ずれがあれば報告する。

### TDD（テスト駆動）
- Red → Green → Refactor を守る。
  1. 失敗するテストを先に書き、実行して **期待した理由で失敗すること** を確認する（Red）
  2. テストを通す最小限の実装を書く（Green）
  3. テストが通ったままリファクタリングする
- テストより先に実装コードを書かない。「後でテストを書く」は禁止。
- テストが失敗したとき、テストを書き換えて通さない。テストが仕様と食い違っている場合のみ、仕様を確認したうえで修正する。
- 外部通信（フィード取得、Anthropic API、SMTP）はテストで必ずモック/フェイクにし、実ネットワークに出さない。
- コミットは `test:`（Red）→ `feat:`（Green）→ `refactor:` の順に分ける。
- 各ステップで、いまどの段階（Red / Green / Refactor）かを短く報告する。

## 開発ルール
- パッケージ管理は uv のみを使う。
- まず Phase 0（初期化）を行い、その後 Phase 1（MVP）だけを実装する。Phase 2 以降の機能を先回りして作らない。
- 実装前に計画を提示し、承認を得てから着手する。大きな変更は小さなステップに分けてコミットする。
- 秘密情報（API キー、SMTP 認証情報）は `.env` のみ。コード・ログ・コミットに含めない。`.env.example` を用意する。
- LLM 呼び出しは必ず 1 つのラッパー経由にし、トークン数と概算コストを記録する。月額 300 円の予算超過を防ぐ上限チェックをそこに実装する。
- 外部アクセス（フィード取得・API・SMTP）は、タイムアウト、リトライ、ソース単位の例外隔離を入れる。1 つの失敗で全体を止めない。
- 外部への HTTP アクセスは、ユーザーエージェントを明示し、同一ホストへの連続アクセスに間隔を置く。
- フィード URL は、実在することを実際に取得して確認してから `sources.yaml` に入れる。推測で書かない。
- 重要なロジック（正規化、重複排除、コスト上限、スコアの閾値判定）は特にテストを厚くする。テストは常に実装より先に書く（TDD）。
- 実際にメールを送る、cron を登録する、VPS に反映するといった操作は、実行前にユーザーへ確認する。

## 型の方針
- すべての関数・メソッドに引数と戻り値の型ヒントを付け、`pyright` の strict モードを通す。
- `Any` や `# type: ignore` は原則使わない。やむを得ない場合は理由をコメントで残す。
- 外部との境界（フィード取得結果、LLM の応答 JSON、`sources.yaml` / `.env`）は pydantic モデルで検証する。検証に失敗した場合はその記事のスキップかリトライとして扱い、全体を止めない。
- 型エラーや lint エラーを、設定の緩和や無視コメントで消さない。コードを直す。

## コードスタイル
- 設定値はハードコードせず `sources.yaml` / `.env` / 設定クラスに置く。
- ログは標準 `logging`。print は使わない。
- コメント・docstring は日本語でよい。識別子は英語。
