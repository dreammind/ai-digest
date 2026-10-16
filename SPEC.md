# AI情報収集アプリ 仕様書（SPEC.md）

## 1. 目的
Claude Code と LLM 全般に関する有用な情報を毎朝1通のダイジェストで受け取り、開発に活かす。
特に **Claude Code を使った開発手法（ワークフロー、プロンプト、hooks、MCP、サブエージェント等）** を最優先で拾う。
海外情報も対象とし、日本語に翻訳・要約して配信する。

## 2. 非目標（やらないこと）
- .NET / Python 自体の開発手法の収集
- 即時通知（毎朝1通のまとめのみ）
- スクレイピング前提の情報源（MVP は RSS/Atom と公開 API のみ）
- 複数ユーザー対応、ログイン機能

## 3. 関心テーマと優先度
| 優先度 | テーマ |
|---|---|
| 高 | Claude Code の新機能・リリース、活用手法、hooks / MCP / サブエージェント / CLAUDE.md の運用ノウハウ |
| 中 | LLM 全般（新モデル、API 更新、エージェント設計、評価、プロンプト技法） |
| 低 | 上記に関連する論文・ツール（arXiv など。実務に効くものだけ） |
| 除外 | .NET / Python の一般的な開発手法、LLM と無関係な技術ニュース |

## 4. 全体フロー
```
cron (毎朝 6:30 JST)
  → 1. 取得        : RSS/API から新着を収集（ソース単位で例外を隔離し、結果を source_health に記録）
  → 2. 正規化      : URL 正規化、content_hash 計算、既読(DB)との重複排除。取得した全記事は、配信の有無にかかわらずこの時点で既読として記録する
  → 3. 事前絞込    : 時間窓（36 時間。`qiita_search` / `zenn_topic` は対象外）、除外キーワード、ソース別ルール（require / hide）で除外（LLM を使わない）
  → 4. ローカルスコア: テーマ別キーワードの加点・減点とソース優先度で採点し、タイトルが似た重複話題を除く（LLM を使わない）
  → 5. LLM 再ランク: 上位 約 30 件のみ、タイトル+抜粋を 1 回の呼び出しで関連度判定し、ローカルスコアと混ぜる（失敗時はローカルスコア順のまま続行）
  → 6. 枠配分      : セクション別の上限（Claude Code 優先）を守って、最終的な N 件を選ぶ
  → 7. 要約・翻訳  : 選んだ N 件のみ本文を取得し、日本語で構造化要約（原文リンク付き）
  → 8. 保存        : SQLite に記事・スコア・要約・LLM 呼び出し履歴を保存
  → 9. 配信        : HTML メールで送信（同内容を静的 HTML としても出力）。取得失敗ソースと月累計コストをフッターに表示
```

テーマごとの重みとキーワード（high / medium / negative）は `topics.yaml` で管理する。具体的なスコア式・しきい値は `specs/` の詳細仕様で定める。

## 5. 情報源（MVP）
2026-10-15 に各ソースを実際に取得し、記事の傾向を確認したうえで次のように決めた。

| 区分 | ソース | 取得方法 | priority |
|---|---|---|---|
| 日本語 | Zenn（`claudecode` トピック） | Zenn の非公式 API（`zenn_topic`。下記「Zenn の取得方法」） | 1 |
| 日本語 | Zenn（`anthropic` トピック） | RSS（`https://zenn.dev/topics/anthropic/feed`） | 1 |
| 日本語 | Zenn（`llm` トピック） | 同上 | 2 |
| 日本語 | Qiita（`ClaudeCode` タグ） | Qiita API v2 の記事検索（`qiita_search`。下記「Qiita の取得方法」） | 1 |
| 日本語 | Qiita（`LLM` タグ） | 同上 | 2 |
| 日本語 | Qiita（`AIエージェント` タグ） | 同上 | 2 |
| コミュニティ | Hacker News（Claude Code） | Algolia HN Search API。タイトルに "Claude Code"、20 ポイント以上 | 2 |
| コミュニティ | Hacker News（LLM） | 同上。タイトルに "LLM"、100 ポイント以上 | 2 |
| コミュニティ | Hacker News（主要モデル） | 同上。タイトルに "Gemini" / "OpenAI" / "Anthropic" / "GPT" のいずれか（OR）、300 ポイント以上。主にモデルの発表を拾う | 2 |
| コミュニティ | Simon Willison's Weblog | Atom（`https://simonwillison.net/atom/everything/`） | 2 |

