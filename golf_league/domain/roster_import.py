"""Pure parsing, normalization and validation for CSV roster imports.

No I/O, no database, no HTTP — standard library plus
`golf_league.domain.roster` only. Every function here takes values and
returns values; none reads a file, a database or a clock.
"""

from collections.abc import Iterable, Sequence

from golf_league.domain.roster import (
    normalize_email,
    normalize_name,
    validate_handicap_strokes,
)

REGULAR_HEADERS = ("Name", "Status", "Gold HC", "White HC", "Email", "Phone #")
SUB_HEADERS = ("First Name", "Last Name", "Phone", "Email", "2025 HC")

HARD_ERRORS = (
    "name_unparseable", "email_missing", "email_malformed", "email_duplicate_in_batch",
    "status_blank", "status_unknown", "handicap_invalid", "phone_invalid",
    "tee_required_for_new_golfer", "protected_field_changed",
)
WARNINGS = (
    "handicap_defaulted_zero", "handicap_blank", "tee_defaulted_white",
    "email_whitespace_trimmed", "email_lowercased", "email_domain_near_miss",
    "name_possibly_reversed",
)
KNOWN_DOMAINS = (
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "icloud.com", "outlook.com",
    "live.com", "msn.com", "me.com", "comcast.net", "att.net", "sbcglobal.net",
)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_DATA_ROWS = 1000
MAX_PHONE_LENGTH = 40


def detect_header_set(header: Sequence[str]) -> str | None:
    """Return `"summer_regular"`, `"summer_sub"` or None for a header row.

    Each cell is stripped before comparison; the comparison is over the
    whole tuple (column count and order both matter) and case-sensitive.
    """
    stripped = tuple(cell.strip() for cell in header)
    if stripped == REGULAR_HEADERS:
        return "summer_regular"
    if stripped == SUB_HEADERS:
        return "summer_sub"
    return None


def split_regular_name(value: str) -> tuple[str, str] | None:
    """Split `"Last, First"` on the first comma into `(first, last)`.

    Returns None when there is no comma, or when either side normalizes
    to the empty string.
    """
    if "," not in value:
        return None
    left, right = value.split(",", 1)
    last_name = normalize_name(left)
    first_name = normalize_name(right)
    if not first_name or not last_name:
        return None
    return (first_name, last_name)


def resolve_status(value: str) -> str | None:
    """Return `"Gold"`, `"White"`, or None for blank or unknown text."""
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered == "gold":
        return "Gold"
    if lowered == "white":
        return "White"
    return None


def parse_handicap_cell(value: str) -> tuple[int | None, bool]:
    """Parse one handicap cell, sharing the admin form's grammar.

    Returns `(number, ok)`. A blank cell is `(None, True)`. Otherwise the
    grammar is delegated to
    `golf_league.domain.roster.validate_handicap_strokes` so the import
    cell and the admin form never diverge.
    """
    stripped = value.strip()
    if not stripped:
        return (None, True)
    error = validate_handicap_strokes(stripped)
    if error is not None:
        return (None, False)
    return (int(stripped), True)


def normalize_email_cell(value: str) -> tuple[str | None, list[str]]:
    """Normalize one email cell and report the warnings it earned.

    The normalized address comes from
    `golf_league.domain.roster.normalize_email`. A blank cell gives
    `(None, [])`. Otherwise `email_whitespace_trimmed` is added when the
    raw value had surrounding whitespace, and `email_lowercased` when the
    stripped value was not already lowercase.
    """
    stripped = value.strip()
    if not stripped:
        return (None, [])
    warnings: list[str] = []
    if value != value.strip():
        warnings.append("email_whitespace_trimmed")
    if value.strip() != value.strip().lower():
        warnings.append("email_lowercased")
    return (normalize_email(value), warnings)


def email_is_wellformed(value: str) -> bool:
    """A deliberately loose structural check, to catch an extraction defect.

    True when `value` has no whitespace, exactly one `@`, at least one
    character before the `@`, and, after the `@`, a `.` that is neither
    the first nor the last character of that part.
    """
    if any(ch.isspace() for ch in value):
        return False
    if value.count("@") != 1:
        return False
    local, domain = value.split("@", 1)
    if not local:
        return False
    for index, ch in enumerate(domain):
        if ch == "." and 0 < index < len(domain) - 1:
            return True
    return False


