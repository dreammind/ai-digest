# 008: LLM の再ランク

- 状態: 承認済み
- 関連: SPEC.md §4 の 5（LLM 再ランク）、§6（再ランクはタイトル + 抜粋を 1 回の呼び出しで、出力は関連度と短い理由、tool use でスキーマを強制し pydantic で検証、縮退動作）、006、007

## 目的
ローカルスコア（006）の上位の記事だけを、1 回の LLM 呼び出しで読者にとっての関連度を判定させ、ローカルスコアと混ぜて並べ直す。
LLM が使えない・失敗したときは、ローカルスコアの順のまま続ける。

## 範囲
- 含む:
  - 上位 `RERANK_TOP_N` 件の取り出し
  - プロンプトと tool（出力のスキーマ）の組み立て、007 の `LlmClient` での呼び出し
  - 応答の検証と、1 回の再試行
  - ローカルスコアとの混合（`final_score`）と並べ替え
  - 失敗時の縮退（ローカルスコアの順）
  - `articles` への記録（`llm_score`、`final_score`、`score_model`）
  - 設定値の追加
- 含まない:
  - セクションへの振り分けと枠配分：009
  - prompt caching・Batch API：再ランクは 1 日 1 回の呼び出しで、5 分以内に同じプロンプトを再利用しないため使わない（決定事項 4）

## 入出力
- 入力: 006 が返した記事（`ScoredArticle`、ローカルスコアの高い順）、`LlmClient`（LLM が使えないときは `None`）、設定
- 出力: 上位 `RERANK_TOP_N` 件の記事を `final_score` の高い順に並べたリスト。各記事は `ScoredArticle`、`llm_score`（失敗時はなし）、`final_score` を持つ。`RERANK_TOP_N` 件より後ろの記事は出力に含めない（後の段階に進まない）

### 追加する設定値（`.env` / 環境変数）
| 変数 | 型 | 既定値 | 用途 |
|---|---|---|---|
| `RERANK_TOP_N` | 1 以上の整数 | `30` | 再ランクに回す件数 |
| `RERANK_EXCERPT_CHARS` | 0 以上の整数 | `200` | 1 件あたりの抜粋の文字数 |
| `RERANK_LLM_WEIGHT` | 0 以上 1 以下の数 | `0.5` | `final_score` での LLM の関連度の重み |
| `RERANK_MAX_TOKENS` | 1 以上の整数 | `4000` | 再ランクの呼び出しの `max_tokens`（思考の分を含む） |

## 振る舞い

### リクエスト
- 上位 `RERANK_TOP_N` 件に、上から 1, 2, … の番号（`id`）を振る。
- `system`：パッケージに同梱するプロンプト（`src/aidigest/prompts/rerank_system.md`）。内容は下の「プロンプト」。
- ユーザーメッセージ：各記事を次の形で並べる。
  ```
  [1] 出典: zenn-claudecode
  タイトル: Claude Code Hooks で監査ログを残す
  抜粋: （本文から HTML タグを除き、空白をまとめた先頭 RERANK_EXCERPT_CHARS 文字。本文が無ければ「なし」）
  ```
- tool：`submit_relevance`（`strict: true`）。入力は `{"scores": [{"id": 整数, "relevance": 数, "reason": 文字列}, ...]}`。
- `tool_choice`：`auto`（プロンプトで必ずこのツールを使うよう指示する。決定事項 1）
- `output_config`：`{"effort": "low"}`。`thinking` は指定しない（Haiku 5.5 の既定の adaptive）。
- `max_tokens`：`RERANK_MAX_TOKENS`。`purpose`：`rerank`。

