"""GL-84: the admin week sheet, the match page, lock, unlock, "Lock all" and clear.

Synthetic data only: the generated golfers `A1 Example` .. `D4 Example`.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_admin_scorecards import FIVES, NOW, _become, _engine, _score_count, _token
from test_admin_scorecards import cards as _cards
from test_matchup_generation import create_generated
from test_scores import _matches

from golf_league.domain.scorecard import Actor
from golf_league.models import Golfer, HoleScore, MatchScoreState, User, Week
from golf_league.services.scores import (
    ScoreEntryError,
    clear_match_scores,
    save_match_scores,
    week_holes,
)

cards = _cards
SIXES = (6,) * 9


@pytest.fixture
def sheet(cards):
    """The logged-in admin, week 1's four matches (A against B) and their golfers."""
    client, ids = cards
    with Session(_engine(client)) as session:
        matches = _matches(session, ids["weeks"][0])
        ids = {**ids, "week": ids["weeks"][0], "matches": [m.id for m in matches],
               "sides": {m.id: (m.a_golfer_id, m.b_golfer_id) for m in matches}}
    return client, ids


def _path(ids, suffix="", week=None):
    return f"/admin/weeks/{week or ids['week']}/scores{suffix}"


def _match_path(ids, index, suffix="/scores", week=None):
    return f"/admin/weeks/{week or ids['week']}/matches/{ids['matches'][index]}{suffix}"


def _sheet_data(client, ids, filled, *, revisions=None, holes=range(1, 10)) -> dict:
    """Week sheet form data: `filled` maps a match index to `{golfer id: nine scores}`."""
    data = {"csrf_token": _token(client)}
    for index, golfers in filled.items():
        match_id = ids["matches"][index]
        data[f"revision_{match_id}"] = str((revisions or {}).get(index, 0))
        for golfer_id, scores in golfers.items():
            for hole, score in zip(holes, scores, strict=True):
                data[f"score_{match_id}_{golfer_id}_{hole}"] = str(score)
    return data


def _both(ids, index, scores=FIVES) -> dict:
    return dict.fromkeys(ids["sides"][ids["matches"][index]], scores)


def _save_sheet(client, ids, filled, **options):
    return client.post(_path(ids), data=_sheet_data(client, ids, filled, **options), follow_redirects=False)


def _match_data(client, ids, index, scores, revision, holes=range(1, 10)) -> dict:
    data = {"csrf_token": _token(client), "revision": str(revision)}
    for golfer_id, values in zip(ids["sides"][ids["matches"][index]], scores, strict=True):
        for hole, score in zip(holes, values, strict=True):
            data[f"score_{golfer_id}_{hole}"] = str(score)
    return data


def _save_match(client, ids, index, scores, revision):
    return client.post(_match_path(ids, index), data=_match_data(client, ids, index, scores, revision),
                       follow_redirects=False)


def _post(client, path, **fields):
    return client.post(path, data={"csrf_token": _token(client), **fields}, follow_redirects=False)


def _state(client, match_id) -> MatchScoreState | None:
    with Session(_engine(client)) as session:
        return session.get(MatchScoreState, match_id)


def _strokes(client, match_id) -> dict[int, int]:
    with Session(_engine(client)) as session:
        return dict(session.execute(
            select(HoleScore.golfer_id, func.sum(HoleScore.strokes))
            .where(HoleScore.player_match_id == match_id).group_by(HoleScore.golfer_id)).all())


def _block(html: str, match_id: int) -> str:
    """The part of a sheet that belongs to one match."""
    return html.split(f'id="match-{match_id}"')[1].split('class="score-match"')[0]


# --- who may use it -----------------------------------------------------------------


def _routes(ids):
    return [
        ("GET", _path(ids)), ("POST", _path(ids)), ("POST", _path(ids, "/lock-all")),
        ("GET", _match_path(ids, 0)), ("POST", _match_path(ids, 0)),
        ("POST", _match_path(ids, 0, "/lock")), ("POST", _match_path(ids, 0, "/unlock")),
        ("GET", _match_path(ids, 0, "/scores/clear")), ("POST", _match_path(ids, 0, "/scores/clear")),
    ]


@pytest.mark.parametrize("role,status", [("anonymous", 401), ("player", 403)])
def test_every_route_refuses_anyone_but_a_verified_admin(sheet, role, status):
    client, ids = sheet
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303
    token = _token(client)
    if role == "anonymous":
        client.cookies.clear()
    else:
        _become(client, admin=False)
    for method, path in _routes(ids):
        response = client.request(method, path, data={"csrf_token": token} if method == "POST" else None,
                                  follow_redirects=False)
        assert response.status_code == status, f"{method} {path}"
    assert _score_count(client) == 18 and _state(client, ids["matches"][0]).locked_at is None


def test_every_post_needs_a_csrf_token_and_every_page_is_never_cached(sheet):
    client, ids = sheet
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303
    for method, path in _routes(ids):
        if method == "POST":
            assert client.post(path, data={}, follow_redirects=False).status_code == 403, path
            assert client.post(path, data={"csrf_token": "wrong"}, follow_redirects=False).status_code == 403, path
        else:
            response = client.get(path)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store", path
    assert _score_count(client) == 18 and _state(client, ids["matches"][0]).locked_at is None


