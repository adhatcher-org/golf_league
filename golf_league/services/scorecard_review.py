"""Scorecard review: the review form's view model and the reading of a submitted form.

The review is mandatory (R-SCORES): what a model read only pre-fills the form.
Names resolve against the week's own golfers, every value is checked again on
the server, and the checks here are warnings for the admin that never block a
submit. This module imports no `fastapi` (enforced by `tests/test_architecture.py`).
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.scorecard import is_blank, validate_strokes
from golf_league.domain.scorecard_review import (
    NameCandidate,
    card_side,
    match_name,
    out_total_mismatch,
)
from golf_league.models import Golfer, Week, WeekHandicap
from golf_league.services.scorecard_vision import ExtractedCard, ExtractedPlayer
from golf_league.services.scores import card_candidates

ROW_COUNT = 4
ACK_FIELD = "acknowledged"
CARD_ERROR = "card"
_MATCH_NOTES = {"check": "Check this match", "handicap": "Matched by handicap"}
_SIDE_HOLES = {"front": "holes 1 to 9", "back": "holes 10 to 18"}


def name_field(row: int) -> str:
    return f"name_{row}"


def golfer_field(row: int) -> str:
    return f"golfer_{row}"


def score_field(row: int, hole: int) -> str:
    return f"score_{row}_{hole}"


def field_names(hole_numbers: Sequence[int]) -> list[str]:
    """Every field the review form may submit, besides the CSRF token."""
    names = [ACK_FIELD]
    for row in range(ROW_COUNT):
        names += [name_field(row), golfer_field(row)]
        names += [score_field(row, hole) for hole in hole_numbers]
    return names


@dataclass(frozen=True)
class GolferOption:
    """One golfer the admin may choose: a side of a generated match in the week."""

    golfer_id: int
    name: str
    label: str  # "Name — plays Opponent"
    player_match_id: int
    opponent_golfer_id: int
    candidate: NameCandidate


def golfer_options(session: Session, week_id: int) -> list[GolferOption]:
    """The week's golfers, each once, with that week's stored handicap."""
    handicaps = {
        row.golfer_id: row.strokes
        for row in session.scalars(select(WeekHandicap).where(WeekHandicap.week_id == week_id))
    }
    options: dict[int, GolferOption] = {}
    for candidate in card_candidates(session, week_id):
        if candidate.golfer_id in options:
            continue
        golfer = session.get(Golfer, candidate.golfer_id)
        options[candidate.golfer_id] = GolferOption(
            golfer_id=candidate.golfer_id,
            name=candidate.name,
            label=f"{candidate.name} — plays {candidate.opponent_name}",
            player_match_id=candidate.player_match_id,
            opponent_golfer_id=candidate.opponent_golfer_id,
            candidate=NameCandidate(golfer.id, golfer.first_name, golfer.last_name,
                                    handicaps.get(golfer.id)),
        )
    return sorted(options.values(), key=lambda option: (option.name.casefold(), option.golfer_id))


# --- what the reader extracted ----------------------------------------------


def _is_blank_player(player: ExtractedPlayer) -> bool:
    return not player.name.strip() and all(score is None for score in player.scores)


def _read_players(card: ExtractedCard | None) -> list[ExtractedPlayer]:
    return [player for player in card.players if not _is_blank_player(player)] if card else []


def _suggestions(players: Sequence[ExtractedPlayer], options: Sequence[GolferOption]):
    """One name match per shown row; a golfer suggested for two rows is suggested for neither."""
    candidates = [option.candidate for option in options]
    matches = [match_name(player.name, candidates, handicap_written=player.handicap_written)
               for player in players[:ROW_COUNT]]
    counts = Counter(match.golfer_id for match in matches if match.golfer_id is not None)
    return [match if match.golfer_id is not None and counts[match.golfer_id] == 1 else None
            for match in matches]


def initial_values(card: ExtractedCard | None, hole_numbers: Sequence[int],
                   options: Sequence[GolferOption]) -> dict[str, str]:
    """The form as first shown: what was read, with a golfer preselected where the name is clear."""
    values = dict.fromkeys(field_names(hole_numbers), "")
    players = _read_players(card)
    for row, (player, match) in enumerate(zip(players, _suggestions(players, options), strict=False)):
        values[name_field(row)] = player.name
        values[golfer_field(row)] = str(match.golfer_id) if match else ""
        for hole, score in zip(hole_numbers, player.scores, strict=False):
            values[score_field(row, hole)] = "" if score is None else str(score)
    return values


# --- the view ---------------------------------------------------------------


@dataclass(frozen=True)
class CellView:
    hole: int
    field: str
    value: str
    note: str  # "", "Not read" or "Unclear": text, never colour alone
    invalid: bool


@dataclass(frozen=True)
class RowView:
    index: int
    number: int
    name: str
    golfer_id: int | None
    match_note: str
    cells: tuple[CellView, ...]
    out_total: int | None
    handicap_written: int | None
    warnings: tuple[str, ...]
    autofocus: bool
    golfer_invalid: bool


@dataclass(frozen=True)
class ReviewView:
    rows: tuple[RowView, ...]
    options: tuple[GolferOption, ...]
    hole_numbers: tuple[int, ...]
    warnings: tuple[tuple[str, str], ...]  # (anchor id or "", message), listed at the top
    unpaired: tuple[str, ...]
    needs_acknowledgement: bool
    acknowledgement_value: str
    acknowledged: bool


def _selected_id(values: Mapping[str, str], row: int, by_id: Mapping[int, GolferOption]) -> int | None:
    raw = values.get(golfer_field(row), "").strip()
    if raw.isascii() and raw.isdecimal() and int(raw) in by_id:
        return int(raw)
    return None


def unpaired_ids(golfer_ids: Sequence[int], by_id: Mapping[int, GolferOption]) -> list[int]:
    """The chosen golfers whose opponent is not on the card, in the order chosen."""
    chosen = list(dict.fromkeys(golfer_ids))
    return [golfer_id for golfer_id in chosen if by_id[golfer_id].opponent_golfer_id not in chosen]


def acknowledgement_token(golfer_ids: Sequence[int]) -> str:
    """The checkbox value for one set of unpaired golfers.

    The tick is only honoured for the golfers it was shown beside: change who
    is on the card and the admin is asked again, with the new names.
    """
    return "-".join(str(golfer_id) for golfer_id in sorted(set(golfer_ids)))


def _row_warnings(who: str, player: ExtractedPlayer | None, scores: Sequence[int | None],
                  stored_handicap: int | None) -> tuple[list[str], bool]:
    """The Out-total and handicap warnings for one row, and whether the total disagrees."""
    if player is None:
        return [], False
    warnings: list[str] = []
    mismatch = out_total_mismatch(scores, player.out_total, player.handicap_written)
    if mismatch is not None:
        message = (f"{who}: the card's Out total is {mismatch.out_total}, but the nine scores shown "
                   f"add up to {mismatch.shown_sum}. A cell may have been misread.")
        if mismatch.may_be_net:
            message += " The card's total may be the net score."
        warnings.append(message)
    if (player.handicap_written is not None and stored_handicap is not None
            and player.handicap_written != stored_handicap):
        warnings.append(f"{who}: the handicap written on the card is {player.handicap_written}, but the "
                        f"stored handicap for this week is {stored_handicap}.")
    return warnings, mismatch is not None


def build_review(card: ExtractedCard | None, week: Week, hole_numbers: Sequence[int],
                 options: Sequence[GolferOption], values: Mapping[str, str] | None = None,
                 invalid_fields: frozenset[str] = frozenset()) -> ReviewView:
    """The review form: `values` are the admin's own on a re-render, else what was read."""
    if values is None:
        values = initial_values(card, hole_numbers, options)
    by_id = {option.golfer_id: option for option in options}
    players = _read_players(card)
    suggestions = _suggestions(players, options)
    top: list[tuple[str, str]] = []

    side = card_side(card.holes_header) if card else None
    if side is not None and week.nine in _SIDE_HOLES and side != week.nine:
        top.append(("", f"The photo shows the {side} nine ({_SIDE_HOLES[side]}), but week {week.index} "
                        f"is a {week.nine}-nine week ({_SIDE_HOLES[week.nine]})."))
    if len(players) > ROW_COUNT:
        top.append(("", f"The reader found {len(players)} players. Only the first {ROW_COUNT} are shown; "
                        "a card takes two to four."))

    rows: list[RowView] = []
    focused = False
    for row in range(ROW_COUNT):
        player = players[row] if row < len(players) else None
        golfer_id = _selected_id(values, row, by_id)
        name = values.get(name_field(row), "")
        who = by_id[golfer_id].name if golfer_id is not None else (name.strip() or f"Player {row + 1}")
        raw_scores = [values.get(score_field(row, hole), "") for hole in hole_numbers]
        warnings, total_disagrees = _row_warnings(
            who, player, [validate_strokes(raw) for raw in raw_scores],
            by_id[golfer_id].candidate.handicap if golfer_id is not None else None,
        )
        suggestion = suggestions[row] if row < len(suggestions) else None
        match_note = (_MATCH_NOTES.get(suggestion.flag, "")
                      if suggestion and suggestion.golfer_id == golfer_id else "")
        cells = []
        for position, hole in enumerate(hole_numbers):
            note = ""
            if player is not None and position < len(player.scores):
                if player.scores[position] is None:
                    note = "Not read"
                elif player.uncertain[position]:
                    note = "Unclear"
            cells.append(CellView(hole, score_field(row, hole), raw_scores[position], note,
                                  score_field(row, hole) in invalid_fields))
        rows.append(RowView(
            index=row, number=row + 1, name=name, golfer_id=golfer_id, match_note=match_note,
            cells=tuple(cells), out_total=player.out_total if player else None,
            handicap_written=player.handicap_written if player else None,
            # With errors to fix, the page stays at the error summary instead.
            warnings=tuple(warnings), autofocus=total_disagrees and not focused and not invalid_fields,
            golfer_invalid=golfer_field(row) in invalid_fields,
        ))
        focused = focused or total_disagrees
        top.extend((f"player-{row + 1}", message) for message in warnings)

    chosen = [row.golfer_id for row in rows if row.golfer_id is not None]
    unpaired = unpaired_ids(chosen, by_id) if len(set(chosen)) >= 2 else []
    token = acknowledgement_token(unpaired)
    return ReviewView(
        rows=tuple(rows), options=tuple(options), hole_numbers=tuple(hole_numbers), warnings=tuple(top),
        unpaired=tuple(by_id[golfer_id].name for golfer_id in unpaired),
        needs_acknowledgement=bool(unpaired), acknowledgement_value=token,
        acknowledged=bool(unpaired) and values.get(ACK_FIELD, "") == token,
    )


