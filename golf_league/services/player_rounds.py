"""Player round history service.

Provides data for displaying a player's past rounds with scores and results.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from golf_league.domain.scoring import GolferCard, score_result_match
from golf_league.models import (
    Golfer,
    HoleScore,
    PlayerMatch,
    TeamMatch,
    Week,
    WeekHandicap,
)
from golf_league.services.scores import week_holes


@dataclass(frozen=True)
class PlayerRound:
    """One round in a player's history."""

    week_id: int
    week_index: int
    play_date: str
    week_type: str
    week_status: str
    match_id: int
    opponent_name: str
    opponent_golfer_id: int
    gross_total: int | None
    net_total: int | None
    result: str | None
    holes: Sequence["PlayerHole"]


@dataclass(frozen=True)
class PlayerHole:
    """One hole in a player's round."""

    hole_number: int
    gross_strokes: int
    net_strokes: int | None
    stroke_index: int


def _golfer_ids_for_player(match, golfer_id: int) -> tuple[int, int]:
    if match.a_golfer_id == golfer_id:
        return match.a_golfer_id, match.b_golfer_id
    return match.b_golfer_id, match.a_golfer_id


def _opponent_name(session: Session, opponent_golfer_id: int) -> str:
    opponent = session.get(Golfer, opponent_golfer_id)
    return opponent.name if opponent else "Unknown"


def _split_strokes(
    match_hole_scores, player_golfer_id: int, opponent_golfer_id: int
) -> tuple[dict[int, int], dict[int, int]]:
    player_strokes: dict[int, int] = {}
    opponent_strokes: dict[int, int] = {}
    for hs in match_hole_scores:
        if hs.golfer_id == player_golfer_id:
            player_strokes[hs.hole_id] = hs.strokes
        elif hs.golfer_id == opponent_golfer_id:
            opponent_strokes[hs.hole_id] = hs.strokes
    return player_strokes, opponent_strokes


def _strokes_by_hole_number(
    strokes_by_id: dict[int, int], week_holes_list
) -> dict[int, int]:
    hole_id_to_number = {h.id: h.number for h in week_holes_list}
    result: dict[int, int] = {}
    for hole_id, strokes in strokes_by_id.items():
        number = hole_id_to_number.get(hole_id)
        if number is not None:
            result[number] = strokes
    return result


def _net_strokes(gross: int, hole, week_handicap: int | None) -> int | None:
    if week_handicap is None:
        return None
    allocated = 1 if hole.stroke_index_9 <= week_handicap else 0
    return gross - allocated


def _build_holes(
    week_holes_list, player_by_number: dict[int, int], week_handicap: int | None
) -> list[PlayerHole]:
    holes: list[PlayerHole] = []
    for hole in week_holes_list:
        gross = player_by_number.get(hole.number, 0)
        holes.append(
            PlayerHole(
                hole_number=hole.number,
                gross_strokes=gross,
                net_strokes=_net_strokes(gross, hole, week_handicap),
                stroke_index=hole.stroke_index_9,
            )
        )
    return holes


def _result_from_points(points: int | None) -> str | None:
    if points is None:
        return None
    elif points >= 2:
        return "win"
    elif points == 1:
        return "tie"
    return "loss"


