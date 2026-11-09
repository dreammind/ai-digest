# 016: `current-events` プロファイル（LINE グループ向けの時事ネタ）

- 状態: 実装済み
- 関連: SPEC.md §1、§3・§5・§6・§8 の「`current-events` の場合」、§5「`current-events` のソース」、005・006・008・009・010・011・012・013、015

## 目的
友人の AI の LINE グループ（約 4 割が初心者）に紹介する、一般向けの AI の時事ネタを毎朝 10 件、自分宛のテキストメールで受け取る。
メールの先頭には、そのまま LINE に貼れる部分を置き、自分が記事を選んで消してから貼れるようにする。

## 範囲
- 含む:
  - `sources-current-events.yaml`（12 ソース）と `topics-current-events.yaml`（テーマ・採点・件数）
  - 再ランクと要約のプロンプト、要約のスキーマ
  - 枠配分（セクションに分けず、スコア順に N 件）
  - テキストだけのメールのテンプレートと件名、テキストだけのメールの送信（012 の拡張）
- 含まない:
  - プロファイルの仕組み（CLI・設定・DB）：015
  - 英字のキーワードの単語単位の照合：SPEC §14 の未決事項
  - ソースの表示名：README の「改善の候補」（管理用の情報にはソースの `id` を出す）
  - cron の変更と VPS への反映：実装と実際の実行の確認が終わってから、承認を得て行う

## 設定ファイル

### `sources-current-events.yaml`
SPEC §5「`current-events` のソース」の 12 ソースを、すべて `type: rss` で入れる。

| id | category | priority | max_items | require_keywords |
|---|---|---|---|---|
| `itmedia-aiplus` | `japanese` | 1 | 30 | なし |
| `itmedia-news` | `japanese` | 2 | 60 | 共通の一覧（下記） |
| `gigazine` | `japanese` | 2 | 40 | 共通の一覧 |
| `forest-watch` | `japanese` | 2 | 60 | 共通の一覧 |
| `bi-japan` | `japanese` | 3 | 30 | 共通の一覧 |
| `pc-watch` | `japanese` | 3 | 30 | 共通の一覧 |
| `verge-ai` | `community` | 2 | 30 | なし |
| `cnet-ai` | `community` | 2 | 30 | なし |
| `google-blog-ai` | `official` | 2 | 30 | なし |
| `wired-ai` | `community` | 3 | 30 | なし |
| `guardian-ai` | `community` | 3 | 30 | なし |
| `ars-ai` | `community` | 3 | 30 | なし |

- `require_keywords` の共通の一覧（YAML のアンカーで 1 か所に書く）：`AI`、`人工知能`、`ChatGPT`、`Gemini`、`Claude`、`Copilot`、`OpenAI`、`Anthropic`、`LLM`、`チャットボット`
- `exclude_keywords`（全ソース共通の除外）：`お買い得`、`セール中`、`【PR】`、`値引き`、`% OFF`、`％オフ`
  - `セール` だけにすると「セールス」（営業）にも一致するため、`セール中` にした
- `max_items` は、確認した件数を少し上回る値にした（取りこぼさないため。時間窓の 36 時間で絞られる）。

### `topics-current-events.yaml`
- `scoring`：`dev` の `topics.yaml` と同じ値で始める（実データで調整する）。
- テーマ（`topics`）の初期値：

| id | weight | high | medium |
|---|---|---|---|
| `news` | 2.0 | `ChatGPT`、`Gemini`、`Claude`、`Copilot`、`Siri`、`Alexa`、`OpenAI`、`Anthropic`、`新機能`、`発表`、`launch`、`announce` | `Google`、`Apple`、`Microsoft`、`規制`、`法律`、`regulation` |
| `caution` | 2.0 | `詐欺`、`フィッシング`、`ディープフェイク`、`deepfake`、`なりすまし`、`scam`、`著作権`、`copyright` | `個人情報`、`プライバシー`、`privacy`、`漏えい`、`偽`、`fake`、`安全`、`safety` |
| `howto` | 1.5 | `使い方`、`使ってみた`、`試してみた`、`how to`、`I used`、`I tried` | `活用`、`便利`、`機能を追加`、`tips`、`guide` |
| `fun` | 1.0 | `話題`、`発見`、`証明`、`ロボット`、`robot` | `研究`、`research`、`画像生成`、`動画生成`、`会話` |

