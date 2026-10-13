"""GL-82: the scorecard upload, review, submit and saved-scores pages.

Synthetic data only: golfers are the generated `A1 Example` .. `D4 Example`,
images are made in memory, and the provider is always a fake.
"""

import io
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_matchup_generation import create_generated, seed_fixture

from golf_league.models import (
    Golfer,
    HoleScore,
    ScorecardDraft,
    Season,
    User,
    Week,
    WeekHandicap,
)
from golf_league.security import generate_csrf_token
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.handicaps import ensure_week_handicap
from golf_league.services.scorecard_drafts import image_file
from golf_league.services.scorecard_vision import (
    ExtractedCard,
    ExtractedPlayer,
    ProviderTimeout,
)

NOW = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)  # 27 August in the league's time zone
FIVES = (5,) * 9
MODEL = "example-model:1b"


def _player(name, scores=FIVES, uncertain=None, **extra) -> ExtractedPlayer:
    return ExtractedPlayer(name, tuple(scores), tuple(uncertain or (False,) * 9), **extra)


def _card(*players, header=tuple(range(1, 10))) -> ExtractedCard:
    return ExtractedCard(players=tuple(players), model_name=MODEL, holes_header=header)


class FakeProvider:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def read_scorecard(self, image_jpeg: bytes, *, hole_count: int) -> ExtractedCard:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        function(*args, **kwargs)


class DeferredExecutor:
    """Holds jobs until `run`, so a test can see the `extracting` state."""

    def __init__(self):
        self.jobs = []

    def submit(self, function, *args, **kwargs):
        self.jobs.append((function, args, kwargs))

    def run(self):
        while self.jobs:
            function, args, kwargs = self.jobs.pop(0)
            function(*args, **kwargs)


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (640, 400), (40, 90, 160)).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def cards(admin_client):
    """A logged-in admin, an active season with week 1 generated (A against B), and a fake reader."""
    client = admin_client
    with Session(client.app.state.engine) as session:
        ids = seed_fixture(session)
        session.get(Season, ids["season"]).status = "active"
        create_generated(session, ids, week_index=0)
        session.commit()
    state = client.app.state
    state.scorecard_utc_now = lambda: NOW
    state.scorecard_executor = InlineExecutor()
    state.vision_provider = FakeProvider(_card(_player("A1"), _player("B1")))
    return client, ids


def _engine(client):
    return client.app.state.engine


def _score_count(client) -> int:
    with Session(_engine(client)) as session:
        return session.scalar(select(func.count(HoleScore.id)))


def _draft_count(client) -> int:
    with Session(_engine(client)) as session:
        return session.scalar(select(func.count(ScorecardDraft.id)))


def _photos(client) -> list[str]:
    directory = client.app.state.scorecard_dir
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def _token(client) -> str:
    """The CSRF token the pages embed: it is bound to the session cookie."""
    return generate_csrf_token(client.cookies.get("session") or "")


def _upload(client, week_id, *, raw=None, **extra):
    data = {"week_id": str(week_id), "csrf_token": _token(client), **extra}
    files = {"photo": ("IMG_0001.HEIC", raw if raw is not None else _jpeg(), "image/heic")}
    return client.post("/admin/scorecards", data=data, files=files, follow_redirects=False)


def _manual(client, week_id):
    response = client.post("/admin/scorecards", follow_redirects=False,
                           data={"week_id": str(week_id), "manual": "1", "csrf_token": _token(client)})
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _draft_id(response) -> int:
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _form(client, rows, *, holes=range(1, 10), acknowledged=(), left_off=(), **extra) -> dict:
    """Review-form data: `rows` are `(name, golfer id or "", scores)`; `acknowledged` the unpaired ids ticked."""
    data = {"csrf_token": _token(client), **extra}
    for index, (name, golfer_id, scores) in enumerate(rows):
        data[f"name_{index}"] = name
        data[f"golfer_{index}"] = str(golfer_id)
        for hole, score in zip(holes, scores, strict=True):
            data[f"score_{index}_{hole}"] = str(score)
    if acknowledged:
        # The checkbox value names the unpaired golfers it was shown beside.
        data["acknowledged"] = "-".join(str(golfer_id) for golfer_id in sorted(acknowledged))
    if left_off:
        # The hidden field names the golfers the form showed as left off.
        data["left_off"] = "-".join(str(golfer_id) for golfer_id in sorted(left_off))
    return data


def _submit(client, draft_id, rows, **options):
    return client.post(f"/admin/scorecards/{draft_id}/submit", data=_form(client, rows, **options),
                       follow_redirects=False)


def _selected(html: str, row: int) -> str:
    block = re.search(rf'<select id="golfer_{row}".*?</select>', html, re.DOTALL).group(0)
    match = re.search(r'value="(\d+)" selected', block)
    return match.group(1) if match else ""


def _value(html: str, field: str) -> str:
    return re.search(rf'name="{field}" value="([^"]*)"', html).group(1)


def _become(client, *, admin: bool) -> None:
    with Session(_engine(client)) as session:
        user = User(username="other@example.test", email="other@example.test", display_name="Other",
                    password_hash=hash_password("synthetic-password"), is_admin=admin,
                    email_verified_at=datetime.now(UTC).replace(tzinfo=None))
        session.add(user)
        session.commit()
        cookie = create_session_cookie(user.id, user.session_version, client.app.state.settings.session_secret)
    client.cookies.clear()
    client.cookies.set("session", cookie)


# --- who may use it -----------------------------------------------------------------


def _routes(draft_id, week_id):
    return [
        ("GET", "/admin/scorecards/new"), ("POST", "/admin/scorecards"),
        ("GET", f"/admin/scorecards/{draft_id}"), ("GET", f"/admin/scorecards/{draft_id}/status"),
        ("GET", f"/admin/scorecards/{draft_id}/image"), ("POST", f"/admin/scorecards/{draft_id}/submit"),
        ("POST", f"/admin/scorecards/{draft_id}/discard"), ("GET", f"/admin/weeks/{week_id}/scores"),
    ]


@pytest.mark.parametrize("role,status", [("anonymous", 401), ("player", 403)])
def test_every_route_refuses_anyone_but_a_verified_admin(cards, role, status):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    token = _token(client)
    if role == "anonymous":
        client.cookies.clear()
    else:
        _become(client, admin=False)
    for method, path in _routes(draft_id, ids["weeks"][0]):
        response = client.request(method, path, data={"csrf_token": token} if method == "POST" else None,
                                  follow_redirects=False)
        assert response.status_code == status, f"{method} {path}"
    assert _draft_count(client) == 1 and len(_photos(client)) == 1 and _score_count(client) == 0