# --- the week sheet -----------------------------------------------------------------


def test_the_sheet_lists_every_match_in_order_saved_or_not(sheet):
    client, ids = sheet
    assert _save_sheet(client, ids, {1: _both(ids, 1)}).status_code == 303
    page = client.get(_path(ids)).text
    assert "1 of 4 matches entered, 0 locked." in page and "A vs B" in page
    assert re.findall(r'id="match-(\d+)"', page) == [str(match_id) for match_id in ids["matches"]]
    assert re.findall(r"<h3>Match (\d): ", page) == ["1", "2", "3", "4"]
    saved, empty = _block(page, ids["matches"][1]), _block(page, ids["matches"][0])
    # The saved match is read-only, with its total and who last changed it; the empty one has 18 inputs.
    assert "Entered. Not locked. Last changed by Admin on Aug 27, 2026." in saved
    assert "score-input" not in saved and saved.count("<td>45</td>") == 2 and ">Lock<" in saved
    assert "Not entered. Not locked." in empty and empty.count("score-input") == 18
    assert ">Lock<" not in empty and "Open match" in empty
    assert page.count('form="sheet-form"') == 3 * 18 + 1  # every input, and the one Save button
    assert page.count(">Save scores<") == 1 and ">Lock all<" in page
    # The sheet is reachable from the season's week list.
    weeks = client.get(f"/admin/seasons/{ids['season']}/weeks").text
    assert f'href="/admin/weeks/{ids["week"]}/scores">Scores</a>' in weeks


def test_one_save_writes_every_match_filled_in_and_skips_blank_ones(sheet):
    client, ids = sheet
    response = _save_sheet(client, ids, {0: _both(ids, 0), 2: _both(ids, 2, SIXES)})
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/weeks/{ids['week']}/scores?saved=2"
    assert response.headers["cache-control"] == "no-store"
    assert _score_count(client) == 36
    assert set(_strokes(client, ids["matches"][0]).values()) == {45}
    assert set(_strokes(client, ids["matches"][2]).values()) == {54}
    assert _strokes(client, ids["matches"][1]) == {}
    state = _state(client, ids["matches"][2])
    assert (state.revision, state.scores_changed_at) == (1, NOW)
    page = client.get(response.headers["location"]).text
    assert "The scores for 2 matches were saved." in page and "2 of 4 matches entered" in page


def test_nothing_typed_is_422(sheet):
    client, ids = sheet
    response = _save_sheet(client, ids, {})
    assert response.status_code == 422 and "Enter the scores for at least one match." in response.text
    assert _score_count(client) == 0


@pytest.mark.parametrize("bad,message", [
    ("", "A1 Example, hole 4: enter a score."),
    ("0", "A1 Example, hole 4: enter a whole number from 1 to 15."),
    ("16", "A1 Example, hole 4: enter a whole number from 1 to 15."),
    ("4.5", "A1 Example, hole 4: enter a whole number from 1 to 15."),
])
def test_a_half_filled_match_is_422_keeps_what_was_typed_and_saves_nothing(sheet, bad, message):
    client, ids = sheet
    first, second = ids["matches"][0], ids["matches"][1]
    a1, b1 = ids["sides"][first]
    scores = list(FIVES)
    scores[3] = bad
    response = _save_sheet(client, ids, {0: {a1: scores, b1: SIXES}, 1: _both(ids, 1, (7,) * 9)})
    assert response.status_code == 422 and "There is a problem" in response.text and message in response.text
    assert _score_count(client) == 0  # the complete second match is not saved either
    field = f"score_{first}_{a1}_4"
    assert f'href="#{field}"' in response.text
    assert re.search(rf'name="{field}" value="{re.escape(bad)}"[^>]*aria-invalid="true"', response.text)
    assert f'name="score_{first}_{b1}_9" value="6"' in response.text
    assert f'name="score_{second}_{ids["sides"][second][0]}_1" value="7"' in response.text


def test_a_match_with_only_one_golfer_typed_is_422(sheet):
    client, ids = sheet
    a1, _ = ids["sides"][ids["matches"][0]]
    response = _save_sheet(client, ids, {0: {a1: FIVES}})
    assert response.status_code == 422 and "B1 Example, hole 1: enter a score." in response.text
    assert _score_count(client) == 0


def test_a_match_saved_by_someone_else_meanwhile_is_409_and_the_rest_is_kept(sheet):
    client, ids = sheet
    first, second = ids["matches"][0], ids["matches"][1]
    data = _sheet_data(client, ids, {0: _both(ids, 0, SIXES), 1: _both(ids, 1, (7,) * 9)})
    # Before the admin saves, the players of match 1 enter their own scores.
    with Session(_engine(client)) as session:
        other = User(username="p@example.test", email="p@example.test", display_name="P", password_hash="x")
        session.add(other)
        session.flush()
        a1, b1 = ids["sides"][first]
        numbers = [hole.number for hole in week_holes(session, ids["week"])]
        save_match_scores(session, ids["week"], first, {a1: dict.fromkeys(numbers, 4), b1: dict.fromkeys(numbers, 4)},
                          Actor(other.id, False, a1), NOW.date(), base_revision=0, now=NOW)
        session.commit()
    response = client.post(_path(ids), data=data, follow_redirects=False)
    assert response.status_code == 409 and _score_count(client) == 18  # nothing of the admin's was saved
    assert set(_strokes(client, first).values()) == {36} and _strokes(client, second) == {}
    assert "Match 1: A1 Example and B1 Example was changed after you opened this page." in response.text
    assert "Nothing was saved." in response.text
    # Match 1 is shown as it is saved now; what was typed for match 2 is still in the form.
    assert "score-input" not in _block(response.text, first)
    assert f'name="score_{second}_{ids["sides"][second][0]}_1" value="7"' in response.text


