"""GL-86: a player's own scorecard, uploaded or typed, reviewed and submitted.

Synthetic data only: the generated golfers `A1 Example` .. `D4 Example`,
images made in memory, and a fake reader. Week 1 (27 August) has A against B:
A1 v B1 up to A4 v B4. The logged-in player is A1.
"""

import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_admin_scorecards import (
    FIVES,
    NOW,
    DeferredExecutor,
    FakeProvider,
    InlineExecutor,
    _card,
    _draft_count,
    _engine,
    _form,
    _jpeg,
    _photos,
    _player,
    _score_count,
    _token,
)
from test_player_scores import _as, _match_path, _save, _strokes, _user
from test_player_scores import league as _league

from golf_league.domain.scorecard import Actor
from golf_league.models import Golfer, HoleScore, ScorecardDraft
from golf_league.services.scorecard_vision import ProviderTimeout
from golf_league.services.scores import lock_match

league = _league
NINES = (9,) * 9


@pytest.fixture
def cards(league):
    """The league with a fake reader that reads A1 and B1."""
    client, ids = league
    client.app.state.vision_provider = FakeProvider(_card(_player("A1"), _player("B1")))
    return client, ids


def _start_path(ids, week=None):
    return f"/weeks/{week or ids['week']}/scorecards"


def _upload(client, ids, *, raw=None, week=None, **extra):
    data = {"csrf_token": _token(client), **extra}
    files = {"photo": ("IMG_0001.HEIC", raw if raw is not None else _jpeg(), "image/heic")}
    return client.post(_start_path(ids, week), data=data, files=files, follow_redirects=False)


def _manual(client, ids, week=None):
    return client.post(_start_path(ids, week), data={"csrf_token": _token(client), "manual": "1"},
                       follow_redirects=False)


def _draft_id(response) -> int:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/scorecards/"), location
    return int(location.rsplit("/", 1)[1])


def _submit(client, draft_id, rows, **options):
    return client.post(f"/scorecards/{draft_id}/submit", data=_form(client, rows, **options),
                       follow_redirects=False)


def _routes(draft_id):
    return [("GET", f"/scorecards/{draft_id}"), ("GET", f"/scorecards/{draft_id}/status"),
            ("GET", f"/scorecards/{draft_id}/image"), ("POST", f"/scorecards/{draft_id}/submit"),
            ("POST", f"/scorecards/{draft_id}/discard")]


def _golfers(ids, *names):
    """Golfer ids by label: A1..A4 are golfers 0..3, B1..B4 are 4..7, C1.. are 8.."""
    index = {f"{team}{position}": number * 4 + position - 1
             for number, team in enumerate("ABCD") for position in range(1, 5)}
    return [ids["golfers"][index[name]] for name in names]


# --- who may open a card ------------------------------------------------------------------


def test_another_players_card_is_404_everywhere_and_an_admin_uses_the_admin_addresses(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids))
    for method, path in _routes(draft_id)[:3]:
        assert client.request(method, path).status_code == 200, path

    _as(client, ids["users"]["b1"])
    a1, b1 = _golfers(ids, "A1", "B1")
    for method, path in _routes(draft_id):
        data = _form(client, [("", a1, FIVES), ("", b1, FIVES)]) if method == "POST" else None
        assert client.request(method, path, data=data, follow_redirects=False).status_code == 404, path
    assert _score_count(client) == 0 and _draft_count(client) == 1

    _as(client, _user(client, "boss@example.test", admin=True))
    for _, path in _routes(draft_id)[:3]:
        assert client.get(path.replace("/scorecards", "/admin/scorecards")).status_code == 200, path
        assert client.get(path).status_code == 404, path
    assert "(started by a player)" in client.get("/admin/scorecards/new").text


