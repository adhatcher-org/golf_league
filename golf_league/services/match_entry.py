"""Typed score entry for fixed golfers: field names and the reading of a posted form.

Shared by the admin week sheet, the admin match page and the player match
form (R-SCORES "Typed entry for a match"). The server fixes the golfers; a
form carries only their scores and the revision it was based on. This module
imports no `fastapi` (enforced by `tests/test_architecture.py`).
"""

from collections.abc import Callable, Mapping, Sequence

from golf_league.domain.scorecard import is_blank, validate_strokes

FieldName = Callable[[int, int], str]


def score_field(golfer_id: int, hole: int) -> str:
    """A score input on a one-match form."""
    return f"score_{golfer_id}_{hole}"


def sheet_score_field(player_match_id: int, golfer_id: int, hole: int) -> str:
    """A score input on the week sheet, where several matches share one form."""
    return f"score_{player_match_id}_{golfer_id}_{hole}"


def revision_field(player_match_id: int) -> str:
    return f"revision_{player_match_id}"


def parse_revision(text: str) -> int | None:
    """The revision a form was based on, or None (treated as out of date); never raises."""
    text = text.strip()
    if not (text.isascii() and text.isdecimal()) or len(text) > 9:
        return None
    return int(text)


def any_typed(values: Mapping[str, str], golfer_ids: Sequence[int], holes: Sequence[int],
              field: FieldName = score_field) -> bool:
    """Whether any score input for these golfers holds a value."""
    return any(not is_blank(values.get(field(golfer_id, hole), ""))
               for golfer_id in golfer_ids for hole in holes)


def parse_scores(values: Mapping[str, str], golfers: Sequence[tuple[int, str]], holes: Sequence[int],
                 field: FieldName = score_field) -> tuple[dict[int, dict[int, str]], dict[str, str]]:
    """Read the posted scores of `golfers` (`(golfer_id, name)` pairs) for `holes`.

    Returns the raw scores by golfer and hole, and field errors keyed by field
    name. The score service validates again inside the write.
    """
    scores: dict[int, dict[int, str]] = {}
    errors: dict[str, str] = {}
    for golfer_id, name in golfers:
        scores[golfer_id] = {}
        for hole in holes:
            raw = values.get(field(golfer_id, hole), "")
            scores[golfer_id][hole] = raw
            if is_blank(raw):
                errors[field(golfer_id, hole)] = f"{name}, hole {hole}: enter a score."
            elif validate_strokes(raw) is None:
                errors[field(golfer_id, hole)] = f"{name}, hole {hole}: enter a whole number from 1 to 15."
    return scores, errors
