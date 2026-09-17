"""GL-03: the email transport seam — FakeEmailSender only, no network I/O."""

from golf_league.services.email import EmailSender, FakeEmailSender


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