def test_anonymous_is_401_unverified_403_and_a_post_needs_a_token(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids))
    photo = client.get(f"/scorecards/{draft_id}/image")
    assert photo.headers["cache-control"] == "no-store" and photo.headers["x-content-type-options"] == "nosniff"
    for method, path in [("POST", _start_path(ids)), *_routes(draft_id)]:
        if method == "POST":
            assert client.post(path, data={}, follow_redirects=False).status_code == 403, path
    token = _token(client)
    client.cookies.clear()
    for method, path in [("POST", _start_path(ids)), *_routes(draft_id)]:
        response = client.request(method, path, data={"csrf_token": token} if method == "POST" else None,
                                  follow_redirects=False)
        assert response.status_code == 401, path
    _as(client, _user(client, "late@example.test", _golfers(ids, "A4")[0], verified=False))
    for method, path in [("POST", _start_path(ids)), *_routes(draft_id)]:
        response = client.request(method, path, data={"csrf_token": _token(client)} if method == "POST" else None,
                                  follow_redirects=False)
        assert response.status_code == 403, path
    assert _draft_count(client) == 1


def test_who_may_start_a_card(cards):
    client, ids = cards
    _as(client, ids["users"]["c1"])  # plays no match in week 1
    assert _upload(client, ids).status_code == 404 and _manual(client, ids).status_code == 404
    _as(client, ids["users"]["unlinked"])
    assert _manual(client, ids).status_code == 404
    _as(client, ids["users"]["a1"])
    client.app.state.scorecard_utc_now = lambda: NOW - timedelta(days=1)
    early = _upload(client, ids)
    assert early.status_code == 422 and "can be entered from Aug 27, 2026." in early.text
    assert _manual(client, ids).status_code == 422 and _draft_count(client) == 0
    assert client.post("/weeks/999999/scorecards", data={"csrf_token": _token(client), "manual": "1"}).status_code == 404


# --- submitting -----------------------------------------------------------------------------


def test_a_two_golfer_card_for_the_players_match_saves_18_rows_and_deletes_the_draft(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids))
    assert len(_photos(client)) == 1
    review = client.get(f"/scorecards/{draft_id}").text
    assert "Review your scorecard" in review and f'src="/scorecards/{draft_id}/image"' in review
    assert f'action="/scorecards/{draft_id}/submit"' in review
    a1, b1 = _golfers(ids, "A1", "B1")
    response = _submit(client, draft_id, [("A1", a1, FIVES), ("B1", b1, FIVES)])
    assert response.status_code == 303
    assert response.headers["location"] == f"/weeks/{ids['week']}/scores?saved=1"
    assert _score_count(client) == 18 and _draft_count(client) == 0 and _photos(client) == []
    with Session(_engine(client)) as session:
        assert set(session.scalars(select(HoleScore.entered_by_user_id))) == {ids["users"]["a1"]}