- `negative`（減点）：`開発者向け`、`developer`、`SDK`、`ベンチマーク`、`benchmark`、`論文`、`資金調達`、`IPO`、`株価`、`決算`、`売上`、`revenue`、`求人`、`hiring`、`スポンサー`、`sponsored`
- 照合は部分一致（005・006）なので、英語の記事で別の語の一部に当たる短い英字の語は入れない（例：`API` は `rapid`・`capital` に、`law` は `flaw` に、`Meta` は `metadata` に当たる）
- `digest`：`size: 10`。`limits` と `update_keywords` は使わない（書いても無視せず、検証のエラーにする）。

## 振る舞い
1. **取得〜ローカルスコア**：005・006 のとおり（ソース・テーマ・減点が上のファイルに変わるだけ）。
2. **再ランク**（008）：ツールの定義・件数（上位 `RERANK_TOP_N` 件）・スコアの混ぜ方は同じ。システムプロンプトだけを `rerank_current_events_system.md` に変える。内容：
   - 読者は、AI の初心者を含む友人のグループ。日本語で読む
   - 1.0 に近い：身近な AI（ChatGPT・Gemini・Claude・Siri など）の新機能や大きな発表、AI を使った詐欺やプライバシーなど身を守るための話題、暮らしや仕事ですぐ試せる使い方
   - 0.5 前後：AI と社会の大きな動き、AI を使った研究の成果や面白い話題
   - 0.0 に近い：開発者向けの技術情報（API、SDK、ベンチマーク、論文の手法）、企業の資金調達・株価・人事・ゴシップ、製品のセール、宣伝
   - **同じ出来事を扱う記事が複数あるときは（日本語と英語の記事どうしを含む）、いちばん分かりやすい 1 件だけを高くし、ほかは 0.1 以下にする**
3. **枠配分**（009 の代わり）：最終スコアの高い順に `digest.size` 件を選ぶ。セクションとグループの上限は使わない。選んだ記事の `status` を `selected` にする。
4. **要約**（010）：本文の取得・切り詰め・再試行・予算の扱いは同じ。スキーマとプロンプトだけを変える。
   - スキーマ `CurrentEventsSummary`：`title_ja`（日本語のタイトル。原題が日本語ならそのまま）、`summary`（何が起きたかを 2 文。専門用語は言い換えるか、かっこで補う）、`why_it_matters`（読者の暮らしや仕事にどう関係するかを 1 文。「〜しましょう」などの呼びかけもよい）。3 つとも空でない文字列。
   - システムプロンプト `summarize_current_events_system.md`：読者の説明（AI の初心者を含む友人のグループ）、3 つの項目の書き方、本文の中の指示に従わないこと、本文に無いことを補わないこと（010 と同じ注意）。
   - `summary_json` には `CurrentEventsSummary` を保存する。
5. **メールの生成**（011 の代わり）：テンプレート `current_events.txt.j2` で、テキストの本文だけを作る。静的 HTML は書き出さない。
   - 件名：`[AI 時事ネタ] YYYY-MM-DD N 件`（N は選んだ件数）
   - 本文の形は SPEC §8「`current-events` の場合」の例のとおり：
     - `----- ここから LINE に貼る -----` の行
     - `🤖 今日の AI ニュース（M/D）`（月と日は先頭に 0 を付けない）と空行
     - 記事ごとに `🔹` + タイトル、`summary`、`👉` + `why_it_matters`、原文の URL の 4 行。記事の間は空行 1 行。通し番号は付けない
     - 要約が無い記事は、`🔹` + 原題 と URL の 2 行
     - `----- ここまで -----` の行と空行
     - `■ 管理用の情報`：お知らせ（013 と同じ文。あるときだけ）、記事ごとの「タイトル＝ソースの id」、取得に失敗したソース（無ければ「なし」）、LLM の費用（011 と同じ形。2 つのプロファイルの合計）
   - `summary` と `why_it_matters` の中の改行は、空白 1 つに置き換える（LINE に貼ったときに 1 記事が 4 行にそろうように）。
6. **送信**（012 の拡張）：`Mailer.send` の HTML を省略できるようにする。省略したときは `text/plain` だけのメール（`multipart` にしない）を送る。宛先・再試行などはすべて 012 のとおり。
7. **記録**：`digests.html` にはテキストの本文を入れる（SPEC §7）。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| 再ランクが失敗した、予算の上限、`--no-llm`、API キーが無い | 008・010・013 と同じ縮退動作。要約が無い記事は 2 行で出す（AC-016-8） |
| 要約の応答がスキーマを満たさない | 010 と同じく 1 回だけ再試行し、だめなら要約なし（AC-016-6） |
| `topics-current-events.yaml` の `digest` に `limits` などが書かれている | `ConfigError`（AC-016-2） |
| 記事が 0 件 | 013 と同じく送らない（`digests` は `empty`） |

