"""CLI と処理全体の統合（specs/013-cli.md）のテスト。

各サイトは httpx.MockTransport、Anthropic API は本物の LlmClient の下の HTTP を
httpx2.MockTransport、メール送信は偽物に差し替える。実際の外部アクセス・送信はしない。
"""

import fcntl
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import httpx
import httpx2
import pytest

from aidigest.cli import RunOptions, main, run_digest, setup_logging
from aidigest.config import PricingConfig, Settings, load_settings
from aidigest.fetchers.http import HttpClient
from aidigest.llm import LlmCaller, LlmClient
from aidigest.mailer import MailError
from tests.fetchers.helpers import make_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)  # 日本時間 10/17 06:30
API_KEY = "sk-ant-test-cli-secret"
SMTP_PASSWORD = "smtp-test-secret-pw"
ARTICLE_HTML = (
    "<html><body><article><h1>見出し</h1><p>"
    + "Claude Code の hooks を使うと、ツールの実行前後に任意のコマンドを走らせることができる。" * 4
    + "</p></article></body></html>"
)
SOURCES_YAML = """\
sources:
  - id: feed-a
    type: rss
    url: https://a.example.com/feed.xml
    category: community
    priority: 1
    max_items: 10
  - id: feed-b
    type: rss
    url: https://b.example.com/feed.xml
    category: community
    priority: 1
    max_items: 10
"""


def rss(host: str, titles: list[str], published: datetime) -> str:
    date = published.strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = "".join(
        f"<item><title>{t}</title><link>https://{host}/{i}</link><pubDate>{date}</pubDate></item>"
        for i, t in enumerate(titles)
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{items}</channel></rss>'
    )


class FakeSites:
    """フィードと記事のページを返すサイト。`feeds` はホスト → (状態コード, タイトルの一覧)。"""

    def __init__(self, feeds: dict[str, tuple[int, list[str]]], published: datetime) -> None:
        self.feeds = feeds
        self.published = published
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if request.url.path == "/feed.xml":
            status, titles = self.feeds.get(host, (200, []))
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, text=rss(host, titles, self.published))
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})


class FakeAnthropic:
    """Messages API の偽物。ツール名を見て、再ランクか要約の正しい応答を返す。"""

    def __init__(self) -> None:
        self.tool_names: list[str] = []  # 呼ばれた順のツール名（再ランク / 要約）

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        tool = str(body["tools"][0]["name"])
        text = str(body["messages"][0]["content"])
        self.tool_names.append(tool)
        if tool == "submit_relevance":
            count_match = re.search(r"次の (\d+) 件", text)
            assert count_match is not None
            count = int(count_match.group(1))
            tool_input: dict[str, object] = {
                "scores": [
                    {"id": i, "relevance": 0.8, "reason": "関連"} for i in range(1, count + 1)
                ]
            }
        else:
            title_match = re.search(r"原題: (.+)", text)
            assert title_match is not None
            title = title_match.group(1)
            tool_input = {
                "title_ja": f"訳:{title}",
                "tldr": "一文。",
                "key_takeaways": ["要点"],
                "summary": f"{title} の要約。",
                "topics": [],
                "novelty": 3,
                "apply_to_claude_code": "活用。",
            }
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": tool, "input": tool_input}
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 500},
            },
        )


class FakeMailer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    def send(self, subject: str, html: str, text: str) -> None:
        self.sent.append((subject, html, text))
        if self.fail:
            raise MailError("SMTPServerDisconnected: gone")


