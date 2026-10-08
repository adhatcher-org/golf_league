import asyncio
import re
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from golf_league.models import (
    Golfer,
    GolferSetPasswordToken,
    InviteRateWindow,
    LeagueInviteLink,
    User,
)
from golf_league.services.auth import hash_password
from golf_league.services.invites import admit_join_send, create_invite

NEUTRAL_TEXT = "If that address is on the league roster, we've sent it a link."


class _ASGIFrameRecorder:
    def __init__(self, app):
        self.app = app
        self.state = app.state
        self.events = []

    async def __call__(self, scope, receive, send):
        async def recorded_send(message):
            if scope["type"] == "http" and message["type"] in {"http.response.start", "http.response.body"}:
                if message["type"] == "http.response.start" or not message.get("more_body", False):
                    self.events.append((message["type"], time.perf_counter()))
            await send(message)

        await self.app(scope, receive, recorded_send)


class _TrustedClientScope:
    """Stand-in for trusted proxy middleware populating request.client."""

    def __init__(self, app, client_host):
        self.app = app
        self.state = app.state
        self.client_host = client_host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self.client_host, 443)
        await self.app(scope, receive, send)


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _invite(client):
    with Session(bind=client.app.state.engine) as session:
        admin = User(
            username="admin@example.test", email="admin@example.test", display_name="Admin",
            password_hash=hash_password("synthetic-password"), email_verified_at=datetime.now(UTC).replace(tzinfo=None),
            is_admin=True,
        )
        session.add(admin)
        session.flush()
        invite = create_invite(session, created_by_user_id=admin.id, label="Synthetic", now=datetime.now(UTC))
        session.commit()
        return invite.raw_token


def _golfer(client, email="player@example.test", *, active=True):
    with Session(bind=client.app.state.engine) as session:
        tee_set_id = session.scalar(select(Golfer.default_tee_set_id).where(Golfer.default_tee_set_id.is_not(None)))
        if tee_set_id is None:
            from golf_league.models import TeeSet
            tee_set_id = session.scalar(select(TeeSet.id).limit(1))
        golfer = Golfer(
            first_name="Synthetic", last_name="Player", email=email, phone=None,
            default_tee_set_id=tee_set_id, handicap_source="self_reported",
            handicap_status="ok", is_active=active,
        )
        session.add(golfer)
        session.commit()


