"""Verified read-only week detail includes matchup snapshots and safe navigation."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_my_schedule import _seed

from golf_league.models import PlayerMatch, Season, TeamMatch, User, Week


def test_week_detail_shows_all_matchups_substitutions_and_snapshot_handicaps(client):
    ids = _seed(client)
    page = client.get(f"/weeks/{ids['weeks'][0]}")
    assert page.status_code == 200
    assert "A1 Example" in page.text and "B1 Example" in page.text
    assert "Home handicap" in page.text and "-1" in page.text
    assert "My Schedule" in page.text and f"season_id={ids['season']}" in page.text
    assert "Edit week" not in page.text
    assert "viewer@example.test" not in page.text


def test_week_detail_auth_unknown_ids_and_verified_users_can_view_any_season(client):
    ids = _seed(client)
    assert client.get("/weeks/99999").status_code == 404
    assert client.get(f"/weeks/{ids['weeks'][0]}").status_code == 200
    client.cookies.clear()
    assert client.get(f"/weeks/{ids['weeks'][0]}").status_code == 401


def test_week_detail_admin_link_and_self_match_explanation(client):
    ids = _seed(client, self_match=True)
    with Session(client.app.state.engine) as session:
        golfer = session.scalar(select(Season).where(Season.id == ids["season"]))
        session.add(Season(
            year=2027, course_id=golfer.course_id, start_date=date(2027, 8, 26),
            end_date=date(2027, 10, 28), play_weekday=3, status="draft", first_week_nine="front",
        ))
        session.commit()
    player_page = client.get(f"/weeks/{ids['weeks'][0]}?season_id=999")
    assert player_page.status_code == 200
    assert "against each golfer&#39;s own handicap" in player_page.text or "against each golfer's own handicap" in player_page.text
    assert "P1vP2" in player_page.text and "Against own handicap" in player_page.text
    assert "Edit week" not in player_page.text

    with Session(client.app.state.engine) as session:
        viewer = session.scalar(select(User))
        viewer.is_admin = True
        session.commit()
    admin_page = client.get(f"/weeks/{ids['weeks'][0]}")
    assert "Edit week" in admin_page.text
    assert client.get(f"/admin/seasons/{ids['season']}/weeks").status_code == 200


def test_week_detail_escapes_names_and_never_shows_roster_contacts(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        week = session.get(Week, ids["weeks"][0])
        match = session.scalar(select(TeamMatch).where(TeamMatch.week_id == week.id))
        match.home_team.name = "<script>alert(1)</script>"
        session.commit()
    page = client.get(f"/weeks/{ids['weeks'][0]}")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<script>alert(1)</script>" not in page.text
    for private in ("@example.test", "phone", "email", "notes"):
        assert private not in page.text.lower()


def test_week_detail_keeps_team_pairing_visible_before_player_slots_exist(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        for row in session.scalars(select(PlayerMatch)):
            session.delete(row)
        session.commit()
    page = client.get(f"/weeks/{ids['weeks'][0]}")
    assert "A vs B" in page.text
    assert "Player match slots have not been generated" in page.text