def test_a_four_golfer_card_saves_36_rows_and_the_scorekeeper_can_change_the_other_pair(cards):
    client, ids = cards
    a1, b1, a2, b2 = _golfers(ids, "A1", "B1", "A2", "B2")
    draft_id = _draft_id(_manual(client, ids))
    rows = [("", a1, FIVES), ("", b1, FIVES), ("", a2, FIVES), ("", b2, FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303
    assert _score_count(client) == 36
    assert _save(client, ids, 1, ((6,) * 9, (6,) * 9), revision=1).status_code == 303
    assert set(_strokes(client, ids["matches"][1]).values()) == {54}
    _as(client, ids["users"]["c1"])
    assert client.get(_match_path(ids, 1)).status_code == 404


@pytest.mark.parametrize("case,message", [
    ("own_missing", "Your own name must be on the card."),
    ("third_match", "A card can hold your match and one other match."),
    ("second_other", "You have already kept score for another match this week."),
])
def test_a_card_outside_the_players_scope_is_422_and_writes_nothing(cards, case, message):
    client, ids = cards
    a1, b1, a2, b2, a3, b3 = _golfers(ids, "A1", "B1", "A2", "B2", "A3", "B3")
    if case == "second_other":
        first = _draft_id(_manual(client, ids))
        assert _submit(client, first, [("", a1, FIVES), ("", b1, FIVES), ("", a2, FIVES), ("", b2, FIVES)]).status_code == 303
    before = _score_count(client)
    rows = {"own_missing": [("", a2, FIVES), ("", b2, FIVES)],
            "third_match": [("", a1, FIVES), ("", a2, FIVES), ("", a3, FIVES)],
            "second_other": [("", a1, FIVES), ("", a3, FIVES), ("", b3, FIVES)]}[case]
    unpaired = {"own_missing": [], "third_match": [a1, a2, a3], "second_other": [a1]}[case]
    draft_id = _draft_id(_manual(client, ids))
    response = _submit(client, draft_id, rows, acknowledged=unpaired)
    assert response.status_code == 422 and message in response.text
    assert _score_count(client) == before and _draft_count(client) == 1


def test_golfers_already_saved_or_locked_are_left_off_and_all_left_off_is_409(cards):
    client, ids = cards
    a1, b1, a2, b2 = _golfers(ids, "A1", "B1", "A2", "B2")
    admin = _user(client, "boss@example.test", admin=True)
    with Session(_engine(client)) as session:
        lock_match(session, ids["week"], ids["matches"][1], Actor(admin, True), base_revision=0, now=NOW)
        session.commit()
    draft_id = _draft_id(_manual(client, ids))
    rows = [("", a1, FIVES), ("", b1, FIVES), ("", a2, FIVES), ("", b2, FIVES)]
    first = _submit(client, draft_id, rows)
    assert first.status_code == 409 and "A2 Example, B2 Example already have saved scores" in first.text
    assert first.text.count("Already saved or locked: left off this card.") == 2 and _score_count(client) == 0
    assert _submit(client, draft_id, rows, left_off=[a2, b2]).status_code == 303
    assert _score_count(client) == 18 and _strokes(client, ids["matches"][1]) == {}
    again = _draft_id(_manual(client, ids))
    response = _submit(client, again, [("", a1, NINES), ("", b1, NINES)])
    assert response.status_code == 409 and "Every player on this card already has saved scores" in response.text
    assert set(_strokes(client, ids["matches"][0]).values()) == {45}


# --- limits on photos ------------------------------------------------------------------------


def test_a_second_upload_goes_to_the_open_card_and_typed_cards_are_not_limited(cards):
    client, ids = cards
    client.app.state.scorecard_executor = DeferredExecutor()
    first = _draft_id(_upload(client, ids))
    assert _draft_id(_upload(client, ids)) == first and len(_photos(client)) == 1
    manual = [_draft_id(_manual(client, ids)) for _ in range(8)]
    assert len(set(manual)) == 8 and _draft_count(client) == 9


def test_the_seventh_upload_in_an_hour_is_409(cards):
    client, ids = cards
    for _ in range(6):
        draft_id = _draft_id(_upload(client, ids))
        assert client.post(f"/scorecards/{draft_id}/discard", data={"csrf_token": _token(client)}).status_code == 200
    response = _upload(client, ids)
    assert response.status_code == 409
    assert "You have uploaded several photos in the last hour. Type your scores instead" in response.text
    assert "Type a card for up to four players" in response.text and _draft_count(client) == 0


def test_with_eight_cards_waiting_an_upload_is_409_and_an_admin_is_not_limited(cards):
    client, ids = cards
    with Session(_engine(client)) as session:
        for number in range(8):
            session.add(ScorecardDraft(week_id=ids["week"], created_by_user_id=ids["users"]["b1"],
                                       status="extracting", image_path=f"{number:032x}.jpg", created_at=NOW,
                                       expires_at=NOW + timedelta(hours=24)))
        session.commit()
    response = _upload(client, ids)
    assert response.status_code == 409
    assert "The photo reader is busy: 8 cards are waiting. Type your scores instead" in response.text
    assert 'name="manual" value="1"' in response.text and _draft_count(client) == 8
    assert _manual(client, ids).status_code == 303
    _as(client, _user(client, "boss@example.test", admin=True))
    client.app.state.scorecard_executor = DeferredExecutor()
    for _ in range(7):
        upload = client.post("/admin/scorecards", data={"week_id": str(ids["week"]), "csrf_token": _token(client)},
                             files={"photo": ("card.jpg", _jpeg(), "image/jpeg")}, follow_redirects=False)
        assert upload.status_code == 303


@pytest.mark.parametrize("already_waiting", [0, 7])
def test_overlapping_uploads_are_admitted_one_at_a_time(cards, monkeypatch, already_waiting):
    """Found in review: the limits were checked before the photo was prepared, so two uploads
    in flight together both passed them. Admission now runs under the prepare lock."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import golf_league.routers.player_scorecards as routes
    import golf_league.routers.scorecard_flow as flow

    client, ids = cards
    client.app.state.scorecard_executor = DeferredExecutor()
    with Session(_engine(client)) as session:
        for number in range(already_waiting):
            session.add(ScorecardDraft(week_id=ids["week"], created_by_user_id=ids["users"]["b1"],
                                       status="extracting", image_path=f"{number:032x}.jpg", created_at=NOW,
                                       expires_at=NOW + timedelta(hours=24)))
        session.commit()
    events, started, release = [], threading.Event(), threading.Event()
    real_prepare, real_open, real_create = flow.prepare_image, routes.open_photo_draft, routes.create_draft_from_image

    def prepare(raw):
        started.set()
        assert release.wait(15)
        return real_prepare(raw)

    def check(*args):
        events.append("checked")
        return real_open(*args)

    def create(*args, **kwargs):
        draft = real_create(*args, **kwargs)
        events.append("created")
        return draft

    monkeypatch.setattr(flow, "prepare_image", prepare)
    monkeypatch.setattr(routes, "open_photo_draft", check)
    monkeypatch.setattr(routes, "create_draft_from_image", create)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_upload, client, ids)
        assert started.wait(15)
        second = pool.submit(_upload, client, ids)
        time.sleep(0.3)  # the second upload is in flight while the first is being prepared
        release.set()
        responses = [first.result(15), second.result(15)]
    assert events == ["checked", "created", "checked"]  # the second is admitted after the first is stored
    assert _draft_count(client) == already_waiting + 1
    assert [response.status_code for response in responses] == [303, 303]
    assert responses[0].headers["location"] == responses[1].headers["location"]  # sent to the open card


def test_the_waiting_page_counts_the_cards_ahead_and_offers_typed_entry(cards):
    client, ids = cards
    client.app.state.scorecard_executor = DeferredExecutor()
    _as(client, ids["users"]["b1"])
    _draft_id(_upload(client, ids))
    _as(client, ids["users"]["a1"])
    draft_id = _draft_id(_upload(client, ids))
    page = client.get(f"/scorecards/{draft_id}").text
    assert "1 card is ahead of yours." in page
    assert f'href="/weeks/{ids["week"]}/scores/enter">Type your scores instead' in page
    assert f'data-scorecard-status-url="/scorecards/{draft_id}/status"' in page
    assert f'href="/scorecards/{draft_id}">Refresh this page' in page
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(minutes=10)
    assert "taking longer than expected" not in client.get(f"/scorecards/{draft_id}").text  # one is ahead
    status = client.get(f"/scorecards/{draft_id}/status")
    assert status.json() == {"status": "extracting", "error_code": None}


def test_the_image_is_prepared_off_the_event_loop_one_at_a_time(cards, monkeypatch):
    import golf_league.routers.scorecard_flow as flow

    client, ids = cards
    seen = []

    def fake_prepare(raw):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((on_loop, client.app.state.scorecard_prepare_lock.locked()))
        return _jpeg()

    monkeypatch.setattr(flow, "prepare_image", fake_prepare)
    _draft_id(_upload(client, ids))
    _as(client, _user(client, "boss@example.test", admin=True))
    client.post("/admin/scorecards", data={"week_id": str(ids["week"]), "csrf_token": _token(client)},
                files={"photo": ("card.jpg", _jpeg(), "image/jpeg")}, follow_redirects=False)
    assert seen == [(False, True), (False, True)]


# --- the entry page and failures -------------------------------------------------------------


def test_the_entry_page_offers_a_photo_with_no_capture_and_a_typed_card(cards):
    client, ids = cards
    page = client.get(f"/weeks/{ids['week']}/scores/enter").text
    assert '<input type="file" id="photo" name="photo" accept="image/*">' in page and "capture" not in page
    assert ">Upload photo<" in page and ">Type a card for up to four players<" in page
    assert f'action="/weeks/{ids["week"]}/scorecards"' in page
    draft_id = _draft_id(_manual(client, ids))
    assert f'href="/scorecards/{draft_id}">Open your card in progress' in client.get(
        f"/weeks/{ids['week']}/scores/enter").text
    client.app.state.vision_provider = None
    page = client.get(f"/weeks/{ids['week']}/scores/enter").text
    assert 'type="file"' not in page and "Photo reading is not set up." in page
    refused = _upload(client, ids)
    assert refused.status_code == 422 and "Photo reading is not set up. Type the card instead." in refused.text


def test_a_locked_own_match_still_lets_the_player_keep_score_for_the_other_pair(cards):
    client, ids = cards
    admin = _user(client, "boss@example.test", admin=True)
    with Session(_engine(client)) as session:
        lock_match(session, ids["week"], ids["matches"][0], Actor(admin, True), base_revision=0, now=NOW)
        session.commit()
    assert ">Type a card for up to four players<" in client.get(f"/weeks/{ids['week']}/scores/enter").text
    a1, b1, a2, b2 = _golfers(ids, "A1", "B1", "A2", "B2")
    draft_id = _draft_id(_manual(client, ids))
    rows = [("", a1, FIVES), ("", b1, FIVES), ("", a2, FIVES), ("", b2, FIVES)]
    assert _submit(client, draft_id, rows, left_off=[a1, b1]).status_code == 303
    assert _score_count(client) == 18 and _strokes(client, ids["matches"][0]) == {}


def test_a_provider_failure_offers_retry_typing_and_discard(cards):
    client, ids = cards
    client.app.state.vision_provider = FakeProvider(ProviderTimeout())
    draft_id = _draft_id(_upload(client, ids))
    page = client.get(f"/scorecards/{draft_id}").text
    assert "Your scorecard could not be read" in page and "The photo reader took too long to answer." in page
    assert ">Retry with a new photo<" in page and f'href="/scorecards/{draft_id}?manual=1">Type the card instead' in page
    assert ">Discard this card<" in page
    assert "Players and scores" in client.get(f"/scorecards/{draft_id}?manual=1").text
    retry = client.post(f"/scorecards/{draft_id}/discard", data={"csrf_token": _token(client), "retry": "1"},
                        follow_redirects=False)
    assert retry.status_code == 303 and retry.headers["location"] == f"/weeks/{ids['week']}/scores/enter?discarded=1"
    assert "The card was discarded. Its photo has been deleted." in client.get(retry.headers["location"]).text
    assert _draft_count(client) == 0 and _photos(client) == []


@pytest.mark.parametrize("raw,message", [
    (b"not an image", "The photo could not be opened."),
    (b"x" * (10 * 1024 * 1024 + 1), "The photo is larger than 10 MB."),
], ids=["not_an_image", "over_10_mb"])
def test_a_refused_image_is_422_and_keeps_nothing(cards, raw, message):
    client, ids = cards
    response = _upload(client, ids, raw=raw)
    assert response.status_code == 422 and message in response.text
    assert _draft_count(client) == 0 and _photos(client) == []


def test_no_photo_chosen_and_a_declared_oversize_upload_are_422(cards):
    client, ids = cards
    response = client.post(_start_path(ids), data={"csrf_token": _token(client)})
    assert response.status_code == 422 and "Choose a photo of the scorecard." in response.text
    big = client.post(_start_path(ids), content=b"x" * 64, headers={
        "content-type": "multipart/form-data; boundary=x", "content-length": str(11 * 1024 * 1024)})
    assert big.status_code == 422 and "larger than 10 MB" in big.text and _draft_count(client) == 0


def test_an_expired_card_is_gone_on_every_route(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids))
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(hours=24)
    for method, path in _routes(draft_id)[:4]:
        data = {"csrf_token": _token(client)} if method == "POST" else None
        assert client.request(method, path, data=data, follow_redirects=False).status_code == 404, path
    client.get(f"/scorecards/{draft_id}")  # a page that opens drafts purges the expired ones
    assert _draft_count(client) == 0 and _photos(client) == []
    assert client.post(f"/scorecards/{draft_id}/discard", data={"csrf_token": _token(client)}).status_code == 404


def test_names_render_escaped_and_no_log_carries_a_name_a_score_or_image_bytes(cards, caplog):
    client, ids = cards
    with Session(_engine(client)) as session:
        session.get(Golfer, ids["golfers"][0]).first_name = "<i>Markup</i>"
        session.commit()
    with caplog.at_level(logging.DEBUG):
        draft_id = _draft_id(_upload(client, ids))
        page = client.get(f"/scorecards/{draft_id}").text
        a1, b1 = _golfers(ids, "A1", "B1")
        assert _submit(client, draft_id, [("", a1, (7,) * 9), ("", b1, (8,) * 9)]).status_code == 303
    assert "<i>Markup</i>" not in page and "&lt;i&gt;Markup&lt;/i&gt;" in page
    assert "player scorecard started" in caplog.text
    for record in caplog.records:
        message = record.getMessage()
        assert "Markup" not in message and "Example" not in message and "JFIF" not in message
        assert "63" not in message and "72" not in message


def test_an_upload_writes_no_score_before_the_reviewed_form_is_submitted(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids))
    assert _score_count(client) == 0  # nothing reaches the scores before the reviewed form is submitted
    with Session(_engine(client)) as session:
        assert session.scalar(select(func.count(HoleScore.id))) == 0
        assert session.get(ScorecardDraft, draft_id).status == "ready"


def test_every_link_on_the_player_score_pages_keeps_a_44px_target_at_every_width(cards):
    """Found in review: error-summary and card-warning links missed the desktop target size.
    Every link inside <main> on the M9 player pages, in each state, is a `.tap-link` or a
    `.button-link` (both 44 px at every width)."""
    import re

    from test_player_scores import _data

    client, ids = cards
    a1, b1 = _golfers(ids, "A1", "B1")
    client.app.state.vision_provider = FakeProvider(_card(_player("A1", out_total=40), _player("B1")))
    client.app.state.scorecard_executor = DeferredExecutor()
    pages = {"entry": client.get(f"/weeks/{ids['week']}/scores/enter").text,
             "week": client.get(f"/weeks/{ids['week']}/scores").text,
             "match errors": client.post(_match_path(ids, 0), data=_data(client, ids, 0, (("x",) * 9, FIVES), 0)).text}
    card = _draft_id(_upload(client, ids))
    pages["waiting"] = client.get(f"/scorecards/{card}").text
    client.app.state.scorecard_executor.run()
    pages["review with warnings"] = client.get(f"/scorecards/{card}").text
    pages["review errors"] = _submit(client, card, [("", a1, ("x",) * 9), ("", b1, FIVES)]).text
    client.app.state.vision_provider = FakeProvider(ProviderTimeout())
    client.app.state.scorecard_executor = InlineExecutor()
    client.post(f"/scorecards/{card}/discard", data={"csrf_token": _token(client)})
    pages["failed"] = client.get(f"/scorecards/{_draft_id(_upload(client, ids))}").text
    assert "Check before you submit" in pages["review with warnings"]
    assert 'class="error-summary"' in pages["match errors"]
    for label, html in pages.items():
        main = html.split("<main")[1].split("</main>")[0]
        links = re.findall(r"<a [^>]*>", main)
        assert links, label
        bare = [link for link in links if "tap-link" not in link and "button-link" not in link]
        assert not bare, f"{label}: {bare}"
