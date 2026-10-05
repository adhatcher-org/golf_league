"""Pure calendar and nine generation for a season schedule."""

from datetime import date, datetime, timedelta


def generate_weeks(
    start_date: date,
    weekday: int,
    count: int,
    first_week_nine: str,
) -> list[tuple[date, str]]:
    """Return the printed weekly dates and alternating nines without database access."""
    if not isinstance(start_date, date) or isinstance(start_date, datetime):
        raise ValueError("Start date must be a calendar date.")
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        raise ValueError("Weekday must be between 0 and 6.")
    if start_date.weekday() != weekday:
        raise ValueError("Start date must fall on the selected weekday.")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("Week count must be a positive whole number.")
    if first_week_nine not in {"front", "back"}:
        raise ValueError("First-week nine must be front or back.")
    other = "back" if first_week_nine == "front" else "front"
    try:
        return [
            (start_date + timedelta(days=7 * offset), first_week_nine if offset % 2 == 0 else other)
            for offset in range(count)
        ]
    except OverflowError as exc:
        raise ValueError("Generated dates exceed the calendar range.") from exc


__all__ = ["generate_weeks"]
