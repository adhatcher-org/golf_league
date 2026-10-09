"""Email transport seam.

Two implementations of `EmailSender`:

- `SmtpEmailSender` delivers through an SMTP relay using only the standard
  library (`smtplib`, `ssl`, `email`). The application installs it at
  startup when `SMTP_HOST` is set (see `golf_league/app.py`).
- `FakeEmailSender` records messages in memory and never opens a network
  connection. It is the default when no relay is configured, and every test
  uses it or a monkeypatched fake SMTP class.

Senders are synchronous. Callers run them after the HTTP response (a
background task) and own all failure handling: the SMTP sender neither logs
nor catches anything.
"""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

SMTP_TIMEOUT_SECONDS = 15


class EmailSender(Protocol):
    """Anything that can deliver a plain-text email."""

    def send(self, to: str, subject: str, body: str) -> None:
        """Send `body` to `to` with `subject`."""
        ...


class SmtpEmailSender:
    """Plain-text delivery through an SMTP relay.

    `tls_mode` is `starttls` (plain connection upgraded with STARTTLS and a
    verifying default SSL context), `ssl` (TLS from the first byte) or
    `none` (no encryption). Login happens only when a username is set.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        from_email: str,
        from_name: str,
        tls_mode: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.from_email = from_email
        self.from_name = from_name
        self.tls_mode = tls_mode

    def __repr__(self) -> str:
        return f"SmtpEmailSender(host={self.host!r}, port={self.port!r}, tls_mode={self.tls_mode!r})"

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((self.from_name, self.from_email))
        message["To"] = to
        message.set_content(body)

        if self.tls_mode == "ssl":
            # smtplib's own default context does not verify certificates.
            connection = smtplib.SMTP_SSL(
                self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS, context=ssl.create_default_context()
            )
        else:
            connection = smtplib.SMTP(self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS)
        with connection as smtp:
            if self.tls_mode == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self._password or "")
            smtp.send_message(message)


class FakeEmailSender:
    """A test double that records every message instead of sending it.

    Used by every test in this suite so nothing ever opens a real network
    connection.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
