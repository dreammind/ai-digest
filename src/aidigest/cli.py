"""CLI と処理全体の統合（specs/013-cli.md）。

`python -m aidigest.cli run [--dry-run] [--no-llm]` で、取得からメール送信までを 1 回流す。
失敗しても LLM の費用の記録（`llm_calls`）は残し、失敗をログと通知メールで知らせる。
"""

import argparse
import fcntl
import html
import logging
import sqlite3
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Protocol

from aidigest.config import (
    ConfigError,
    LoadedSources,
    PricingConfig,
    Settings,
    TopicsConfig,
    load_pricing,
    load_settings,
    load_sources,
    load_topics,
)
from aidigest.db import DbError, open_db
from aidigest.fetchers.http import HttpClient
from aidigest.fetchers.runner import fetch_all, record_health
from aidigest.llm import JST, LlmCaller, LlmClient, LlmUnavailable, spent_usd
from aidigest.mailer import MailConfigError, Mailer, MailError
from aidigest.pipeline.allocation import allocate
from aidigest.pipeline.dedupe import register_items
from aidigest.pipeline.prefilter import prefilter
from aidigest.pipeline.rerank import RerankParams, rerank
from aidigest.pipeline.scoring import score_articles
from aidigest.pipeline.summarize import SummaryParams, summarize_articles
from aidigest.render import render_digest, write_static

logger = logging.getLogger(__name__)

