# 015: プロファイルの切り替え

- 状態: 承認済み
- 関連: SPEC.md §1（プロファイルの表）、§4「プロファイルの切り替え」、§6「`current-events` の場合」（予算の共有）、§7「プロファイル」、§9（cron）、001・002・003・007・013

## 目的
同じ処理の流れを、設定違いの 2 つの配信（`dev` と `current-events`）で動かせるようにする。
この仕様では、プロファイルを選ぶ仕組み（CLI・設定・DB）だけを作る。`current-events` の中身（ソース、キーワード、プロンプト、メールの形）は 016 で作る。

## 範囲
- 含む:
  - CLI の `--profile`
  - プロファイルごとの設定ファイルのパス
  - DB のマイグレーション（`runs` と `articles` に `profile`。既読判定をプロファイルごとにする）
  - 予算ガードと費用の表示を、2 つのプロファイルの合計にすること（今の実装のままで満たすことを、テストで確かめる）
  - 失敗通知メールの件名へのプロファイル名
- 含まない:
  - `current-events` のソース・キーワード・プロンプト・メールの形：016
  - cron の行の変更と VPS への反映：016 の実装が終わってから（014 の手順で行う）

## 追加する設定値
| 変数 | 型 | 既定値 | 用途 |
|---|---|---|---|
| `CURRENT_EVENTS_SOURCES_PATH` | パス | `sources-current-events.yaml` | `current-events` のソース |
| `CURRENT_EVENTS_TOPICS_PATH` | パス | `topics-current-events.yaml` | `current-events` のテーマ・採点・件数 |

`dev` は今の `SOURCES_PATH`・`TOPICS_PATH` を使う。DB・ロック・ログ・単価表・予算・SMTP・宛先は、2 つのプロファイルで共通にする。

## 振る舞い
1. **CLI**: `python -m aidigest.cli run [--profile {dev,current-events}] [--dry-run] [--no-llm]`。`--profile` を指定しなければ `dev`。ほかの値は argparse のエラー（終了コード 2）。
2. **設定ファイルの選択**: `dev` は `SOURCES_PATH`・`TOPICS_PATH`、`current-events` は `CURRENT_EVENTS_SOURCES_PATH`・`CURRENT_EVENTS_TOPICS_PATH` を読む。読めない・不正なときの扱いは 013 と同じ（`ConfigError` → 失敗通知、終了コード 1）。
3. **プロファイルごとに切り替える部品**（016 で `current-events` 用を作るまでは、`dev` の部品だけがある）:
   - 再ランクのシステムプロンプト
   - 要約のスキーマ・ツール定義・システムプロンプト
   - 枠配分の方法
   - メールのテンプレート・件名、静的 HTML を書き出すかどうか
4. **DB**（マイグレーション `0002_profiles`）:
   - `runs` に `profile TEXT NOT NULL DEFAULT 'dev' CHECK (profile IN ('dev', 'current-events'))` を足す。
   - `articles` に同じ `profile` を足し、一意の制約を `url` だけから `(profile, url)` の組に変える。SQLite は制約を後から変えられないため、テーブルを作り直して行を移す（`id` は変えない）。既存の行は `dev` になる。
   - `runs` には、その実行のプロファイルを記録する。
5. **既読判定**（003）: 同じプロファイルの記事の中で URL を照合する。別のプロファイルに同じ URL があっても、未読として登録する。
6. **予算と費用**: 予算ガード（007）と、メールに出す当日・当月の費用は、プロファイルに関係なく `llm_calls` 全体の合計で求める（今の実装のまま。`llm_calls` は変えない）。
7. **ログ**: 実行の開始のログに、プロファイル名を出す。
8. **失敗通知メール**: 件名を `[AI Digest] 実行に失敗しました（<プロファイル名>） YYYY-MM-DD` にする（`dev` も含む）。
9. **ロック**: ロックファイルは共通。2 つのプロファイルは同時に動かさず、cron で順に実行する（SPEC §9）。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| 未知のプロファイル名 | argparse のエラーで、終了コード 2（AC-015-1） |
| `current-events` の設定ファイルが無い・不正 | 013 と同じく失敗通知メールを送り、終了コード 1（AC-015-3） |
| マイグレーションの途中で失敗 | 002 と同じくロールバックし、DB はバージョン 1 のまま（AC-015-5） |
| ほかのプロファイルの実行がロックを持っている | 013 と同じく何もせず、終了コード 2（AC-015-10） |