class Env:
    """テスト用の設定・偽物一式。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        (tmp_path / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
        self.values: dict[str, object] = {
            "ANTHROPIC_API_KEY": API_KEY,
            "SMTP_PASSWORD": SMTP_PASSWORD,
            "SOURCES_PATH": tmp_path / "sources.yaml",
            "TOPICS_PATH": PROJECT_ROOT / "topics.yaml",
            "PRICING_PATH": PROJECT_ROOT / "pricing.yaml",
            "TEMPLATES_DIR": PROJECT_ROOT / "templates",
            "DB_PATH": tmp_path / "data" / "test.db",
            "OUTPUT_DIR": tmp_path / "output",
            "LOCK_PATH": tmp_path / "data" / "test.lock",
            "LOG_DIR": tmp_path / "logs",
            "LLM_MAX_RETRIES": 0,
        }
        self.sites = FakeSites(
            {
                "a.example.com": (200, ["Claude Code の hooks 活用", "Claude Code と MCP"]),
                "b.example.com": (200, ["LLM の新しいベンチマーク"]),
            },
            NOW - timedelta(hours=1),
        )
        self.anthropic = FakeAnthropic()
        self.mailer = FakeMailer()

    def settings(self) -> Settings:
        env = self.tmp / "test.env"
        env.write_text(
            "".join(f"{k}={v}\n" for k, v in self.values.items() if v is not None),
            encoding="utf-8",
        )
        return load_settings(env)

    def run(self, *, dry_run: bool = False, no_llm: bool = False, now: datetime = NOW) -> int:
        settings = self.settings()
        transport = httpx2.MockTransport(self.anthropic)

        def llm_factory(conn: sqlite3.Connection, run_id: int, pricing: PricingConfig) -> LlmCaller:
            return LlmClient(
                conn,
                run_id,
                settings,
                pricing,
                http_client=anthropic.DefaultHttpxClient(transport=transport),
                clock=lambda: now,
            )

        http: HttpClient = make_client(self.sites, retries=0)
        return run_digest(
            settings,
            RunOptions(dry_run=dry_run, no_llm=no_llm),
            http=http,
            llm_factory=llm_factory,
            mailer=self.mailer,
            clock=lambda: now,
        )

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.tmp / "data" / "test.db")
        conn.row_factory = sqlite3.Row
        return conn

    def rows(self, sql: str) -> list[sqlite3.Row]:
        conn = self.db()
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def subjects(self) -> list[str]:
        return [s[0] for s in self.mailer.sent]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ["ANTHROPIC_API_KEY", "SMTP_PASSWORD", "MAIL_TO", "LOG_DIR", "DB_PATH"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ------------------------------------------------------------ 正常系


def test_normal_run(env: Env) -> None:
    """AC-013-2"""
    code = env.run()

    assert code == 0
    assert env.subjects() == ["[AI Digest] 2026-10-17 注目 3 件"]
    _, html, _ = env.mailer.sent[0]
    assert "訳:Claude Code の hooks 活用" in html
    assert (env.tmp / "output" / "2026-10-17.html").read_text(encoding="utf-8") == html
    (run,) = env.rows("SELECT * FROM runs")
    assert run["finished_at"] is not None
    assert (run["fetched_count"], run["selected_count"], run["sent_count"]) == (3, 3, 3)
    assert run["error"] is None
    (digest,) = env.rows("SELECT * FROM digests")
    assert digest["status"] == "sent"
    assert digest["article_count"] == 3
    assert digest["html"] == html
    assert len(json.loads(digest["article_ids"])) == 3
    statuses = {r["status"] for r in env.rows("SELECT status FROM articles")}
    assert statuses == {"sent"}
    assert len(env.rows("SELECT * FROM source_health")) == 2
    purposes = sorted(r["purpose"] for r in env.rows("SELECT purpose FROM llm_calls"))
    assert purposes == ["rerank", "summarize", "summarize", "summarize"]


def test_not_sent_two_days_in_a_row(env: Env) -> None:
    """AC-013-3"""
    assert env.run() == 0
    assert env.run(now=NOW + timedelta(days=1)) == 0

    assert env.subjects() == ["[AI Digest] 2026-10-17 注目 3 件"]
    (_, second) = env.rows("SELECT status FROM digests ORDER BY id")
    assert second["status"] == "empty"


def test_one_source_failure_does_not_stop(env: Env) -> None:
    """AC-013-4"""
    env.sites.feeds["b.example.com"] = (500, [])

    code = env.run()

    assert code == 0
    (subject, html, _) = env.mailer.sent[0]
    assert subject == "[AI Digest] 2026-10-17 注目 2 件"
    assert "feed-b" in html
    health = {r["source_id"]: r["status"] for r in env.rows("SELECT * FROM source_health")}
    assert health == {"feed-a": "ok", "feed-b": "error"}


def test_no_llm_option(env: Env) -> None:
    """AC-013-5"""
    code = env.run(no_llm=True)

    assert code == 0
    assert env.anthropic.tool_names == []
    _, html, _ = env.mailer.sent[0]
    assert "LLM を使わずに作成しました" in html
    assert "要約なし" in html
    assert "訳:" not in html


def test_missing_api_key(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """AC-013-6"""
    env.values["ANTHROPIC_API_KEY"] = None

    with caplog.at_level(logging.ERROR):
        code = env.run()

    assert code == 0
    assert env.anthropic.tool_names == []
    _, html, _ = env.mailer.sent[0]
    assert "LLM が使えないため、要約なしで作成しました" in html
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_budget_notice(env: Env) -> None:
    """AC-013-7: 再ランク 1 回で日次の上限を超え、要約は呼ばれない"""
    env.values["BUDGET_DAILY_USD"] = 0.0001

    code = env.run()

    assert code == 0
    assert env.anthropic.tool_names == ["submit_relevance"]
    _, html, _ = env.mailer.sent[0]
    assert "予算の上限" in html
    assert "要約なし" in html


def test_dry_run(env: Env) -> None:
    """AC-013-8"""
    code = env.run(dry_run=True)

    assert code == 0
    assert env.mailer.sent == []
    assert (env.tmp / "output" / "2026-10-17.html").exists()
    assert env.rows("SELECT * FROM articles") == []
    assert len(env.rows("SELECT * FROM runs")) == 1
    assert len(env.rows("SELECT * FROM source_health")) == 2
    assert len(env.rows("SELECT * FROM llm_calls")) == 4
    (digest,) = env.rows("SELECT status FROM digests")
    assert digest["status"] == "dry_run"

    assert env.run() == 0
    assert env.subjects() == ["[AI Digest] 2026-10-17 注目 3 件"]


def test_empty_day(env: Env) -> None:
    """AC-013-9"""
    env.sites.feeds = {"a.example.com": (200, []), "b.example.com": (200, [])}

    code = env.run()

    assert code == 0
    assert env.mailer.sent == []
    (digest,) = env.rows("SELECT status FROM digests")
    assert digest["status"] == "empty"
    (run,) = env.rows("SELECT finished_at, error FROM runs")
    assert run["finished_at"] is not None
    assert run["error"] is None


# ------------------------------------------------------------ 失敗


def test_unexpected_failure(env: Env) -> None:
    """AC-013-10: 要約の後、HTML の生成でテンプレートが見つからず失敗する"""
    env.values["TEMPLATES_DIR"] = env.tmp / "no-templates"

    code = env.run()

    assert code == 1
    (run,) = env.rows("SELECT error, finished_at FROM runs")
    assert run["error"] is not None
    assert run["finished_at"] is not None
    assert len(env.rows("SELECT * FROM llm_calls")) == 4
    assert env.rows("SELECT * FROM articles") == []
    assert len(env.subjects()) == 1
    assert env.subjects()[0].startswith("[AI Digest] 実行に失敗しました 2026-10-17")
    assert API_KEY not in env.mailer.sent[0][2]


def test_config_error(env: Env) -> None:
    """AC-013-11"""
    broken = env.tmp / "topics.yaml"
    broken.write_text("scoring: [unclosed\n", encoding="utf-8")
    env.values["TOPICS_PATH"] = broken

    code = env.run()

    assert code == 1
    assert env.subjects()[0].startswith("[AI Digest] 実行に失敗しました")
    assert env.sites.requests == []


def test_lock_held(env: Env) -> None:
    """AC-013-12"""
    lock_path = env.tmp / "data" / "test.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        code = env.run()

    assert code == 2
    assert env.sites.requests == []
    assert env.mailer.sent == []


def test_send_failure(env: Env) -> None:
    """AC-013-13"""
    env.mailer.fail = True

    code = env.run()

    assert code == 1
    (digest,) = env.rows("SELECT status FROM digests")
    assert digest["status"] == "failed"
    (run,) = env.rows("SELECT error, sent_count FROM runs")
    assert run["error"] is not None
    assert run["sent_count"] == 0
    assert "sent" not in {r["status"] for r in env.rows("SELECT status FROM articles")}


# ------------------------------------------------------------ ログと CLI


@pytest.fixture
def log_handlers(tmp_path: Path) -> Iterator[Path]:
    log_dir = tmp_path / "logs"
    handlers = setup_logging(log_dir, 30)
    yield log_dir
    root = logging.getLogger()
    for handler in handlers:
        root.removeHandler(handler)
        handler.close()


def test_log_file(env: Env, log_handlers: Path) -> None:
    """AC-013-14"""
    env.values["MAIL_TO"] = "me@example.com"
    env.run()

    log = (log_handlers / "aidigest.log").read_text(encoding="utf-8")
    for stage in ["事前の絞り込み", "ローカルスコア", "枠配分", "要約", "LLM を呼び出しました"]:
        assert stage in log, stage
    assert API_KEY not in log
    assert SMTP_PASSWORD not in log


@pytest.mark.parametrize("argv", [[], ["unknown"], ["run", "--bogus"]])
def test_usage_error(argv: list[str]) -> None:
    """AC-013-15"""
    with pytest.raises(SystemExit) as exc_info:
        main(argv)

    assert exc_info.value.code == 2
