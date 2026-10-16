# AI Digest

Claude Code と LLM に関する記事を毎朝集め、Claude Haiku で日本語に要約して、1 通のメールで届ける Python アプリです。
Zenn・Qiita・Hacker News・Simon Willison のブログから記事を取得し、キーワードと LLM で選んだ最大 8 件を、要約と「AI活用」（Claude Code の開発にどう活かせるか）つきで配信します。

- 本番：さくらの VPS で、cron により毎朝 6:30（日本時間）に実行
- 費用：LLM は 1 日あたり約 1.3 円（1 か月 約 40 円）。予算ガードつき
- 状態：Phase 1（MVP）の実装と VPS への反映が完了（2026-10-16）

## 文書

| 文書 | 内容 |
|---|---|
| [doc/Setup.md](doc/Setup.md) | 環境設定ガイド（設定項目、実行方法、VPS への配置、運用、困ったとき） |
| [SPEC.md](SPEC.md) | 全体の仕様 |
| [specs/](specs/) | 機能ごとの詳細な仕様と受け入れ基準（書き方は [specs/README.md](specs/README.md)） |
| [doc/BugHistory.md](doc/BugHistory.md) | バグ修正記録（症状・原因・修正内容） |
| [doc/Retrospective.md](doc/Retrospective.md) | 振り返りの記録（うまくいったこと、問題と対策、決めたこと） |
| [CLAUDE.md](CLAUDE.md) | 開発のルール（仕様駆動・テスト駆動、型、秘密情報の扱い、コミットの仕方） |

## すぐに使うコマンド

```bash
uv sync                                          # 環境を作る
uv run python -m aidigest.cli run --dry-run      # メールを送らずに試す
uv run python -m aidigest.cli run                # 取得からメール送信までを 1 回実行する
uv run pyright && uv run ruff check . && uv run pytest   # 変更後の確認
```

設定（`.env` など）は [doc/Setup.md](doc/Setup.md) の 5 章を見てください。

## 機能と仕様の状態

| # | 機能 | 状態 |
|---|---|---|
| [001](specs/001-config.md) | 設定の読み込み（`sources.yaml` / `.env`） | 実装済み |
| [002](specs/002-db.md) | SQLite のスキーマとマイグレーション | 実装済み |
| [003](specs/003-normalize-dedupe.md) | URL 正規化と既読判定 | 実装済み |
| [004](specs/004-fetchers.md) | フェッチャー（RSS / HN 検索 / Qiita 検索 / Zenn） | 実装済み |
| [005](specs/005-prefilter.md) | 事前の絞り込み（時間窓・除外キーワード・ソース別ルール） | 実装済み |
| [006](specs/006-local-score.md) | ローカルスコアと重複話題の除去 | 実装済み |
| [007](specs/007-llm-client.md) | LLM ラッパーと予算ガード | 実装済み |
| [008](specs/008-rerank.md) | LLM の再ランク | 実装済み |
| [009](specs/009-slot-allocation.md) | セクションへの振り分けと枠配分 | 実装済み |
| [010](specs/010-summarize.md) | 本文の取得と日本語の要約 | 実装済み |
| [011](specs/011-render.md) | ダイジェストの HTML の生成 | 実装済み |
| [012](specs/012-mailer.md) | メール送信（SMTP） | 実装済み |
| [013](specs/013-cli.md) | CLI と処理全体の統合 | 実装済み |
| [014](specs/014-deploy.md) | VPS への反映と cron の登録 | 反映済み（初回の配信は 2026-10-17 の朝に確認） |

テスト：408 件、カバレッジ 99%（2026-10-16 時点）。

## 改善の候補（未着手）

運用の中で気づいたことです。着手するときは、`specs/` に仕様を書いて承認を得てから進めます（[CLAUDE.md](CLAUDE.md)）。

| 候補 | きっかけ |
|---|---|
| LLM のエラー（クレジットの残高切れなど）が続いたら、メールにお知らせを付ける | いまはお知らせが付くのが「API キーが無い」「予算の上限」のときだけで、残高切れに気づきにくい |
| 要点（`key_takeaways`）に空の文字列が混ざっていたら、不正とせずに取り除く | VPS での dry-run で 1 回、これが原因の再試行が起きた |
| 「その他」の枠が埋まらない日がある（7 件しか選ばれない） | 上位 30 件がすべて Claude Code か LLM のテーマに当てはまる。上限の合計や枠の融通を検討する |
| 記事の選ばれ方・要約の長さの調整 | 要約は指示の「2〜3 文」より長い 4〜7 文になる。数日分の配信を見て判断する |
| ソース名を `zenn-claudecode` などの id ではなく、読みやすい表示名にする | `sources.yaml` に表示名の項目が無い（[011](specs/011-render.md) の決定事項） |
| 保持期間による古い記録の削除 | SPEC §7。Phase 1 の範囲外にした（[002](specs/002-db.md)） |
| このアプリ専用の API キーにする（Anthropic Console の Workspace を分け、利用上限を設定する） | Console の費用に別の用途の分が混ざり、アプリの費用が見分けにくかった |
| 配信されなかったことに気づく仕組み（外部の死活監視。例：実行の最後に healthchecks.io などへ通知する） | VPS や cron が止まると、失敗通知のメールも送られないため気づけない |
| 要約の長さの仕様と実際のずれを解消する（仕様は 2〜3 文、実際は 4〜7 文） | [010](specs/010-summarize.md) と実際の出力が合っていない。仕様を直すか、プロンプトを直すかを決める |
| VPS の uv を更新する（0.10.0 → 手元と同じ版） | 手元と VPS で uv の版が違う。ロックファイルの扱いの差を避ける |
| DB（`data/aidigest.db`）のバックアップ | 既読の記録と費用の記録が VPS の 1 ファイルにしか無い |
| 仕様のエラー時の表の各行にも受け入れ基準の ID を振る（`specs/README.md` のルール） | 表の行がテストと対応づかず、抜けに気づきにくい |
| Phase 2 以降（ソースの拡充、静的 HTML の Web 配信、検索、フィードバック） | [SPEC.md](SPEC.md) §12 |

## 新しいセッションで作業を始めるとき

Claude Code では、機能（`specs/` の 1 つ）ごとにセッションを分けると、ルールの見落としが減ります。状態はこの README・`specs/`・コミットの履歴に残っているので、たとえば次のように始めます。

> CLAUDE.md と README.md を読んで。改善の候補の「LLM のエラーが続いたらお知らせを付ける」に取り組みたい。まず specs/015 の草案を書いて。