- HN は会社名で検索すると企業のゴシップ（逮捕、売上、IPO など）も高いポイントで混ざる。ポイントだけでは区別できないため、`topics.yaml` の減点キーワードで落とす（006）。
- Qiita の検索は、タグを空白で並べると AND になり、`OR` は他の条件まで外れてしまう。そのため、タグごとに別のソースにする。重複した記事は既読判定（003）で 1 件にまとまる。

MVP で使わないことにしたソースと理由:
- Claude Code の GitHub Releases：1〜2 日ごとに変更点が数十項目あり、利用者の関心と合わなかった。新機能は Zenn や HN の記事を通して拾う
- Anthropic の News / Engineering（第三者が生成するフィード）：内容を確認したが、関心と合う記事が少なかった
- Qiita のタグ RSS：どのタグでも最新 4 件程度しか返らないため、API に置き換えた
- Zenn の `gemini` / `openai` / `mcp` トピック、Qiita の `Gemini` / `OpenAI` / `MCP` / `生成AI` タグ：ハッカソン作品や関係の薄い連載が多い、件数が少ない、ほかのタグと重なる
- OpenAI / Google DeepMind などの公式ブログ、arXiv：Phase 2 以降で検討する

### Zenn の取得方法
- Zenn のトピックの RSS は 20 件しか返さない。`claudecode` トピックは 1 日に約 48 本の投稿があり、RSS では約 12 時間分（`llm` は約 14 時間分）しか取れず、毎朝 1 回の取得では半分以上を取りこぼす（2026-10-15 の実測）。
- そのため、`claudecode` と `llm` は Zenn の非公式 API（`/api/articles`）を使う。過去 1 週間の人気順（`order=weekly`、1 ページ 48 件）を `pages` ページ取り、投稿から `lookback_days` 日以内で、いいね数が `min_likes` 以上の記事だけを残す（初期値は 5、7 日、2 ページ。10/7〜10/15 の実データで、いいね 5 以上は `claudecode` が 1 日約 2.7 件、`llm` が約 2.4 件）。
- Qiita と同じく、基準に届いた日に初めて取得され、既読になる。時間窓（36 時間）は適用しない。
- 非公式 API は公式に案内されていないため、予告なく変わったり止まったりしうる。止まった場合は `source_health` とダイジェストのフッターで分かる。
- `anthropic` トピックは RSS の 20 件で約 88 時間分を取れており取りこぼしが無いため、RSS のままにする。

### Qiita の取得方法
- 投稿直後の記事はいいね・ストックが付いておらず、質の判断ができない。そのため、投稿から `lookback_days` 日以内で、ストック数が `min_stocks` 以上になった記事だけを取得する（初期値は 3 と 7 日。10/1〜10/7 の実データで 1 日約 1.4 件）。
- 基準に届いた日に初めて取得され、既読になる。基準に届かなかった記事は取得しないので、既読にもならない。
- 記事は数日前に投稿されたものになるため、時間窓（36 時間）は適用しない（取得時に `lookback_days` で絞っている）。
- いいね数による絞り込みは API の検索で使えなかったため、ストック数を使う。
- API は認証なしで 1 時間に 60 回まで。1 日 1 回の取得で足りる。

ソースは `sources.yaml` で管理し、追加・削除をコード変更なしで行えるようにする。

