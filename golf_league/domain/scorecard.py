"""Pure scorecard rules: stroke validation and grouping a card by match."""

from collections.abc import Iterable
from dataclasses import dataclass

MAX_STROKES_PER_HOLE = 15
MIN_STROKES_PER_HOLE = 1


def is_blank(value: object) -> bool:
    """A blank cell is a missing score: None or an all-whitespace string."""
    return value is None or (isinstance(value, str) and not value.strip())


def validate_strokes(value: object) -> int | None:
    """Return strokes 1..15, or None for a blank or rejected value. Never raises.

    Only ASCII digits are accepted: ``str.isdecimal()`` alone is true for
    non-ASCII digits, which ``int()`` would silently convert.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdecimal()):
            return None
        number = int(text)
    else:
        return None
    if MIN_STROKES_PER_HOLE <= number <= MAX_STROKES_PER_HOLE:
        return number
    return None


@dataclass(frozen=True)
class MatchGrouping:
    """Card golfers grouped by player match, and whether any match is half-covered."""

    groups: dict[int, tuple[int, ...]]
    incomplete: bool


def group_by_match(entries: Iterable[tuple[int, int, int]]) -> MatchGrouping:
    """Group ``(golfer_id, player_match_id, opponent_golfer_id)`` entries by match.

    A match is complete when both of its sides are on the card. A match with
    one side only, or golfers from different matches paired by the card, makes
    the grouping incomplete.
    """
    rows = list(entries)
    on_card = {golfer_id for golfer_id, _, _ in rows}
    groups: dict[int, list[int]] = {}
    incomplete = False
    for golfer_id, match_id, opponent_id in rows:
        groups.setdefault(match_id, []).append(golfer_id)
        if opponent_id not in on_card:
            incomplete = True
    return MatchGrouping({key: tuple(sorted(value)) for key, value in groups.items()}, incomplete)
