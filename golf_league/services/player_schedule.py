"""Read-only projections for a verified player's schedule and weekly matchups."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from golf_league.models import (
    Golfer,
    PlayerMatch,
    Season,
    TeamMatch,
    TeamMember,
    Week,
    WeekHandicap,
)
from golf_league.services.schedule import list_weeks
from golf_league.services.seasons import get_season, list_seasons


@dataclass(frozen=True)
class PlayerSlot:
    label: str
    golfer: Golfer
    is_sub: bool
    handicap: int | None
    vs_own_handicap: bool


@dataclass(frozen=True)
class MatchView:
    pairing: TeamMatch
    slots: tuple[tuple[PlayerSlot, PlayerSlot], ...]


@dataclass(frozen=True)
class WeekView:
    week: Week
    state: str
    source_week: Week | None
    makeup_week: Week | None
    participated: bool
    matches: tuple[MatchView, ...]


@dataclass(frozen=True)
class PlayerSchedule:
    season: Season | None
    seasons: tuple[Season, ...]
    weeks: tuple[WeekView, ...]


def select_player_season(session: Session, season_id: int | None = None) -> Season | None:
    """Choose the requested season, otherwise the active or most recent one."""
    seasons = list_seasons(session)
    if season_id is not None:
        return get_season(session, season_id)
    active = sorted(
        (season for season in seasons if season.status == "active"),
        key=lambda season: (season.start_date, season.id), reverse=True,
    )
    if active:
        return active[0]
    return max(seasons, key=lambda season: (season.start_date, season.id), default=None)


def _match_views(session: Session, weeks: list[Week], *, golfer_id: int | None = None,
                 include_empty_pairs: bool = False):
    ids = [week.id for week in weeks]
    if not ids:
        return {}
    pairs = list(session.scalars(
        select(TeamMatch).where(TeamMatch.week_id.in_(ids)).order_by(
            TeamMatch.sort_order, TeamMatch.id
        )
    ))
    pairs_by_week = {week_id: [] for week_id in ids}
    for pair in pairs:
        pairs_by_week[pair.week_id].append(pair)
    pair_ids = [pair.id for pair in pairs]
    rows = list(session.scalars(
        select(PlayerMatch).where(PlayerMatch.team_match_id.in_(pair_ids)).order_by(
            PlayerMatch.id
        )
    )) if pair_ids else []
    rows_by_pair = {pair_id: [] for pair_id in pair_ids}
    for row in rows:
        rows_by_pair[row.team_match_id].append(row)
    golfer_ids = {gid for row in rows for gid in (row.a_golfer_id, row.b_golfer_id)}
    golfers = {g.id: g for g in session.scalars(select(Golfer).where(Golfer.id.in_(golfer_ids)))} if golfer_ids else {}
    snapshots = list(session.scalars(
        select(WeekHandicap).where(WeekHandicap.week_id.in_(ids))
    ))
    handicap_by_week_golfer = {(item.week_id, item.golfer_id): item.strokes for item in snapshots}
    result = {}
    for week in weeks:
        views = []
        for pair in pairs_by_week[week.id]:
            slots = []
            for row in rows_by_pair[pair.id]:
                if golfer_id is not None and golfer_id not in (row.a_golfer_id, row.b_golfer_id):
                    continue
                a = PlayerSlot(row.position_label, golfers[row.a_golfer_id], row.a_is_sub,
                               handicap_by_week_golfer.get((week.id, row.a_golfer_id)), row.vs_own_handicap)
                b = PlayerSlot(row.position_label, golfers[row.b_golfer_id], row.b_is_sub,
                               handicap_by_week_golfer.get((week.id, row.b_golfer_id)), row.vs_own_handicap)
                slots.append((a, b))
            if slots or include_empty_pairs:
                views.append(MatchView(pair, tuple(slots)))
        result[week.id] = tuple(views)
    return result


def get_player_schedule(session: Session, golfer_id: int, *, season_id: int | None = None) -> PlayerSchedule:
    """Project every week and the golfer's actual generated match slots."""
    seasons = tuple(list_seasons(session))
    season = select_player_season(session, season_id)
    if season is None:
        return PlayerSchedule(None, seasons, ())
    weeks = list_weeks(session, season.id)
    matches = _match_views(session, weeks, golfer_id=golfer_id)
    source_ids = {week.makeup_for_week_id for week in weeks if week.makeup_for_week_id is not None}
    makeup_ids = {week.id for week in weeks if week.makeup_for_week_id is not None}
    related = {week.id: week for week in weeks if week.id in source_ids | makeup_ids}
    is_member = session.scalar(select(TeamMember.id).where(
        TeamMember.season_id == season.id, TeamMember.golfer_id == golfer_id,
    ).limit(1)) is not None
    views = []
    for week in weeks:
        participating = bool(matches.get(week.id))
        if week.status == "cancelled":
            state = "cancelled"
        elif week.week_type == "play_with_team":
            state = "play_with_team"
        elif week.week_type == "rain_date":
            state = "reserved"
        elif week.makeup_for_week_id is not None:
            state = "makeup"
        elif participating:
            state = "match"
        elif is_member and week.week_type == "match" and week.status == "scheduled":
            state = "awaiting_pairing"
        else:
            state = "no_participation"
        makeup = next((candidate for candidate in weeks if candidate.makeup_for_week_id == week.id), None)
        views.append(WeekView(week, state, related.get(week.makeup_for_week_id), makeup,
                              participating, matches.get(week.id, ())))
    return PlayerSchedule(season, seasons, tuple(views))