### ソース設定（sources.yaml）の項目
- `id`、`type`（`rss` / `github_release` / `hn_search` / `arxiv` / `qiita_search` / `zenn_topic`）、取得先（`url`、`repo`、`query`、`topic`）、`category`、`priority`（1〜3）
- ソース別ルール: `require_keywords`（いずれかを含む記事だけ通す）、`hide_keywords`（含む記事を除外）、`highlight_keywords`（含む記事を加点）
- 取得上限: `max_items`（1 回の取得で扱う最大件数）
- `min_stocks`（`qiita_search` のみ）、`min_likes` と `pages`（`zenn_topic` のみ）、`lookback_days`（`qiita_search` と `zenn_topic`）。上記「Zenn の取得方法」「Qiita の取得方法」
- `min_score`（Hacker News のみ。一定スコア未満を除外）。候補の確認数は `max_items` の 3 倍までとし、API 呼び出しが増えすぎないようにする
- `recheck_changed`（true のソースのみ、同じ URL でも `content_hash` が変わったら再収集する。GitHub Releases や changelog 向け。通常のブログ記事は再配信しない）

### ソースの健全性記録
- 取得の成否、エラー内容、件数を実行ごとに `source_health` へ保存する。
- 取得に失敗したソースは、ダイジェストのフッターに表示する（受け入れ基準「1 ソースの失敗で全体が止まらない」の確認にも使う）。

### Anthropic のフィードについて
- 2026-10-15 の確認で、関心と合う記事が少なかったため、MVP では使わない（上記）。以下は調査の記録として残す。
- 2026-10-07 時点で、第三者のリポジトリ（Olshansk/rss-feeds）が生成する Anthropic News と Engineering のフィードが取得でき（HTTP 200）、直近まで更新されていた。ただしスクレイピング由来のため、Anthropic 側の変更や作者の更新停止で止まる可能性がある。
- 利用する場合は `source_health` で死活を監視し、更新が一定期間止まったら失敗通知メールの対象にする。

## 6. LLM 利用方針とコスト設計
- 使用モデル: 再ランク・要約ともに Haiku クラス（安価なモデル）を基本とする。要約品質に不満が出た場合のみ要約だけ上位モデルを検討。
- LLM を使うのは次の 2 か所だけ。**全記事を LLM で採点しない**（採点はローカルスコアで行う）。
  1. 再ランク: 上位 約 30 件を 1 回の呼び出しで判定
  2. 要約: 最終的に選んだ N 件（初期値 8）を 1 件ずつ要約
- **月額予算: 約 300 円（約 $2）**。日次で約 10 円が目安。
- コスト抑制策:
  1. LLM の前にローカルで絞る（時間窓、除外キーワード、ローカルスコア、重複話題の除去、既読）
  2. 再ランクはタイトル+抜粋 200 字程度を、**1 回のリクエストにまとめて** 渡す。出力は関連度（0.0〜1.0）と 5 語以内の理由
  3. 本文要約は **N 件のみ**（初期値 8）。本文は trafilatura で抽出し、先頭 約 4,000 字で切り詰める
  4. 同じ固定プロンプトは prompt caching を利用する
  5. 毎朝の定期実行は即時性が不要なので、Batch API（非同期・割引）の採用を検討する
  6. **予算ガード**: LLM を呼ぶ前に、当日と当月の累計費用を確認する。日次または月次の上限に達していたら LLM 呼び出しを止め、タイトルのみで配信する。初期値は月 $2、日 $0.15（暫定。実測して調整する）。円換算の為替レートは設定値とする
  7. 全 LLM 呼び出しを `llm_calls` に記録する（用途、モデル、入出力トークン数、費用）。Anthropic API は費用を返さずトークン数だけ返すため、費用は設定の単価表から計算する。ダイジェスト末尾に当日の費用と月累計を表示する
- 料金は変動するため、実装時に公式の最新価格を確認して設定値に反映する。

### LLM 出力の扱い
- 再ランク・要約とも、Claude の tool use でスキーマを強制し、応答は pydantic で検証する（正規表現による JSON 抽出はしない）。
- 要約のスキーマ（日本語で出力）: `title_ja`（日本語のタイトル。§8 の「タイトル（日本語）」に使う）、`tldr`（1 文）、`key_takeaways`（箇条書き）、`summary`、`topics`、`novelty`（1〜5）、`apply_to_claude_code`（Claude Code を使った開発にどう活かせるか。無ければ空）。
- 応答が途中で切れた場合（`stop_reason` が `max_tokens`）や検証に失敗した場合は 1 回だけリトライし、それでも失敗した記事は、原題と原文リンクだけで配信する（翻訳も要約の一部なので、この場合は原題のまま）。全体は止めない。
- API の一時エラー（レート制限など）は SDK の自動リトライ設定で扱い、待ち時間と回数に上限を置く。