LOG_FILE = "aidigest.log"
NOTICE_NO_LLM = "LLM を使わずに作成しました（--no-llm）。要約はありません。"
NOTICE_LLM_UNAVAILABLE = "LLM が使えないため、要約なしで作成しました。設定を確認してください。"
NOTICE_BUDGET = (
    "LLM の予算の上限（1 日 ${daily}／1 か月 ${monthly}）に達したため、"
    "要約していない記事があります。"
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 2


@dataclass(frozen=True)
class RunOptions:
    dry_run: bool
    no_llm: bool


class MailSender(Protocol):
    def send(self, subject: str, html: str, text: str) -> None: ...


LlmFactory = Callable[[sqlite3.Connection, int, PricingConfig], LlmCaller]


class _LockHeld(Exception):
    pass


def setup_logging(log_dir: Path, backup_days: int) -> list[logging.Handler]:
    """`log_dir/aidigest.log`（日ごとにローテーション）と標準エラーにログを出す。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = TimedRotatingFileHandler(
        log_dir / LOG_FILE, when="midnight", backupCount=backup_days, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [file_handler, logging.StreamHandler()]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    return handlers


@contextmanager
def _lock(path: Path) -> Generator[None]:
    """ロックファイルの排他ロックを取る。ほかの実行が持っていれば `_LockHeld`。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _LockHeld from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def run_digest(
    settings: Settings,
    options: RunOptions,
    *,
    http: HttpClient,
    llm_factory: LlmFactory,
    mailer: MailSender,
    clock: Callable[[], datetime],
) -> int:
    """1 回の実行。終了コード（0 成功、1 失敗、2 二重実行で中止）を返す。"""
    try:
        with _lock(settings.lock_path):
            return _Run(settings, options, http, llm_factory, mailer, clock).execute()
    except _LockHeld:
        logger.warning("ほかの実行が動いているため中止します（%s）", settings.lock_path)
        return EXIT_LOCKED


class _Run:
    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        http: HttpClient,
        llm_factory: LlmFactory,
        mailer: MailSender,
        clock: Callable[[], datetime],
    ) -> None:
        self.settings = settings
        self.options = options
        self.http = http
        self.llm_factory = llm_factory
        self.mailer = mailer
        self.clock = clock
        self.now = clock()
        self.day: date = self.now.astimezone(JST).date()

    def execute(self) -> int:
        s = self.settings
        logger.info(
            "実行を開始します（dry_run=%s, no_llm=%s）", self.options.dry_run, self.options.no_llm
        )
        try:
            loaded = load_sources(s.sources_path)
            topics = load_topics(s.topics_path)
            pricing = None if self.options.no_llm else load_pricing(s.pricing_path)
            conn = open_db(s.db_path)
        except (ConfigError, DbError) as e:
            logger.error("設定・DB の準備に失敗しました: %s", e)
            self._notify_failure("設定・DB の準備", e)
            return EXIT_FAILED

        try:
            return self._pipeline(conn, loaded, topics, pricing)
        finally:
            conn.close()

    def _pipeline(
        self,
        conn: sqlite3.Connection,
        loaded: LoadedSources,
        topics: TopicsConfig,
        pricing: PricingConfig | None,
    ) -> int:
        s = self.settings
        row = conn.execute(
            "INSERT INTO runs (started_at) VALUES (?) RETURNING id", (_iso(self.now),)
        ).fetchone()
        run_id = int(row[0])
        conn.commit()  # 途中で落ちても実行の記録を残し、llm_calls の外部キーを満たすため

        stage = "準備"
        fetched = 0
        selected_count = 0
        try:
            llm, notices = self._prepare_llm(conn, run_id, pricing)

            stage = "取得"
            result = fetch_all(self.http, loaded.sources, self.now, s.time_window_hours)
            record_health(conn, run_id, result.health)
            fetched = len(result.items)
            sources = {source.id: source for source in loaded.sources}

            stage = "既読判定"
            new = register_items(conn, result.items, sources, self.now)
            stage = "事前の絞り込み"
            passed = prefilter(
                conn, new, sources, loaded.exclude_keywords, self.now, s.time_window_hours
            )
            stage = "ローカルスコア"
            scored = score_articles(conn, passed, sources, topics)
            stage = "再ランク"
            ranked = rerank(conn, scored, llm, RerankParams.from_settings(s))
            stage = "枠配分"
            selected = allocate(conn, ranked, topics.digest)
            selected_count = len(selected)
            stage = "要約"
            summarized = summarize_articles(
                conn, selected, llm, self.http, SummaryParams.from_settings(s)
            )

            stage = "生成"
            spent = spent_usd(conn, self.now)
            if llm is not None and (
                spent.today >= s.budget_daily_usd or spent.month >= s.budget_monthly_usd
            ):
                notices.append(
                    NOTICE_BUDGET.format(daily=s.budget_daily_usd, monthly=s.budget_monthly_usd)
                )
            if self.options.dry_run:
                notices = []
            digest = render_digest(
                summarized,
                self.day,
                result.health,
                spent,
                s.usd_jpy_rate,
                notices,
                s.templates_dir,
            )
            write_static(digest.html, self.day, s.output_dir)
        except Exception as e:  # どの段階の失敗でも、費用の記録を残して通知するため受け止める
            logger.exception("%s の段階で失敗しました", stage)
            _rollback_keeping_records(conn, run_id)
            self._finish(conn, run_id, fetched, 0, 0, f"{stage}: {_describe(e)}")
            conn.commit()
            self._notify_failure(stage, e)
            return EXIT_FAILED

        article_ids = [a.selected.ranked.scored.article.id for a in summarized]
        error = None
        sent_count = 0
        if self.options.dry_run:
            _rollback_keeping_records(conn, run_id)  # 記事の状態は残さない（specs/013）
            status = "dry_run"
        elif not summarized:
            logger.info("記事が 0 件のため、メールは送りません")
            status = "empty"
        else:
            try:
                self.mailer.send(digest.subject, digest.html, digest.text)
                status = "sent"
                sent_count = len(article_ids)
                conn.executemany(
                    "UPDATE articles SET status = 'sent' WHERE id = ?",
                    [(article_id,) for article_id in article_ids],
                )
            except (MailError, MailConfigError) as e:
                logger.error("ダイジェストを送れませんでした: %s", e)
                status = "failed"
                error = f"送信: {_describe(e)}"

        conn.execute(
            "INSERT INTO digests (run_id, created_at, status, article_count, html, article_ids)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, _iso(self.clock()), status, len(article_ids), digest.html, _json(article_ids)),
        )
        self._finish(conn, run_id, fetched, selected_count, sent_count, error)
        conn.commit()
        logger.info("実行を終了しました（status=%s, 記事 %d 件）", status, len(article_ids))
        if status == "failed":
            self._notify_failure("送信", MailError(error or ""))
            return EXIT_FAILED
        return EXIT_OK

    def _prepare_llm(
        self, conn: sqlite3.Connection, run_id: int, pricing: PricingConfig | None
    ) -> tuple[LlmCaller | None, list[str]]:
        if self.options.no_llm or pricing is None:
            logger.info("--no-llm のため、LLM を使いません")
            return None, [NOTICE_NO_LLM]
        try:
            return self.llm_factory(conn, run_id, pricing), []
        except LlmUnavailable as e:
            logger.error("LLM が使えないため、要約なしで続けます: %s", e)
            return None, [NOTICE_LLM_UNAVAILABLE]

    def _finish(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        fetched: int,
        selected: int,
        sent: int,
        error: str | None,
    ) -> None:
        conn.execute(
            "UPDATE runs SET finished_at = ?, fetched_count = ?, selected_count = ?,"
            " sent_count = ?, error = ? WHERE id = ?",
            (_iso(self.clock()), fetched, selected, sent, error, run_id),
        )

    def _notify_failure(self, stage: str, error: Exception) -> None:
        """失敗通知メールを送る（--dry-run のときは送らない）。送れなければログだけ。"""
        if self.options.dry_run:
            return
        subject = f"[AI Digest] 実行に失敗しました {self.day.isoformat()}"
        text = (
            f"段階: {stage}\nエラー: {_describe(error)}\nログ: {self.settings.log_dir / LOG_FILE}\n"
        )
        try:
            self.mailer.send(subject, f"<pre>{html.escape(text)}</pre>", text)
        except (MailError, MailConfigError) as e:
            logger.error("失敗通知メールも送れませんでした: %s", e)