def test_join_email_and_set_password_create_one_verified_linked_user(client, caplog):
    invite = _invite(client)
    _golfer(client)
    page = client.get(f"/join/{invite}")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert 'autocomplete="off"' in page.text
    assert "Synthetic" not in page.text
    assert 'name="first_name"' not in page.text
    csrf = _csrf(page.text)

    submitted = client.post(
        f"/join/{invite}",
        data={"email": "  PLAYER@EXAMPLE.TEST ", "csrf_token": csrf},
        headers={"host": "attacker.invalid", "x-forwarded-host": "evil.invalid", "x-forwarded-for": "203.0.113.9"},
    )
    assert submitted.status_code == 200
    assert submitted.text.count("If that address is on the league roster, we've sent it a link.") == 1
    assert submitted.headers["cache-control"] == "no-store"
    assert submitted.headers["referrer-policy"] == "no-referrer"
    sent = client.app.state.email_sender.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "player@example.test"
    assert sent[0]["body"].startswith("Use this link")
    assert "https://golfleague.aaronhatcher.com/set-password/" in sent[0]["body"]

    raw = sent[0]["body"].rsplit("/set-password/", 1)[1]
    assert "player@example.test" not in caplog.text
    assert raw not in caplog.text
    set_page = client.get(f"/set-password/{raw}")
    assert set_page.status_code == 200
    assert "autocomplete=\"new-password\"" in set_page.text
    set_csrf = _csrf(set_page.text)
    done = client.post(
        f"/set-password/{raw}", data={"password": "synthetic-password-2", "csrf_token": set_csrf},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/"
    assert done.cookies.get("session")
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 2
        user = session.scalar(select(User).where(User.email == "player@example.test"))
        golfer = session.scalar(select(Golfer).where(Golfer.email == "player@example.test"))
        token = session.scalar(select(GolferSetPasswordToken))
        invite_row = session.scalar(select(LeagueInviteLink))
        assert user.golfer_id == golfer.id
        assert user.email_verified_at is not None and not user.is_admin
        assert token.consumed_at is not None
        assert invite_row.send_count == 1


def test_join_completion_establishes_session_for_scheduled_my_schedule(client):
    from test_my_schedule import _seed

    ids = _seed(client)
    with Session(bind=client.app.state.engine) as session:
        user = session.scalar(select(User).where(User.email == "viewer@example.test"))
        golfer = session.get(Golfer, ids["golfers"][0])
        golfer.email = user.email
        invite = create_invite(session, created_by_user_id=user.id, label="Synthetic schedule", now=datetime.now(UTC))
        invite_raw = invite.raw_token
        session.commit()
    page = client.get(f"/join/{invite_raw}")
    request = client.post(
        f"/join/{invite_raw}",
        data={"email": "viewer@example.test", "csrf_token": _csrf(page.text)},
    )
    assert request.status_code == 200
    delivery = client.app.state.email_sender.sent[-1]
    raw = delivery["body"].rsplit("/set-password/", 1)[1]
    form = client.get(f"/set-password/{raw}")
    completed = client.post(
        f"/set-password/{raw}",
        data={"password": "schedule-access-password", "csrf_token": _csrf(form.text)},
        follow_redirects=False,
    )
    assert completed.status_code == 303
    # The fixture's external base URL marks session cookies Secure; TestClient
    # uses HTTP, so explicitly present the issued cookie as a browser would over HTTPS.
    client.cookies.set("session", completed.cookies["session"])
    schedule = client.get("/my/schedule")
    assert schedule.status_code == 200
    assert "My Schedule" in schedule.text and "A1 Example" in schedule.text
    assert "B1 Example" in schedule.text


@pytest.mark.parametrize("csrf_state", ["missing", "malformed", "foreign"])
def test_join_csrf_failure_is_private_and_never_sends(client, csrf_state):
    invite = _invite(client)
    client.get(f"/join/{invite}")
    data = {"email": "player@example.test"}
    if csrf_state == "malformed":
        data["csrf_token"] = "malformed"
    elif csrf_state == "foreign":
        from golf_league.security import generate_csrf_token

        data["csrf_token"] = generate_csrf_token("different-csrf-seed")
    response = client.post(f"/join/{invite}", data=data)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert client.app.state.email_sender.sent == []
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 0


@pytest.mark.parametrize("branch", ["invalid_parent", "capacity", "inactive", "identity_drift", "phone_challenge", "suppressed"])
def test_join_neutral_frame_matches_across_invalid_and_ineligible_branches(client, branch):
    invite = _invite(client)
    email = f"neutral-{branch}@example.test"
    page = client.get(f"/join/{invite}")
    payload = {"email": email, "csrf_token": _csrf(page.text)}
    cookie_snapshot = dict(client.cookies)
    baseline = client.post(f"/join/{invite}", data=payload)
    assert baseline.status_code == 200

    if branch in {"capacity", "inactive", "identity_drift", "suppressed"}:
        _golfer(client, email, active=branch != "inactive")
    if branch == "capacity":
        with Session(bind=client.app.state.engine) as session:
            password_hash = hash_password("synthetic-neutral-capacity")
            for number in range(149):
                value = f"neutral-capacity-{number}@example.test"
                session.add(User(
                    username=value, email=value, display_name="Neutral Capacity",
                    password_hash=password_hash, is_admin=False,
                ))
            session.commit()
    elif branch == "identity_drift":
        with Session(bind=client.app.state.engine) as session:
            golfer = session.scalar(select(Golfer).where(Golfer.email == email))
            session.add(User(
                username="unrelated-identity@example.test", email="unrelated-identity@example.test",
                display_name="Unrelated", password_hash=hash_password("synthetic-unrelated-password"),
                golfer_id=golfer.id,
            ))
            session.commit()
    elif branch == "phone_challenge":
        with Session(bind=client.app.state.engine) as session:
            row = session.scalar(select(LeagueInviteLink))
            row.require_phone_last_four = True
            session.commit()
    elif branch == "suppressed":
        now = datetime.now(UTC)
        for _ in range(3):
            with Session(bind=client.app.state.engine, autoflush=False) as session:
                session.execute(text("BEGIN IMMEDIATE"))
                delivery = admit_join_send(
                    session, raw_invite_token=invite, email=email, client_ip="quota-prime",
                    secret=client.app.state.settings.session_secret, now=now,
                )
                assert delivery is not None
                session.commit()

    path = "/join/unknown-parent" if branch == "invalid_parent" else f"/join/{invite}"
    after = client.post(path, data=payload)
    assert (after.status_code, after.content, dict(after.headers)) == (
        baseline.status_code, baseline.content, dict(baseline.headers),
    )
    assert dict(client.cookies) == cookie_snapshot
    assert after.status_code == 200 and NEUTRAL_TEXT.encode() in after.content
    assert client.app.state.email_sender.sent == []


def test_trusted_proxy_client_scope_controls_ip_quota_not_forwarded_headers(tmp_path):
    from fastapi.testclient import TestClient

    from golf_league.app import create_app
    from golf_league.config import Settings

    app = create_app(Settings(
        database_url=f"sqlite:///{tmp_path / 'trusted-proxy-ip.db'}",
        session_secret="synthetic-proxy-scope-secret",
    ))
    wrapped = _TrustedClientScope(app, "198.51.100.17")
    with TestClient(wrapped) as client:
        invite = _invite(client)
        emails = [f"proxy-{index}@example.test" for index in range(11)]
        for email in emails:
            _golfer(client, email)
        page = client.get(f"/join/{invite}")
        csrf = _csrf(page.text)
        responses = [client.post(
            f"/join/{invite}", data={"email": email, "csrf_token": csrf},
            headers={"x-forwarded-for": f"203.0.113.{index + 1}"},
        ) for index, email in enumerate(emails)]
        assert all(response.status_code == 200 for response in responses)
        assert len(client.app.state.email_sender.sent) == 10
        with Session(bind=client.app.state.engine) as session:
            assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 10
            ip_window = session.scalar(select(InviteRateWindow).where(InviteRateWindow.tier == "ip"))
            assert ip_window.count == 10
            row = session.scalar(select(LeagueInviteLink))
            assert row.suppressed_count == 1


def test_full_account_capacity_suppresses_issuance_without_debit(client):
    invite = _invite(client)
    _golfer(client)
    with Session(bind=client.app.state.engine) as session:
        password_hash = hash_password("synthetic-capacity-password")
        for number in range(149):
            email = f"full-cap-{number}@example.test"
            session.add(User(
                username=email, email=email, display_name=f"Cap {number}",
                password_hash=password_hash, is_admin=False,
            ))
        session.commit()
        assert session.scalar(select(func.count()).select_from(User)) == 150
    page = client.get(f"/join/{invite}")
    response = client.post(
        f"/join/{invite}",
        data={"email": "player@example.test", "csrf_token": _csrf(page.text)},
    )
    assert response.status_code == 200
    assert NEUTRAL_TEXT in response.text
    assert client.app.state.email_sender.sent == []
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 0
        assert session.scalar(select(func.count()).select_from(InviteRateWindow)) == 0
        invite_row = session.scalar(select(LeagueInviteLink).where(LeagueInviteLink.token_hash.is_not(None)).limit(1))
        assert invite_row.suppressed_count == 0


def test_join_invalid_parent_is_neutral_and_does_not_debit_or_send(client):
    page = client.get("/login")
    response = client.post(
        "/join/unknown-parent",
        data={"email": "unknown@example.test", "csrf_token": _csrf(page.text)},
    )
    assert response.status_code == 200
    assert "If that address is on the league roster, we've sent it a link." in response.text
    assert client.app.state.email_sender.sent == []
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 0


def test_join_floor_sleeps_only_remaining_time_on_valid_csrf_branch(client):
    seed_page = client.get("/login")
    ticks = iter((10.0, 10.025))
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    client.app.state.join_monotonic = lambda: next(ticks)
    client.app.state.join_sleep = fake_sleep
    response = client.post(
        "/join/unknown-parent",
        data={"email": "ignored@example.test", "csrf_token": _csrf(seed_page.text)},
    )
    client.app.state.join_monotonic = time.monotonic
    client.app.state.join_sleep = asyncio.sleep
    assert response.status_code == 200
    assert len(slept) == 1
    assert round(slept[0], 3) == 0.075


def test_send_failure_keeps_reservation_and_revokes_credential(client, caplog):
    invite = _invite(client)
    _golfer(client)
    page = client.get(f"/join/{invite}")
    csrf = _csrf(page.text)

    def fail_send(**_kwargs):
        raise RuntimeError("synthetic transport error")

    client.app.state.email_sender.send = fail_send
    response = client.post(
        f"/join/{invite}",
        data={"email": "player@example.test", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert NEUTRAL_TEXT in response.text
    assert "shared invite email send failed" in caplog.text
    assert "player@example.test" not in caplog.text
    with Session(bind=client.app.state.engine) as session:
        token = session.scalar(select(GolferSetPasswordToken))
        invite_row = session.scalar(select(LeagueInviteLink))
        assert token.revoked_at is not None
        assert invite_row.failed_send_count == 1
        assert invite_row.send_count == 0


def test_outcome_recording_failure_never_reinvokes_sender_or_fabricates_failure(client, monkeypatch, caplog):
    import golf_league.routers.join as join_routes

    invite = _invite(client)
    _golfer(client)
    page = client.get(f"/join/{invite}")
    csrf = _csrf(page.text)

    def fail_recording(*_args, **_kwargs):
        raise RuntimeError("synthetic database error")

    monkeypatch.setattr(join_routes, "record_send_success", fail_recording)
    response = client.post(
        f"/join/{invite}", data={"email": "player@example.test", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert len(client.app.state.email_sender.sent) == 1
    assert "shared invite outcome recording failed" in caplog.text
    assert "player@example.test" not in caplog.text
    with Session(bind=client.app.state.engine) as session:
        token = session.scalar(select(GolferSetPasswordToken))
        invite_row = session.scalar(select(LeagueInviteLink))
        assert token.revoked_at is None
        assert invite_row.failed_send_count == 0
        assert invite_row.send_count == 0


def test_asgi_frames_meet_floor_and_precede_blocked_fake_sender(tmp_path):
    from golf_league.app import create_app
    from golf_league.config import Settings

    app = create_app(Settings(
        database_url=f"sqlite:///{tmp_path / 'frame-test.db'}",
        session_secret="synthetic-frame-secret",
    ))
    recorder = _ASGIFrameRecorder(app)
    with TestClient(recorder) as client:
        page = client.get("/login")
        csrf = _csrf(page.text)
        clock_values = []

        def measured_clock():
            value = time.perf_counter()
            clock_values.append(value)
            return value

        client.app.state.join_monotonic = measured_clock
        recorder.events.clear()
        response = client.post(
            "/join/unknown-parent",
            data={"email": "unknown@example.test", "csrf_token": csrf},
        )
        assert response.status_code == 200
        response_frames = [event for event in recorder.events if event[0] in {"http.response.start", "http.response.body"}]
        assert [event[0] for event in response_frames[-2:]] == ["http.response.start", "http.response.body"]
        assert response_frames[-1][1] - clock_values[0] >= 0.095

        invite = _invite(client)
        _golfer(client)
        join_page = client.get(f"/join/{invite}")
        csrf = _csrf(join_page.text)
        sender_started = threading.Event()
        sender_release = threading.Event()

        class BlockingFakeSender:
            def send(self, **_message):
                recorder.events.append(("sender", time.perf_counter()))
                sender_started.set()
                sender_release.wait(timeout=5)

        client.app.state.email_sender = BlockingFakeSender()
        recorder.events.clear()
        output = []

        def submit():
            output.append(client.post(
                f"/join/{invite}",
                data={"email": "player@example.test", "csrf_token": csrf},
            ))

        worker = threading.Thread(target=submit)
        worker.start()
        assert sender_started.wait(timeout=3)
        event_types = [event[0] for event in recorder.events]
        assert "http.response.body" in event_types
        assert event_types.index("http.response.body") < event_types.index("sender")
        sender_release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert output[0].status_code == 200


@pytest.mark.parametrize("tier", ["email", "ip", "invite", "global"])
def test_each_quota_tier_keeps_the_same_neutral_http_frame(client, tier, caplog):
    parent = _invite(client)
    join_page = client.get(f"/join/{parent}")
    csrf = _csrf(join_page.text)
    payload = {"email": "no-roster-match@example.test", "csrf_token": csrf}
    before = client.post(f"/join/{parent}", data=payload)
    assert before.status_code == 200
    cookie_snapshot = dict(client.cookies)

    now = datetime.now(UTC)
    windows = {"email": 3, "ip": 10, "invite": 40, "global": 100}
    tiers = [parent]
    with Session(bind=client.app.state.engine) as session:
        admin = session.scalar(select(User).where(User.is_admin.is_(True)))
        if tier == "global":
            tiers = [parent]
            tiers.extend(
                create_invite(session, created_by_user_id=admin.id, label=f"Quota {index}", now=now).raw_token
                for index in range(2)
            )
        emails = [f"quota-{tier}-{index}@example.test" for index in range(windows[tier])]
        if tier == "email":
            emails = [payload["email"]] * windows[tier]
        for email in set(emails):
            session.add(Golfer(
                first_name="Quota", last_name="Player", email=email,
                handicap_source="self_reported", handicap_status="ok",
            ))
        session.commit()

    for index, email in enumerate(emails):
        if tier == "email":
            client_ip = f"quota-email-client-{index}"
            invite_index = 0
        elif tier == "ip":
            client_ip = "testclient"
            invite_index = 0
        elif tier == "invite":
            client_ip = f"quota-invite-client-{index}"
            invite_index = 0
        else:
            client_ip = f"quota-global-client-{index}"
            invite_index = min(index // 34, 2)
        with Session(bind=client.app.state.engine, autoflush=False) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            admitted = admit_join_send(
                session,
                raw_invite_token=tiers[invite_index],
                email=email,
                client_ip=client_ip,
                secret=client.app.state.settings.session_secret,
                now=now,
            )
            assert admitted is not None
            session.commit()
    after = client.post(f"/join/{parent}", data=payload)
    assert after.status_code == 200
    assert after.content == before.content
    assert dict(after.headers) == dict(before.headers)
    assert dict(client.cookies) == cookie_snapshot
    assert "no-roster-match@example.test" not in caplog.text
    assert parent not in caplog.text
    with Session(bind=client.app.state.engine) as session:
        invite_row = session.scalar(select(LeagueInviteLink).where(LeagueInviteLink.token_hash.is_not(None)).order_by(LeagueInviteLink.id).limit(1))
        assert invite_row.suppressed_count == 1
    assert client.app.state.email_sender.sent == []
