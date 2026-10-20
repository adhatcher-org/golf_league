"""M9 aggregate gate: player score entry and week scores (R-SCORES version 2).

Exit criterion: a verified player enters or photographs the scores for their
own match and one other match from an "Enter scores" link, and those allowed
can change them until an admin locks the match; an admin sees, enters,
changes, locks and unlocks a whole week's scores on one page; every verified
player can see the week's scores; and no write reaches a locked match,
another player's card, or a match outside R-SCORES.

Everything goes through the real routes, with the CSRF token scraped from the
page that carries the form, a fake photo reader and synthetic data only:
golfers `A1 Example` .. `D4 Example`; week 1 (27 August) has A against B.
"""

import re
from datetime import timedelta

import pytest
from conftest import _extract_csrf
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_admin_scorecards import FIVES, NOW, FakeProvider, _card, _jpeg, _player
from test_player_scores import _as, _user
from test_player_scores import league as _league

from golf_league.models import HoleScore, MatchScoreState, ScorecardDraft, Week

league = _league
SIXES = (6,) * 9


@pytest.fixture
def m9(league):
    client, ids = league
    client.app.state.vision_provider = FakeProvider(_card(_player("A1"), _player("B1")))
    ids = {**ids, "users": {**ids["users"], "admin": _user(client, "boss@example.test", admin=True)}}
    return client, ids


# --- reading the real pages --------------------------------------------------------------------


def _engine(client):
    return client.app.state.engine


def _href(html: str, text: str) -> str:
    match = re.search(rf'<a (?:class="tap-link" )?href="([^"]+)">{re.escape(text)}', html)
    assert match, f"no link {text!r}"
    return match.group(1)


def _revision(html: str, name: str = "revision") -> str:
    return re.search(rf'name="{name}" value="([^"]*)"', html).group(1)


def _form(html: str, scores: dict[int, tuple], *, holes=range(1, 10), name="score_{golfer}_{hole}") -> dict:
    """The form as the page shows it: its token and revision, with `scores` typed in."""
    data = {"csrf_token": _extract_csrf(html), "revision": _revision(html)}
    for golfer, values in scores.items():
        data.update({name.format(golfer=golfer, hole=hole): str(value) for hole, value in zip(holes, values, strict=True)})
    return data


def _scores(client, match_id) -> dict[int, int]:
    with Session(_engine(client)) as session:
        return dict(session.execute(select(HoleScore.golfer_id, func.sum(HoleScore.strokes))
                                    .where(HoleScore.player_match_id == match_id).group_by(HoleScore.golfer_id)).all())


def _rows(client) -> int:
    with Session(_engine(client)) as session:
        return session.scalar(select(func.count(HoleScore.id)))


def _drafts(client) -> int:
    with Session(_engine(client)) as session:
        return session.scalar(select(func.count(ScorecardDraft.id)))


def _photos(client) -> list[str]:
    directory = client.app.state.scorecard_dir
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def _sides(ids, index):
    return ids["sides"][ids["matches"][index]]


def _manual_card(client, ids) -> str:
    entry = client.get(f"/weeks/{ids['week']}/scores/enter").text
    assert ">Type a card for up to four players<" in entry
    response = client.post(f"/weeks/{ids['week']}/scorecards", follow_redirects=False,
                           data={"csrf_token": _extract_csrf(entry), "manual": "1"})
    assert response.status_code == 303
    return response.headers["location"]


def _card_form(html: str, golfers: list[int], scores=FIVES, *, acknowledged=None, left_off=None) -> dict:
    data = {"csrf_token": _extract_csrf(html), "left_off": left_off or _revision(html, "left_off")}
    for row, golfer in enumerate(golfers):
        data[f"golfer_{row}"] = str(golfer)
        data.update({f"score_{row}_{hole}": str(value) for hole, value in zip(range(1, 10), scores, strict=True)})
    if acknowledged:
        data["acknowledged"] = "-".join(str(golfer) for golfer in sorted(acknowledged))
    return data


# --- 1. enter, change, lock, unlock ------------------------------------------------------------