def _osa_distance(left: str, right: str) -> int:
    """Optimal-string-alignment distance: Levenshtein plus an adjacent swap.

    Standard dynamic-programming table; no substring is edited more than
    once.
    """
    len_l, len_r = len(left), len(right)
    d = [[0] * (len_r + 1) for _ in range(len_l + 1)]
    for i in range(len_l + 1):
        d[i][0] = i
    for j in range(len_r + 1):
        d[0][j] = j

    for i in range(1, len_l + 1):
        for j in range(1, len_r + 1):
            cost = 0 if left[i - 1] == right[j - 1] else 1
            d[i][j] = min(
                d[i - 1][j] + 1,
                d[i][j - 1] + 1,
                d[i - 1][j - 1] + cost,
            )
            if (
                i > 1
                and j > 1
                and left[i - 1] == right[j - 2]
                and left[i - 2] == right[j - 1]
            ):
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len_l][len_r]


def near_miss_domain(domain: str) -> str | None:
    """Suggest a known domain within edit distance 1-2, when unambiguous.

    Compares the whole lowercased `domain` with each `KNOWN_DOMAINS`
    entry. A listed domain earns no warning. Otherwise the candidates
    within distance 1 or 2 are considered; the closest is suggested only
    when exactly one candidate sits at the smallest such distance.
    """
    lowered = domain.lower()
    if lowered in KNOWN_DOMAINS:
        return None

    candidates = []
    for known in KNOWN_DOMAINS:
        distance = _osa_distance(lowered, known)
        if distance in (1, 2):
            candidates.append((distance, known))

    if not candidates:
        return None

    smallest = min(distance for distance, _ in candidates)
    at_smallest = [known for distance, known in candidates if distance == smallest]
    if len(at_smallest) == 1:
        return at_smallest[0]
    return None


def likely_reversed(
    rows: Sequence[tuple[int, str, str, str | None]],
) -> set[int]:
    """Positions whose first name matches another row's last name.

    `rows` is `(position, first_name, last_name, email)`. A row at
    `position` is flagged when some *other* row's last name equals its
    first name, case-insensitively, and the two rows' emails are not the
    same (two emails are the same only when both are non-None and equal;
    any other pairing, absent-absent included, counts as different).
    """
    flagged: set[int] = set()
    for position, first_name, _last_name, email in rows:
        first_lower = first_name.lower()
        for other_position, _other_first, other_last, other_email in rows:
            if other_position == position:
                continue
            if other_last.lower() != first_lower:
                continue
            same_email = (
                email is not None and other_email is not None and email == other_email
            )
            if not same_email:
                flagged.add(position)
                break
    return flagged


def selected_handicap(
    *,
    source_role: str,
    tee_label: str | None,
    handicap_gold: int | None,
    handicap_white: int | None,
    handicap_single: int | None,
) -> int | None:
    """The one handicap number an apply may ever persist for this row."""
    if source_role == "summer_regular":
        if tee_label == "Gold":
            return handicap_gold
        if tee_label == "White":
            return handicap_white
        return None
    if source_role == "summer_sub":
        return handicap_single
    return None


def phones_equal(left: str | None, right: str | None) -> bool:
    """True when the ASCII digits of `left` and `right`, in order, match.

    Two absent values are equal; one absent value is never equal to a
    present one.
    """
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    digits = "0123456789"
    left_digits = "".join(ch for ch in left if ch in digits)
    right_digits = "".join(ch for ch in right if ch in digits)
    return left_digits == right_digits


def sanitize_display_name(value: str) -> str:
    """Return `value` as display text only: never a path, never opened."""
    text = value.rsplit("/", 1)[-1]
    text = text.rsplit("\\", 1)[-1]
    text = "".join(ch for ch in text if ord(ch) >= 32)
    text = text.strip()
    text = text[:255]
    if not text:
        return "upload.csv"
    return text


def encode_warnings(codes: Iterable[str]) -> str:
    """Encode a set of warning codes as a fixed-order, comma-separated list."""
    code_set = set(codes)
    ordered = [code for code in WARNINGS if code in code_set]
    return ",".join(ordered)


def decode_warnings(text: str) -> list[str]:
    """Reverse `encode_warnings`; an empty string means no warnings."""
    if not text:
        return []
    return text.split(",")


__all__ = [
    "REGULAR_HEADERS",
    "SUB_HEADERS",
    "HARD_ERRORS",
    "WARNINGS",
    "KNOWN_DOMAINS",
    "MAX_UPLOAD_BYTES",
    "MAX_DATA_ROWS",
    "MAX_PHONE_LENGTH",
    "detect_header_set",
    "split_regular_name",
    "resolve_status",
    "parse_handicap_cell",
    "normalize_email_cell",
    "email_is_wellformed",
    "near_miss_domain",
    "likely_reversed",
    "selected_handicap",
    "phones_equal",
    "sanitize_display_name",
    "encode_warnings",
    "decode_warnings",
]
