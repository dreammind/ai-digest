"""メール送信（specs/012-mailer.md）。

SMTP リレー（Gmail・さくらインターネットなど）経由で、HTML とテキストの両方を持つメールを送る。
接続先と暗号化の方式は設定だけで切り替えられる。パスワードはメッセージ・ログに出さない。
"""

import contextlib
import logging
import smtplib
import ssl
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from typing import Protocol

from aidigest.config import Settings

logger = logging.getLogger(__name__)


class MailConfigError(Exception):
    """送信に必要な設定が足りない。"""


class MailError(Exception):
    """メールを送れなかった（再試行しても失敗、または恒久的なエラー）。"""


class SmtpConnection(Protocol):
    """`smtplib.SMTP` / `SMTP_SSL` のうち、送信に使う部分（テストでは偽物に差し替える）。"""

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]: ...

    def login(self, user: str, password: str) -> tuple[int, bytes]: ...

    def send_message(self, msg: EmailMessage, /) -> dict[str, tuple[int, bytes]]: ...

    def quit(self) -> tuple[int, bytes]: ...


Connect = Callable[[str, int, float, bool], SmtpConnection]  # (host, port, timeout, use_ssl)


def _connect(host: str, port: int, timeout: float, use_ssl: bool) -> SmtpConnection:
    if use_ssl:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def build_message(
    *,
    subject: str,
    html: str,
    text: str,
    mail_from: str,
    mail_to: Sequence[str],
    now: datetime,
) -> EmailMessage:
    """テキスト → HTML の順の multipart/alternative のメールを作る。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)
    msg["Date"] = format_datetime(now)
    domain = parseaddr(mail_from)[1].rpartition("@")[2]
    msg["Message-ID"] = make_msgid(domain=domain or None)
    msg.set_content(text, subtype="plain", charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


class Mailer:
    def __init__(
        self,
        settings: Settings,
        *,
        connect: Connect = _connect,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._connect = connect
        self._sleep = sleep
        self._clock = clock

    def send(self, subject: str, html: str, text: str) -> None:
        """メールを送る。一時的なエラーは再試行し、それでも失敗すれば `MailError`。"""
        s = self._settings
        host, port, user, mail_from = s.smtp_host, s.smtp_port, s.smtp_user, s.mail_from
        password = s.smtp_password.get_secret_value() if s.smtp_password else ""
        mail_to = [a.strip() for a in (s.mail_to or "").split(",") if a.strip()]
        missing = [
            name
            for name, value in [
                ("SMTP_HOST", host),
                ("SMTP_PORT", port),
                ("SMTP_USER", user),
                ("SMTP_PASSWORD", password),
                ("MAIL_FROM", mail_from),
                ("MAIL_TO", mail_to),
            ]
            if not value
        ]
        if missing or host is None or port is None or user is None or mail_from is None:
            raise MailConfigError(f"メールの送信に必要な設定がありません: {', '.join(missing)}")

        msg = build_message(
            subject=subject,
            html=html,
            text=text,
            mail_from=mail_from,
            mail_to=mail_to,
            now=self._clock(),
        )
        last_error = ""
        for attempt in range(s.smtp_retries + 1):
            if attempt > 0:
                self._sleep(s.smtp_retry_wait_seconds)
            try:
                self._send_once(host, port, user, password, msg)
                return
            except smtplib.SMTPRecipientsRefused as e:
                raise MailError(f"すべての宛先が拒否されました: {_describe(e)}") from None
            except smtplib.SMTPResponseException as e:
                if not 400 <= e.smtp_code < 500:
                    raise MailError(f"メールを送れませんでした: {_describe(e)}") from None
                last_error = _describe(e)
            except smtplib.SMTPServerDisconnected as e:
                last_error = _describe(e)
            except smtplib.SMTPException as e:
                raise MailError(f"メールを送れませんでした: {_describe(e)}") from None
            except (
                OSError
            ) as e:  # 接続できない・タイムアウトなど（SMTPException も OSError なので最後に置く）
                last_error = _describe(e)
            logger.warning("メールの送信に失敗しました（%d 回目）: %s", attempt + 1, last_error)
        raise MailError(f"{s.smtp_retries + 1} 回試みて失敗しました: {last_error}")

    def _send_once(self, host: str, port: int, user: str, password: str, msg: EmailMessage) -> None:
        use_ssl = self._settings.smtp_security == "ssl"
        conn = self._connect(host, port, self._settings.smtp_timeout_seconds, use_ssl)
        try:
            if not use_ssl:
                conn.starttls(context=ssl.create_default_context())
            conn.login(user, password)
            refused = conn.send_message(msg)
            if refused:
                logger.warning("一部の宛先に届きませんでした: %s", ", ".join(sorted(refused)))
        finally:
            # 送信の成否はすでに決まっているので、切断の失敗は無視する
            with contextlib.suppress(smtplib.SMTPException, OSError):
                conn.quit()


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"
