"""Pure identity helpers: no I/O, no clock, no persistence."""

import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str) -> str:
    """Strip surrounding whitespace and lowercase an email address."""
    return raw.strip().lower()


def is_valid_email(value: str) -> bool:
    """Return True when `value` looks like a syntactically valid email address.

    This is a coarse shape check only (has a local part, an `@`, and a
    domain with a dot) — not full RFC 5322 validation.
    """
    return bool(_EMAIL_RE.match(value))
