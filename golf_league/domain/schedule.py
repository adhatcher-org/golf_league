"""Pure calendar and nine generation for a season schedule."""

from collections.abc import Sequence
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


def shift_from(weeks: Sequence[tuple[int, date, str]], index: int,
               new_date: date) -> list[tuple[int, date, date]]:
    """Shift the selected and later rows without changing their printed indexes."""
    if not isinstance(new_date, date) or isinstance(new_date, datetime):
        raise ValueError("Choose a valid calendar date.")
    ordered = sorted(weeks)
    selected = next((row for row in ordered if row[0] == index), None)
    if selected is None:
        raise ValueError("Selected week does not exist.")
    if any(not isinstance(row[1], date) or isinstance(row[1], datetime) for row in ordered):
        raise ValueError("Choose a valid calendar date.")
    delta = new_date - selected[1]
    if delta.days % 7:
        raise ValueError("Shift dates by a whole number of weeks to keep the weekday.")
    affected = [row for row in ordered if row[0] >= index]
    if any(row[2] == "played" for row in affected):
        raise ValueError("Played weeks cannot be shifted.")
    try:
        return [(number, old, old + delta) for number, old, _ in affected]
    except OverflowError as exc:
        raise ValueError("Shifted dates exceed the calendar range.") from exc


def rotate_nines(nines_by_index: Sequence[tuple[int, str | None]], from_index: int,
                 new_nine: str) -> list[tuple[int, str | None]]:
    """Alternate scheduled rows, preserving null rain dates without advancing."""
    ordered = sorted(nines_by_index)
    if new_nine not in {"front", "back"} or any(nine not in {None, "front", "back"} for _, nine in ordered):
        raise ValueError("Choose front or back nine.")
    selected = next((row for row in ordered if row[0] == from_index), None)
    if selected is None or selected[1] is None:
        raise ValueError("Selected week must have a nine.")
    next_nine = new_nine
    result = []
    for index, nine in ordered:
        if index < from_index:
            continue
        result.append((index, next_nine if nine is not None else None))
        if nine is not None:
            next_nine = "back" if next_nine == "front" else "front"
    return result


__all__ = ["generate_weeks", "shift_from", "rotate_nines"]