def test_1_a_player_enters_from_my_schedule_the_opponent_changes_and_the_lock_stops_both(m9):
    client, ids = m9
    a1, b1 = _sides(ids, 0)
    match_id = ids["matches"][0]
    entry_path = _href(client.get("/my/schedule").text, "Enter scores")
    assert entry_path == f"/weeks/{ids['week']}/scores/enter"
    entry = client.get(entry_path).text
    saved = client.post(f"/weeks/{ids['week']}/matches/{match_id}/scores", follow_redirects=False,
                        data=_form(entry, {a1: FIVES, b1: FIVES}))
    assert saved.status_code == 303
    week = client.get(saved.headers["location"]).text
    assert "Your scores were saved." in week and week.count("<td>45</td>") == 2

    _as(client, ids["users"]["b1"])
    match_page = client.get(_href(client.get(f"/weeks/{ids['week']}/scores").text, "Update scores")).text
    changed = client.post(f"/weeks/{ids['week']}/matches/{match_id}/scores", follow_redirects=False,
                          data=_form(match_page, {a1: FIVES, b1: (4,) + FIVES[1:]}))
    assert changed.status_code == 303 and _scores(client, match_id) == {a1: 45, b1: 44}
    # Both players have the match open when the admin locks it.
    open_pages = {ids["users"]["b1"]: client.get(f"/weeks/{ids['week']}/matches/{match_id}/scores").text}
    _as(client, ids["users"]["a1"])
    open_pages[ids["users"]["a1"]] = client.get(f"/weeks/{ids['week']}/matches/{match_id}/scores").text

    _as(client, ids["users"]["admin"])
    sheet = client.get(f"/admin/weeks/{ids['week']}/scores").text
    block = sheet.split(f'id="match-{match_id}"')[1]
    lock = client.post(f"/admin/weeks/{ids['week']}/matches/{match_id}/lock", follow_redirects=False,
                       data={"csrf_token": _extract_csrf(sheet), "revision": _revision(block)})
    assert lock.status_code == 303

    for user, page in open_pages.items():
        _as(client, user)
        assert "locked by the league admin" in client.get(f"/weeks/{ids['week']}/matches/{match_id}/scores").text
        refused = client.post(f"/weeks/{ids['week']}/matches/{match_id}/scores", follow_redirects=False,
                              data=_form(page, {a1: SIXES, b1: SIXES}))
        assert refused.status_code == 409 and "Nothing was saved." in refused.text
    assert _scores(client, match_id) == {a1: 45, b1: 44}

    _as(client, ids["users"]["admin"])
    match_admin = client.get(f"/admin/weeks/{ids['week']}/matches/{match_id}/scores").text
    unlocked = client.post(f"/admin/weeks/{ids['week']}/matches/{match_id}/unlock", follow_redirects=False,
                           data={"csrf_token": _extract_csrf(match_admin)})
    assert unlocked.status_code == 303
    _as(client, ids["users"]["a1"])
    page = client.get(f"/weeks/{ids['week']}/matches/{match_id}/scores").text
    again = client.post(f"/weeks/{ids['week']}/matches/{match_id}/scores", follow_redirects=False,
                        data=_form(page, {a1: SIXES, b1: SIXES}))
    assert again.status_code == 303 and _scores(client, match_id) == {a1: 54, b1: 54}


# --- 2. a card for two matches ------------------------------------------------------------------


def test_2_a_four_golfer_card_and_the_limits_of_what_a_card_may_reach(m9):
    client, ids = m9
    (a1, b1), (a2, b2), (a3, b3) = _sides(ids, 0), _sides(ids, 1), _sides(ids, 2)
    card = _manual_card(client, ids)
    review = client.get(card).text
    third = client.post(f"{card}/submit", follow_redirects=False,
                        data=_card_form(review, [a1, a2, a3], acknowledged=[a1, a2, a3]))
    assert third.status_code == 422 and "A card can hold your match and one other match." in third.text
    submitted = client.post(f"{card}/submit", follow_redirects=False, data=_card_form(review, [a1, b1, a2, b2]))
    assert submitted.status_code == 303 and _rows(client) == 36

    other = client.get(_href(client.get(f"/weeks/{ids['week']}/scores/enter").text,
                             "Update scores for A2 Example and B2 Example")).text
    changed = client.post(f"/weeks/{ids['week']}/matches/{ids['matches'][1]}/scores", follow_redirects=False,
                          data=_form(other, {a2: SIXES, b2: SIXES}))
    assert changed.status_code == 303 and _scores(client, ids["matches"][1]) == {a2: 54, b2: 54}

    second = _manual_card(client, ids)
    review = client.get(second).text
    refused = client.post(f"{second}/submit", follow_redirects=False,
                          data=_card_form(review, [a1, a3, b3], acknowledged=[a1]))
    assert refused.status_code == 422
    assert "You have already kept score for another match this week." in refused.text
    assert _rows(client) == 36 and _scores(client, ids["matches"][2]) == {}


# --- 3. the admin's week sheet -------------------------------------------------------------------


