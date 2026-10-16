# 014: VPS への反映と cron の登録

- 状態: 承認済み
- 関連: SPEC.md §9（実行環境：さくらの VPS、cron、uv の絶対パス、`uv sync --frozen`）、§13（受け入れ基準：cron で毎朝届く、`uv sync --frozen` で再現できる）、CLAUDE.md（VPS への反映・cron の登録・メール送信は実行前に確認）、013

## 目的
さくらの VPS（Ubuntu 22.04）にアプリを置き、毎朝 6:30（日本時間）に cron で実行してダイジェストを届ける。

## 前提（2026-10-16 に確認）
- VPS：Ubuntu 22.04.5 LTS（x86_64）、タイムゾーン Asia/Tokyo、uv 0.10.0（`/home/<ユーザー名>/.local/bin/uv`）、システムの Python は 3.10（アプリの 3.12 は uv が別に用意する）
- VPS から GitHub へ ssh で認証できる（`dreammind`）
- crontab には既存の設定（trade 関係）がある。**既存の行には触れない**

## 手順
各手順は、実行する前にユーザーに確認する（CLAUDE.md）。

| # | 手順 | 場所 |
|---|---|---|
| 1 | 履歴に秘密情報が無いことを確かめてから、`main` を GitHub に push する | 手元 |
| 2 | `~/ws` で `git clone git@github.com:dreammind/ai-digest.git` | VPS |
| 3 | `uv sync --frozen` で環境を作り、`uv run pytest` でテストを通す | VPS |
| 4 | 手元の `.env` を `scp` で VPS の `~/ws/ai-digest/.env` に送り、`chmod 600` にする | 手元 → VPS |
| 5 | `uv run --frozen python -m aidigest.cli run --dry-run` を 1 回実行する（メールは送らない、約 1 円） | VPS |
| 6 | crontab をバックアップしてから、末尾に下の 1 行を足す | VPS |
| 7 | 翌朝、メールが届いたこと、`logs/aidigest.log` にエラーが無いことを確かめる | — |

### cron の行
```
30 6 * * * cd /home/<ユーザー名>/ws/ai-digest && /home/<ユーザー名>/.local/bin/uv run --frozen python -m aidigest.cli run >> /home/<ユーザー名>/ws/ai-digest/logs/cron.log 2>&1
```
- uv は絶対パスで呼ぶ（cron の PATH は最小限のため。SPEC §9）。
- アプリのログは `logs/aidigest.log`（日ごとにローテーション、30 日分）。`logs/cron.log` は、uv の起動の失敗など、アプリより前の段階のエラーを拾うため。

## 確認項目
- **AC-014-1**: Given 手元の `main` / When push する前に全履歴を検査する / Then API キーらしい文字列を含むファイルが 1 つも無い
- **AC-014-2**: Given VPS に clone したリポジトリ / When `uv sync --frozen` と `uv run pytest` を実行する / Then 依存がロックどおりに入り、テストがすべて通る（SPEC §13）
- **AC-014-3**: Given VPS の `.env` / When 権限を見る / Then `-rw-------`（所有者だけ読み書きできる）で、Git の管理外になっている
- **AC-014-4**: Given VPS / When `--dry-run` を実行する / Then 終了コード 0 で、`output/` に HTML ができ、メールは送られない
- **AC-014-5**: Given cron を登録した後 / When `crontab -l` を見る / Then 既存の行は変わらず、末尾に上の 1 行だけが足されている
- **AC-014-6**: Given 登録した翌朝 / When 6:30 を過ぎる / Then ダイジェストのメールが届き、`runs` に完了の記録があり、`logs/aidigest.log` にエラーが無い（SPEC §13「cron で毎朝自動実行され、ダイジェストメールが届く」）

## 決定事項
1. **配信の時刻は毎朝 6:30（日本時間）**。SPEC §14 の未決事項から外す。
2. **VPS の `.env` は、手元から `scp` で送る**（ssh で暗号化される）。送った後に `chmod 600` にする。
3. **コードは GitHub から clone する**。更新するときは VPS で `git pull` と `uv sync --frozen` を行う。

## 運用のメモ
- 更新：手元で push → VPS で `cd ~/ws/ai-digest && git pull && uv sync --frozen`
- 止める：crontab の該当の行の先頭に `#` を付ける
- Anthropic Console で、API の月の利用上限を設定しておくと、アプリの予算ガードが効かない場合の最後の歯止めになる（SPEC §14）
