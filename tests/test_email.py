"""The email transport seam: FakeEmailSender (GL-03) and SmtpEmailSender (GL-45).

No test opens a network connection: smtplib.SMTP and smtplib.SMTP_SSL are
replaced with recording fakes through monkeypatch.
"""

import email
import ssl
from email.utils import getaddresses

import pytest

from golf_league.services import email as email_module
from golf_league.services.email import EmailSender, FakeEmailSender, SmtpEmailSender


def test_fake_email_sender_records_messages_in_memory():
    sender = FakeEmailSender()

    sender.send("player@example.com", "Verify your email", "Click the link.")
    sender.send("other@example.com", "Reset your password", "Click this link.")

    assert len(sender.sent) == 2
    assert sender.sent[0] == {
        "to": "player@example.com",
        "subject": "Verify your email",
        "body": "Click the link.",
    }
    assert sender.sent[1]["to"] == "other@example.com"


def test_fake_email_sender_starts_empty():
    sender = FakeEmailSender()
    assert sender.sent == []


def test_fake_email_sender_satisfies_the_email_sender_protocol():
    sender: EmailSender = FakeEmailSender()
    sender.send("someone@example.com", "Subject", "Body")
    assert isinstance(sender, FakeEmailSender)


# --- GL-45: SmtpEmailSender, against monkeypatched fake SMTP classes ---------

SYNTHETIC_PASSWORD = "synthetic-relay-password-91c2"


class _FakeSMTP:
    """Records every call; opens no socket."""

    instances: list["_FakeSMTP"] = []
    fail_on: str | None = None

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[tuple] = []
        self.messages = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))
        return False

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if type(self).fail_on == name:
            raise OSError("simulated relay failure")

    def starttls(self, *, context=None):
        self._record("starttls")
        self.starttls_context = context

    def login(self, username, password):
        self._record("login", username, password)

    def send_message(self, message):
        self._record("send_message")
        self.messages.append(message)


class _FakeSMTPSSL(_FakeSMTP):
    instances: list["_FakeSMTP"] = []


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances, _FakeSMTPSSL.instances = [], []
    _FakeSMTP.fail_on = _FakeSMTPSSL.fail_on = None
    monkeypatch.setattr(email_module.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_module.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    return _FakeSMTP, _FakeSMTPSSL


def _sender(**overrides) -> SmtpEmailSender:
    values = {
        "host": "relay.example.test", "port": 587, "username": "synthetic-user",
        "password": SYNTHETIC_PASSWORD, "from_email": "league@example.test",
        "from_name": "Synthetic League", "tls_mode": "starttls",
    }
    values.update(overrides)
    return SmtpEmailSender(**values)


def _names(instance) -> list[str]:
    return [call[0] for call in instance.calls]


def test_starttls_mode_upgrades_then_logs_in_then_sends(fake_smtp):
    plain, secure = fake_smtp
    _sender().send("player@example.test", "Subject", "Body text")

    assert secure.instances == []
    (connection,) = plain.instances
    assert (connection.host, connection.port) == ("relay.example.test", 587)
    assert connection.kwargs["timeout"] == 15
    assert _names(connection) == ["starttls", "login", "send_message", "quit"]
    assert connection.calls[1] == ("login", "synthetic-user", SYNTHETIC_PASSWORD)
    assert connection.starttls_context.verify_mode == ssl.CERT_REQUIRED


def test_ssl_mode_uses_the_ssl_class_and_never_starttls(fake_smtp):
    plain, secure = fake_smtp
    _sender(tls_mode="ssl", port=465).send("player@example.test", "Subject", "Body")

    assert plain.instances == []
    (connection,) = secure.instances
    assert connection.kwargs["timeout"] == 15
    assert connection.kwargs["context"].check_hostname is True
    assert "starttls" not in _names(connection)
    assert _names(connection) == ["login", "send_message", "quit"]


def test_none_mode_neither_upgrades_nor_uses_ssl(fake_smtp):
    plain, secure = fake_smtp
    _sender(tls_mode="none", port=25).send("player@example.test", "Subject", "Body")

    assert secure.instances == []
    (connection,) = plain.instances
    assert _names(connection) == ["login", "send_message", "quit"]


@pytest.mark.parametrize("username", [None, ""])
def test_no_username_means_no_login(fake_smtp, username):
    plain, _ = fake_smtp
    _sender(username=username, password=None).send("player@example.test", "Subject", "Body")
    assert _names(plain.instances[0]) == ["starttls", "send_message", "quit"]


def test_login_with_username_but_no_password_sends_empty_password(fake_smtp):
    plain, _ = fake_smtp
    _sender(password=None).send("player@example.test", "Subject", "Body")
    assert plain.instances[0].calls[1] == ("login", "synthetic-user", "")


def test_message_headers_and_body(fake_smtp):
    plain, _ = fake_smtp
    _sender().send("player@example.test", "Reset your password", "Use this link: https://example.test/reset/x")

    (message,) = plain.instances[0].messages
    parsed = email.message_from_string(message.as_string())
    assert parsed["Subject"] == "Reset your password"
    assert parsed["To"] == "player@example.test"
    assert getaddresses([parsed["From"]]) == [("Synthetic League", "league@example.test")]
    assert "https://example.test/reset/x" in message.get_content()


@pytest.mark.parametrize("from_name", ['Synthetic, "Quoted" League', 'Comma, League', 'Say "hi"'])
def test_awkward_from_name_still_produces_one_valid_address(fake_smtp, from_name):
    plain, _ = fake_smtp
    _sender(from_name=from_name).send("player@example.test", "Subject", "Body")

    (message,) = plain.instances[0].messages
    parsed = email.message_from_string(message.as_string())
    assert getaddresses([parsed["From"]]) == [(from_name, "league@example.test")]


def test_repr_carries_no_credentials_or_addresses():
    text = repr(_sender())
    assert text == "SmtpEmailSender(host='relay.example.test', port=587, tls_mode='starttls')"
    for private in ("synthetic-user", SYNTHETIC_PASSWORD, "league@example.test"):
        assert private not in text


@pytest.mark.parametrize("failing", ["starttls", "login", "send_message"])
def test_transport_errors_propagate(fake_smtp, failing):
    plain, _ = fake_smtp
    plain.fail_on = failing
    with pytest.raises(OSError, match="simulated relay failure"):
        _sender().send("player@example.test", "Subject", "Body")
    assert _names(plain.instances[0])[-1] == "quit"


def test_smtp_sender_satisfies_the_email_sender_protocol():
    sender: EmailSender = _sender()
    assert callable(sender.send)