def test_3_an_admin_saves_several_matches_at_once_and_lock_all_names_the_rest(m9):
    client, ids = m9
    _as(client, ids["users"]["admin"])
    sheet_path = f"/admin/weeks/{ids['week']}/scores"
    sheet = client.get(sheet_path).text
    data = {"csrf_token": _extract_csrf(sheet)}
    for index in (1, 2):
        match_id = ids["matches"][index]
        data[f"revision_{match_id}"] = _revision(sheet, f"revision_{match_id}")
        for golfer in _sides(ids, index):
            data.update({f"score_{match_id}_{golfer}_{hole}": "5" for hole in range(1, 10)})
    saved = client.post(sheet_path, data=data, follow_redirects=False)
    assert saved.status_code == 303 and _rows(client) == 36
    shown = client.get(saved.headers["location"]).text
    assert "The scores for 2 matches were saved." in shown and "2 of 4 matches entered, 0 locked." in shown

    # Match 1 is entered by its player after the admin opened the page.
    lock_form = shown.split('id="lock-all-title"')[1]
    _as(client, ids["users"]["a1"])
    entry = client.get(f"/weeks/{ids['week']}/scores/enter").text
    a1, b1 = _sides(ids, 0)
    assert client.post(f"/weeks/{ids['week']}/matches/{ids['matches'][0]}/scores", follow_redirects=False,
                       data=_form(entry, {a1: FIVES, b1: FIVES})).status_code == 303

    _as(client, ids["users"]["admin"])
    data = {"csrf_token": _extract_csrf(lock_form),
            **dict(re.findall(r'name="(revision_\d+)" value="(\d+)"', lock_form))}
    lock_all = client.post(f"{sheet_path}/lock-all", data=data, follow_redirects=False)
    page = client.get(lock_all.headers["location"]).text
    assert "2 matches were locked." in page and "1 match is not fully entered and was left open." in page
    assert "Match 1: A1 Example and B1 Example was changed after you opened the page and was not locked." in page
    with Session(_engine(client)) as session:
        locked = {state.player_match_id for state in session.scalars(select(MatchScoreState))
                  if state.locked_at is not None}
    assert locked == {ids["matches"][1], ids["matches"][2]}


# --- 4. who sees the week's scores -----------------------------------------------------------------


def test_4_every_verified_user_sees_the_weeks_scores_and_an_anonymous_visitor_does_not(m9):
    client, ids = m9
    path = f"/weeks/{ids['week']}/scores"
    for user in ("a1", "c1", "unlinked", "admin"):
        _as(client, ids["users"][user])
        page = client.get(path)
        assert page.status_code == 200 and len(re.findall(r'id="match-\d+"', page.text)) == 4, user
    assert client.get(f"/weeks/{ids['week']}/scores/enter").status_code == 404  # the admin plays no match
    _as(client, ids["users"]["unlinked"])  # a verified account with no golfer
    assert client.get(f"/weeks/{ids['week']}/scores/enter").status_code == 404
    client.cookies.clear()
    assert client.get(path).status_code == 401


# --- 5. another player's card -----------------------------------------------------------------------


def test_5_another_players_card_is_404_on_every_address(m9):
    client, ids = m9
    entry = client.get(f"/weeks/{ids['week']}/scores/enter").text
    upload = client.post(f"/weeks/{ids['week']}/scorecards", data={"csrf_token": _extract_csrf(entry)},
                         files={"photo": ("card.jpg", _jpeg(), "image/jpeg")}, follow_redirects=False)
    card = upload.headers["location"]
    assert client.get(f"{card}/image").status_code == 200

    _as(client, ids["users"]["b1"])
    token = _extract_csrf(client.get(f"/weeks/{ids['week']}/scores/enter").text)
    a1, b1 = _sides(ids, 0)
    for method, path in (("GET", card), ("GET", f"{card}/status"), ("GET", f"{card}/image"),
                         ("POST", f"{card}/submit"), ("POST", f"{card}/discard")):
        data = {"csrf_token": token, "golfer_0": str(a1), "golfer_1": str(b1),
                **{f"score_{row}_{hole}": "5" for row in (0, 1) for hole in range(1, 10)}}
        response = client.request(method, path, data=data if method == "POST" else None, follow_redirects=False)
        assert response.status_code == 404, f"{method} {path}"
    assert _rows(client) == 0 and _drafts(client) == 1 and len(_photos(client)) == 1


# --- 6. the rain-date guard ----------------------------------------------------------------------------


