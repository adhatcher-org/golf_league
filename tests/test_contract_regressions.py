"""GL-61 contract regression assertions that no other test pins down directly.

Covered elsewhere (deliberately not repeated here):
- nullable golfer email: tests/test_roster.py::test_two_golfers_may_both_have_no_email;
  CSV email required: tests/test_admin_imports.py::test_sub_missing_email_csv_reports_email_missing_and_email_malformed
- selected-only strokes: tests/test_roster_import_parse.py (selected_handicap);
  current season seed override: tests/test_participants.py and the snapshot cases in
  tests/test_matchup_generation.py
- pure domain role helper: tests/test_participants.py (role_from_membership) plus the
  domain purity scan in tests/test_architecture.py
- optional (selected, no team) substitute included:
  tests/test_substitutions.py::test_selected_player_without_team_can_sub_and_core_does_not_commit
- full public course sums: tests/test_course_seed.py and tests/milestones/M3

Added here: the schema absences on a migrated database, "a negative handicap is
never an error or a warning" through a real import, and the complete 28-pairing
round robin for eight teams over seven match weeks (MVP Build Plan, M5 done
criterion), with synthetic teams and golfers only.
"""

import io
from datetime import UTC, date, datetime, timedelta
from itertools import combinations

from conftest import _extract_csrf
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from golf_league.database import make_engine
from golf_league.migrations import upgrade_to_head
from golf_league.models import (
    Course,
    Golfer,
    PlayerMatch,
    Season,
    SeasonGolfer,
    Team,
    TeamMatch,
    TeamMember,
    TeeSet,
    Week,
)
from golf_league.services.course_seed import seed_wyandot
from golf_league.services.matchups import create_team_match, generate_player_matches
from golf_league.services.roster_imports import review_rows

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def _clock():
    return NOW


def test_migrated_schema_has_no_role_columns_or_tee_handicap_table(tmp_path):
    url = f"sqlite:///{tmp_path / 'schema.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert not [name for name in tables if "tee_handicap" in name or "handicaps_by_tee" in name]
        golfer_columns = {column["name"]: column for column in inspector.get_columns("golfers")}
        assert "default_role" not in golfer_columns and "role" not in golfer_columns
        assert golfer_columns["email"]["nullable"] is True
        participant_columns = {column["name"] for column in inspector.get_columns("season_participants")}
        assert not {name for name in participant_columns if "role" in name}
        assert not {column["name"] for column in inspector.get_columns("season_golfers") if "role" in column["name"]}
    finally:
        engine.dispose()


def test_negative_and_zero_handicaps_import_without_errors_or_warnings(admin_client):
    page = admin_client.get("/admin/roster/imports/new")
    with Session(admin_client.app.state.engine) as session:
        course_id = session.scalars(select(Course.id)).first()
    csv_bytes = (
        b"FirstName,LastName,Tee,Handicap,Email,PhoneNumber\n"
        b"Plus,Example,White,-3,plus.example@example.test,555-0100\n"
        b"Scratch,Example,Gold,0,scratch.example@example.test,555-0101\n"
    )
    response = admin_client.post(
        "/admin/roster/imports/new",
        data={"course_id": str(course_id), "csrf_token": _extract_csrf(page.text)},
        files=[("files", ("synthetic.csv", io.BytesIO(csv_bytes), "text/csv"))],
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    batch_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[1])
    with Session(admin_client.app.state.engine) as session:
        rows = review_rows(session, batch_id)
    assert [row["handicap_single"] for row in rows] == [-3, 0]
    for row in rows:
        assert row["validation_error"] is None
        assert row["warning_codes"] == []


def _round_robin(teams: list[int]) -> list[list[tuple[int, int]]]:
    """Circle method: n-1 rounds, every pair exactly once."""
    fixed, rotating = teams[0], teams[1:]
    rounds = []
    for _ in range(len(teams) - 1):
        lineup = [fixed, *rotating]
        half = len(lineup) // 2
        rounds.append(list(zip(lineup[:half], reversed(lineup[half:]), strict=True)))
        rotating = rotating[-1:] + rotating[:-1]
    return rounds


def test_eight_teams_seven_match_weeks_form_exactly_28_unique_pairings(tmp_path):
    url = f"sqlite:///{tmp_path / 'fall.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        with Session(engine) as session:
            seed_wyandot(session)
            tee = session.scalar(select(TeeSet).where(TeeSet.name == "Deer"))
            season = Season(year=2026, course_id=tee.course_id, start_date=date(2026, 8, 27),
                            end_date=date(2026, 10, 29), play_weekday=3, status="draft",
                            first_week_nine="front")
            session.add(season)
            session.flush()
            teams = []
            for number in range(1, 9):
                team = Team(season_id=season.id, name=f"Team {number}", number=number, sort_order=number)
                session.add(team)
                session.flush()
                teams.append(team.id)
                for position in range(1, 5):
                    golfer = Golfer(first_name=f"T{number}P{position}", last_name="Synthetic",
                                    default_tee_set_id=tee.id, handicap_strokes=position,
                                    handicap_source="self_reported", handicap_status="ok")
                    session.add(golfer)
                    session.flush()
                    session.add(SeasonGolfer(season_id=season.id, golfer_id=golfer.id))
                    session.add(TeamMember(team_id=team.id, season_id=season.id, golfer_id=golfer.id,
                                           position=position))
            weeks = []
            for index in range(1, 8):
                week = Week(season_id=season.id, index=index,
                            play_date=date(2026, 9, 3) + timedelta(days=7 * (index - 1)),
                            nine="front" if index % 2 else "back", week_type="match", status="scheduled")
                session.add(week)
                session.flush()
                weeks.append(week.id)
            session.commit()

            for week_id, pairings in zip(weeks, _round_robin(teams), strict=True):
                for team_a, team_b in pairings:
                    pairing, _ = create_team_match(session, week_id, team_a, team_b, clock=_clock)
                    assert len(generate_player_matches(session, pairing.id, clock=_clock)) == 4
            session.commit()

            matches = session.scalars(select(TeamMatch)).all()
            pairs = {frozenset((match.home_team_id, match.away_team_id)) for match in matches}
            assert len(matches) == 28
            assert pairs == {frozenset(pair) for pair in combinations(teams, 2)}
            for week_id in weeks:
                in_week = [m for m in matches if m.week_id == week_id]
                playing = [team for m in in_week for team in (m.home_team_id, m.away_team_id)]
                assert sorted(playing) == sorted(teams)
            assert session.scalar(select(func.count(PlayerMatch.id))) == 112
    finally:
        engine.dispose()
