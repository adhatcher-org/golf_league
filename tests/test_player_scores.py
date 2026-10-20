"""GL-85: a player's typed score entry, the week's scores page and the links to them.

Synthetic data only: the generated golfers `A1 Example` .. `D4 Example`, and
accounts at `example.test`. Week 1 (27 August) has A against B: A1 v B1 up to
A4 v B4.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_admin_scorecards import (
    FIVES,
    NOW,
    InlineExecutor,
    _engine,
    _jpeg,
    _score_count,
    _token,
)
from test_matchup_generation import clock, create_generated, seed_fixture
from test_scores import _matches

from golf_league.domain.scorecard import Actor
from golf_league.models import (
    Golfer,
    HoleScore,
    MatchScoreState,
    ScorecardDraft,
    Season,
    User,
    Week,
)
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.matchups import substitute_player
from golf_league.services.scorecard_drafts import create_draft
from golf_league.services.scores import lock_match, submit_card, unlock_match

SIXES = (6,) * 9


def _user(client, email, golfer_id=None, *, admin=False, verified=True) -> int:
    with Session(_engine(client)) as session:
        user = User(username=email, email=email, display_name=email.split("@")[0].title(),
                    password_hash=hash_password("synthetic-password"), is_admin=admin, golfer_id=golfer_id,
                    email_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None)
        session.add(user)
        session.commit()
        return user.id


def _as(client, user_id) -> None:
    with Session(_engine(client)) as session:
        user = session.get(User, user_id)
        cookie = create_session_cookie(user.id, user.session_version, client.app.state.settings.session_secret)
    client.cookies.clear()
    client.cookies.set("session", cookie)


@pytest.fixture
def league(client):
    """Week 1 generated (A against B), a clock on its play date, and the regular player A1 logged in."""
    with Session(_engine(client)) as session:
        ids = seed_fixture(session)
        session.get(Season, ids["season"]).status = "active"
        create_generated(session, ids, week_index=0)
        session.commit()
        matches = _matches(session, ids["weeks"][0])
    state = client.app.state
    state.scorecard_utc_now = lambda: NOW
    state.scorecard_executor = InlineExecutor()
    golfers = ids["golfers"]
    users = {
        "a1": _user(client, "a1@example.test", golfers[0]),
        "b1": _user(client, "b1@example.test", golfers[4]),
        "a2": _user(client, "a2@example.test", golfers[1]),
        "c1": _user(client, "c1@example.test", golfers[8]),  # plays no match in week 1
        "unlinked": _user(client, "unlinked@example.test"),
    }
    ids = {**ids, "week": ids["weeks"][0], "matches": [match.id for match in matches],
           "sides": {match.id: (match.a_golfer_id, match.b_golfer_id) for match in matches}, "users": users}
    _as(client, users["a1"])
    return client, ids


def _week_path(ids, suffix="", week=None):
    return f"/weeks/{week or ids['week']}/scores{suffix}"


def _match_path(ids, index, week=None):
    return f"/weeks/{week or ids['week']}/matches/{ids['matches'][index]}/scores"


def _data(client, ids, index, scores, revision, *, holes=range(1, 10), golfers=None) -> dict:
    data = {"csrf_token": _token(client), "revision": str(revision)}
    for golfer_id, values in zip(golfers or ids["sides"][ids["matches"][index]], scores, strict=True):
        for hole, score in zip(holes, values, strict=True):
            data[f"score_{golfer_id}_{hole}"] = str(score)
    return data


def _save(client, ids, index, scores=(FIVES, FIVES), revision=0, **options):
    return client.post(_match_path(ids, index), data=_data(client, ids, index, scores, revision, **options),
                       follow_redirects=False)


def _state(client, match_id) -> MatchScoreState | None:
    with Session(_engine(client)) as session:
        return session.get(MatchScoreState, match_id)


def _strokes(client, match_id) -> dict[int, int]:
    with Session(_engine(client)) as session:
        return dict(session.execute(
            select(HoleScore.golfer_id, func.sum(HoleScore.strokes))
            .where(HoleScore.player_match_id == match_id).group_by(HoleScore.golfer_id)).all())


def _block(html: str, match_id: int) -> str:
    return html.split(f'id="match-{match_id}"')[1].split("</section>")[0]


def _admin(ids) -> Actor:
    return Actor(ids["users"]["admin"], True)


def _with_admin(client, ids) -> dict:
    return {**ids, "users": {**ids["users"], "admin": _user(client, "boss@example.test", admin=True)}}


# --- who may use it ---------------------------------------------------------------------


def _routes(ids):
    return [("GET", _week_path(ids)), ("GET", _week_path(ids, "/enter")),
            ("GET", _match_path(ids, 0)), ("POST", _match_path(ids, 0))]


def test_anonymous_is_401_and_unverified_is_403_on_every_route(league):
    client, ids = league
    token = _token(client)
    client.cookies.clear()
    for method, path in _routes(ids):
        response = client.request(method, path, data={"csrf_token": token} if method == "POST" else None,
                                  follow_redirects=False)
        assert response.status_code == 401, f"{method} {path}"
    _as(client, _user(client, "late@example.test", ids["golfers"][3], verified=False))
    for method, path in _routes(ids):
        response = client.request(method, path, data={"csrf_token": _token(client)} if method == "POST" else None,
                                  follow_redirects=False)
        assert response.status_code == 403, f"{method} {path}"
    assert _score_count(client) == 0


def test_a_post_without_a_token_is_403_and_every_page_is_never_cached(league):
    client, ids = league
    data = _data(client, ids, 0, (FIVES, FIVES), 0)
    for token in (None, "wrong"):
        posted = {**data, "csrf_token": token} if token else {k: v for k, v in data.items() if k != "csrf_token"}
        assert client.post(_match_path(ids, 0), data=posted, follow_redirects=False).status_code == 403
    assert _score_count(client) == 0
    for _, path in _routes(ids)[:3]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store", path
        assert response.headers["referrer-policy"] == "no-referrer", path
    saved = _save(client, ids, 0)
    assert saved.status_code == 303 and saved.headers["cache-control"] == "no-store"
    missing = client.get(_match_path(ids, 1))
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"


# --- entering and changing --------------------------------------------------------------


def test_a_first_entry_writes_18_rows_and_the_opponent_can_change_one(league):
    client, ids = league
    match_id = ids["matches"][0]
    page = client.get(_week_path(ids, "/enter"))
    assert page.status_code == 200 and "<h2 id=\"own-match-title\">A1 Example and B1 Example</h2>" in page.text
    assert page.text.count("score-input") == 18 and 'name="revision" value="0"' in page.text
    response = _save(client, ids, 0)
    assert response.status_code == 303
    assert response.headers["location"] == f"/weeks/{ids['week']}/scores?saved=1#match-{match_id}"
    assert _score_count(client) == 18 and _state(client, match_id).revision == 1
    week = client.get(response.headers["location"]).text
    assert "Your scores were saved." in week

    _as(client, ids["users"]["b1"])
    form = client.get(_match_path(ids, 0)).text
    assert 'name="revision" value="1"' in form and form.count('value="5"') == 18
    changed = (FIVES, (4,) + FIVES[1:])
    assert _save(client, ids, 0, changed, revision=1).status_code == 303
    assert _strokes(client, match_id) == {ids["golfers"][0]: 45, ids["golfers"][4]: 44}
    with Session(_engine(client)) as session:
        entered_by = set(session.scalars(select(HoleScore.entered_by_user_id)))
    assert entered_by == {ids["users"]["a1"]}  # the change keeps who first entered the score
    assert _state(client, match_id).revision == 2


def test_an_unchanged_save_keeps_the_revision(league):
    client, ids = league
    assert _save(client, ids, 0).status_code == 303
    assert _save(client, ids, 0, revision=1).status_code == 303
    assert _state(client, ids["matches"][0]).revision == 1


def test_two_players_from_the_same_revision_the_second_gets_409_and_the_saved_values(league):
    client, ids = league
    assert _save(client, ids, 0, (FIVES, FIVES), revision=0).status_code == 303
    _as(client, ids["users"]["b1"])
    response = _save(client, ids, 0, (SIXES, SIXES), revision=0)
    assert response.status_code == 409
    assert "These scores were changed after you opened this page. Nothing was saved." in response.text
    assert response.text.count('value="5"') == 18 and 'value="6"' not in response.text
    assert 'name="revision" value="1"' in response.text
    assert set(_strokes(client, ids["matches"][0]).values()) == {45}


def test_a_form_with_no_readable_revision_or_stale_mistakes_is_out_of_date(league):
    client, ids = league
    for revision in ("", "x", "-1"):
        response = _save(client, ids, 0, revision=revision)
        assert response.status_code == 409 and "Nothing was saved" in response.text, revision
    assert _save(client, ids, 0, revision=0).status_code == 303
    stale = _save(client, ids, 0, (SIXES, ("0",) * 9), revision=0)
    assert stale.status_code == 409 and 'value="0"' not in stale.text and 'name="revision" value="1"' in stale.text
    assert set(_strokes(client, ids["matches"][0]).values()) == {45}


@pytest.mark.parametrize("bad,message", [
    ("", "A1 Example, hole 4: enter a score."),
    ("0", "A1 Example, hole 4: enter a whole number from 1 to 15."),
    ("16", "A1 Example, hole 4: enter a whole number from 1 to 15."),
    ("4.5", "A1 Example, hole 4: enter a whole number from 1 to 15."),
])
def test_a_bad_value_is_422_keeps_what_was_typed_and_saves_nothing(league, bad, message):
    client, ids = league
    scores = (FIVES[:3] + (bad,) + FIVES[4:], SIXES)
    response = _save(client, ids, 0, scores)
    assert response.status_code == 422 and message in response.text
    assert response.text.count('value="6"') == 9 and 'name="revision" value="0"' in response.text
    assert _score_count(client) == 0


def test_a_locked_match_is_read_only_and_409_until_it_is_unlocked(league):
    client, ids = league
    ids = _with_admin(client, ids)
    assert _save(client, ids, 0).status_code == 303
    with Session(_engine(client)) as session:
        lock_match(session, ids["week"], ids["matches"][0], _admin(ids), base_revision=1, now=NOW)
        session.commit()
    for path in (_match_path(ids, 0), _week_path(ids, "/enter")):
        page = client.get(path).text
        assert "These scores are locked by the league admin. Ask the admin if something is wrong." in page
        assert "score-input" not in page and "Save scores" not in page
    week = client.get(_week_path(ids)).text
    assert "Update scores" not in _block(week, ids["matches"][0])
    response = _save(client, ids, 0, (SIXES, SIXES), revision=2)
    assert response.status_code == 409
    assert "These scores were locked by the league admin. Nothing was saved." in response.text
    assert set(_strokes(client, ids["matches"][0]).values()) == {45}
    with Session(_engine(client)) as session:
        unlock_match(session, ids["week"], ids["matches"][0], _admin(ids), now=NOW)
        session.commit()
    assert _save(client, ids, 0, (SIXES, SIXES), revision=3).status_code == 303
    assert set(_strokes(client, ids["matches"][0]).values()) == {54}


def test_a_match_locked_while_the_form_was_open_is_409(league):
    client, ids = league
    ids = _with_admin(client, ids)
    with Session(_engine(client)) as session:
        lock_match(session, ids["week"], ids["matches"][0], _admin(ids), base_revision=0, now=NOW)
        session.commit()
    response = _save(client, ids, 0, (FIVES, ("x",) * 9), revision=0)
    assert response.status_code == 409 and "Nothing was saved." in response.text
    assert _score_count(client) == 0


def test_a_match_without_rights_is_404_and_a_field_outside_the_match_is_ignored(league):
    client, ids = league
    assert client.get(_match_path(ids, 1)).status_code == 404
    assert _save(client, ids, 1).status_code == 404
    assert client.get(f"/weeks/{ids['weeks'][1]}/matches/{ids['matches'][0]}/scores").status_code == 404
    assert client.get(f"/weeks/999999/matches/{ids['matches'][0]}/scores").status_code == 404
    assert client.get(f"/weeks/{ids['week']}/matches/999999/scores").status_code == 404
    data = _data(client, ids, 0, (FIVES, FIVES), 0)
    outsider = ids["sides"][ids["matches"][1]][0]
    data.update({f"score_{outsider}_{hole}": "7" for hole in range(1, 10)})
    assert client.post(_match_path(ids, 0), data=data, follow_redirects=False).status_code == 303
    assert _score_count(client) == 18 and _strokes(client, ids["matches"][1]) == {}


def test_a_conflict_keeps_what_was_typed(league, monkeypatch):
    import golf_league.routers.player_scores as module
    from golf_league.services.scores import ScoreEntryError

    client, ids = league

    def busy(*args, **kwargs):
        raise ScoreEntryError("conflict", "busy")

    monkeypatch.setattr(module, "save_match_scores", busy)
    response = _save(client, ids, 0, (SIXES, SIXES))
    assert response.status_code == 409 and "Nothing was saved; save again." in response.text
    assert response.text.count('value="6"') == 18 and _score_count(client) == 0


def test_a_database_failure_is_503_and_keeps_what_was_typed(league, monkeypatch, caplog):
    import golf_league.routers.player_scores as module

    client, ids = league

    def broken(*args, **kwargs):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(module, "save_match_scores", broken)
    with caplog.at_level(logging.INFO):
        response = _save(client, ids, 0, (SIXES, SIXES))
    assert response.status_code == 503 and "could not be saved just now" in response.text
    assert response.text.count('value="6"') == 18 and _score_count(client) == 0
    assert "player match save failed" in caplog.text and "Example" not in caplog.text


# --- when a week takes scores --------------------------------------------------------------


def test_before_the_play_date_entry_says_when_it_opens_and_a_post_is_422(league):
    client, ids = league
    client.app.state.scorecard_utc_now = lambda: NOW - timedelta(days=1)
    page = client.get(_week_path(ids, "/enter"))
    assert page.status_code == 200 and "Scores for week 1 can be entered from Aug 27, 2026." in page.text
    assert "score-input" not in page.text
    match = client.get(_match_path(ids, 0)).text
    assert "can be entered from Aug 27, 2026." in match and "score-input" not in match
    response = _save(client, ids, 0)
    assert response.status_code == 422 and "can be entered from Aug 27, 2026." in response.text
    assert _score_count(client) == 0
    assert "Enter scores" not in client.get(_week_path(ids)).text
    for later in (timedelta(0), timedelta(days=21)):
        client.app.state.scorecard_utc_now = lambda later=later: NOW + later
        assert "score-input" in client.get(_week_path(ids, "/enter")).text
    assert _save(client, ids, 0).status_code == 303


@pytest.mark.parametrize("change", ["cancelled", "played", "not_a_match_week"])
def test_a_closed_week_takes_no_scores(league, change):
    client, ids = league
    with Session(_engine(client)) as session:
        week = session.get(Week, ids["week"])
        if change == "not_a_match_week":
            week.week_type = "play_with_team"
        else:
            week.status = change
        session.commit()
    page = client.get(_week_path(ids, "/enter"))
    assert page.status_code == 200 and "Scores cannot be entered for this week." in page.text
    assert "score-input" not in page.text
    assert "Scores cannot be entered for this week." in client.get(_match_path(ids, 0)).text
    response = _save(client, ids, 0)
    assert response.status_code == 422 and "Scores cannot be entered for this week." in response.text
    assert _score_count(client) == 0


# --- personas --------------------------------------------------------------------------------


def test_a_verified_account_with_no_golfer_can_view_but_not_enter(league):
    client, ids = league
    _as(client, ids["users"]["unlinked"])
    week = client.get(_week_path(ids))
    assert week.status_code == 200 and week.text.count('id="match-') == 4
    assert "Enter scores" not in week.text and "Update scores" not in week.text
    assert client.get(_week_path(ids, "/enter")).status_code == 404
    assert client.get(_match_path(ids, 0)).status_code == 404
    assert _save(client, ids, 0).status_code == 404
    assert "/scores/enter" not in client.get("/my/schedule").text


def test_a_golfer_not_playing_that_week_gets_404(league):
    client, ids = league
    _as(client, ids["users"]["c1"])
    assert client.get(_week_path(ids, "/enter")).status_code == 404
    assert client.get(_match_path(ids, 0)).status_code == 404
    assert _save(client, ids, 0).status_code == 404
    assert client.get(_week_path(ids)).status_code == 200
    assert client.get("/weeks/999999/scores").status_code == 404
    assert client.get("/weeks/999999/scores/enter").status_code == 404


def test_a_substitute_with_an_account_enters_and_the_replaced_player_gets_404(league):
    client, ids = league
    with Session(_engine(client)) as session:
        sub = Golfer(first_name="Sam", last_name="Substitute", handicap_strokes=0,
                     handicap_source="self_reported", handicap_status="ok")
        session.add(sub)
        session.commit()
        sub_id = sub.id
        substitute_player(session, ids["season"], ids["matches"][0], side="a", golfer_id=sub_id, clock=clock)
    ids["sides"][ids["matches"][0]] = (sub_id, ids["golfers"][4])
    assert client.get(_week_path(ids, "/enter")).status_code == 404  # A1 was replaced
    assert _save(client, ids, 0).status_code == 404
    _as(client, _user(client, "sam@example.test", sub_id))
    page = client.get(_week_path(ids, "/enter")).text
    assert "Sam Substitute (sub)" in page and page.count("score-input") == 18
    assert _save(client, ids, 0).status_code == 303
    assert _strokes(client, ids["matches"][0]) == {sub_id: 45, ids["golfers"][4]: 45}


def test_a_self_match_reads_as_two_golfers_and_never_says_opponent(league):
    client, ids = league
    with Session(_engine(client)) as session:
        create_generated(session, ids, self_match=True, week_index=1)
        session.commit()
        match = _matches(session, ids["weeks"][1])[0]
        match_id, sides = match.id, (match.a_golfer_id, match.b_golfer_id)
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(days=7)
    page = client.get(_week_path(ids, "/enter", week=ids["weeks"][1])).text
    main = page.split('<main')[1].split("</main>")[0]
    assert "A1 Example and A2 Example" in main and "opponent" not in main.lower()
    data = {"csrf_token": _token(client), "revision": "0",
            **{f"score_{golfer}_{hole}": "5" for golfer in sides for hole in range(10, 19)}}
    response = client.post(f"/weeks/{ids['weeks'][1]}/matches/{match_id}/scores", data=data,
                           follow_redirects=False)
    assert response.status_code == 303 and _strokes(client, match_id) == dict.fromkeys(sides, 45)


def test_an_admin_who_plays_is_treated_as_a_player_here(league):
    client, ids = league
    admin_player = _user(client, "captain@example.test", ids["golfers"][2], admin=True)
    _as(client, admin_player)
    assert _save(client, ids, 0).status_code == 404  # not their match, admin or not
    assert _save(client, ids, 2).status_code == 303
    with Session(_engine(client)) as session:
        assert set(session.scalars(select(HoleScore.entered_by_user_id))) == {admin_player}
    client.app.state.scorecard_utc_now = lambda: NOW - timedelta(days=1)
    assert _save(client, ids, 2, revision=1).status_code == 422  # a player waits for the play date


def test_a_scorekeeper_can_change_the_other_match_they_kept_and_a_third_player_cannot(league):
    client, ids = league
    a1, a2 = ids["users"]["a1"], ids["golfers"][1]
    b2 = ids["golfers"][5]
    with Session(_engine(client)) as session:
        entries = [(golfer, dict.fromkeys(range(1, 10), 5)) for golfer in (ids["golfers"][0], ids["golfers"][4], a2, b2)]
        submit_card(session, ids["week"], entries, Actor(a1, False, ids["golfers"][0]), NOW.date(),
                    acknowledged=False, now=NOW)
        session.commit()
    page = client.get(_week_path(ids, "/enter")).text
    assert f'href="{_match_path(ids, 1)}">Update scores for A2 Example and B2 Example' in page
    week = client.get(_week_path(ids)).text
    assert "Update scores" in _block(week, ids["matches"][1]) and "Update scores" in _block(week, ids["matches"][0])
    assert "Enter scores" not in _block(week, ids["matches"][2])
    assert _save(client, ids, 1, (SIXES, SIXES), revision=1).status_code == 303
    assert set(_strokes(client, ids["matches"][1]).values()) == {54}
    _as(client, ids["users"]["c1"])
    assert _save(client, ids, 1, (FIVES, FIVES), revision=2).status_code == 404


def test_a_typed_save_leaves_the_players_scorecard_draft_and_photo_in_place(league):
    client, ids = league
    state = client.app.state
    with Session(_engine(client)) as session:
        draft = create_draft(session, week_id=ids["week"], user_id=ids["users"]["a1"], raw=_jpeg(),
                             today=NOW.date(), clock=lambda: NOW, directory=state.scorecard_dir,
                             actor=Actor(ids["users"]["a1"], False, ids["golfers"][0]))
        draft_id, photo = draft.id, draft.image_path
    assert _save(client, ids, 0).status_code == 303
    with Session(_engine(client)) as session:
        assert session.get(ScorecardDraft, draft_id) is not None
    assert (state.scorecard_dir / photo).is_file()


# --- the week page and the links ----------------------------------------------------------


def test_the_week_page_shows_every_match_to_any_verified_user_with_no_name_of_who_entered(league):
    client, ids = league
    assert _save(client, ids, 0).status_code == 303
    _as(client, ids["users"]["c1"])
    page = client.get(_week_path(ids)).text
    assert re.findall(r'id="match-(\d+)"', page) == [str(match_id) for match_id in ids["matches"]]
    # Both golfers of the first match shot 45; scores render in the week's combined table.
    assert page.count("<td>45</td>") == 2
    # A not-entered match renders every hole blank.
    assert '<td class="score-hole">—</td>' in page
    assert "A1@" not in page and "A1 " not in page.replace("A1 Example", "")
    assert "by A1" not in page and ">A1<" not in page  # the entering account's display name


def test_link_text_follows_the_players_own_state(league):
    client, ids = league
    ids = _with_admin(client, ids)
    enter = f'href="/weeks/{ids["week"]}/scores/enter">'
    client.app.state.scorecard_utc_now = lambda: NOW - timedelta(days=1)
    for path in ("/my/schedule", f"/weeks/{ids['week']}"):
        assert "/scores/enter" not in client.get(path).text, path  # not open yet
    client.app.state.scorecard_utc_now = lambda: NOW
    schedule, week = client.get("/my/schedule").text, client.get(f"/weeks/{ids['week']}").text
    assert f"{enter}Enter scores" in schedule and f"{enter}Enter scores" in week
    assert f'href="/weeks/{ids["week"]}/scores">Scores</a>' in week
    assert "/scores" not in client.get(f"/weeks/{ids['weeks'][1]}").text  # no generated matches
    assert _save(client, ids, 0).status_code == 303
    schedule, week = client.get("/my/schedule").text, client.get(f"/weeks/{ids['week']}").text
    assert f"{enter}Update scores" in schedule and f"{enter}Update scores" in week
    with Session(_engine(client)) as session:
        lock_match(session, ids["week"], ids["matches"][0], _admin(ids), base_revision=1, now=NOW)
        session.commit()
    view = f'href="/weeks/{ids["week"]}/scores">View scores'
    assert view in client.get("/my/schedule").text and view in client.get(f"/weeks/{ids['week']}").text
    with Session(_engine(client)) as session:
        session.get(Week, ids["week"]).status = "cancelled"
        session.commit()
    assert "/scores/enter" not in client.get("/my/schedule").text
    assert "View scores" not in client.get("/my/schedule").text


def test_the_home_prompt_appears_only_for_an_open_week_and_goes_once_saved(league):
    client, ids = league
    prompt = f'<a class="tap-link" href="/weeks/{ids["week"]}/scores/enter">Week 1: Enter scores</a>'
    client.app.state.scorecard_utc_now = lambda: NOW - timedelta(days=1)
    assert prompt not in client.get("/").text
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(days=3)
    assert prompt in client.get("/").text
    _as(client, ids["users"]["unlinked"])
    assert "Enter scores" not in client.get("/").text
    _as(client, ids["users"]["a1"])
    assert _save(client, ids, 0).status_code == 303
    assert "Enter scores" not in client.get("/").text


def test_names_render_escaped_and_no_log_carries_a_name_or_score(league, caplog):
    client, ids = league
    with Session(_engine(client)) as session:
        session.get(Golfer, ids["golfers"][0]).first_name = "<i>Markup</i>"
        session.commit()
    with caplog.at_level(logging.DEBUG):
        for path in (_week_path(ids), _week_path(ids, "/enter"), _match_path(ids, 0)):
            page = client.get(path).text
            assert "<i>Markup</i>" not in page and "&lt;i&gt;Markup&lt;/i&gt;" in page, path
        assert _save(client, ids, 0, ((7,) * 9, (8,) * 9)).status_code == 303
    assert "player match saved" in caplog.text
    for record in caplog.records:
        message = record.getMessage()
        assert "Markup" not in message and "Example" not in message and "63" not in message