def test_a_tampered_or_missing_revision_is_treated_as_out_of_date(sheet):
    client, ids = sheet
    for revision in ("1", "abc", "-1", "", "9999999999"):
        data = _sheet_data(client, ids, {0: _both(ids, 0)})
        data[f"revision_{ids['matches'][0]}"] = revision
        assert client.post(_path(ids), data=data, follow_redirects=False).status_code == 409, revision
    assert _score_count(client) == 0


def test_a_half_entered_match_takes_only_the_missing_golfer(sheet):
    client, ids = sheet
    first = ids["matches"][0]
    a1, b1 = ids["sides"][first]
    a3 = ids["sides"][ids["matches"][2]][0]
    # A card with A1 and a golfer of another match leaves match 1 half entered.
    from test_admin_scorecards import _manual, _submit
    draft = _manual(client, ids["week"])
    assert _submit(client, draft, [("", a1, SIXES), ("", a3, SIXES)], acknowledged=[a1, a3]).status_code == 303
    page = client.get(_path(ids)).text
    block = _block(page, first)
    assert "One of two players entered. Not locked." in block
    assert block.count("score-input") == 9 and f'name="score_{first}_{a1}_1"' not in block
    assert f'name="revision_{first}" value="1"' in page

    response = _save_sheet(client, ids, {0: {b1: FIVES}}, revisions={0: 1})
    assert response.status_code == 303
    assert _strokes(client, first) == {a1: 54, b1: 45}  # the saved golfer is untouched
    assert _state(client, first).revision == 2


def test_fields_for_another_matchs_golfers_are_ignored(sheet):
    client, ids = sheet
    first, second = ids["matches"][0], ids["matches"][1]
    stranger = ids["sides"][second][0]
    data = _sheet_data(client, ids, {0: _both(ids, 0)})
    data.update({f"score_{first}_{stranger}_{hole}": "9" for hole in range(1, 10)})
    assert client.post(_path(ids), data=data, follow_redirects=False).status_code == 303
    assert set(_strokes(client, first)) == set(ids["sides"][first]) and _score_count(client) == 18


@pytest.mark.parametrize("status", ["cancelled", "played"])
def test_a_week_that_cannot_take_scores_shows_no_inputs_and_refuses_a_save(sheet, status):
    client, ids = sheet
    with Session(_engine(client)) as session:
        session.get(Week, ids["week"]).status = status
        session.commit()
    page = client.get(_path(ids)).text
    assert f"This week is {status}." in page and "score-input" not in page and "Save scores" not in page
    response = _save_sheet(client, ids, {0: _both(ids, 0)})
    assert response.status_code == 422 and _score_count(client) == 0


def test_a_database_failure_is_503_and_keeps_what_was_typed(sheet, monkeypatch, caplog):
    client, ids = sheet
    import golf_league.routers.admin_week_scores as router_module

    def fail(*args, **kwargs):
        raise OperationalError("UPDATE", {"strokes": "must-not-be-logged"}, Exception("database is locked"))

    monkeypatch.setattr(router_module, "save_week", fail)
    with caplog.at_level(logging.WARNING):
        response = _save_sheet(client, ids, {0: _both(ids, 0, SIXES)})
    assert response.status_code == 503 and "could not be saved just now" in response.text
    a1 = ids["sides"][ids["matches"][0]][0]
    assert f'name="score_{ids["matches"][0]}_{a1}_1" value="6"' in response.text
    assert "week sheet save failed" in caplog.text and "must-not-be-logged" not in caplog.text
    assert _score_count(client) == 0


def test_an_unknown_week_is_404(sheet):
    client, ids = sheet
    assert client.get(_path(ids, week=999999)).status_code == 404
    assert _post(client, _path(ids, week=999999)).status_code == 404
    assert _post(client, _path(ids, "/lock-all", week=999999)).status_code == 404


# --- one match ----------------------------------------------------------------------


def test_the_match_page_enters_and_changes_a_match(sheet):
    client, ids = sheet
    first = ids["matches"][0]
    a1, b1 = ids["sides"][first]
    page = client.get(_match_path(ids, 0))
    assert page.status_code == 200 and "Match 1: A1 Example and B1 Example" in page.text
    assert page.text.count("score-input") == 18 and 'name="revision" value="0"' in page.text
    assert "Clear scores" not in page.text and "Lock this match" in page.text

    response = _save_match(client, ids, 0, (FIVES, SIXES), 0)
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/weeks/{ids['week']}/scores?saved=1#match-{first}"
    assert _strokes(client, first) == {a1: 45, b1: 54}

    edit = client.get(_match_path(ids, 0)).text
    assert f'name="score_{a1}_1" value="5"' in edit and f'name="score_{b1}_9" value="6"' in edit
    assert 'name="revision" value="1"' in edit and "Clear scores" in edit
    changed = (4,) + FIVES[1:]
    assert _save_match(client, ids, 0, (changed, SIXES), 1).status_code == 303
    assert _strokes(client, first) == {a1: 44, b1: 54}
    with Session(_engine(client)) as session:
        assert session.scalar(select(func.count(HoleScore.id))) == 18  # updated in place, not re-entered
    assert _state(client, first).revision == 2


