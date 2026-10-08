"""Persisted quotas share the pure fixed-window policy across connections."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_invites import NOW, SECRET, admission, seed_environment

from golf_league.database import make_engine
from golf_league.models import (
    Base,
    GolferSetPasswordToken,
    InviteRateWindow,
    LeagueInviteLink,
)
from golf_league.services.invites import create_invite, rotate_invite


@pytest.fixture
def env(engine):
    return engine, *seed_environment(engine)


def windows(engine):
    with Session(engine) as session:
        return {(r.tier, r.key_digest): (r.window_start, r.count) for r in session.scalars(select(InviteRateWindow))}


@pytest.mark.parametrize("tier,limit", [("email", 3), ("ip", 10), ("invite", 40), ("global", 100)])
def test_each_tier_nth_allowed_next_refused_with_no_partial_debit(env, tier, limit):
    engine, admin, _, created = env
    parents = [created]
    if tier == "global":
        with Session(engine) as session:
            parents += [create_invite(session, created_by_user_id=admin, label=f"More{i}", now=NOW) for i in range(3)]
            session.commit()
    for i in range(limit):
        email = "golfer0@example.test" if tier == "email" else f"golfer{i}@example.test"
        ip = "192.0.2.1" if tier == "ip" else f"192.0.2.{i}"
        parent = parents[i // 30] if tier == "global" else created
        assert admission(engine, parent, email=email, ip=ip)
    before = windows(engine)
    parent = parents[-1] if tier == "global" else created
    email = "golfer0@example.test" if tier == "email" else f"golfer{limit}@example.test"
    ip = "192.0.2.1" if tier == "ip" else "198.51.100.1"
    assert admission(engine, parent, email=email, ip=ip) is None
    assert windows(engine) == before
    with Session(engine) as session:
        assert session.get(LeagueInviteLink, parent.invite_id).suppressed_count == 1
        assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == limit


@pytest.mark.parametrize("tier,seconds", [("email", 900), ("ip", 3600), ("invite", 3600), ("global", 3600)])
def test_exact_rollover_uses_fresh_window(env, tier, seconds):
    engine, _, _, created = env
    assert admission(engine, created)
    # Seed only the tier under test to its ceiling. Other budgets remain 1.
    with Session(engine) as session:
        row = session.scalar(select(InviteRateWindow).where(InviteRateWindow.tier == tier))
        row.count = {"email": 3, "ip": 10, "invite": 40, "global": 100}[tier]
        session.commit()
    assert admission(engine, created, now=NOW + timedelta(seconds=seconds - .001)) is None
    assert admission(engine, created, now=NOW + timedelta(seconds=seconds))
    with Session(engine) as session:
        row = session.scalar(select(InviteRateWindow).where(InviteRateWindow.tier == tier))
        assert row.window_start == (NOW + timedelta(seconds=seconds)).timestamp()
        assert row.count == 1


def test_unknown_does_not_start_window_but_can_be_suppressed(env):
    engine, _, _, created = env
    for bad in ("unknown@example.test", "malformed"):
        assert admission(engine, created, email=bad) is None
    assert windows(engine) == {}
    for i in range(10):
        assert admission(engine, created, email=f"golfer{i}@example.test")
    before = windows(engine)
    assert admission(engine, created, email="unknown@example.test") is None
    assert windows(engine) == before
    with Session(engine) as session:
        assert session.get(LeagueInviteLink, created.invite_id).suppressed_count == 1
        assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 10


def test_rotation_keeps_email_ip_global_and_restarts_only_invite(env):
    engine, admin, _, created = env
    for _ in range(3):
        assert admission(engine, created)
    before = windows(engine)
    with Session(engine) as session:
        replacement = rotate_invite(session, invite_id=created.invite_id, created_by_user_id=admin, now=NOW)
        session.commit()
    assert admission(engine, replacement) is None
    assert windows(engine) == before
    assert admission(engine, replacement, email="golfer1@example.test")
    after = windows(engine)
    assert len([key for key in after if key[0] == "invite"]) == 2
    for key in before:
        if key[0] in ("ip", "global"):
            assert after[key][1] == before[key][1] + 1
        else:
            assert after[key] == before[key]


def test_hmac_keys_hide_cleartext_and_purpose_separate(env):
    engine, _, _, created = env
    assert admission(engine, created)
    rows = windows(engine)
    assert len(rows) == 4
    keys = [key[1] for key in rows]
    assert len(set(keys)) == 4 and all(len(key) == 64 for key in keys)
    assert "golfer0@example.test" not in repr(rows)
    assert "192.0.2.1" not in repr(rows) and SECRET not in repr(rows)


def test_rollback_leaves_no_partial_quota_token_revocation_or_counter(env):
    engine, _, _, created = env
    first = admission(engine, created)
    before = windows(engine)
    assert admission(engine, created, commit=False)
    assert windows(engine) == before
    with Session(engine) as session:
        assert session.get(GolferSetPasswordToken, first.token_id).revoked_at is None
        assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 1
    assert admission(engine, created)
    assert admission(engine, created)
    assert admission(engine, created, commit=False) is None
    with Session(engine) as session:
        assert session.get(LeagueInviteLink, created.invite_id).suppressed_count == 0


def test_invalid_parent_has_no_effect(env):
    engine, _, _, created = env
    assert admission(engine, created, now=NOW + timedelta(days=30)) is None
    assert windows(engine) == {}
    with Session(engine) as session:
        assert session.get(LeagueInviteLink, created.invite_id).suppressed_count == 0


def test_independent_engines_recreation_and_concurrent_final_reservation(tmp_path):
    url = f"sqlite:///{tmp_path / 'quotas.db'}"
    first_engine = make_engine(url)
    Base.metadata.create_all(first_engine)
    _, _, created = seed_environment(first_engine)
    assert admission(first_engine, created)
    assert admission(first_engine, created)
    first_engine.dispose()
    second_engine = make_engine(url)
    third_engine = make_engine(url)
    try:
        # Every worker owns a different connection/Session and starts immediate
        # before reads. Eight race for the single remaining email reservation.
        def attempt(i):
            return admission(second_engine if i % 2 else third_engine, created)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        assert sum(result is not None for result in outcomes) == 1
        with Session(second_engine) as session:
            counts = {r.tier: r.count for r in session.scalars(select(InviteRateWindow))}
            assert counts == {"email": 3, "ip": 3, "invite": 3, "global": 3}
            assert session.get(LeagueInviteLink, created.invite_id).suppressed_count == 7
            assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 3
        second_engine.dispose()
        recreated = make_engine(url)
        try:
            assert admission(recreated, created) is None
            with Session(recreated) as session:
                assert session.get(LeagueInviteLink, created.invite_id).suppressed_count == 8
        finally:
            recreated.dispose()
    finally:
        second_engine.dispose()
        third_engine.dispose()


def test_concurrent_callbacks_do_not_lose_counts(tmp_path):
    from golf_league.services.invites import record_send_failure, record_send_success

    url = f"sqlite:///{tmp_path / 'callbacks.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    _, _, created = seed_environment(engine)
    deliveries = [admission(engine, created, email=f"golfer{i}@example.test") for i in range(8)]
    try:
        def callback(i):
            with Session(engine) as session:
                session.execute(text("BEGIN IMMEDIATE"))
                method = record_send_success if i % 2 else record_send_failure
                method(session, token_id=deliveries[i].token_id, now=NOW)
                session.commit()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(callback, range(8)))
        with Session(engine) as session:
            row = session.get(LeagueInviteLink, created.invite_id)
            assert row.send_count == row.failed_send_count == 4
            assert row.suppressed_count == 0
    finally:
        engine.dispose()
