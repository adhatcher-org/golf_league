"""Pure validation and normalization for the roster.

No I/O, no database, no HTTP — standard library only. Every normalizer
returns the value to store (never raises); `validate_handicap_strokes`
returns an error message or None, mirroring the course domain module's
validators so a route can re-render a form with per-field messages.
"""

HANDICAP_SOURCES = ("imported", "self_reported", "computed")
HANDICAP_STATUSES = ("ok", "needs_entry", "needs_contact")


def normalize_email(value: str | None) -> str | None:
    """Strip whitespace and lowercase; blank becomes None.

    Does not validate deliverability and does not reject unusual
    characters — only whitespace and case are normalized.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped.lower()


def normalize_phone(value: str | None) -> str | None:
    """Strip surrounding whitespace; blank becomes None.

    No digit stripping, no punctuation removal, no country code
    inference, no reformatting: the number is stored exactly as entered.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


def normalize_name(value: str | None) -> str:
    """Strip surrounding whitespace and collapse internal whitespace runs.

    Case is never changed, so `de Vries` and `McLeod` survive intact.
    """
    if value is None:
        return ""
    return " ".join(value.split())


def validate_handicap_strokes(value: object) -> str | None:
    """Return an error message, or None when `value` is a whole number or absent.

    Absent (None, empty string, whitespace-only string) is valid and means
    NULL. `0` is valid and means a scratch golfer. A leading `-` is
    accepted with no lower bound; there is no upper bound either. The
    grammar is an optional leading `-` followed by one or more decimal
    digits, tested with `str.isdecimal()` (never `str.isdigit()`, which
    disagrees with `int()` on characters like `"²"`).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "Handicap must be a whole number."
    if isinstance(value, int):
        return None

    text = str(value).strip()
    if not text:
        return None

    digits = text[1:] if text.startswith("-") else text
    if not digits or not digits.isdecimal():
        return "Handicap must be a whole number."
    return None


def derive_handicap_status(*, email: str | None, handicap_strokes: int | None) -> str:
    """Return `needs_contact`, `needs_entry` or `ok`.

    The two absences are judged independently and in that order: a
    missing email always yields `needs_contact`, even when strokes are
    present. A present email with absent strokes yields `needs_entry`.
    """
    if email is None:
        return "needs_contact"
    if handicap_strokes is None:
        return "needs_entry"
    return "ok"


__all__ = [
    "HANDICAP_SOURCES",
    "HANDICAP_STATUSES",
    "normalize_email",
    "normalize_phone",
    "normalize_name",
    "validate_handicap_strokes",
    "derive_handicap_status",
]
