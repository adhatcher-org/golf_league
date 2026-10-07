"""Read-only projections for a verified player's schedule and weekly matchups."""

from dataclasses import dataclass

from sqlalchemy import select
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
