"""Pure validation for courses, tee sets and tee ratings.

No I/O, no database, no HTTP. Every validator returns a list of field
errors (empty when the value is valid) so a route can re-render a form
with per-field messages instead of raising.
"""

from decimal import Decimal, InvalidOperation

VALID_GENDERS = ("men", "women")
VALID_SCOPES = ("front", "back", "full")


def validate_gender(value: str) -> str | None:
    """Return an error message, or None when `value` is `men` or `women`."""
    if value not in VALID_GENDERS:
        return "Gender must be 'men' or 'women'."
    return None


def validate_scope(value: str) -> str | None:
    """Return an error message, or None when `value` is a valid scope."""
    if value not in VALID_SCOPES:
        return "Scope must be 'front', 'back' or 'full'."
    return None


def validate_positive_int(value: object, field_name: str) -> str | None:
    """Return an error message, or None when `value` is a positive integer.

    Accepts an `int` or a string that parses cleanly as one; rejects
    floats-as-strings, empty strings, and non-positive values.
    """
    if isinstance(value, bool):
        return f"{field_name} must be a positive whole number."
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        if not text or not (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
            return f"{field_name} must be a positive whole number."
        parsed = int(text)
    if parsed <= 0:
        return f"{field_name} must be a positive whole number."
    return None


def validate_rating(value: object) -> str | None:
    """Return an error message, or None when `value` is a finite rating number.

    Rejects empty strings, non-numeric text (`abc`), and non-finite values
    (`NaN`, `Infinity`) — none of these are silently clamped to 0.
    """
    text = str(value).strip()
    if not text:
        return "Rating is required."
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return "Rating must be a finite number."
    if not parsed.is_finite():
        return "Rating must be a finite number."
    return None


def to_rating_decimal(value: object) -> Decimal:
    """Convert an already-validated rating value to a `Decimal`.

    Callers must call `validate_rating` first; this raises for invalid
    input rather than guessing a default.
    """
    return Decimal(str(value).strip())
