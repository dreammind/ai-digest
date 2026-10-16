# 012: メール送信（SMTP）

- 状態: 実装済み
- 関連: SPEC.md §8（SMTP リレー経由で送信、VPS から直接は送らない）、§9（失敗時の通知メール）、§14（SMTP サービス）、CLAUDE.md（外部アクセスはタイムアウト・再試行、秘密情報は `.env` のみ）、001、011

## 目的
011 で作ったダイジェスト（件名・HTML・テキスト）を、SMTP リレー経由でメールとして送る。
送信先のサービスは設定だけで切り替えられるようにする（まず Gmail、だめならさくらインターネットの SMTP。決定事項 1）。

## 範囲
- 含む:
  - メールの組み立て（HTML とテキストの multipart/alternative）
  - SMTP の接続（STARTTLS / SSL）、認証、送信
  - タイムアウトと再試行、エラーの分類
  - 設定値の追加
- 含まない:
  - いつ送るか・送らないか（`--dry-run`、記事 0 件の日など）、失敗通知メールの文面：013（この機能は渡された件名と本文を送るだけ）

## 設定（`.env` / 環境変数）
001 で定義済みのもの：`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`MAIL_FROM`、`MAIL_TO`。次を追加する。

| 変数 | 型 | 既定値 | 用途 |
|---|---|---|---|
| `SMTP_SECURITY` | `starttls` / `ssl` | `starttls` | 暗号化の方式。`starttls` は平文で接続してから暗号化（Gmail の 587 番）、`ssl` は最初から暗号化（465 番） |
| `SMTP_TIMEOUT_SECONDS` | 正の数 | `30` | 接続・送受信のタイムアウト |
| `SMTP_RETRIES` | 0 以上の整数 | `1` | 一時的なエラーのときの再試行の回数 |
| `SMTP_RETRY_WAIT_SECONDS` | 0 以上の数 | `10` | 再試行の前に待つ時間 |

- `MAIL_TO` はカンマ区切りで複数の宛先を書ける（前後の空白は無視）。
- 暗号化なしの送信は用意しない（パスワードを平文で送らないため）。

### Gmail の場合の例
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=<Gmail のアドレス>
SMTP_PASSWORD=<アプリパスワード（16 文字）>
MAIL_FROM=<Gmail のアドレス>
MAIL_TO=<受け取るアドレス>
```

## 振る舞い

### メールの組み立て
- `Subject`：011 の件名（UTF-8 でエンコード）
- `From`：`MAIL_FROM`、`To`：`MAIL_TO` の宛先（複数ならカンマ区切り）
- `Date`：送信時刻、`Message-ID`：`MAIL_FROM` のドメインで生成
- 本文：テキスト（`text/plain`）と HTML（`text/html`）の `multipart/alternative`。この順に入れる（メールソフトは後の HTML を優先して表示する）。どちらも UTF-8。

