"""メール送信（specs/012-mailer.md）のテスト。SMTP の接続は偽物に差し替え、実際には送らない。"""

import logging
import smtplib
import ssl
from collections.abc import Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from aidigest.config import Settings, load_settings
from aidigest.mailer import MailConfigError, Mailer, MailError, build_message

PASSWORD = "abcd efgh ijkl mnop"
NOW = datetime(2026, 10, 16, 21, 30, tzinfo=UTC)
SMTP_ENV = [
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "MAIL_FROM",
    "MAIL_TO",
    "SMTP_SECURITY",
    "SMTP_RETRIES",
    "SMTP_RETRY_WAIT_SECONDS",
    "SMTP_TIMEOUT_SECONDS",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in SMTP_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": 587,
        "SMTP_USER": "me@example.com",
        "SMTP_PASSWORD": PASSWORD,
        "MAIL_FROM": "AI Digest <me@example.com>",
        "MAIL_TO": "a@example.com, b@example.com",
        "SMTP_RETRY_WAIT_SECONDS": 10,
        **overrides,
    }
    env = tmp_path / "test.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))
    return load_settings(env)


class FakeSmtp:
    """SMTP の接続の偽物。決まった順に例外を送出するか、成功する。"""

    def __init__(self, server: "FakeServer") -> None:
        self.server = server

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]:
        self.server.log.append("starttls")
        self.server.contexts.append(context)
        return (220, b"ready")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.server.log.append(f"login:{user}")
        self.server.step("login")
        return (235, b"ok")

    def send_message(self, msg: EmailMessage, /) -> dict[str, tuple[int, bytes]]:
        self.server.log.append("send")
        self.server.step("send")
        self.server.sent.append(msg)
        return dict(self.server.refused)

    def quit(self) -> tuple[int, bytes]:
        self.server.log.append("quit")
        return (221, b"bye")


class FakeServer:
    def __init__(
        self,
        errors: Sequence[tuple[str, Exception]] = (),
        refused: dict[str, tuple[int, bytes]] | None = None,
    ) -> None:
        self.errors = list(errors)  # (どの段階で, 何を送出するか) を 1 回ずつ
        self.refused = refused or {}
        self.log: list[str] = []
        self.connects: list[tuple[str, int, float, bool]] = []
        self.contexts: list[ssl.SSLContext] = []
        self.sent: list[EmailMessage] = []

    def connect(self, host: str, port: int, timeout: float, use_ssl: bool) -> FakeSmtp:
        self.connects.append((host, port, timeout, use_ssl))
        self.log.append("connect")
        self.step("connect")
        return FakeSmtp(self)

    def step(self, stage: str) -> None:
        if self.errors and self.errors[0][0] == stage:
            raise self.errors.pop(0)[1]


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def send(
    tmp_path: Path, server: FakeServer, sleeps: Sleeps | None = None, **overrides: object
) -> None:
    mailer = Mailer(
        make_settings(tmp_path, **overrides),
        connect=server.connect,
        sleep=sleeps or Sleeps(),
        clock=lambda: NOW,
    )
    mailer.send("[AI Digest] 2026-10-17 注目 7 件", "<p>本文</p>", "本文")


# ------------------------------------------------------------ 組み立て


def test_message_is_multipart_alternative() -> None:
    """AC-012-2"""
    msg = build_message(
        subject="[AI Digest] 2026-10-17 注目 7 件",
        html="<p>HTML の本文</p>\n",
        text="テキストの本文\n",
        mail_from="AI Digest <me@example.com>",
        mail_to=["a@example.com", "b@example.com"],
        now=NOW,
    )

    assert msg["Subject"] == "[AI Digest] 2026-10-17 注目 7 件"
    assert msg["From"] == "AI Digest <me@example.com>"
    assert msg["To"] == "a@example.com, b@example.com"
    assert msg["Date"] is not None
    message_id = msg["Message-ID"]
    assert message_id is not None
    assert message_id.endswith("@example.com>")
    assert msg.get_content_type() == "multipart/alternative"
    text_part, html_part = list(msg.iter_parts())
    assert isinstance(text_part, EmailMessage)
    assert isinstance(html_part, EmailMessage)
    assert text_part.get_content_type() == "text/plain"
    assert html_part.get_content_type() == "text/html"
    assert text_part.get_content_charset() == "utf-8"
    assert html_part.get_content_charset() == "utf-8"
    assert text_part.get_content() == "テキストの本文\n"
    assert html_part.get_content() == "<p>HTML の本文</p>\n"
    assert "=?utf-8?" in msg.as_string()