def get_week_detail(session: Session, week_id: int):
    """Return one week, its season list and complete generated matchup detail."""
    week = session.get(Week, week_id)
    if week is None:
        return None
    season = get_season(session, week.season_id)
    weeks = list_weeks(session, week.season_id)
    all_matches = _match_views(session, weeks, include_empty_pairs=True)
    source = next((item for item in weeks if item.id == week.makeup_for_week_id), None)
    makeup = next((item for item in weeks if item.makeup_for_week_id == week.id), None)
    if week.status == "cancelled":
        state = "cancelled"
    elif week.week_type == "play_with_team":
        state = "play_with_team"
    elif week.week_type == "rain_date":
        state = "reserved"
    elif week.makeup_for_week_id is not None:
        state = "makeup"
    else:
        state = "match"
    return season, tuple(list_seasons(session)), WeekView(
        week, state, source, makeup, bool(all_matches.get(week.id)), all_matches.get(week.id, ()),
    )


def get_home_view(session: Session, today: date) -> tuple[Season | None, WeekView | None]:
    """Project the selected season's next scheduled week without reading the clock."""
    season = select_player_season(session)
    if season is None:
        return None, None
    has_matches = select(TeamMatch.id).where(TeamMatch.week_id == Week.id).exists()
    week = session.scalar(
        select(Week).where(
            Week.season_id == season.id,
            Week.play_date >= today,
            Week.status == "scheduled",
            or_(Week.week_type != "rain_date", has_matches),
        ).order_by(Week.play_date, Week.index).limit(1)
    )
    if week is None:
        return season, None
    return season, get_week_detail(session, week.id)[2]


def get_opponent_contact(
    session: Session,
    viewer_golfer_id: int | None,
    week_id: int,
    opponent_golfer_id: int,
) -> tuple[Week, Golfer] | None:
    """Return contact details only for a scheduled direct opponent."""
    if viewer_golfer_id is None or viewer_golfer_id == opponent_golfer_id:
        return None

    week = session.scalar(
        select(Week)
        .join(TeamMatch, TeamMatch.week_id == Week.id)
        .join(PlayerMatch, PlayerMatch.team_match_id == TeamMatch.id)
        .where(
            Week.id == week_id,
            Week.status == "scheduled",
            or_(
                (PlayerMatch.a_golfer_id == viewer_golfer_id)
                & (PlayerMatch.b_golfer_id == opponent_golfer_id),
                (PlayerMatch.b_golfer_id == viewer_golfer_id)
                & (PlayerMatch.a_golfer_id == opponent_golfer_id),
            ),
        )
        .limit(1)
    )
    if week is None:
        return None

    golfer = session.get(Golfer, opponent_golfer_id)
    return (week, golfer) if golfer is not None else None