def test_an_old_revision_is_409_and_shows_the_saved_scores(sheet):
    client, ids = sheet
    first = ids["matches"][0]
    a1, _ = ids["sides"][first]
    assert _save_match(client, ids, 0, (FIVES, FIVES), 0).status_code == 303
    response = _save_match(client, ids, 0, ((9,) * 9, (9,) * 9), 0)  # based on the empty page
    assert response.status_code == 409
    assert "These scores were changed after you opened this page. Nothing was saved." in response.text
    # The form holds the saved values, not the ones just typed, and the current revision.
    assert f'name="score_{a1}_1" value="5"' in response.text and 'value="9"' not in response.text
    assert 'name="revision" value="1"' in response.text
    assert set(_strokes(client, first).values()) == {45}


def test_bad_values_on_the_match_page_are_422_and_kept(sheet):
    client, ids = sheet
    a1, b1 = ids["sides"][ids["matches"][0]]
    scores = list(FIVES)
    scores[8] = "x"
    response = _save_match(client, ids, 0, (scores, SIXES), 0)
    assert response.status_code == 422 and "A1 Example, hole 9: enter a whole number from 1 to 15." in response.text
    assert f'name="score_{a1}_9" value="x"' in response.text and f'name="score_{b1}_1" value="6"' in response.text
    assert _score_count(client) == 0


def test_a_match_from_another_week_is_404_on_every_match_route(sheet):
    client, ids = sheet
    with Session(_engine(client)) as session:
        create_generated(session, ids, week_index=2)
        session.commit()
        other_week = ids["weeks"][2]
    for suffix, method in (("/scores", "GET"), ("/scores", "POST"), ("/lock", "POST"), ("/unlock", "POST"),
                           ("/scores/clear", "GET"), ("/scores/clear", "POST")):
        # Week 1's match addressed under week 3, and a match that does not exist.
        for path in (_match_path(ids, 0, suffix, week=other_week),
                     f"/admin/weeks/{ids['week']}/matches/999999{suffix}"):
            response = (client.get(path) if method == "GET"
                        else _post(client, path, revision="0"))
            assert response.status_code == 404, f"{method} {path}"
    assert _score_count(client) == 0 and _state(client, ids["matches"][0]) is None


# --- lock, unlock, lock all ----------------------------------------------------------


def test_lock_and_unlock_from_the_sheet_and_a_locked_match_takes_no_change(sheet):
    client, ids = sheet
    first = ids["matches"][0]
    assert _save_match(client, ids, 0, (FIVES, FIVES), 0).status_code == 303
    # A lock based on an old page is refused, and the match page shows the scores to check.
    stale = _post(client, _match_path(ids, 0, "/lock"), revision="0")
    assert stale.status_code == 409 and "Check them and lock again." in stale.text
    assert _state(client, first).locked_at is None

    locked = _post(client, _match_path(ids, 0, "/lock"), revision="1")
    assert locked.status_code == 303
    assert locked.headers["location"] == f"/admin/weeks/{ids['week']}/scores?locked=1#match-{first}"
    state = _state(client, first)
    assert state.locked_at == NOW and state.revision == 2
    page = client.get(locked.headers["location"]).text
    assert "1 match was locked." in page and "1 of 4 matches entered, 1 locked." in page
    assert "Entered. Locked on Aug 27, 2026 by Admin." in _block(page, first)
    assert ">Unlock<" in _block(page, first) and ">Lock<" not in _block(page, first)

    # Locked: the match page has no inputs, and a save, a clear or a card is refused.
    match_page = client.get(_match_path(ids, 0)).text
    assert "This match is locked." in match_page and "score-input" not in match_page
    assert "Unlock this match" in match_page and "Clear scores" not in match_page
    refused = _save_match(client, ids, 0, ((9,) * 9, (9,) * 9), 2)
    assert refused.status_code == 409 and "This match was locked. Nothing was saved." in refused.text
    assert "score-input" not in refused.text
    assert client.get(_match_path(ids, 0, "/scores/clear")).status_code == 409
    assert _post(client, _match_path(ids, 0, "/scores/clear"), revision="2").status_code == 409
    assert set(_strokes(client, first).values()) == {45}

    unlocked = _post(client, _match_path(ids, 0, "/unlock"))
    assert unlocked.status_code == 303 and "unlocked=1" in unlocked.headers["location"]
    assert "The match was unlocked." in client.get(unlocked.headers["location"]).text
    assert _state(client, first).locked_at is None
    assert _save_match(client, ids, 0, (SIXES, SIXES), 3).status_code == 303