# --- a submitted form -------------------------------------------------------


@dataclass(frozen=True)
class Submission:
    entries: tuple[tuple[int, dict[int, int]], ...]
    errors: dict[str, str]  # field name (or CARD_ERROR) to a plain sentence
    acknowledgement: str  # the checkbox value as posted, "" when not ticked

    @property
    def invalid_fields(self) -> frozenset[str]:
        return frozenset(self.errors)


def _read_scores(label: str, row: int, raw_scores: Mapping[int, str],
                 errors: dict[str, str]) -> dict[int, int]:
    scores: dict[int, int] = {}
    for hole, raw in raw_scores.items():
        strokes = validate_strokes(raw)
        if is_blank(raw):
            errors[score_field(row, hole)] = f"{label}, hole {hole}: enter a score."
        elif strokes is None:
            errors[score_field(row, hole)] = f"{label}, hole {hole}: enter a whole number from 1 to 15."
        else:
            scores[hole] = strokes
    return scores


def parse_submission(values: Mapping[str, str], hole_numbers: Sequence[int],
                     options: Sequence[GolferOption]) -> Submission:
    """Check every submitted value; a row with no name, golfer or score is ignored."""
    by_id = {option.golfer_id: option for option in options}
    errors: dict[str, str] = {}
    entries: list[tuple[int, dict[int, int]]] = []
    chosen: dict[int, int] = {}
    for row in range(ROW_COUNT):
        name = values.get(name_field(row), "").strip()
        golfer_raw = values.get(golfer_field(row), "").strip()
        raw_scores = {hole: values.get(score_field(row, hole), "") for hole in hole_numbers}
        if not name and not golfer_raw and all(is_blank(raw) for raw in raw_scores.values()):
            continue
        label = f"Player {row + 1}"
        golfer_id = _selected_id(values, row, by_id)
        if not golfer_raw:
            errors[golfer_field(row)] = f"{label}: choose the golfer."
        elif golfer_id is None:
            errors[golfer_field(row)] = f"{label}: choose a golfer from this week's matchups."
        elif golfer_id in chosen:
            errors[golfer_field(row)] = (f"{label}: {by_id[golfer_id].name} is already chosen for "
                                         f"player {chosen[golfer_id] + 1}.")
        else:
            chosen[golfer_id] = row
        scores = _read_scores(label, row, raw_scores, errors)
        if golfer_id is not None:
            entries.append((golfer_id, scores))
    if not errors and not 2 <= len(entries) <= ROW_COUNT:
        errors[CARD_ERROR] = "A card needs two to four golfers. Fill in at least two players."
    return Submission(tuple(entries), errors, values.get(ACK_FIELD, "").strip())


def unpaired_on_card(submission: Submission, options: Sequence[GolferOption]) -> list[GolferOption]:
    """The submitted golfers with no opponent on the card (R-SCORES "Grouping is derived")."""
    by_id = {option.golfer_id: option for option in options}
    return [by_id[golfer_id]
            for golfer_id in unpaired_ids([golfer_id for golfer_id, _ in submission.entries], by_id)]