def _build_player_round(
    session: Session,
    match,
    golfer_id: int,
    hole_scores,
    week,
    week_holes_list,
    week_handicap: int | None,
    stroke_index_by_hole: dict[int, int],
) -> PlayerRound | None:
    player_golfer_id, opponent_golfer_id = _golfer_ids_for_player(match, golfer_id)
    opponent_name = _opponent_name(session, opponent_golfer_id)

    match_hole_scores = [
        hs for hs in hole_scores if hs.player_match_id == match.id
    ]
    player_strokes, opponent_strokes = _split_strokes(
        match_hole_scores, player_golfer_id, opponent_golfer_id
    )
    player_by_number = _strokes_by_hole_number(player_strokes, week_holes_list)
    opponent_by_number = _strokes_by_hole_number(
        opponent_strokes, week_holes_list
    )

    # Only score rounds where the player has complete scores
    if len(player_by_number) != len(week_holes_list):
        return None

    gross_total = sum(player_by_number.values())
    net_total = (
        None if week_handicap is None else gross_total - week_handicap
    )

    player_golfer = session.get(Golfer, player_golfer_id)
    player_card = GolferCard(
        golfer_id=player_golfer_id,
        name=player_golfer.name if player_golfer else "Unknown",
        handicap=week_handicap,
        strokes_by_hole=player_by_number,
    )
    opponent_card = GolferCard(
        golfer_id=opponent_golfer_id,
        name=opponent_name,
        handicap=week_handicap,
        strokes_by_hole=opponent_by_number,
    )

    result_match = score_result_match(
        "match_play",
        stroke_index_by_hole,
        player_card,
        opponent_card,
    )
    if result_match is None:
        return None

    player_round_result = None
    for round_result in result_match.rounds:
        if round_result.golfer_id == player_golfer_id:
            player_round_result = round_result
            break
    if player_round_result is None:
        return None

    holes = _build_holes(week_holes_list, player_by_number, week_handicap)
    result = _result_from_points(player_round_result.points)

    return PlayerRound(
        week_id=week.id,
        week_index=week.index,
        play_date=week.play_date.isoformat(),
        week_type=week.week_type,
        week_status=week.status,
        match_id=match.id,
        opponent_name=opponent_name,
        opponent_golfer_id=opponent_golfer_id,
        gross_total=gross_total,
        net_total=net_total,
        result=result,
        holes=tuple(holes),
    )


def get_player_rounds(session: Session, golfer_id: int) -> Sequence[PlayerRound]:
    """Fetch all past rounds for a golfer, with scores and results.

    Returns rounds in chronological order (by week index).
    """
    # Get all weeks the golfer participated in
    weeks = list(
        session.scalars(
            select(Week)
            .join(TeamMatch, TeamMatch.week_id == Week.id)
            .join(PlayerMatch, PlayerMatch.team_match_id == TeamMatch.id)
            .where(
                or_(
                    PlayerMatch.a_golfer_id == golfer_id,
                    PlayerMatch.b_golfer_id == golfer_id,
                ),
            )
            .order_by(Week.index)
        )
    )

    if not weeks:
        return ()

    # Get all player matches for those weeks
    player_matches = list(
        session.scalars(
            select(PlayerMatch)
            .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
            .where(TeamMatch.week_id.in_([w.id for w in weeks]))
            .order_by(PlayerMatch.id)
        )
    )

    # Get hole scores for those matches
    hole_scores = list(
        session.scalars(
            select(HoleScore)
            .where(
                HoleScore.player_match_id.in_([m.id for m in player_matches])
            )
            .order_by(HoleScore.hole_id, HoleScore.player_match_id)
        )
    )

    # Get handicaps for those weeks
    handicaps = list(
        session.scalars(
            select(WeekHandicap)
            .where(
                WeekHandicap.week_id.in_([w.id for w in weeks]),
                WeekHandicap.golfer_id == golfer_id,
            )
        )
    )

    handicap_by_week = {h.week_id: h.strokes for h in handicaps}

    # Index matches by week
    matches_by_week: dict[int, list] = {}
    for match in player_matches:
        tm = session.get(TeamMatch, match.team_match_id)
        matches_by_week.setdefault(tm.week_id, []).append(match)

    # Build rounds
    rounds: list[PlayerRound] = []
    for week in weeks:
        week_matches = matches_by_week.get(week.id, [])
        week_handicap = handicap_by_week.get(week.id)

        # Get holes for this week's nine
        week_holes_list = week_holes(session, week.id)
        stroke_index_by_hole = {
            h.number: h.stroke_index_9 for h in week_holes_list
        }

        for match in week_matches:
            round_ = _build_player_round(
                session,
                match,
                golfer_id,
                hole_scores,
                week,
                week_holes_list,
                week_handicap,
                stroke_index_by_hole,
            )
            if round_ is not None:
                rounds.append(round_)

    return tuple(rounds)
