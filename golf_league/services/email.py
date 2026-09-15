"""Email transport seam.

No SMTP client lives here. `FakeEmailSender` is the only implementation
this task ships; it records messages in memory and never opens a network
connection. A real transport (e.g. SMTP) is a later task's concern.
"""

from typing import Protocol


class EmailSender(Protocol):
    """Anything that can deliver a plain-text email."""

    def send(self, to: str, subject: str, body: str) -> None:
        """Send `body` to `to` with `subject`."""
        ...


class FakeEmailSender:
    """A test double that records every message instead of sending it.

    Used by every test in this suite so nothing ever opens a real network
    connection.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
