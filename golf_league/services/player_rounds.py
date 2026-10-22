"""Read-only projections of a linked golfer's saved nine-hole rounds."""

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from golf_league.domain.scoring import GolferCard, score_result_match
from golf_league.models import (
    Golfer,
    HoleScore,
    PlayerMatch,
    Season,
    TeamMatch,
    Week,
    WeekHandicap,
)
from golf_league.services.scores import week_holes


@dataclass(frozen=True)
class PlayerHole:
    number: int
    gross: int


@dataclass(frozen=True)
class PlayerRound:
    week_id: int
    week_index: int
    play_date: str
    nine: str
    opponent_name: str
    holes: tuple[PlayerHole, ...]
    gross_total: int
    net_total: int | None
    result: str | None
    points: int | None


def get_player_rounds(session: Session, golfer_id: int, week_id: int | None = None) -> tuple[PlayerRound, ...]:
    """Show only complete saved cards for this golfer, with derived results.

    An absent snapshot or incomplete opponent card leaves the match result
    pending. A saved player card remains visible in either case.
    """
    query = (
        select(PlayerMatch, Week)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .join(Week, Week.id == TeamMatch.week_id)
        .where(or_(PlayerMatch.a_golfer_id == golfer_id,
                   PlayerMatch.b_golfer_id == golfer_id))
        .order_by(Week.play_date.desc(), Week.index.desc(), PlayerMatch.id)
    )
    if week_id is not None:
        query = query.where(Week.id == week_id)

    rounds: list[PlayerRound] = []
    for match, week in session.execute(query):
        holes = week_holes(session, week.id)
        if not holes:
            continue
        hole_number = {hole.id: hole.number for hole in holes}
        strokes: dict[int, dict[int, int]] = {match.a_golfer_id: {}, match.b_golfer_id: {}}
        for row in session.scalars(select(HoleScore).where(HoleScore.player_match_id == match.id)):
            number = hole_number.get(row.hole_id)
            if number is not None and row.golfer_id in strokes:
                strokes[row.golfer_id][number] = row.strokes

        own = strokes[golfer_id]
        if len(own) != len(holes):
            continue
        opponent_id = (match.b_golfer_id if match.a_golfer_id == golfer_id
                       else match.a_golfer_id)
        opponent = session.get(Golfer, opponent_id)
        opponent_name = (f"{opponent.first_name} {opponent.last_name}"
                         if opponent is not None else "Unknown")
        snapshots = {
            row.golfer_id: row.strokes
            for row in session.scalars(select(WeekHandicap).where(
                WeekHandicap.week_id == week.id,
                WeekHandicap.golfer_id.in_((golfer_id, opponent_id)),
            ))
        }
        own_handicap = snapshots.get(golfer_id)
        gross = sum(own.values())
        net = gross - own_handicap if own_handicap is not None else None
        result = None
        points = None
        if len(strokes[opponent_id]) == len(holes):
            season = session.get(Season, week.season_id)
            scored = score_result_match(
                season.scoring_type,
                {hole.number: hole.stroke_index_9 for hole in holes},
                GolferCard(golfer_id, "Player", own_handicap, own),
                GolferCard(opponent_id, opponent_name,
                           snapshots.get(opponent_id), strokes[opponent_id]),
            )
            if scored is not None:
                own_result = next(row for row in scored.rounds if row.golfer_id == golfer_id)
                other_result = next(row for row in scored.rounds if row.golfer_id == opponent_id)
                points = own_result.points
                result = ("Won" if points > other_result.points else
                          "Lost" if points < other_result.points else "Halved")

        rounds.append(PlayerRound(
            week_id=week.id, week_index=week.index,
            play_date=week.play_date.isoformat(), nine=week.nine,
            opponent_name=opponent_name,
            holes=tuple(PlayerHole(hole.number, own[hole.number]) for hole in holes),
            gross_total=gross, net_total=net, result=result, points=points,
        ))
    return tuple(rounds)