## 受け入れ基準
- **AC-015-1**: Given `--profile` なし／`--profile current-events`／`--profile foo` / When CLI の引数を解釈する / Then それぞれ `dev`・`current-events` になり、`foo` は使い方を表示して終了コード 2
- **AC-015-2**: Given 設定値を設定していない / When `Settings` を作る / Then `CURRENT_EVENTS_SOURCES_PATH`・`CURRENT_EVENTS_TOPICS_PATH` は表の既定値になる。`.env.example` に載っている（AC-001-13 のテストで確認する）
- **AC-015-3**: Given `dev` と `current-events` で別の内容のソース・テーマのファイル / When それぞれのプロファイルで `run` を実行する / Then それぞれ自分のファイルのソースから取得する。`current-events` のファイルが無いときは、失敗通知メールが送られて終了コード 1
- **AC-015-4**: Given バージョン 1 の DB（記事と実行の行が入っている） / When DB を開く / Then 0002 が適用されてバージョン 2 になり、既存の `articles` と `runs` の行は `profile = 'dev'` で、`articles` の `id` とほかの列の値は変わらない
- **AC-015-5**: Given 0002 の途中で失敗するようにした DB / When DB を開く / Then `DbError` になり、DB はバージョン 1 の状態のまま
- **AC-015-6**: Given `dev` で登録済みの URL の記事 / When `current-events` で既読判定する / Then 未読として `profile = 'current-events'` で登録され、出力に含まれる。`dev` の行は変わらない
- **AC-015-7**: Given 同じプロファイルで登録済みの URL の記事 / When 同じプロファイルで既読判定する / Then 出力に含まれない（AC-003-11 がプロファイルごとに成り立つ）
- **AC-015-8**: Given `dev` の実行で使った費用が記録されている / When `current-events` で LLM を呼ぶ / Then 予算ガードは `dev` の費用も合わせた当日・当月の累計で判定し、メールの費用の表示も合計になる
- **AC-015-9**: Given `--profile current-events` / When `run` を実行する / Then `runs.profile` が `current-events` になり、開始のログにプロファイル名が出る。失敗したときの通知メールの件名にも `（current-events）` が入る
- **AC-015-10**: Given `dev` の実行がロックを持っている / When `current-events` で `run` を実行する / Then 何もせず、終了コード 2
- **AC-015-11**: Given `--profile` なし / When `run` を実行する / Then 失敗通知メールの件名にプロファイル名が入ること以外は、今の 013 の受け入れ基準をすべて満たす（013 のテストがそのまま通る）

## 実装方針（参考）
- プロファイルは `Literal["dev", "current-events"]` で表す。プロファイルごとの部品（プロンプトのファイル名、要約のスキーマ、テンプレート、件名、枠配分）は、1 か所の対応表（frozen dataclass）にまとめ、`_Run` に渡す。
- `articles` の作り直し：`CREATE TABLE articles_new (...)` → `INSERT INTO articles_new SELECT ...` → `DROP TABLE articles` → `ALTER TABLE articles_new RENAME TO articles` → インデックスを作り直す。ほかのテーブルから `articles` への外部キーは無い（`digests.article_ids` は JSON）ので、外部キーの設定は変えなくてよい。
- `register_items` に `profile` の引数を足す。

## 決定事項
1. **`profile` の列は `runs` と `articles` だけに足す**（SPEC §7 からの変更の提案）：SPEC §7 では `digests` と `llm_calls` にも足すとしたが、どちらも `run_id` から `runs.profile` をたどれる。同じ情報を 2 か所に持つと食い違いうるので、足さない。SPEC §7 もこれに合わせて直した。
2. **失敗通知の件名には `dev` のときもプロファイル名を入れる**：2 つのプロファイルが同じ朝に続けて動くので、どちらが失敗したのかを件名で分かるようにする。013 の失敗通知の件名を変えることになる。
3. **設定ファイルのパスはプロファイルごとの変数にする**：プロファイル名からファイル名を組み立てる方法（`sources-<名前>.yaml`）もあるが、`dev` の `sources.yaml` が規則から外れるため、今の変数と並べて明示する。

## 未決事項
- なし