### プロンプト（`rerank_system.md`）
```
あなたは、Claude Code と LLM の技術情報を毎朝まとめるダイジェストの編集者です。
読者は、Claude Code を使って日々ソフトウェアを開発しているエンジニアです。

渡された記事それぞれについて、読者にとってどれだけ役立つかを 0.0〜1.0 で評価してください。
- 1.0 に近い：Claude Code を使った開発手法の具体的なノウハウ（ワークフロー、プロンプト、hooks、MCP、サブエージェント、CLAUDE.md の運用など）、Claude Code の新機能
- 0.5 前後：LLM 全般の重要な動向（新しいモデル、API の更新、エージェント設計、評価、プロンプト技法）
- 0.2 前後：関連する論文やツールのうち、実務に役立ちそうなもの
- 0.0 に近い：.NET や Python そのものの一般的な開発手法、LLM と関係の無い技術ニュース、企業のゴシップ、宣伝、入門的すぎる内容

必ず submit_relevance ツールで、渡したすべての記事について 1 回で回答してください。
reason は判断の理由を 5 語以内の日本語で書いてください。
```

### 応答の検証
次のすべてを満たせば成功とする。満たさなければ「不正な応答」とする。
1. `stop_reason` が `max_tokens` でも `refusal` でもない。
2. `submit_relevance` の `tool_use` ブロックがちょうど 1 つある。
3. 入力が pydantic のモデルで検証できる（`relevance` は 0.0 以上 1.0 以下、`reason` は文字列）。
4. `id` が、渡した番号とちょうど一致する（重複・抜け・余分が無い）。
- `reason` の語数は検証しない（指示するだけ）。

### 再試行と縮退
- 不正な応答なら、同じリクエストで 1 回だけ再試行する。再試行も不正なら縮退する。
- `BudgetExceeded`・`LlmError` のときは再試行せずに縮退する（`LlmError` は SDK がすでに再試行している）。
- `LlmClient` が `None` のときは、呼び出さずに縮退する。
- 縮退：`llm_score` はなし、`final_score` は正規化したローカルスコア（下記）とし、ローカルスコアの順のまま返す。理由を WARNING ログに出す。

### final_score
```
正規化したローカルスコア = max(ローカルスコア, 0) / 上位 RERANK_TOP_N 件のローカルスコアの最大値
                         （最大値が 0 以下なら、すべて 0）
final_score = (1 − RERANK_LLM_WEIGHT) × 正規化したローカルスコア + RERANK_LLM_WEIGHT × llm_score
```
- `final_score` の高い順に並べる。同じなら、ローカルスコアの順（入力の順）。

### 記録
- 出力するすべての記事について、`llm_score`（縮退時は NULL）、`final_score`、`score_model`（LLM を使ったときはモデル名、縮退時は NULL）を記録する。
- `status` は変えない（`scored` のまま。009 で `selected` にする）。コミットはしない。

## エラー時の挙動
| 状況 | 動作 |
|---|---|
| 不正な応答（ツールを使わない、スキーマ違反、id の不一致、`max_tokens`・`refusal` で終了） | 1 回再試行。それも不正なら縮退 |
| `BudgetExceeded`・`LlmError` | 再試行せず縮退 |
| LLM が使えない（`LlmClient` が `None`） | 呼び出さず縮退 |
| 入力が 0 件 | 呼び出さず、空のリストを返す |