def test_6_a_week_with_a_saved_score_or_a_lock_cannot_move_to_a_rain_date(m9):
    client, ids = m9
    with Session(_engine(client)) as session:
        rain = session.get(Week, ids["weeks"][2])
        rain.week_type, rain.nine, rain.makeup_for_week_id = "rain_date", None, ids["week"]
        session.commit()
    a1, b1 = _sides(ids, 0)
    entry = client.get(f"/weeks/{ids['week']}/scores/enter").text
    assert client.post(f"/weeks/{ids['week']}/matches/{ids['matches'][0]}/scores", follow_redirects=False,
                       data=_form(entry, {a1: FIVES, b1: FIVES})).status_code == 303

    _as(client, ids["users"]["admin"])
    preview = f"/admin/seasons/{ids['season']}/weeks/{ids['week']}/rain-date/preview"

    def try_move():
        token = _extract_csrf(client.get(f"/admin/seasons/{ids['season']}/weeks").text)
        return client.post(preview, data={"csrf_token": token, "target_week_id": str(ids["weeks"][2])},
                           follow_redirects=False)

    scored = try_move()
    assert scored.status_code == 409 and "cannot be moved to a rain date" in scored.text
    base = f"/admin/weeks/{ids['week']}/matches/{ids['matches'][0]}"
    confirm = client.get(f"{base}/scores/clear").text
    assert client.post(f"{base}/scores/clear", follow_redirects=False,
                       data={"csrf_token": _extract_csrf(confirm), "revision": _revision(confirm)}).status_code == 303
    match_page = client.get(f"{base}/scores").text
    lock_form = match_page.split("/lock")[1]
    assert client.post(f"{base}/lock", follow_redirects=False,
                       data={"csrf_token": _extract_csrf(match_page), "revision": _revision(lock_form)}).status_code == 303
    locked = try_move()
    assert locked.status_code == 409 and "cannot be moved to a rain date" in locked.text
    with Session(_engine(client)) as session:
        assert session.get(Week, ids["week"]).status == "scheduled"
        assert session.get(Week, ids["weeks"][2]).week_type == "rain_date"


# --- 7. nothing of a card outlives it -------------------------------------------------------------------


def test_7_after_a_submit_a_discard_and_24_hours_no_draft_and_no_photo_remain(m9):
    client, ids = m9
    a1, b1 = _sides(ids, 0)

    def upload() -> str:
        entry = client.get(f"/weeks/{ids['week']}/scores/enter").text
        response = client.post(f"/weeks/{ids['week']}/scorecards", data={"csrf_token": _extract_csrf(entry)},
                               files={"photo": ("card.jpg", _jpeg(), "image/jpeg")}, follow_redirects=False)
        assert response.status_code == 303
        return response.headers["location"]

    submitted = upload()
    review = client.get(submitted).text
    assert client.post(f"{submitted}/submit", data=_card_form(review, [a1, b1]),
                       follow_redirects=False).status_code == 303
    assert _drafts(client) == 0 and _photos(client) == []

    discarded = upload()
    page = client.get(discarded).text
    assert client.post(f"{discarded}/discard", data={"csrf_token": _extract_csrf(page)},
                       follow_redirects=False).status_code == 303
    assert _drafts(client) == 0 and _photos(client) == []

    upload()
    assert _drafts(client) == 1 and len(_photos(client)) == 1
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(hours=24)
    client.get(f"/weeks/{ids['week']}/scores/enter")  # a page that lists the player's card purges old ones
    assert _drafts(client) == 0 and _photos(client) == []


# --- 8. out of date ---------------------------------------------------------------------------------------


def test_8_a_save_from_an_out_of_date_form_writes_nothing(m9):
    client, ids = m9
    a1, b1 = _sides(ids, 0)
    match_path = f"/weeks/{ids['week']}/matches/{ids['matches'][0]}/scores"
    old = client.get(match_path).text
    assert client.post(match_path, data=_form(old, {a1: FIVES, b1: FIVES}), follow_redirects=False).status_code == 303
    stale = client.post(match_path, data=_form(old, {a1: SIXES, b1: SIXES}), follow_redirects=False)
    assert stale.status_code == 409 and _scores(client, ids["matches"][0]) == {a1: 45, b1: 45}

    _as(client, ids["users"]["admin"])
    sheet_path = f"/admin/weeks/{ids['week']}/scores"
    sheet = client.get(sheet_path).text
    a2, b2 = _sides(ids, 1)
    match_id = ids["matches"][1]
    data = {"csrf_token": _extract_csrf(sheet), f"revision_{match_id}": _revision(sheet, f"revision_{match_id}"),
            **{f"score_{match_id}_{golfer}_{hole}": "5" for golfer in (a2, b2) for hole in range(1, 10)}}
    _as(client, ids["users"]["a2"])
    entry = client.get(f"/weeks/{ids['week']}/scores/enter").text
    assert client.post(f"/weeks/{ids['week']}/matches/{match_id}/scores", follow_redirects=False,
                       data=_form(entry, {a2: SIXES, b2: SIXES})).status_code == 303
    _as(client, ids["users"]["admin"])
    refused = client.post(sheet_path, data=data, follow_redirects=False)
    assert refused.status_code == 409 and _scores(client, match_id) == {a2: 54, b2: 54}
