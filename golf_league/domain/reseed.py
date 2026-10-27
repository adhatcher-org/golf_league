"""Pure re-seed plan logic: categorises golfer handicap candidates."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReseedCandidate:
    """Input data for one golfer's re-seed evaluation."""

    golfer_id: int
    golfer_name: str
    current_strokes: int | None  # None means no WeekHandicap row exists
    effective_handicap: int | None  # None means no effective seed/tee
    manually_adjusted: bool


@dataclass(frozen=True)
class ReseedChange:
    """A WeekHandicap row that should be overwritten by re-seed."""

    golfer_id: int
    golfer_name: str
    current_strokes: int
    new_strokes: int


@dataclass(frozen=True)
class ReseedPlan:
    """Categorised re-seed candidates."""

    changes: tuple[ReseedChange, ...]
    unchanged: tuple[int, ...]  # golfer_ids whose strokes already match
    blocked_no_seed: tuple[int, ...]  # golfer_ids with no effective seed/tee
    manually_adjusted: tuple[int, ...]  # golfer_ids with a manually_adjusted match slot
    excluded_no_row: tuple[int, ...]  # golfer_ids with no WeekHandicap row


def reseed_plan(candidates: list[ReseedCandidate]) -> ReseedPlan:
    """Categorise re-seed candidates into changes, unchanged, blocked, and excluded.

    Pure function: no I/O, no database access. Categorisation rules:
    - `current_strokes is None` → excluded (no WeekHandicap row to overwrite)
    - `manually_adjusted is True` → manually_adjusted (never touched)
    - `effective_handicap is None` → blocked_no_seed (no effective seed/tee)
    - `current_strokes == effective_handicap` → unchanged (already correct)
    - otherwise → changes (strokes differ, needs overwrite)
    """
    changes: list[ReseedChange] = []
    unchanged: list[int] = []
    blocked_no_seed: list[int] = []
    manually_adjusted_list: list[int] = []
    excluded_no_row: list[int] = []

    for c in candidates:
        if c.current_strokes is None:
            excluded_no_row.append(c.golfer_id)
        elif c.manually_adjusted:
            manually_adjusted_list.append(c.golfer_id)
        elif c.effective_handicap is None:
            blocked_no_seed.append(c.golfer_id)
        elif c.current_strokes != c.effective_handicap:
            changes.append(ReseedChange(
                golfer_id=c.golfer_id,
                golfer_name=c.golfer_name,
                current_strokes=c.current_strokes,
                new_strokes=c.effective_handicap,
            ))
        else:
            unchanged.append(c.golfer_id)

    return ReseedPlan(
        changes=tuple(changes),
        unchanged=tuple(unchanged),
        blocked_no_seed=tuple(blocked_no_seed),
        manually_adjusted=tuple(manually_adjusted_list),
        excluded_no_row=tuple(excluded_no_row),
    )