## 受け入れ基準
- **AC-008-1**: Given 設定値を設定していない / When `Settings` を作る / Then 表の既定値になる。`.env.example` に載っている（AC-001-13 のテストで確認する）
- **AC-008-2**: Given ローカルスコアの高い順の記事 35 件と `RERANK_TOP_N = 30` / When 再ランクする / Then 呼び出しは 1 回で、ユーザーメッセージには上位 30 件だけが 1〜30 の番号で並び、`system` は同梱のプロンプト、tool は `submit_relevance`（`strict: true`）、`tool_choice` は `auto`、`effort` は `low`、`max_tokens` は `RERANK_MAX_TOKENS`、`purpose` は `rerank` になる
- **AC-008-3**: Given HTML タグと連続する空白・改行を含む本文、`RERANK_EXCERPT_CHARS` より長い本文、本文の無い記事 / When リクエストを組み立てる / Then 抜粋はタグを除き空白をまとめた先頭 `RERANK_EXCERPT_CHARS` 文字、本文の無い記事は「なし」になる
- **AC-008-4**: Given 正しい応答 / When 再ランクする / Then 各記事の `llm_score` は応答の `relevance`、`final_score` は計算式どおりになり、`final_score` の高い順（同じならローカルスコアの順）に返る
- **AC-008-5**: Given ローカルスコア 10・5・−2 の 3 件、`llm_score` 0.2・0.9・1.0、`RERANK_LLM_WEIGHT = 0.5` / When 再ランクする / Then 正規化したローカルスコアは 1.0・0.5・0.0、`final_score` は 0.6・0.7・0.5 で、順は 2 件目・1 件目・3 件目になる
- **AC-008-6**: Given 1 回目が不正な応答（ツールを使わない／`relevance` が 1.5／id の抜け／id の重複／`stop_reason` が `max_tokens`／`refusal`）、2 回目が正しい応答 / When 再ランクする / Then 2 回呼ばれ、2 回目の結果が使われる
- **AC-008-7**: Given 2 回とも不正な応答 / When 再ランクする / Then 2 回呼ばれ、縮退する（ローカルスコアの順、`llm_score` はなし、`final_score` は正規化したローカルスコア）。WARNING ログが出る
- **AC-008-8**: Given `BudgetExceeded` または `LlmError` を送出する `LlmClient` / When 再ランクする / Then 1 回だけ呼ばれ、縮退する
- **AC-008-9**: Given `LlmClient` が `None` / When 再ランクする / Then 縮退する
- **AC-008-10**: Given 入力が 0 件 / When 再ランクする / Then 呼び出さず、空のリストを返す
- **AC-008-11**: Given 正しい応答 / When 再ランクする / Then 出力した記事の `llm_score`・`final_score`・`score_model`（モデル名）が `articles` に記録され、`status` は `scored` のまま。縮退時は `llm_score` と `score_model` が NULL。呼び出し側がロールバックすると元に戻る
- **AC-008-12**: Given すべての記事のローカルスコアが 0 以下 / When 縮退する / Then 正規化したローカルスコアはすべて 0 になる（0 で割らない）

## 実装方針（参考）
- モジュール：`src/aidigest/pipeline/rerank.py`、プロンプトは `src/aidigest/prompts/rerank_system.md`（`importlib.resources` で読む）
- テストでは `LlmClient` を偽物に差し替え（`create` を持つオブジェクト）、応答は SDK の `Message` 型で作る
- 実装と単体テストの後に、実際の API で 1 回だけ呼んで確かめる（API キーが必要。費用の目安は 1 回 $0.001 未満）

## 決定事項
1. **`tool_choice` は `auto` + `strict: true`**：Haiku 5.5 は思考が既定で有効で、思考と「ツールの強制」を併用できるかが資料で確認できなかった。資料は思考を切るより `effort` を下げることを勧めているため、思考は既定のまま `effort: low` とし、ツールはプロンプトで指示して、使われなければ再試行する。SPEC §6 の「tool use でスキーマを強制」は、`strict: true` と pydantic の検証で満たす。
2. **再ランクも 1 回だけ再試行する**：SPEC §6 は要約について 1 回の再試行を定めている。再ランクの 1 回の費用は小さい（30 件で入力 4,000 トークン程度、$0.001 未満）ので、同じ方針にする。
3. **id の一部が欠けた応答は、全体を不正とする**：一部だけ使うと、欠けた記事の扱い（LLM の点なしで混ぜる）が複雑になるため。再試行で救う。
4. **prompt caching を使わない**：再ランクは 1 日 1 回で、キャッシュの有効期間（5 分）内に同じプロンプトを再利用しないため、書き込みの割増（1.25 倍）だけがかかる。要約（010）は 8 回続けて呼ぶので、そこで検討する。
5. **`RERANK_LLM_WEIGHT` の初期値は 0.5**：実際の配信を見て調整する。
6. **上位 `RERANK_TOP_N` 件より後ろの記事は、後の段階に進めない**：SPEC §4 の「上位約 30 件のみ」に合わせる。