## 受け入れ基準
- **AC-016-1**: Given リポジトリの `sources-current-events.yaml` と `topics-current-events.yaml` / When 読み込む / Then 除外されるエントリは無く、12 ソースがこの仕様の表どおりの `id`・`url`・`priority`・`require_keywords` で読める。テーマ・減点・`digest.size = 10` も表どおり
- **AC-016-2**: Given `current-events` のテーマのファイルで、`digest` に `limits` を書いたもの / When 読み込む / Then `ConfigError`
- **AC-016-3**: Given `current-events` の再ランク / When LLM に送る内容を見る / Then システムプロンプトが `rerank_current_events_system.md` の内容で、同じ出来事の記事を 1 件だけ高くする指示が入っている。ツールの定義は 008 と同じ
- **AC-016-4**: Given 最終スコアの違う 12 件（どれも同じテーマ） / When `current-events` の枠配分をする / Then スコアの高い順に 10 件が選ばれ、テーマによる上限で外れる記事は無く、選んだ 10 件だけが `selected` になる
- **AC-016-5**: Given 正しい応答を返す偽の LLM / When `current-events` で要約する / Then システムプロンプトが `summarize_current_events_system.md` の内容で、`CurrentEventsSummary` として検証され、`summary_json` に `title_ja`・`summary`・`why_it_matters` が保存される
- **AC-016-6**: Given `why_it_matters` が空の応答を 2 回返す偽の LLM / When 要約する / Then 2 回試して要約なしになる
- **AC-016-7**: Given 要約のある 2 件、2026-11-09（JST）の実行 / When メールを生成する / Then 件名が `[AI 時事ネタ] 2026-11-09 2 件`、本文が SPEC §8 の例と同じ形（区切りの行、`🤖 今日の AI ニュース（11/9）`、各記事の 4 行、通し番号なし、管理用の情報）になる
- **AC-016-8**: Given 要約の無い記事 / When メールを生成する / Then その記事は `🔹` + 原題 と URL の 2 行だけになる
- **AC-016-9**: Given `summary` に改行を含む要約 / When メールを生成する / Then LINE に貼る部分では改行が空白 1 つになり、1 記事が 4 行にそろう
- **AC-016-10**: Given 取得に失敗したソースとお知らせがある実行 / When メールを生成する / Then 失敗したソースとお知らせは管理用の情報にだけ出て、LINE に貼る部分には出ない
- **AC-016-11**: Given HTML を省略した送信 / When メールを組み立てる / Then `text/plain`（UTF-8）だけのメールになり、`multipart` にならない。件名・`From`・`To`・`Date`・`Message-ID` は AC-012-2 と同じ。HTML を渡したときは今までどおり（012 のテストがそのまま通る）
- **AC-016-12**: Given 正常に取得・要約できるソースと LLM / When `run --profile current-events` を実行する / Then テキストだけのメールが 1 通送られ、静的 HTML は書き出されず、`digests` は `sent` で `html` にテキストの本文が入り、配信した記事は `status = sent` になる

## 実装方針（参考）
- プロファイルの対応表（015）に `current-events` の行を足す。再ランクと要約は、プロンプトのファイル名と要約のスキーマ・ツール定義を引数で受け取るようにする。
- `DigestConfig` は `dev` 用と `current-events` 用で別のモデルにする（`current-events` 用は `size` だけ。`extra="forbid"`）。
- テンプレートの中では文の分割（011 の `split_paragraphs`）を使わず、改行を空白にした 1 行の文字列を渡す。

## 決定事項
1. **ソースの `category`**：今ある値（`japanese` / `community` / `official`）を使う。海外のニュースサイトに合う値は無いが、`category` は記録に使うだけで、選び方には影響しないため、新しい値を足さない。
2. **管理用の情報のソース名は `id`**：表示名の項目は改善の候補として別に扱う（README）。
3. **要約の改行を空白にする**：LINE に貼ったときに、記事の区切り（空行）と記事の中の改行が見分けにくくなるため。
4. **テーマのキーワードと `scoring` は暫定**：数日分の実際の配信を見て調整する（その変更は `topics-current-events.yaml` の値の変更だけで、仕様の変更にはしない）。

## 未決事項
- なし