### LLM が使えないときの縮退動作
| 状況 | 動作 |
|---|---|
| 再ランクが失敗した | ローカルスコア順のまま続行する |
| 要約が失敗した、または予算上限に達した | タイトルと原文リンクだけで配信する |
| LLM 全体が使えない（API キー不備など） | ローカルスコア順・タイトルのみでダイジェストを配信し、失敗をログとメールに残す |
| `--no-llm` 指定 | 上記の「LLM 全体が使えない」と同じ動作で実行する |

## 7. データ構造（SQLite）
- `articles`: id, url（UNIQUE・正規化済み）, source_id, category, title, published_at, fetched_at, content_hash, local_score, llm_score, final_score, matched_topics（JSON）, summary_json（日本語の要約。§6 のスキーマ）, score_model, summary_model, status（new / scored / selected / summarized / sent / skipped）
- `runs`: id, started_at, finished_at, fetched_count, selected_count, sent_count, error
- `digests`: id, run_id, created_at, status（sent / dry_run / empty / failed）, article_count, html, article_ids（JSON）
- `llm_calls`: id, run_id, purpose（rerank / summarize）, model, input_tokens, output_tokens, cost_usd, created_at
- `source_health`: run_id, source_id, status（ok / error）, error, item_count
- `schema_version`: マイグレーション管理用（スキーマ変更は番号付きのマイグレーションで行う）
- 既読判定は `articles.url`（正規化済み）で行う。取得した全記事は、スコアが低くて落ちた記事も含めて、取得時点で記録する（配信対象が 0 件でも翌日に再採点しない）。
- 保持期間: 選ばれなかった記事は 90 日、選ばれた記事は 1 年、`digests.html` は 90 日（その後は NULL にしてメタデータだけ残す）。

### 重複排除の方針
1. **URL**: `utm_*` などの追跡パラメータや末尾スラッシュなどを除いて正規化し、完全一致で判定する。
2. **内容の変化**: 同じ URL で `content_hash` が変わった場合の再収集は、`recheck_changed: true` のソースだけ（§5）。
3. **別ソースの同一話題**: ローカルスコアで並べた後に、タイトルの類似度で重複を除く。上位のスコアの記事が残るよう、並べ替えの後に行う。日本語は単語区切りがないため、文字 n-gram による類似度を使う。しきい値は設定値とし、実データで調整する。
4. 埋め込み（意味的類似度）による重複排除は MVP では行わない（別途 embedding の提供元が必要になるため）。

## 8. 配信
- 毎朝 1 通。件名は `[AI Digest] 2026-10-08 注目 N 件`
- 構成: 「今日の最重要（Claude Code 手法）」→「Claude Code 更新情報」→「LLM 動向」→「その他」。各記事は タイトル（日本語）/ 要約 2〜3 行 / AI活用（要約の `apply_to_claude_code`。空なら出さない）/ 原文リンク / ソース名
- **枠配分**: 最終的に選ぶ件数 N の初期値は 8。セクション別の上限（初期値、設定可）は、Claude Code（手法・更新の合計）4 件、LLM 動向 3 件、その他（論文を含む）2 件とする。上限を超えない範囲で、空いた枠は全体のスコア順に埋める。Claude Code を最優先にするため、均等配分ではなくこの上限方式にする。
- **フッター**: 取得に失敗したソース（§5 の `source_health`）、当日の LLM 費用と月累計を表示する。
- 送信は SMTP リレー経由（Gmail の SMTP、SendGrid、Amazon SES など）。VPS から直接送信はしない。SPF/DKIM は利用サービスの設定に従う。
- 同内容を `output/YYYY-MM-DD.html` として出力。将来の Web UI は、nginx + Basic 認証でこのディレクトリを配信するだけで実現する（Phase 2）。