def test_an_empty_match_can_be_locked_from_its_own_page(sheet):
    client, ids = sheet
    first = ids["matches"][0]
    assert _post(client, _match_path(ids, 0, "/lock"), revision="0").status_code == 303
    assert _state(client, first).locked_at == NOW
    page = client.get(_path(ids)).text
    assert "Not entered. Locked on Aug 27, 2026 by Admin." in _block(page, first)
    assert "score-input" not in _block(page, first)  # a locked match takes no entry
    # Scores typed on a page opened before the lock are refused, not dropped.
    refused = _save_sheet(client, ids, {0: _both(ids, 0), 1: _both(ids, 1)})
    assert refused.status_code == 409 and _score_count(client) == 0
    assert "Match 1: A1 Example and B1 Example was locked after you opened this page." in refused.text


def test_lock_all_locks_what_was_checked_and_names_what_it_left(sheet):
    client, ids = sheet
    first, second, third, fourth = ids["matches"]
    a3 = ids["sides"][third][0]
    assert _save_sheet(client, ids, {0: _both(ids, 0), 1: _both(ids, 1)}).status_code == 303
    # Match 3 is half entered through a card with a golfer of match 4.
    from test_admin_scorecards import _manual, _submit
    a4 = ids["sides"][fourth][0]
    assert _submit(client, _manual(client, ids["week"]), [("", a3, FIVES), ("", a4, FIVES)],
                   acknowledged=[a3, a4]).status_code == 303

    page = client.get(_path(ids)).text
    data = {"csrf_token": _token(client),
            **dict(re.findall(r'name="(revision_\d+)" value="(\d+)"', page))}
    assert {f"revision_{match_id}" for match_id in ids["matches"]} <= set(data)
    # After the admin opened the page, someone changes match 2.
    assert _save_match(client, ids, 1, (SIXES, SIXES), 1).status_code == 303

    response = client.post(_path(ids, "/lock-all"), data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/admin/weeks/{ids['week']}/scores?locked=1&incomplete=2&changed={second}")
    assert _state(client, first).locked_at == NOW
    assert _state(client, second).locked_at is None and _state(client, third).locked_at is None
    result = client.get(response.headers["location"]).text
    assert "1 match was locked." in result
    assert "2 matches are not fully entered and were left open." in result
    assert "Match 2: A2 Example and B2 Example was changed after you opened the page and was not locked." in result
    # Checked again and locked.
    again = client.get(_path(ids)).text
    data = {"csrf_token": _token(client),
            **dict(re.findall(r'name="(revision_\d+)" value="(\d+)"', again))}
    assert client.post(_path(ids, "/lock-all"), data=data, follow_redirects=False).headers["location"].endswith(
        "?locked=1&incomplete=2")
    assert _state(client, second).locked_at == NOW


def test_lock_all_with_nothing_posted_locks_nothing(sheet):
    client, ids = sheet
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303
    response = _post(client, _path(ids, "/lock-all"))
    assert response.status_code == 303 and "locked=0" in response.headers["location"]
    assert _state(client, ids["matches"][0]).locked_at is None
    # A match ID in the address that is not in this week is never named.
    page = client.get(_path(ids) + "?changed=999999-abc").text
    assert "was changed after you opened the page" not in page


# --- clearing -----------------------------------------------------------------------


def test_clear_needs_the_confirmation_and_deletes_only_that_match(sheet):
    client, ids = sheet
    first, second = ids["matches"][0], ids["matches"][1]
    assert client.get(_match_path(ids, 0, "/scores/clear")).status_code == 404  # nothing to clear
    assert _save_sheet(client, ids, {0: _both(ids, 0), 1: _both(ids, 1, SIXES)}).status_code == 303

    confirm = client.get(_match_path(ids, 0, "/scores/clear"))
    assert confirm.status_code == 200 and "Clear these scores?" in confirm.text
    assert "Match 1: A1 Example and B1 Example" in confirm.text and confirm.text.count("<td>45</td>") == 2
    assert _score_count(client) == 36  # the page deleted nothing

    stale = _post(client, _match_path(ids, 0, "/scores/clear"), revision="0")
    assert stale.status_code == 409 and "Nothing was cleared." in stale.text and _score_count(client) == 36

    response = _post(client, _match_path(ids, 0, "/scores/clear"), revision="1")
    assert response.status_code == 303 and "cleared=1" in response.headers["location"]
    assert _strokes(client, first) == {} and set(_strokes(client, second).values()) == {54}
    page = client.get(response.headers["location"]).text
    assert "The match&#39;s scores were cleared. It can be entered again." in page
    assert "Not entered. Not locked. Last changed by Admin" in _block(page, first)
    # It can be entered again, from the revision the clear left.
    assert _save_sheet(client, ids, {0: _both(ids, 0, SIXES)}, revisions={0: 2}).status_code == 303
    assert set(_strokes(client, first).values()) == {54}


# --- a form with mistakes never comes back on a newer revision ----------------------


def _save_form_revision(html: str) -> str:
    """The revision the match page's save form carries (the lock form has its own)."""
    return re.search(r'<form class="scorecard-form"[^>]*>.*?name="revision" value="(\d*)"', html, re.S).group(1)


def test_mistakes_on_an_out_of_date_match_form_are_409_and_cannot_overwrite_the_newer_scores(sheet):
    client, ids = sheet
    match_id = ids["matches"][0]
    a1 = ids["sides"][match_id][0]
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303            # revision 1: fives
    assert _save_match(client, ids, 0, (SIXES, SIXES), 1).status_code == 303          # someone else: sixes

    # The first admin's page still shows fives on revision 1; they mistype one cell.
    old_form = _match_data(client, ids, 0, (FIVES, FIVES), 1)
    old_form[f"score_{a1}_1"] = "99"
    response = client.post(_match_path(ids, 0), data=old_form, follow_redirects=False)
    assert response.status_code == 409 and "were changed after you opened this page" in response.text
    assert f'name="score_{a1}_1" value="6"' in response.text and 'value="99"' not in response.text
    # Correcting the cell and posting the old form again is still refused.
    old_form[f"score_{a1}_1"] = "5"
    assert client.post(_match_path(ids, 0), data=old_form, follow_redirects=False).status_code == 409
    assert set(_strokes(client, match_id).values()) == {54}


def test_typed_values_go_back_with_the_revision_they_were_typed_on(sheet, monkeypatch):
    client, ids = sheet
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303            # revision 1
    # Mistakes on the current revision: 422, and the form keeps that revision.
    form = _match_data(client, ids, 0, (SIXES, SIXES), 1)
    form[f"score_{ids['sides'][ids['matches'][0]][0]}_1"] = "0"
    response = client.post(_match_path(ids, 0), data=form, follow_redirects=False)
    assert response.status_code == 422 and _save_form_revision(response.text) == "1"
    # A failed save hands the typed values back on the revision they were typed on, not the current one.
    _refuse(monkeypatch, "save_match_scores", OperationalError("UPDATE", {}, Exception("database is locked")))
    response = _save_match(client, ids, 0, (SIXES, SIXES), 0)
    assert response.status_code == 503 and _save_form_revision(response.text) == "0"
    # That page shows sixes that are not saved, so it offers no lock and no clear of the saved fives.
    assert "The scores shown above are not saved." in response.text
    assert "Lock this match" not in response.text and "Clear scores" not in response.text
    assert 'name="revision" value="1"' not in response.text
    # Opened again, the match shows what is saved and can be locked.
    page = client.get(_match_path(ids, 0)).text
    assert "Lock this match" in page and "Clear scores" in page and "are not saved" not in page
    _refuse(monkeypatch, "save_match_scores", ScoreEntryError("conflict", ""))
    response = _save_match(client, ids, 0, (SIXES, SIXES), 0)
    assert response.status_code == 409 and _save_form_revision(response.text) == "0"
    assert set(_strokes(client, ids["matches"][0]).values()) == {45}


@pytest.mark.parametrize("revision", [None, "", "abc", "-1", "9999999999"])
def test_a_match_form_without_a_readable_revision_goes_back_with_the_saved_values(sheet, monkeypatch, revision):
    client, ids = sheet
    match_id = ids["matches"][0]
    a1 = ids["sides"][match_id][0]
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303            # revision 1: fives
    # Even when the write would also have met a conflict, nothing typed comes back to be saved again.
    _refuse(monkeypatch, "save_match_scores", ScoreEntryError("conflict", ""))
    form = _match_data(client, ids, 0, (SIXES, SIXES), 1)
    if revision is None:
        del form["revision"]
    else:
        form["revision"] = revision
    response = client.post(_match_path(ids, 0), data=form, follow_redirects=False)
    assert response.status_code == 409 and "were changed after you opened this page" in response.text
    assert f'name="score_{a1}_1" value="5"' in response.text and 'value="6"' not in response.text
    assert _save_form_revision(response.text) == "1"  # the saved values, on their own revision
    assert set(_strokes(client, match_id).values()) == {45}


def test_mistakes_on_an_out_of_date_sheet_are_409_before_the_mistakes(sheet):
    client, ids = sheet
    match_id = ids["matches"][0]
    a1 = ids["sides"][match_id][0]
    # The match was entered and cleared by someone else, so it is empty again on revision 2.
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303
    assert _post(client, _match_path(ids, 0, "/scores/clear"), revision="1").status_code == 303
    data = _sheet_data(client, ids, {0: _both(ids, 0, SIXES)})                         # typed on revision 0
    data[f"score_{match_id}_{a1}_1"] = "99"
    response = client.post(_path(ids), data=data, follow_redirects=False)
    assert response.status_code == 409 and "was changed after you opened this page" in response.text
    assert 'value="99"' not in response.text and _score_count(client) == 0


def test_a_week_sheet_may_post_as_many_fields_as_the_week_has(sheet, monkeypatch):
    client, ids = sheet
    import golf_league.routers.admin_week_scores as router_module

    # 28 teams give 56 matches: 1,008 score inputs, 56 revisions and the token pass the parser's 1,000.
    assert router_module._field_limit(56, 9) >= 56 * 18 + 56 + 1 > 1000
    assert router_module._field_limit(4, 9) == 1000 and router_module._field_limit(4, 0) == 1000
    # With the parser's own limit made tiny, the four-match sheet still posts: the limit follows the week.
    monkeypatch.setattr(router_module, "_FORM_FIELDS_FLOOR", 10)
    everything = {index: _both(ids, index) for index in range(4)}
    assert len(_sheet_data(client, ids, everything)) == 4 * 18 + 4 + 1
    assert _save_sheet(client, ids, everything).status_code == 303 and _score_count(client) == 72
    lock_all = {f"revision_{match_id}": "1" for match_id in ids["matches"]}
    assert _post(client, _path(ids, "/lock-all"), **lock_all).status_code == 303
    # More fields than the week can have is still refused.
    padded = {**_sheet_data(client, ids, {}), **{f"extra_{n}": "1" for n in range(100)}}
    assert client.post(_path(ids), data=padded, follow_redirects=False).status_code == 400


def _enter_and_clear_elsewhere(client, ids, index):
    """Someone else enters and clears a match, leaving it empty again on revision 2."""
    match_id = ids["matches"][index]
    with Session(_engine(client)) as session:
        admin = Actor(session.scalar(select(User.id).where(User.is_admin)), True)
        numbers = [hole.number for hole in week_holes(session, ids["week"])]
        scores = {golfer_id: dict.fromkeys(numbers, 4) for golfer_id in ids["sides"][match_id]}
        save_match_scores(session, ids["week"], match_id, scores, admin, NOW.date(), base_revision=0, now=NOW)
        session.commit()
        clear_match_scores(session, ids["week"], match_id, admin, base_revision=1, now=NOW)
        session.commit()


def test_every_out_of_date_match_on_the_sheet_is_named_and_none_goes_back_on_a_newer_revision(sheet):
    client, ids = sheet
    first, second, third = ids["matches"][:3]
    data = _sheet_data(client, ids, {0: _both(ids, 0, SIXES), 1: _both(ids, 1, (7,) * 9), 2: _both(ids, 2, (8,) * 9)})
    _enter_and_clear_elsewhere(client, ids, 0)
    _enter_and_clear_elsewhere(client, ids, 1)

    response = client.post(_path(ids), data=data, follow_redirects=False)
    assert response.status_code == 409 and _score_count(client) == 0
    for name in ("Match 1: A1 Example and B1 Example", "Match 2: A2 Example and B2 Example"):
        assert f"{name} was changed after you opened this page." in response.text
    assert "Match 3" not in response.text.split("error-summary")[1].split("</div>")[0]
    # Neither stale match keeps what was typed; the match that is still current does, on its own revision.
    assert 'value="6"' not in _block(response.text, first) and 'value="7"' not in _block(response.text, second)
    assert f'name="score_{third}_{ids["sides"][third][0]}_1" value="8"' in response.text
    assert f'name="revision_{third}" value="0"' in response.text
    # Posting the page that was first opened again is refused again: nothing slips through on a retry.
    assert client.post(_path(ids), data=data, follow_redirects=False).status_code == 409
    assert _score_count(client) == 0


def test_typed_values_kept_on_the_sheet_go_back_with_the_revision_they_were_typed_on(sheet, monkeypatch):
    client, ids = sheet
    second = ids["matches"][1]
    import golf_league.routers.admin_week_scores as router_module

    def changed_meanwhile(*args, **kwargs):
        _enter_and_clear_elsewhere(client, ids, 1)  # match 2 moves to revision 2 during the save
        raise ScoreEntryError("conflict", "")

    monkeypatch.setattr(router_module, "save_week", changed_meanwhile)
    data = _sheet_data(client, ids, {0: _both(ids, 0, SIXES), 1: _both(ids, 1, (7,) * 9)})
    response = client.post(_path(ids), data=data, follow_redirects=False)
    assert response.status_code == 409 and "Nothing was saved; save again." in response.text
    # Match 2's sevens are still in the form, on the revision they were typed on, not the new one.
    assert f'name="score_{second}_{ids["sides"][second][0]}_1" value="7"' in response.text
    assert f'name="revision_{second}" value="0"' in response.text
    monkeypatch.undo()
    # Saving that form again is checked against what the admin saw, so match 2 is refused.
    response = client.post(_path(ids), data=data, follow_redirects=False)
    assert response.status_code == 409 and "Match 2: A2 Example and B2 Example was changed" in response.text
    assert _score_count(client) == 0


def test_a_week_that_is_not_a_match_week_takes_no_scores_even_with_matches(sheet):
    client, ids = sheet
    with Session(_engine(client)) as session:
        session.get(Week, ids["week"]).week_type = "play_with_team"
        session.commit()
    page = client.get(_path(ids)).text
    assert "This week is not a match week." in page and "score-input" not in page and "Save scores" not in page
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 422
    match_page = client.get(_match_path(ids, 0)).text
    assert "This week is not a match week." in match_page and "score-input" not in match_page
    response = _save_match(client, ids, 0, (FIVES, FIVES), 0)
    assert response.status_code == 422 and "not a match week" in response.text
    assert _score_count(client) == 0


# --- refusals the pages cannot cause themselves -------------------------------------


def _refuse(monkeypatch, name, error):
    import golf_league.routers.admin_week_scores as router_module

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(router_module, name, fail)


def test_a_database_failure_on_the_match_page_is_503_and_keeps_what_was_typed(sheet, monkeypatch, caplog):
    client, ids = sheet
    _refuse(monkeypatch, "save_match_scores",
            OperationalError("UPDATE", {"strokes": "must-not-be-logged"}, Exception("database is locked")))
    with caplog.at_level(logging.WARNING):
        response = _save_match(client, ids, 0, (SIXES, SIXES), 0)
    assert response.status_code == 503 and "could not be saved just now" in response.text
    a1 = ids["sides"][ids["matches"][0]][0]
    assert f'name="score_{a1}_1" value="6"' in response.text
    assert "match save failed" in caplog.text and "must-not-be-logged" not in caplog.text
    assert _score_count(client) == 0


@pytest.mark.parametrize(("code", "status", "shown"), [
    ("conflict", 409, "Nothing was saved; save again."),
    ("not_open_yet", 422, "The service said why."),
])
def test_a_refused_save_keeps_what_was_typed_on_both_pages(sheet, monkeypatch, code, status, shown):
    client, ids = sheet
    match_id = ids["matches"][0]
    a1 = ids["sides"][match_id][0]
    error = ScoreEntryError(code, "The service said why.", player_match_id=match_id)

    _refuse(monkeypatch, "save_match_scores", error)
    response = _save_match(client, ids, 0, (SIXES, SIXES), 0)
    assert response.status_code == status and shown in response.text
    assert f'name="score_{a1}_1" value="6"' in response.text

    _refuse(monkeypatch, "save_week", error)
    response = _save_sheet(client, ids, {0: _both(ids, 0, SIXES)})
    assert response.status_code == status and shown in response.text
    assert f'name="score_{match_id}_{a1}_1" value="6"' in response.text
    assert _score_count(client) == 0


def test_a_week_or_match_that_went_away_during_a_write_is_404(sheet, monkeypatch):
    client, ids = sheet
    gone = ScoreEntryError("not_found", "")
    _refuse(monkeypatch, "save_week", gone)
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 404
    _refuse(monkeypatch, "save_match_scores", gone)
    assert _save_match(client, ids, 0, (FIVES, FIVES), 0).status_code == 404
    _refuse(monkeypatch, "lock_week", gone)
    assert _post(client, _path(ids, "/lock-all")).status_code == 404
    assert _score_count(client) == 0


def test_lock_all_during_another_write_is_409_and_locks_nothing(sheet, monkeypatch):
    client, ids = sheet
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303
    _refuse(monkeypatch, "lock_week", ScoreEntryError("conflict", ""))
    response = _post(client, _path(ids, "/lock-all"), **{f"revision_{ids['matches'][0]}": "1"})
    assert response.status_code == 409 and "Nothing was locked; try again." in response.text
    assert _state(client, ids["matches"][0]).locked_at is None


# --- safety -------------------------------------------------------------------------


def test_names_are_escaped_everywhere(sheet):
    client, ids = sheet
    with Session(_engine(client)) as session:
        golfer = session.get(Golfer, ids["sides"][ids["matches"][0]][0])
        golfer.first_name, golfer.last_name = "A1<i>Markup</i>", "O'Example-<b>x</b>"
        session.get(User, session.scalar(select(User.id))).display_name = "<script>admin</script>"
        session.commit()
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303
    assert _post(client, _match_path(ids, 0, "/lock"), revision="1").status_code == 303
    pages = [client.get(_path(ids)).text, client.get(_match_path(ids, 0)).text]
    assert _post(client, _match_path(ids, 0, "/unlock")).status_code == 303
    pages.append(client.get(_match_path(ids, 0, "/scores/clear")).text)
    pages.append(_save_sheet(client, ids, {1: {ids["sides"][ids["matches"][1]][0]: FIVES}}).text)
    for html in pages:
        assert "<i>Markup</i>" not in html and "<b>x</b>" not in html and "<script>admin" not in html
        assert "A1&lt;i&gt;Markup&lt;/i&gt;" in html


def test_no_log_record_carries_a_name_or_a_score(sheet, caplog):
    client, ids = sheet
    scores = (4, 5, 6, 3, 4, 8, 5, 7, 4)
    with caplog.at_level(logging.DEBUG):
        _save_sheet(client, ids, {0: _both(ids, 0, scores)})
        _save_sheet(client, ids, {1: {ids["sides"][ids["matches"][1]][0]: scores}})  # refused: half filled
        _save_match(client, ids, 0, (scores, FIVES), 1)
        _post(client, _match_path(ids, 0, "/lock"), revision="2")
        _post(client, _path(ids, "/lock-all"))
        _post(client, _match_path(ids, 0, "/unlock"))
        _post(client, _match_path(ids, 0, "/scores/clear"), revision="4")
    ours = [r for r in caplog.records if r.name.startswith("golf_league")]
    text = "\n".join(r.getMessage() for r in ours)
    for action in ("week sheet saved", "match saved", "match locked", "week lock-all", "match unlocked",
                   "match cleared"):
        assert action in text, action
    for forbidden in ("A1 Example", "B1 Example", "Example", "4, 5, 6", "[4", "Admin"):
        assert forbidden not in text


def test_times_are_shown_in_the_league_time_zone(sheet):
    client, ids = sheet
    # 02:00 UTC on 28 August is the evening of 27 August in the league's zone.
    client.app.state.scorecard_utc_now = lambda: datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
    assert _save_sheet(client, ids, {0: _both(ids, 0)}).status_code == 303
    assert "Last changed by Admin on Aug 27, 2026." in client.get(_path(ids)).text
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(days=30)
    assert _save_sheet(client, ids, {1: _both(ids, 1)}).status_code == 303  # long after the round, an admin can
    assert "Last changed by Admin on Sep 26, 2026." in client.get(_path(ids)).text