### 送信
1. 必要な設定（`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`MAIL_FROM`、`MAIL_TO`）がそろっているか確かめる。足りなければ、接続せずに `MailConfigError` を送出する（足りない変数の名前をメッセージに入れる。値は入れない）。
2. `SMTP_SECURITY` が `starttls` なら平文で接続して STARTTLS で暗号化し、`ssl` なら最初から TLS で接続する。どちらも証明書を検証する（標準の SSL コンテキスト）。
3. `SMTP_USER` と `SMTP_PASSWORD` で認証し、送信して、接続を閉じる。
4. 一部の宛先だけが拒否されたら、拒否された宛先を WARNING ログに出す（送信は成功とする）。

### 再試行とエラー
- 一時的なエラー（接続できない・タイムアウト・接続が切れた・SMTP の 4xx 応答）：`SMTP_RETRY_WAIT_SECONDS` 待ってから、`SMTP_RETRIES` 回まで再試行する。尽きたら `MailError`。
- 恒久的なエラー（認証の失敗・SMTP の 5xx 応答・すべての宛先が拒否された）：再試行せずに `MailError`。
- `MailError`・`MailConfigError` のメッセージとログには、パスワードを含めない。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| 必要な設定が足りない | 接続せず `MailConfigError` |
| 一時的なエラー | 待ってから再試行。尽きたら `MailError` |
| 認証の失敗・5xx・すべての宛先が拒否 | 再試行せず `MailError` |
| 一部の宛先が拒否 | 成功とし、WARNING ログ |

## 受け入れ基準
- **AC-012-1**: Given 追加した設定値を設定していない / When `Settings` を作る / Then 表の既定値になる。`.env.example` に載っている（AC-001-13 のテストで確認する）。`SMTP_SECURITY` に `starttls`・`ssl` 以外を書くと `ConfigError`
- **AC-012-2**: Given 件名・HTML・テキストと、`MAIL_TO=a@example.com, b@example.com` / When メールを組み立てる / Then 件名（日本語を含む）・`From`・`To`（2 つの宛先）・`Date`・`Message-ID` があり、本文はテキスト → HTML の順の `multipart/alternative` で、どちらも UTF-8 で元の内容に戻せる
- **AC-012-3**: Given `SMTP_SECURITY=starttls` / When 送信する / Then `SMTP_HOST:SMTP_PORT` にタイムアウト付きで接続し、STARTTLS（標準の SSL コンテキスト）→ 認証 → 送信 → 切断の順に行う
- **AC-012-4**: Given `SMTP_SECURITY=ssl` / When 送信する / Then 最初から TLS で接続し（STARTTLS は行わない）、認証 → 送信 → 切断の順に行う
- **AC-012-5**: Given `SMTP_PASSWORD` と `MAIL_TO` が無い設定 / When 送信する / Then 接続せずに `MailConfigError` が送出され、メッセージに 2 つの変数名が含まれる
- **AC-012-6**: Given 1 回目に一時的なエラー（接続の失敗／接続が切れた／421 応答）、2 回目に成功するサーバー / When 送信する / Then `SMTP_RETRY_WAIT_SECONDS` 待ってから再試行し、成功する
- **AC-012-7**: Given 認証に失敗する（535）／ 550 応答／すべての宛先を拒否するサーバー / When 送信する / Then 再試行せずに `MailError` が送出され、メッセージにパスワードが含まれない
- **AC-012-8**: Given 常に一時的なエラーを返すサーバーと `SMTP_RETRIES=1` / When 送信する / Then 2 回試して `MailError` が送出される
- **AC-012-9**: Given 一部の宛先だけを拒否するサーバー / When 送信する / Then 成功し、拒否された宛先が WARNING ログに出る

## 実装方針（参考）
- 標準の `smtplib` と `email` を使う（依存は追加しない）
- モジュール：`src/aidigest/mailer.py`（CLAUDE.md のディレクトリ構成のとおり）
- テストでは SMTP の接続を偽物に差し替え（接続を作る関数を引数で渡す）、待ち時間も差し替える。実際には送らない
- 実装の後、**送信する前にユーザーに確認したうえで**、実際に 1 通だけ自分宛てに送って確かめる（CLAUDE.md）

## 決定事項
1. **送信先は設定で切り替える**：まず Gmail（`smtp.gmail.com:587`、STARTTLS、アプリパスワード）。だめならさくらインターネットの SMTP（`.env` の `SMTP_HOST`・`SMTP_PORT`・`SMTP_SECURITY`・認証情報を書き換えるだけ）。SPEC §14 の未決事項を、この内容で解決済みにする。
2. **暗号化なしの送信は用意しない**：パスワードを平文で送らないため。
3. **再試行は 1 回、10 秒待つ**：メールは 1 日 1 通なので、少し待ってでも届けたい。cron の実行時間が延びても数十秒に収まる。
4. **`MAIL_TO` は複数の宛先を書ける**：013 の失敗通知も同じ宛先に送る。

## 実際の送信での確認（2026-10-16）
- ユーザーの確認を得て、Gmail の SMTP（`smtp.gmail.com:587`、STARTTLS、アプリパスワード）で、011 のサンプルのダイジェストを `MAIL_TO` に 1 通送った。1 回目で成功した（再試行なし）。
- 受信トレイに届き（迷惑メールに入らなかった）、件名・本文の日本語は文字化けせず、HTML 版はプレビューと同じ見た目だった。
- 本物の `smtplib` で接続する関数（単体テストでは偽物に差し替えている部分）も、この送信で動作を確かめた。