## 9. 実行環境
- さくらの VPS（Ubuntu）、cron で毎朝実行。実行時刻は 6:30 JST を想定
- Python 3.12 以上。パッケージ・仮想環境管理は **uv**（`pip` / `venv` は直接使わない）
- 依存は `pyproject.toml` で宣言し、`uv.lock` をコミットして再現性を確保する。VPS では `uv sync --frozen` で環境を作る
- cron からは uv の絶対パスで実行する（cron は PATH が最小限のため）。例: `cd /path/to/app && /home/<user>/.local/bin/uv run --frozen python -m aidigest.cli run`
- 設定は `.env`（API キー、SMTP 認証情報。Git 管理しない）、`sources.yaml`、`topics.yaml`
- ログは `logs/` にローテーション付きで出力。失敗時は管理者宛に失敗通知メールを送る
- cron の二重実行を防ぐためロックファイルを使う

## 10. 推奨ライブラリ（Claude Code に最終判断を任せてよい）
`feedparser`、`httpx`、`trafilatura`（本文抽出）、`anthropic`、`jinja2`、標準の `sqlite3` と `smtplib`、`pydantic`、`pytest`

### 型・品質の方針
- 開発ツール: `uv`、`pyright`（strict）、`ruff`（`ANN` ルールで型ヒントの付け忘れも検出）、`pytest`、`pytest-cov`、`pre-commit`
- 内部コードは全関数に型ヒントを付け、`pyright --strict` を通す。`Any` の安易な使用や `# type: ignore` は避ける（使う場合は理由をコメントする）。
- 外部との境界（フィード取得結果、LLM の応答 JSON、`sources.yaml` / `.env` の読み込み）は pydantic モデルで検証する。LLM の出力は型どおりに返らないことがあるため、検証に失敗した場合はリトライか該当記事のスキップとして扱い、全体は止めない。
- 完了条件: `pyright`・`ruff check`・`pytest` がすべて通ること。

## 11. 開発プロセス（SDD / TDD：必須）

### SDD（仕様駆動開発）
- `SPEC.md` を唯一の正とする。実装は仕様に書かれたことだけを行い、書かれていない挙動を勝手に追加しない。
- 機能追加・挙動変更は必ず **仕様の更新 → テスト → 実装** の順で行う。仕様にない変更が必要になったら実装を止め、仕様の修正案を提示して承認を得る。
- 機能単位の詳細仕様は `specs/NNN-<name>.md` に置く（目的、入出力、振る舞い、エラー時の挙動、受け入れ基準）。
- 受け入れ基準は Given / When / Then 形式で書き、基準ごとに ID（例: `AC-003-2`）を振る。1 つの基準に対して 1 つ以上のテストを対応させる。
- 各テストの名前かコメントに対応する受け入れ基準 ID を含め、仕様とテストの対応を追跡できるようにする。
- 完了前に、仕様・テスト・実装が一致していることを確認する。仕様にない挙動が残っていれば、仕様に追記するか実装から削除する。

### TDD（テスト駆動開発）
各機能で Red → Green → Refactor を守る。
1. 失敗するテストを先に書く
2. 実行して、期待した理由で失敗することを確認する（Red）
3. テストを通す最小限の実装を書く（Green）
4. テストが通ったままリファクタリングする
- テストより先に実装コードを書かない。
- テストが失敗したとき、テストを書き換えて通さない。テストが仕様と食い違っている場合のみ、仕様を確認してから修正する。
- 外部通信（フィード取得、Anthropic API、SMTP）は、テストではすべてモックかフェイクにする。
- カバレッジの下限は 80%（`pytest --cov --cov-fail-under=80`）。

### 強制の仕組み（Phase 0 で整備）
CLAUDE.md の指示だけでは強制力が弱いため、機械的なチェックを併用する。
- `pre-commit`（ruff、pyright、pytest）で、チェックに失敗するコミットを拒否する
- Claude Code の hooks（`.claude/settings.json`）:
  - ファイル編集後に ruff と pyright を自動実行する
  - 作業終了時（Stop）に pytest を実行し、失敗していれば差し戻す
- コミットは `test:`（Red）→ `feat:`（Green）→ `refactor:` の順に分ける
- 任意: `src/` に新しいモジュールを作る際、対応する `tests/` のファイルがなければ警告する hook