# ------------------------------------------------------------ 送信


def test_starttls_flow(tmp_path: Path) -> None:
    """AC-012-3"""
    server = FakeServer()

    send(tmp_path, server, SMTP_TIMEOUT_SECONDS=12)

    assert server.connects == [("smtp.example.com", 587, 12, False)]
    assert server.log == ["connect", "starttls", "login:me@example.com", "send", "quit"]
    (context,) = server.contexts
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    (msg,) = server.sent
    assert msg["To"] == "a@example.com, b@example.com"


def test_ssl_flow(tmp_path: Path) -> None:
    """AC-012-4"""
    server = FakeServer()

    send(tmp_path, server, SMTP_SECURITY="ssl", SMTP_PORT=465)

    assert server.connects == [("smtp.example.com", 465, 30, True)]
    assert server.log == ["connect", "login:me@example.com", "send", "quit"]


def test_missing_settings(tmp_path: Path) -> None:
    """AC-012-5"""
    server = FakeServer()

    with pytest.raises(MailConfigError) as exc_info:
        send(tmp_path, server, SMTP_PASSWORD=None, MAIL_TO=None)

    assert "SMTP_PASSWORD" in str(exc_info.value)
    assert "MAIL_TO" in str(exc_info.value)
    assert server.connects == []


@pytest.mark.parametrize(
    "error",
    [
        ("connect", ConnectionRefusedError("refused")),
        ("send", smtplib.SMTPServerDisconnected("gone")),
        ("connect", smtplib.SMTPConnectError(421, b"busy")),
        ("send", smtplib.SMTPResponseException(451, b"try later")),
    ],
    ids=["refused", "disconnected", "421-connect", "451-send"],
)
def test_transient_error_is_retried(tmp_path: Path, error: tuple[str, Exception]) -> None:
    """AC-012-6"""
    server = FakeServer([error])
    sleeps = Sleeps()

    send(tmp_path, server, sleeps)

    assert len(server.connects) == 2
    assert len(server.sent) == 1
    assert sleeps.calls == [10]


@pytest.mark.parametrize(
    "error",
    [
        ("login", smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")),
        ("send", smtplib.SMTPResponseException(550, b"mailbox unavailable")),
        ("send", smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no such user")})),
    ],
    ids=["auth-535", "550", "all-refused"],
)
def test_permanent_error_is_not_retried(tmp_path: Path, error: tuple[str, Exception]) -> None:
    """AC-012-7"""
    server = FakeServer([error])
    sleeps = Sleeps()

    with pytest.raises(MailError) as exc_info:
        send(tmp_path, server, sleeps)

    assert len(server.connects) == 1
    assert sleeps.calls == []
    assert PASSWORD not in str(exc_info.value)


def test_retries_exhausted(tmp_path: Path) -> None:
    """AC-012-8"""
    disconnected = smtplib.SMTPServerDisconnected("gone")
    server = FakeServer([("send", disconnected), ("send", disconnected)])

    with pytest.raises(MailError):
        send(tmp_path, server, SMTP_RETRIES=1)

    assert len(server.connects) == 2


def test_partially_refused_recipients(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """AC-012-9"""
    server = FakeServer(refused={"b@example.com": (550, b"no such user")})

    with caplog.at_level(logging.WARNING, logger="aidigest.mailer"):
        send(tmp_path, server)

    assert len(server.sent) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("b@example.com" in w for w in warnings)