def test_every_post_needs_a_csrf_token(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    for path in ("/admin/scorecards", f"/admin/scorecards/{draft_id}/submit",
                 f"/admin/scorecards/{draft_id}/discard"):
        assert client.post(path, data={"week_id": str(ids["weeks"][0])}).status_code == 403, path
        assert client.post(path, data={"csrf_token": "wrong"}).status_code == 403, path
    assert _draft_count(client) == 1 and _score_count(client) == 0


def test_another_admin_can_open_and_submit_a_card(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    _become(client, admin=True)
    assert client.get(f"/admin/scorecards/{draft_id}").status_code == 200
    rows = [("A1", ids["golfers"][0], FIVES), ("B1", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303
    assert _score_count(client) == 18


# --- starting a card -----------------------------------------------------------------


def test_the_form_defaults_to_todays_week_and_lists_only_eligible_weeks(cards):
    client, ids = cards
    with Session(_engine(client)) as session:
        create_generated(session, ids, week_index=2)  # 10 September: eligible, later
        session.commit()
    page = client.get("/admin/scorecards/new")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    options = re.findall(r'<option value="(\d+)"( selected)?>Week (\d+)', page.text)
    assert options == [(str(ids["weeks"][0]), " selected", "1"), (str(ids["weeks"][2]), "", "3")]
    assert 'accept="image/*"' in page.text and "capture=" in page.text
    # A week asked for in the address is preselected when it is eligible, ignored when not.
    later = client.get(f"/admin/scorecards/new?week_id={ids['weeks'][2]}").text
    assert f'<option value="{ids["weeks"][2]}" selected>' in later
    ignored = client.get(f"/admin/scorecards/new?week_id={ids['weeks'][1]}").text
    assert f'<option value="{ids["weeks"][0]}" selected>' in ignored


def test_the_default_week_follows_the_round_and_an_older_week_can_still_be_chosen(cards):
    client, ids = cards
    first = ids["weeks"][0]
    # The day after the round its week still has matches to enter, so it is still the default.
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(days=1)
    assert f'<option value="{first}" selected>' in client.get("/admin/scorecards/new").text
    # Three weeks later no week is the default, but the week is still offered.
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(days=21)
    late = client.get("/admin/scorecards/new").text
    assert '<option value="" selected>Choose a week</option>' in late
    assert f'<option value="{first}">Week 1' in late
    assert _upload(client, "").status_code == 422
    # Once the week is cancelled nothing can take a card, and the page says so.
    with Session(_engine(client)) as session:
        session.get(Week, first).status = "cancelled"
        session.commit()
    page = client.get("/admin/scorecards/new")
    assert "No week can take a scorecard" in page.text and "<select" not in page.text


def test_a_card_can_be_entered_long_after_the_round(cards):
    client, ids = cards
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(days=21)
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    rows = [("A1", ids["golfers"][0], FIVES), ("B1", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303 and _score_count(client) == 18


def test_today_follows_the_league_time_zone_not_utc(cards):
    client, ids = cards
    # 02:00 UTC on 28 August is still the evening of 27 August in Chicago.
    client.app.state.scorecard_utc_now = lambda: datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
    assert f'<option value="{ids["weeks"][0]}" selected>' in client.get("/admin/scorecards/new").text


@pytest.mark.parametrize("case", ["cancelled", "played", "no_matchup"])
def test_a_week_that_cannot_take_a_card_is_422_and_writes_nothing(cards, case):
    client, ids = cards
    week_id = ids["weeks"][0]
    if case == "no_matchup":
        week_id = ids["weeks"][1]
    else:
        with Session(_engine(client)) as session:
            session.get(Week, week_id).status = case
            session.commit()
    for response in (_upload(client, week_id), client.post("/admin/scorecards", data={
            "week_id": str(week_id), "manual": "1", "csrf_token": _token(client)})):
        assert response.status_code == 422
        assert "There is a problem" in response.text
    assert _draft_count(client) == 0 and _photos(client) == []


def test_an_unknown_week_is_404_and_a_missing_week_is_422(cards):
    client, _ = cards
    assert _upload(client, 999999).status_code == 404
    for week in ("", "abc", "0", "-1", "١"):
        response = _upload(client, week)
        assert response.status_code == 422 and "Choose a week." in response.text
    assert _draft_count(client) == 0 and _photos(client) == []


@pytest.mark.parametrize("raw,message", [
    (b"this is a text file named like a photo", "could not be opened"),
    (b"\x00" * (10 * 1024 * 1024 + 1), "larger than 10 MB"),
])
def test_a_refused_image_is_422_and_writes_nothing(cards, raw, message):
    client, ids = cards
    response = _upload(client, ids["weeks"][0], raw=raw)
    assert response.status_code == 422 and message in response.text
    assert _draft_count(client) == 0 and _photos(client) == []
    assert client.app.state.vision_provider.calls == 0


def test_no_photo_chosen_is_422(cards):
    client, ids = cards
    response = client.post("/admin/scorecards", data={"week_id": str(ids["weeks"][0]), "csrf_token": _token(client)})
    assert response.status_code == 422 and "Choose a photo of the scorecard." in response.text
    assert _draft_count(client) == 0


def test_an_upload_declared_far_too_large_is_refused_before_it_is_parsed(cards):
    client, ids = cards
    body = b"x" * (11 * 1024 * 1024)
    response = client.post("/admin/scorecards", content=body,
                           headers={"content-type": "multipart/form-data; boundary=synthetic"})
    assert response.status_code == 422 and "larger than 10 MB" in response.text
    assert _draft_count(client) == 0 and _photos(client) == []


def test_upload_is_extracting_then_ready_and_the_review_shows_what_was_read(cards):
    client, ids = cards
    deferred = DeferredExecutor()
    client.app.state.scorecard_executor = deferred
    client.app.state.vision_provider = FakeProvider(_card(
        _player("A1", (4, 5, 6, 3, 4, None, 5, 7, 4), (False,) * 5 + (True,) + (False,) * 3),
        _player("b1", uncertain=(True,) + (False,) * 8),
    ))
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))

    waiting = client.get(f"/admin/scorecards/{draft_id}")
    assert waiting.status_code == 200 and "Reading the scorecard" in waiting.text
    assert f'data-scorecard-status-url="/admin/scorecards/{draft_id}/status"' in waiting.text
    assert "/static/scorecard-status.js" in waiting.text and "Refresh this page" in waiting.text
    status = client.get(f"/admin/scorecards/{draft_id}/status")
    assert status.json() == {"status": "extracting", "error_code": None}
    assert status.headers["cache-control"] == "no-store"
    assert client.app.state.vision_provider.calls == 0  # nothing is read on the request thread
    # A card still being read cannot be submitted.
    assert _submit(client, draft_id, []).status_code == 409

    deferred.run()
    assert client.get(f"/admin/scorecards/{draft_id}/status").json() == {"status": "ready", "error_code": None}
    review = client.get(f"/admin/scorecards/{draft_id}")
    assert "Review scorecard" in review.text and review.headers["cache-control"] == "no-store"
    assert f'src="/admin/scorecards/{draft_id}/image"' in review.text
    assert _value(review.text, "name_0") == "A1" and _value(review.text, "name_1") == "b1"
    assert _selected(review.text, 0) == str(ids["golfers"][0])
    assert _selected(review.text, 1) == str(ids["golfers"][4])
    assert "A1 Example — plays B1 Example" in review.text
    assert _value(review.text, "score_0_1") == "4" and _value(review.text, "score_0_6") == ""
    # Unread and unclear cells are marked in text, not by colour alone.
    assert 'id="score_0_6_note">Not read<' in review.text
    assert 'id="score_1_1_note">Unclear<' in review.text
    assert review.text.count("score-note") == 2
    assert _value(review.text, "name_2") == "" and _selected(review.text, 2) == ""
    assert "Week 1" in review.text and "cannot be changed here" in review.text
    assert _score_count(client) == 0  # nothing is saved before the explicit submit


def test_a_slow_read_says_so_without_changing_the_draft(cards):
    client, ids = cards
    client.app.state.scorecard_executor = DeferredExecutor()
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    assert "longer than expected" not in client.get(f"/admin/scorecards/{draft_id}").text
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(minutes=6)
    assert "longer than expected" in client.get(f"/admin/scorecards/{draft_id}").text
    assert client.get(f"/admin/scorecards/{draft_id}/status").json()["status"] == "extracting"


# --- submitting ---------------------------------------------------------------------


def test_a_corrected_two_golfer_card_saves_18_rows_and_deletes_the_draft(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    assert len(_photos(client)) == 1
    a1, b1 = ids["golfers"][0], ids["golfers"][4]
    rows = [("A1", a1, (4, 5, 6, 3, 4, 5, 5, 7, 4)), ("B1", b1, FIVES)]
    response = _submit(client, draft_id, rows)
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/weeks/{ids['weeks'][0]}/scores?saved=1"
    assert _score_count(client) == 18
    assert _draft_count(client) == 0 and _photos(client) == []
    assert client.get(f"/admin/scorecards/{draft_id}").status_code == 404
    assert client.get(f"/admin/scorecards/{draft_id}/image").status_code == 404

    with Session(_engine(client)) as session:
        strokes = dict(session.execute(
            select(HoleScore.golfer_id, func.sum(HoleScore.strokes)).group_by(HoleScore.golfer_id)).all())
        assert strokes == {a1: 43, b1: 45}
        assert {row.entered_by_user_id for row in session.scalars(select(HoleScore))} == {
            session.scalar(select(User.id).where(User.email == "admin@example.test"))}

    scores = client.get(response.headers["location"])
    assert scores.status_code == 200 and scores.headers["cache-control"] == "no-store"
    assert "The card was saved" in scores.text
    assert "A1 Example" in scores.text and "B1 Example" in scores.text
    assert "<td>43</td>" in scores.text and "<td>45</td>" in scores.text
    assert "<form" not in scores.text.split('id="main-content"')[1].split("</main>")[0]  # read-only
    assert f'href="/admin/weeks/{ids["weeks"][0]}/scores"' in client.get("/admin/scorecards/new").text


def test_a_four_golfer_card_across_two_matches_saves_36_rows(cards):
    client, ids = cards
    draft_id = _manual(client, ids["weeks"][0])
    golfers = ids["golfers"]
    rows = [("", golfers[0], FIVES), ("", golfers[4], FIVES), ("", golfers[1], FIVES), ("", golfers[5], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303
    assert _score_count(client) == 36


def test_a_three_golfer_card_needs_the_acknowledgement(cards):
    client, ids = cards
    draft_id = _manual(client, ids["weeks"][0])
    golfers = ids["golfers"]
    rows = [("", golfers[0], FIVES), ("", golfers[4], FIVES), ("", golfers[1], FIVES)]
    refused = _submit(client, draft_id, rows)
    assert refused.status_code == 422 and _score_count(client) == 0 and _draft_count(client) == 1
    assert "A2 Example is not paired with an opponent on this card" in refused.text
    assert 'name="acknowledged"' in refused.text
    assert _value(refused.text, "score_2_9") == "5"  # the admin's values are kept
    assert f'name="acknowledged" value="{golfers[1]}"' in refused.text
    assert _submit(client, draft_id, rows, acknowledged=[golfers[1]]).status_code == 303
    assert _score_count(client) == 27


def test_a_mismatched_pair_needs_the_acknowledgement(cards):
    client, ids = cards
    draft_id = _manual(client, ids["weeks"][0])
    rows = [("", ids["golfers"][0], FIVES), ("", ids["golfers"][5], FIVES)]  # A1 and B2: different matches
    refused = _submit(client, draft_id, rows)
    assert refused.status_code == 422
    assert "A1 Example, B2 Example are not paired with an opponent on this card" in refused.text
    assert _submit(client, draft_id, rows, acknowledged=[ids["golfers"][0], ids["golfers"][5]]).status_code == 303
    assert _score_count(client) == 18


def test_the_acknowledgement_only_counts_for_the_golfers_it_was_shown_beside(cards):
    client, ids = cards
    golfers = ids["golfers"]
    draft_id = _manual(client, ids["weeks"][0])
    shown = _submit(client, draft_id, [("", golfers[0], FIVES), ("", golfers[5], FIVES)])  # A1 and B2
    assert shown.status_code == 422 and "A1 Example, B2 Example are not paired" in shown.text
    # Ticked for A1 and B2, then the golfers are changed to A3 and B4: asked again, with the new names.
    changed = [("", golfers[2], FIVES), ("", golfers[7], FIVES)]
    again = _submit(client, draft_id, changed, acknowledged=[golfers[0], golfers[5]])
    assert again.status_code == 422 and "A3 Example, B4 Example are not paired" in again.text
    assert not re.search(r'name="acknowledged"[^>]*checked', again.text)
    assert _score_count(client) == 0
    # A bare tick with no golfers named is not enough either.
    assert client.post(f"/admin/scorecards/{draft_id}/submit", follow_redirects=False,
                       data={**_form(client, changed), "acknowledged": "1"}).status_code == 422
    assert _submit(client, draft_id, changed, acknowledged=[golfers[2], golfers[7]]).status_code == 303
    assert _score_count(client) == 18


def test_a_tick_that_still_matches_stays_ticked_when_the_form_comes_back(cards):
    client, ids = cards
    golfers = ids["golfers"]
    draft_id = _manual(client, ids["weeks"][0])
    rows = [("", golfers[0], ("x",) * 9), ("", golfers[5], FIVES)]  # a bad score keeps the form open
    response = _submit(client, draft_id, rows, acknowledged=[golfers[0], golfers[5]])
    assert response.status_code == 422
    assert re.search(r'name="acknowledged"[^>]*checked', response.text)


def test_the_acknowledgement_is_offered_when_the_reading_is_already_unpaired(cards):
    client, ids = cards
    client.app.state.vision_provider = FakeProvider(_card(_player("A1"), _player("B2")))
    review = client.get(f"/admin/scorecards/{_draft_id(_upload(client, ids['weeks'][0]))}").text
    assert 'name="acknowledged"' in review and "A1 Example, B2 Example are not paired" in review
    paired = client.get(f"/admin/scorecards/{_manual(client, ids['weeks'][0])}").text
    assert 'name="acknowledged"' not in paired


@pytest.mark.parametrize("case,message", [
    ("outsider", "choose a golfer from this week&#39;s matchups"),
    ("unresolved", "Player 2: choose the golfer."),
    ("missing_hole", "Player 1, hole 4: enter a score."),
    ("zero", "Player 1, hole 4: enter a whole number from 1 to 15."),
    ("sixteen", "Player 1, hole 4: enter a whole number from 1 to 15."),
    ("decimal", "Player 1, hole 4: enter a whole number from 1 to 15."),
    ("duplicate", "A1 Example is already chosen for player 1"),
    ("one_golfer", "A card needs two to four golfers"),
])
def test_bad_values_are_422_keep_the_admins_values_and_write_nothing(cards, case, message):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    a1, b1, c1 = ids["golfers"][0], ids["golfers"][4], ids["golfers"][8]
    first, second = list(FIVES), ("B1", b1, (6,) * 9)
    if case == "outsider":
        second = ("C1", c1, (6,) * 9)  # a real golfer, but not in this week's matchups
    elif case == "unresolved":
        second = ("Somebody", "", (6,) * 9)
    elif case == "duplicate":
        second = ("A1 again", a1, (6,) * 9)
    elif case != "one_golfer":
        first[3] = {"missing_hole": "", "zero": "0", "sixteen": "16", "decimal": "4.5"}[case]
    rows = [("A1", a1, first)] + ([] if case == "one_golfer" else [second])
    response = _submit(client, draft_id, rows)
    assert response.status_code == 422, response.text
    assert "There is a problem" in response.text and message in response.text
    assert _score_count(client) == 0 and _draft_count(client) == 1 and len(_photos(client)) == 1
    # The form comes back with what the admin typed, not what was read.
    assert _value(response.text, "name_0") == "A1" and _value(response.text, "score_0_1") == "5"
    assert _value(response.text, "score_0_4") == first[3].__str__()
    if case != "one_golfer":
        assert _value(response.text, "name_1") == second[0] and _value(response.text, "score_1_9") == "6"
    if case in ("missing_hole", "zero", "sixteen", "decimal"):
        assert re.search(r'name="score_0_4"[^>]*aria-invalid="true"', response.text)
        assert 'href="#score_0_4"' in response.text


def test_a_tampered_golfer_value_is_refused(cards):
    client, ids = cards
    draft_id = _manual(client, ids["weeks"][0])
    for value in ("abc", "999999", "1 OR 1=1", "-1", "１"):
        response = _submit(client, draft_id, [("x", value, FIVES), ("y", ids["golfers"][4], FIVES)])
        assert response.status_code == 422 and "choose a golfer from this week" in response.text
    assert _score_count(client) == 0


def test_a_card_only_adds_and_golfers_already_saved_are_left_off(cards):
    client, ids = cards
    a1, b1, a2 = ids["golfers"][0], ids["golfers"][4], ids["golfers"][1]
    rows = [("", a1, FIVES), ("", b1, FIVES)]
    assert _submit(client, _manual(client, ids["weeks"][0]), rows).status_code == 303

    # The same two golfers again: everyone on the card is already saved.
    second = _manual(client, ids["weeks"][0])
    again = _submit(client, second, [("", a1, (9,) * 9), ("", b1, (9,) * 9)])
    assert again.status_code == 409
    assert "Every player on this card already has saved scores or is in a locked match" in again.text
    assert _value(again.text, "score_0_1") == "9"

    # A card sharing one golfer with the saved card comes back naming who is left off.
    shared_rows = [("", a1, (9,) * 9), ("", a2, (9,) * 9)]
    shared = _submit(client, second, shared_rows, acknowledged=[a1, a2])
    assert shared.status_code == 409 and _score_count(client) == 18 and _draft_count(client) == 1
    assert "A1 Example already has saved scores or are in a locked match" in shared.text
    assert "Nothing was saved." in shared.text
    assert shared.text.count("Already saved or locked: left off this card.") == 1
    assert f'name="left_off" value="{a1}"' in shared.text
    assert _value(shared.text, "score_1_9") == "9"  # what was typed is kept
    # A form that names the wrong golfer as left off is refused again.
    wrong = _submit(client, second, shared_rows, acknowledged=[a1, a2], left_off=[a2])
    assert wrong.status_code == 409 and _score_count(client) == 18

    # Submitted again as shown, the rest is written and the saved scores are untouched.
    done = _submit(client, second, shared_rows, acknowledged=[a1, a2], left_off=[a1])
    assert done.status_code == 303 and _draft_count(client) == 0
    with Session(_engine(client)) as session:
        by_golfer = dict(session.execute(
            select(HoleScore.golfer_id, func.sum(HoleScore.strokes)).group_by(HoleScore.golfer_id)).all())
    assert by_golfer == {a1: 45, b1: 45, a2: 81}


def test_the_review_form_shows_a_saved_golfer_as_left_off_before_any_submit(cards):
    client, ids = cards
    a1, b1 = ids["golfers"][0], ids["golfers"][4]
    assert _submit(client, _manual(client, ids["weeks"][0]), [("", a1, FIVES), ("", b1, FIVES)]).status_code == 303
    client.app.state.vision_provider = FakeProvider(_card(_player("A1"), _player("B1"), _player("A2"), _player("B2")))
    review = client.get(f"/admin/scorecards/{_draft_id(_upload(client, ids['weeks'][0]))}").text
    assert review.count("Already saved or locked: left off this card.") == 2
    assert f'name="left_off" value="{a1}-{b1}"' in review


def test_a_back_nine_week_uses_holes_10_to_18(cards):
    client, ids = cards
    back_week = ids["weeks"][1]
    with Session(_engine(client)) as session:
        create_generated(session, ids, week_index=1)
        session.commit()
    draft_id = _manual(client, back_week)
    review = client.get(f"/admin/scorecards/{draft_id}").text
    assert "Hole 10" in review and "Hole 18" in review and 'name="score_0_1"' not in review
    rows = [("", ids["golfers"][0], FIVES), ("", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows, holes=range(10, 19)).status_code == 303
    scores = client.get(f"/admin/weeks/{back_week}/scores").text
    assert "Hole 10" in scores and "Hole 18" in scores and "<td>45</td>" in scores


def test_a_week_that_stops_being_eligible_refuses_the_submit_and_keeps_the_draft(cards):
    client, ids = cards
    draft_id = _manual(client, ids["weeks"][0])
    with Session(_engine(client)) as session:
        session.get(Week, ids["weeks"][0]).status = "cancelled"
        session.commit()
    response = _submit(client, draft_id, [("", ids["golfers"][0], FIVES), ("", ids["golfers"][4], FIVES)])
    assert response.status_code == 422 and "That week is cancelled." in response.text
    assert _score_count(client) == 0 and _draft_count(client) == 1


# --- discard, expiry and the photo ---------------------------------------------------


def test_discard_removes_the_draft_and_the_photo(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    response = client.post(f"/admin/scorecards/{draft_id}/discard", data={"csrf_token": _token(client)},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/scorecards/new?discarded=1"
    assert _draft_count(client) == 0 and _photos(client) == []
    assert "The card was discarded" in client.get(response.headers["location"]).text
    assert client.post(f"/admin/scorecards/{draft_id}/discard",
                       data={"csrf_token": _token(client)}).status_code == 404


def test_the_photo_is_private_and_never_cached(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    image = client.get(f"/admin/scorecards/{draft_id}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert image.headers["cache-control"] == "no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert Image.open(io.BytesIO(image.content)).format == "JPEG"
    # The photo is not under any static path, and its stored name is not the upload's.
    stored = _photos(client)[0]
    assert "IMG_0001" not in stored and client.get(f"/static/{stored}").status_code == 404
    assert client.get(f"/static/scorecards/{stored}").status_code == 404
    assert client.get("/admin/scorecards/999999/image").status_code == 404


def test_an_expired_draft_is_gone_everywhere(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    client.app.state.scorecard_utc_now = lambda: NOW + timedelta(hours=24)
    # Not yet purged, but already past its 24 hours: no route serves it.
    assert client.get(f"/admin/scorecards/{draft_id}/image").status_code == 404
    assert client.get(f"/admin/scorecards/{draft_id}/status").status_code == 404
    assert _draft_count(client) == 1  # those GETs changed nothing
    rows = [("", ids["golfers"][0], FIVES), ("", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 404
    assert _score_count(client) == 0
    # Opening the scorecard pages purges it and its photo.
    assert client.get(f"/admin/scorecards/{draft_id}").status_code == 404
    assert _draft_count(client) == 0 and _photos(client) == []


def test_a_missing_photo_file_is_404_and_the_review_still_works(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    with Session(_engine(client)) as session:
        name = session.get(ScorecardDraft, draft_id).image_path
    image_file(client.app.state.scorecard_dir, name).unlink()
    assert client.get(f"/admin/scorecards/{draft_id}/image").status_code == 404
    review = client.get(f"/admin/scorecards/{draft_id}")
    assert review.status_code == 200 and "No photo." in review.text and "<img" not in review.text


def test_cards_in_progress_are_listed(cards):
    client, ids = cards
    first = _draft_id(_upload(client, ids["weeks"][0]))
    page = client.get("/admin/scorecards/new").text
    assert "Cards in progress" in page and f'href="/admin/scorecards/{first}"' in page
    assert "ready to review" in page


# --- when the reader fails or is not set up ---------------------------------------------


def test_a_provider_failure_offers_retry_manual_and_discard_and_manual_submits(cards):
    client, ids = cards
    client.app.state.vision_provider = FakeProvider(ProviderTimeout())
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    assert client.get(f"/admin/scorecards/{draft_id}/status").json() == {
        "status": "failed", "error_code": "provider_timeout"}
    failed = client.get(f"/admin/scorecards/{draft_id}")
    assert failed.status_code == 200 and "could not be read" in failed.text
    assert "The photo reader took too long to answer." in failed.text
    assert "provider_timeout" not in failed.text  # plain words, not the code
    for control in ("Retry with a new photo", "Enter manually", "Discard this card"):
        assert control in failed.text

    manual = client.get(f"/admin/scorecards/{draft_id}?manual=1")
    assert "Review scorecard" in manual.text and f'src="/admin/scorecards/{draft_id}/image"' in manual.text
    assert _value(manual.text, "name_0") == ""
    rows = [("", ids["golfers"][0], FIVES), ("", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303
    assert _score_count(client) == 18 and _draft_count(client) == 0 and _photos(client) == []


def test_retry_discards_the_failed_card_and_returns_to_the_same_week(cards):
    client, ids = cards
    client.app.state.vision_provider = FakeProvider(ProviderTimeout())
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    response = client.post(f"/admin/scorecards/{draft_id}/discard", follow_redirects=False,
                           data={"csrf_token": _token(client), "retry": "1"})
    assert response.headers["location"] == f"/admin/scorecards/new?week_id={ids['weeks'][0]}"
    assert _draft_count(client) == 0 and _photos(client) == []


def test_with_photo_entry_not_configured_only_manual_entry_works(cards):
    client, ids = cards
    client.app.state.vision_provider = None
    page = client.get("/admin/scorecards/new")
    assert "Photo reading is not set up." in page.text
    assert 'type="file"' not in page.text and "Upload photo" not in page.text
    assert "Enter scores manually" in page.text
    refused = _upload(client, ids["weeks"][0])
    assert refused.status_code == 422 and "Photo reading is not set up" in refused.text
    assert _draft_count(client) == 0 and _photos(client) == []
    draft_id = _manual(client, ids["weeks"][0])
    rows = [("", ids["golfers"][0], FIVES), ("", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303 and _score_count(client) == 18


# --- names on a real card: first names and handicaps --------------------------------------


def _snapshot_handicaps(client, ids) -> dict[int, int]:
    """Record week 1's handicap snapshots for the four A golfers and B1; return strokes by golfer."""
    with Session(_engine(client)) as session:
        for golfer_id in [*ids["golfers"][:4], ids["golfers"][4]]:
            ensure_week_handicap(session, ids["weeks"][0], golfer_id, clock=lambda: NOW)
        session.commit()
        return dict(session.execute(select(WeekHandicap.golfer_id, WeekHandicap.strokes)).all())


def _rename(client, golfer_id, first, last="Example"):
    with Session(_engine(client)) as session:
        golfer = session.get(Golfer, golfer_id)
        golfer.first_name, golfer.last_name = first, last
        session.commit()


def _review(client, ids, *players, header=tuple(range(1, 10))) -> tuple[int, str]:
    client.app.state.vision_provider = FakeProvider(_card(*players, header=header))
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    return draft_id, client.get(f"/admin/scorecards/{draft_id}").text


def test_a_first_name_only_preselects_the_single_golfer_with_that_name(cards):
    client, ids = cards
    _rename(client, ids["golfers"][0], "Aaron", "Hatchwell")
    _, review = _review(client, ids, _player("Aaron"), _player("B1"))
    assert _selected(review, 0) == str(ids["golfers"][0])
    assert "Check this match" not in review and "Matched by handicap" not in review


def test_a_close_name_preselects_and_is_flagged(cards):
    client, ids = cards
    _rename(client, ids["golfers"][0], "Aaron", "Hatchwell")
    _, review = _review(client, ids, _player("Aron"), _player("B1"))
    assert _selected(review, 0) == str(ids["golfers"][0]) and "Check this match." in review


def test_two_golfers_sharing_a_first_name_leave_the_choice_empty_unless_the_handicap_settles_it(cards):
    client, ids = cards
    first, other = ids["golfers"][0], ids["golfers"][1]  # A1 (handicap -1) and A2 (handicap 0)
    _rename(client, first, "Aaron", "Hatchwell")
    _rename(client, other, "Aaron", "Otherly")
    stored = _snapshot_handicaps(client, ids)
    assert stored[first] != stored[other]

    _, plain = _review(client, ids, _player("Aaron"), _player("B1"))
    assert _selected(plain, 0) == "" and _selected(plain, 1) == str(ids["golfers"][4])
    _, wrong = _review(client, ids, _player("Aaron", handicap_written=30), _player("B1"))
    assert _selected(wrong, 0) == ""
    _, settled = _review(client, ids, _player("Aaron", handicap_written=stored[other]), _player("B1"))
    assert _selected(settled, 0) == str(other) and "Matched by handicap." in settled


def test_a_golfer_read_for_two_rows_is_preselected_for_neither(cards):
    client, ids = cards
    _, review = _review(client, ids, _player("A1"), _player("A1"))
    assert _selected(review, 0) == "" and _selected(review, 1) == ""


def test_only_this_weeks_golfers_are_offered(cards):
    client, ids = cards
    _, review = _review(client, ids, _player("C1"), _player("B1"))
    assert _selected(review, 0) == "" and "C1 Example" not in review  # C1 is not in week 1's matchups


def test_the_out_total_mismatch_shows_the_net_hint_and_still_submits(cards):
    client, ids = cards
    misread = (6, 6, 6, 6, 6, 6, 6, 5, 5)  # adds up to 52
    draft_id, review = _review(client, ids,
                               _player("A1", misread, handicap_written=7, out_total=43), _player("B1"))
    warning = ("A1 Example: the card&#39;s Out total is 43, but the nine scores shown add up to 52. "
               "A cell may have been misread. The card&#39;s total may be the net score.")
    assert review.count(warning) == 2  # listed at the top and beside the row
    assert "Check before you submit" in review and 'href="#player-1"' in review
    assert "Check this player:" in review and "do not stop you submitting" in review
    assert "handicap 7" in review and "Out total 43" in review
    # The first mismatching row takes focus on load.
    assert re.search(r'name="score_0_1"[^>]*autofocus', review) and review.count("autofocus") == 1

    # The admin corrects the cells so the gross is 50, and submits.
    corrected = (6, 6, 6, 6, 6, 5, 5, 5, 5)
    rows = [("A1", ids["golfers"][0], corrected), ("B1", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303
    with Session(_engine(client)) as session:
        total = session.scalar(select(func.sum(HoleScore.strokes)).where(HoleScore.golfer_id == ids["golfers"][0]))
    assert total == 50


def test_a_card_whose_out_total_still_disagrees_can_be_submitted(cards):
    client, ids = cards
    misread = (6, 6, 6, 6, 6, 6, 6, 5, 5)
    draft_id, review = _review(client, ids, _player("A1", misread, out_total=43), _player("B1"))
    assert "Out total is 43" in review
    # The warning is advice: the values on the form are what is saved.
    rows = [("A1", ids["golfers"][0], misread), ("B1", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303 and _score_count(client) == 18


def test_a_mismatch_without_a_written_handicap_has_no_net_hint(cards):
    client, ids = cards
    _, review = _review(client, ids, _player("A1", out_total=47), _player("B1", out_total=45))
    assert "the card&#39;s Out total is 47, but the nine scores shown add up to 45" in review
    assert "may be the net score" not in review
    assert review.count("Check this player:") == 1  # B1's total agrees


def test_a_form_with_errors_stays_at_the_error_summary(cards):
    client, ids = cards
    misread = (6, 6, 6, 6, 6, 6, 6, 5, 5)
    draft_id, review = _review(client, ids, _player("A1", misread, out_total=43), _player("B1"))
    assert review.count("autofocus") == 1
    # The total still disagrees, but there is an error to fix first: nothing takes focus away from it.
    refused = _submit(client, draft_id, [("A1", ids["golfers"][0], misread), ("B1", "", FIVES)])
    assert refused.status_code == 422 and "Out total is 43" in refused.text
    assert "autofocus" not in refused.text


def test_the_out_warning_follows_the_admins_corrections_on_a_re_render(cards):
    client, ids = cards
    draft_id, _ = _review(client, ids, _player("A1", (6,) * 9, out_total=45), _player("B1"))
    fixed = _submit(client, draft_id, [("A1", ids["golfers"][0], FIVES), ("B1", "", FIVES)])
    assert fixed.status_code == 422  # player 2 has no golfer, so the form comes back
    assert "Out total is 45" not in fixed.text  # player 1's scores now add up to 45


def test_a_wrong_nine_header_warns_and_still_submits(cards):
    client, ids = cards
    draft_id, review = _review(client, ids, _player("A1"), _player("B1"), header=tuple(range(10, 19)))
    assert ("The photo shows the back nine (holes 10 to 18), but week 1 is a front-nine week "
            "(holes 1 to 9).") in review
    rows = [("A1", ids["golfers"][0], FIVES), ("B1", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303 and _score_count(client) == 18


def test_a_handicap_mismatch_warns_and_still_submits(cards):
    client, ids = cards
    stored = _snapshot_handicaps(client, ids)[ids["golfers"][0]]
    draft_id, review = _review(client, ids, _player("A1", handicap_written=stored + 5), _player("B1"))
    assert (f"A1 Example: the handicap written on the card is {stored + 5}, but the stored handicap for "
            f"this week is {stored}.") in review
    _, agrees = _review(client, ids, _player("A1", handicap_written=stored), _player("B1"))
    assert "the handicap written on the card" not in agrees
    rows = [("A1", ids["golfers"][0], FIVES), ("B1", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 303
    # No handicap is changed by a card.
    with Session(_engine(client)) as session:
        assert session.scalar(
            select(WeekHandicap.strokes).where(WeekHandicap.golfer_id == ids["golfers"][0])) == stored


def test_more_than_four_players_read_shows_four_and_says_so(cards):
    client, ids = cards
    players = [_player(f"Reader{n}") for n in range(6)] + [_player("", (None,) * 9)]
    _, review = _review(client, ids, *players)
    assert "The reader found 6 players. Only the first 4 are shown" in review
    assert _value(review, "name_3") == "Reader3" and "Reader4" not in review


# --- safety ---------------------------------------------------------------------------------


def test_names_and_model_text_are_escaped(cards):
    client, ids = cards
    _rename(client, ids["golfers"][0], "A1<i>Markup</i>", "O'Example-<b>x</b>")
    draft_id, review = _review(client, ids, _player('<script>alert("x")</script>'), _player("B1"))
    assert "<script>alert" not in review and "<i>Markup</i>" not in review and "<b>x</b>" not in review
    assert "&lt;script&gt;alert" in review and "A1&lt;i&gt;Markup&lt;/i&gt;" in review
    rows = [('<script>alert("x")</script>', ids["golfers"][0], FIVES), ("B1", ids["golfers"][4], FIVES)]
    scores = client.get(_submit(client, draft_id, rows).headers["location"]).text
    assert "<i>Markup</i>" not in scores and "A1&lt;i&gt;Markup&lt;/i&gt;" in scores


def test_no_log_record_carries_a_name_a_score_or_image_bytes(cards, caplog):
    client, ids = cards
    scores = (4, 5, 6, 3, 4, 8, 5, 7, 4)
    with caplog.at_level(logging.DEBUG):
        draft_id, _ = _review(client, ids, _player("Zzyzx", scores), _player("Quibble", scores))
        _submit(client, draft_id, [("Zzyzx", "", scores)])  # refused
        rows = [("Zzyzx", ids["golfers"][0], scores), ("Quibble", ids["golfers"][4], scores)]
        assert _submit(client, draft_id, rows).status_code == 303
        client.get(f"/admin/weeks/{ids['weeks'][0]}/scores")
        failing = FakeProvider(ProviderTimeout())
        client.app.state.vision_provider = failing
        client.post(f"/admin/scorecards/{_draft_id(_upload(client, ids['weeks'][0]))}/discard",
                    data={"csrf_token": _token(client)})
    ours = [r for r in caplog.records if r.name.startswith("golf_league")]
    assert any("scorecard submitted" in r.getMessage() for r in ours)
    text = "\n".join(r.getMessage() for r in ours)
    for forbidden in ("Zzyzx", "Quibble", "A1 Example", "B1 Example", "JFIF", "4, 5, 6", "[4", MODEL):
        assert forbidden not in text


def test_gets_never_change_anything(cards):
    client, ids = cards
    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    before = (_draft_count(client), _photos(client), _score_count(client))
    for path in ("/admin/scorecards/new", f"/admin/scorecards/{draft_id}", f"/admin/scorecards/{draft_id}?manual=1",
                 f"/admin/scorecards/{draft_id}/status", f"/admin/scorecards/{draft_id}/image",
                 f"/admin/weeks/{ids['weeks'][0]}/scores"):
        assert client.get(path).status_code == 200, path
    assert (_draft_count(client), _photos(client), _score_count(client)) == before
    assert client.app.state.vision_provider.calls == 1  # only the upload's background read


def test_unknown_ids_are_404(cards):
    client, ids = cards
    assert client.get("/admin/scorecards/999999").status_code == 404
    assert client.get("/admin/scorecards/999999/status").status_code == 404
    assert client.get("/admin/weeks/999999/scores").status_code == 404
    assert _submit(client, 999999, []).status_code == 404


def test_the_scores_page_with_nothing_saved_says_so(cards):
    client, ids = cards
    page = client.get(f"/admin/weeks/{ids['weeks'][0]}/scores")
    assert page.status_code == 200 and "No scores are saved for this week yet." in page.text


def test_the_admin_menu_links_to_enter_scorecard_and_players_see_no_link(cards):
    client, ids = cards
    assert '<a href="/admin/scorecards/new">Enter scorecard</a>' in client.get("/").text
    _become(client, admin=False)
    for path in ("/", "/roster", "/my/schedule"):
        page = client.get(path).text
        assert "/admin/scorecards" not in page and "Enter scorecard" not in page, path
        assert "/scores" not in page, path


def test_no_season_at_all_says_so(empty_admin_client):
    page = empty_admin_client.get("/admin/scorecards/new")
    assert page.status_code == 200 and "There is no season yet." in page.text


def test_a_drafts_times_come_from_the_injected_clock(cards):
    client, ids = cards
    draft_id = _manual(client, ids["weeks"][0])
    with Session(_engine(client)) as session:
        draft = session.get(ScorecardDraft, draft_id)
        assert draft.created_at == NOW and draft.expires_at == NOW + timedelta(hours=24)


def test_a_purge_failure_never_fails_the_page(cards, monkeypatch, caplog):
    client, ids = cards
    import golf_league.routers.admin_scorecards as router_module

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic purge failure")

    monkeypatch.setattr(router_module, "purge_expired", explode)
    with caplog.at_level(logging.WARNING):
        assert client.get("/admin/scorecards/new").status_code == 200
    assert "scorecard draft purge failed type=RuntimeError" in caplog.text
    assert "synthetic purge failure" not in caplog.text


def test_a_busy_database_is_409_and_keeps_the_draft_and_the_admins_values(cards):
    client, ids = cards
    draft_id = _manual(client, ids["weeks"][0])
    rows = [("", ids["golfers"][0], (7,) * 9), ("", ids["golfers"][4], (7,) * 9)]
    engine = _engine(client)

    @event.listens_for(engine, "connect")
    def _impatient(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA busy_timeout=1")

    engine.dispose()  # new connections only, so the short timeout applies to the request
    with Session(engine) as writer:
        writer.connection().exec_driver_sql("BEGIN IMMEDIATE")
        response = _submit(client, draft_id, rows)
        writer.rollback()
    assert response.status_code == 409 and "Nothing was saved; submit the card again." in response.text
    assert _value(response.text, "score_0_1") == "7"
    assert _score_count(client) == 0 and _draft_count(client) == 1
    assert _submit(client, draft_id, rows).status_code == 303  # the same card goes through afterwards


def test_a_draft_discarded_while_it_is_being_submitted_saves_nothing(cards, monkeypatch):
    client, ids = cards
    import golf_league.routers.admin_scorecards as router_module

    draft_id = _manual(client, ids["weeks"][0])
    real_submit = router_module.submit_card

    def submit_then_lose_the_draft(session, *args, **kwargs):
        result = real_submit(session, *args, **kwargs)
        session.delete(session.get(ScorecardDraft, draft_id))  # as if discarded in the same instant
        session.flush()
        return result

    monkeypatch.setattr(router_module, "submit_card", submit_then_lose_the_draft)
    rows = [("", ids["golfers"][0], FIVES), ("", ids["golfers"][4], FIVES)]
    assert _submit(client, draft_id, rows).status_code == 404
    assert _score_count(client) == 0  # the scores were rolled back with it


def test_a_stale_page_for_a_submitted_card_never_reaches_a_later_card(cards):
    client, ids = cards
    rows = [("", ids["golfers"][0], FIVES), ("", ids["golfers"][4], FIVES)]
    first = _manual(client, ids["weeks"][0])
    assert _submit(client, first, rows).status_code == 303
    second = _manual(client, ids["weeks"][0])
    assert second != first  # a deleted draft's id is not handed to the next card
    # The old page's form, submitted late, finds nothing and leaves the new card alone.
    assert _submit(client, first, [("", ids["golfers"][1], FIVES), ("", ids["golfers"][5], FIVES)]).status_code == 404
    assert client.get(f"/admin/scorecards/{first}/status").status_code == 404
    assert _draft_count(client) == 1 and _score_count(client) == 18


# --- when saving fails -------------------------------------------------------------------------


def test_a_photo_that_cannot_be_removed_stops_the_submit_and_saves_nothing(cards, monkeypatch, caplog):
    client, ids = cards
    import golf_league.routers.admin_scorecards as router_module

    draft_id = _draft_id(_upload(client, ids["weeks"][0]))

    def refuse(session, draft, *, directory):
        raise PermissionError("synthetic: /private/path/must-not-be-logged.jpg")

    monkeypatch.setattr(router_module, "delete_draft", refuse)
    rows = [("A1", ids["golfers"][0], (7,) * 9), ("B1", ids["golfers"][4], (7,) * 9)]
    with caplog.at_level(logging.WARNING):
        response = _submit(client, draft_id, rows)
    assert response.status_code == 503 and "could not be saved just now" in response.text
    assert _value(response.text, "score_0_1") == "7"  # what the admin typed comes back
    assert _score_count(client) == 0 and _draft_count(client) == 1 and len(_photos(client)) == 1
    assert "scorecard submit failed" in caplog.text and "must-not-be-logged" not in caplog.text
    monkeypatch.undo()
    assert _submit(client, draft_id, rows).status_code == 303 and _score_count(client) == 18


def test_a_failed_commit_saves_no_scores_and_brings_the_form_back(cards, monkeypatch):
    client, ids = cards
    import golf_league.routers.admin_scorecards as router_module

    draft_id = _draft_id(_upload(client, ids["weeks"][0]))
    real_delete = router_module.delete_draft

    def delete_then_fail(session, draft, *, directory):
        real_delete(session, draft, directory=directory)  # the photo is removed first, as specified
        raise OperationalError("COMMIT", {}, Exception("synthetic disk failure"))

    monkeypatch.setattr(router_module, "delete_draft", delete_then_fail)
    rows = [("A1", ids["golfers"][0], (7,) * 9), ("B1", ids["golfers"][4], (7,) * 9)]
    response = _submit(client, draft_id, rows)
    assert response.status_code == 503 and _value(response.text, "score_1_9") == "7"
    assert "No photo." in response.text
    # No score was written and the draft row came back with the rollback; only the photo is gone.
    assert _score_count(client) == 0 and _draft_count(client) == 1 and _photos(client) == []
    monkeypatch.undo()
    assert _submit(client, draft_id, rows).status_code == 303 and _score_count(client) == 18


def test_a_busy_database_during_upload_is_a_plain_503_and_keeps_nothing(cards, monkeypatch, caplog):
    client, ids = cards
    import golf_league.routers.admin_scorecards as router_module

    def locked(session, **kwargs):
        raise OperationalError("INSERT INTO scorecard_drafts", {"image_path": "must-not-be-logged.jpg"},
                               Exception("database is locked"))

    monkeypatch.setattr(router_module, "create_draft", locked)
    with caplog.at_level(logging.WARNING):
        response = _upload(client, ids["weeks"][0])
    assert response.status_code == 503 and "could not be started just now" in response.text
    assert _draft_count(client) == 0 and _photos(client) == []
    assert "scorecard draft not created type=OperationalError" in caplog.text
    assert "must-not-be-logged" not in caplog.text