## 12. フェーズ計画
- **Phase 0（初期化）**: `uv init`、`pyproject.toml`（ruff / pyright / pytest の設定）、`pre-commit`、Claude Code の hooks、`specs/` ディレクトリを整備し、空のテストが通る状態にする
- **Phase 1（MVP）**: 3〜4 ソースの取得 → 重複排除 → ローカルスコア → LLM 再ランク → 枠配分 → 要約 → SQLite → メール送信、cron 登録
- **Phase 2**: ソース拡充、静的 HTML 配信（Web UI）、スコア基準のチューニング
- **Phase 3**: 検索機能、週次まとめ、フィードバック（有用/不要）によるスコア改善
  - フィードバックはカテゴリ別・ソース別のスコア補正に使い、古い評価ほど影響を弱める（半減期 約 30 日）。TF-IDF、埋め込み、閲覧などの暗黙シグナルは対象外とする。

## 13. 受け入れ基準（Phase 1）
- [ ] cron で毎朝自動実行され、ダイジェストメールが届く
- [ ] 同じ記事が 2 日続けて配信されない
- [ ] 取得した記事は配信の有無にかかわらず取得時点で既読になり、翌日に再採点されない
- [ ] 同じ話題を扱う別ソースの記事は、スコアの高い 1 件だけが残る（日本語タイトルでも機能する）
- [ ] 英語記事が自然な日本語で要約され、原文リンクがある
- [ ] 月間概算コストが 300 円以内に収まり、コストが記録される
- [ ] 1 ソースの取得失敗で全体が止まらない（失敗はログとメールに残り、ダイジェストのフッターにも表示される）
- [ ] LLM の再ランクが失敗しても、ローカルスコア順でダイジェストが配信される
- [ ] 要約の応答がスキーマを満たさない場合、1 回リトライし、それでも失敗した記事はタイトルと原文リンクだけで配信される
- [ ] 日次または月次の予算上限に達すると LLM 呼び出しが止まり、タイトルのみで配信される
- [ ] セクション別の上限（Claude Code 4 / LLM 3 / その他 2）を超えない
- [ ] `--dry-run`（送信・DB 更新なし）と `--no-llm`（LLM 呼び出しなし）オプションで動作確認できる
- [ ] `pyright --strict`、`ruff check`、`pytest` がすべてエラーなしで通る
- [ ] 各受け入れ基準 ID に対応するテストが存在し、カバレッジが 80% 以上
- [ ] `uv sync --frozen` で、クリーンな環境（VPS を含む）に同じ依存を再現できる
- [ ] pre-commit と Claude Code の hooks が動作し、失敗するコミットが拒否される

## 14. 未決事項
- 送信に使う SMTP サービス（Gmail / SendGrid / SES）
- `topics.yaml` のキーワード（high / medium / negative）の初期リスト
- 重複話題判定の方式としきい値（日本語タイトルの文字 n-gram）
- 予算の初期値（月 $2 / 日 $0.15）と為替レートの設定値、LLM の単価表
- 配信時刻（6:30 でよいか）
- Anthropic Console 側での月次の利用上限設定

## 付録 A. 参考にした OSS
2026-10-07 に次の 2 つのリポジトリを読み、設計の考え方だけを取り込んだ。**コードはコピーしない**（ai_digest は README が MIT と書くが LICENSE ファイルがなく、ライセンスが不明確。condenseit は MIT）。
- ai_digest（Pappet/ai_digest）: 2 段階の LLM パイプライン、LLM 利用不可時のキーワードによる代替、ダイジェストのアーカイブと保持期間、使ったモデル名の記録
- condenseit（wildlifechorus/condenseit）: ローカルスコアで絞ってから LLM を使う順序、上位のみの再ランクとスコアの混合、日次・月次の予算ガード、ソース別ルールとソースの健全性記録、カテゴリ枠の選択、タイトル類似度による重複話題の除去

どちらも、こちらの基準（pyright strict、SDD / TDD）には達していない。例えば ai_digest にはテストがなく、condenseit は型チェッカーを strict で使っていない。