def _rollback_keeping_records(conn: sqlite3.Connection, run_id: int) -> None:
    """ロールバックする。ただし、その実行の llm_calls と source_health は残す。

    SQLite は書き込みを 1 つの接続しか持てないため、読み出してからロールバックし、入れ直す。
    """
    calls = conn.execute(
        "SELECT run_id, purpose, model, input_tokens, output_tokens, cost_usd, created_at"
        " FROM llm_calls WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    health = conn.execute(
        "SELECT run_id, source_id, status, error, item_count FROM source_health WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    conn.rollback()
    conn.executemany(
        "INSERT INTO llm_calls"
        " (run_id, purpose, model, input_tokens, output_tokens, cost_usd, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [tuple(row) for row in calls],
    )
    conn.executemany(
        "INSERT INTO source_health (run_id, source_id, status, error, item_count)"
        " VALUES (?, ?, ?, ?, ?)",
        [tuple(row) for row in health],
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _json(ids: Sequence[int]) -> str:
    return "[" + ", ".join(str(i) for i in ids) + "]"


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aidigest", description="AI Digest を作って送る")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="取得からメール送信までを 1 回実行する")
    run.add_argument("--dry-run", action="store_true", help="メールを送らず、記事の状態を残さない")
    run.add_argument("--no-llm", action="store_true", help="LLM を使わない")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(Path(".env"))
    except ConfigError as e:
        logging.basicConfig(level=logging.ERROR)
        logger.error("設定を読み込めません: %s", e)
        return EXIT_FAILED
    setup_logging(settings.log_dir, settings.log_backup_days)
    http = HttpClient.from_settings(settings)

    def llm_factory(conn: sqlite3.Connection, run_id: int, pricing: PricingConfig) -> LlmCaller:
        return LlmClient(conn, run_id, settings, pricing)

    try:
        return run_digest(
            settings,
            RunOptions(dry_run=bool(args.dry_run), no_llm=bool(args.no_llm)),
            http=http,
            llm_factory=llm_factory,
            mailer=Mailer(settings),
            clock=lambda: datetime.now(UTC),
        )
    finally:
        http.close()


if __name__ == "__main__":
    sys.exit(main())
