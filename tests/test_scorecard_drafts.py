"""GL-81: scorecard drafts, background extraction and deletion.

Synthetic data only: images are generated in memory, names are invented, and the
provider is always a fake. Nothing here touches a network.
"""

import io
import logging
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_matchup_generation import create_generated, seed_fixture
from test_matchup_generation import matchup_db as _matchup_db

import golf_league.app as app_module
from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.domain.scorecard import Actor
from golf_league.models import Hole, HoleScore, HoleYardage, ScorecardDraft, User, Week
from golf_league.services import scorecard_drafts
from golf_league.services.scorecard_drafts import (
    create_draft,
    create_manual_draft,
    delete_draft,
    discard_draft,
    draft_card,
    get_draft,
    image_file,
    list_open_drafts,
    mark_interrupted,
    purge_expired,
    run_extraction,
    scorecard_directory,
    start_extraction,
)
from golf_league.services.scorecard_image import ImageRefused
from golf_league.services.scorecard_vision import (
    ExtractedCard,
    ExtractedPlayer,
    ProviderOutputError,
    ProviderTimeout,
    ProviderUnavailable,
)
from golf_league.services.scores import ScoreEntryError

matchup_db = _matchup_db

TODAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
NAME = "Zzyzx Quibble"
SCORES = (4, 5, 6, 3, 4, None, 5, 7, 4)
UNCERTAIN = (False,) * 5 + (True,) + (False,) * 3
CARD = ExtractedCard(
    players=(ExtractedPlayer(NAME, SCORES, UNCERTAIN, handicap_written=8, out_total=43),),
    model_name="example-model:1b",
    holes_header=tuple(range(1, 10)),
)


def clock():
    return NOW


def _jpeg(size=(320, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 90, 160)).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeProvider:
    """Returns a canned card or raises; records every call."""

    def __init__(self, result=CARD):
        self.result = result
        self.calls: list[tuple[bytes, int]] = []
        self.during_call = None

    def read_scorecard(self, image_jpeg: bytes, *, hole_count: int) -> ExtractedCard:
        self.calls.append((image_jpeg, hole_count))
        if self.during_call is not None:
            self.during_call()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class InlineExecutor:
    """Runs a submitted job at once, on the calling thread."""

    def __init__(self):
        self.submitted = 0

    def submit(self, function, *args, **kwargs):
        self.submitted += 1
        function(*args, **kwargs)


class _State:
    """The slice of `app.state` that `start_extraction` reads."""

    def __init__(self, engine, directory, provider):
        self.engine = engine
        self.scorecard_dir = directory
        self.vision_provider = provider
        self.scorecard_executor = InlineExecutor()


@pytest.fixture
def db(matchup_db, tmp_path):
    """An engine with week 1 generated, an admin user and a scorecard directory."""
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids, week_index=0)
        user = User(username="admin@example.test", email="admin@example.test", display_name="Admin",
                    password_hash="x")
        session.add(user)
        session.commit()
        ids = {**ids, "user": user.id, "week": ids["weeks"][0]}
    return engine, ids, tmp_path / "data" / "scorecards"


def _factory(engine):
    return lambda: Session(bind=engine)


def _create(engine, ids, directory, **overrides) -> int:
    values = {"week_id": ids["week"], "user_id": ids["user"], "raw": _jpeg(), "today": TODAY,
              "clock": clock, "directory": directory, "actor": Actor(ids["user"], True)}
    values.update(overrides)
    with Session(engine) as session:
        return create_draft(session, **values).id


def _draft(engine, draft_id) -> ScorecardDraft | None:
    with Session(engine) as session:
        return session.get(ScorecardDraft, draft_id)


def _count(engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count(ScorecardDraft.id)))


def _files(directory) -> list[str]:
    return sorted(os.listdir(directory)) if directory.exists() else []


# --- the directory ------------------------------------------------------------------


def test_the_scorecard_directory_is_beside_the_managed_file(tmp_path):
    assert scorecard_directory(tmp_path / "data" / ".env") == tmp_path / "data" / "scorecards"
    assert scorecard_directory("/app/data/.env").as_posix() == "/app/data/scorecards"


@pytest.mark.parametrize("name", [None, "", "../.env", "/etc/passwd", "a.jpg", "A" * 32 + ".jpg",
                                  "0" * 32 + ".jpg/../x", "0" * 32 + ".png", " " + "0" * 32 + ".jpg"])
def test_only_names_this_module_writes_resolve_to_a_file(tmp_path, name):
    assert image_file(tmp_path, name) is None


def test_a_written_name_resolves_inside_the_directory(tmp_path):
    assert image_file(tmp_path, "0123456789abcdef" * 2 + ".jpg") == tmp_path / ("0123456789abcdef" * 2 + ".jpg")


# --- create_draft ----------------------------------------------------------------------


def test_create_draft_stores_a_private_photo_and_an_extracting_row(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    draft = _draft(engine, draft_id)
    assert draft.status == "extracting" and draft.week_id == ids["week"]
    assert draft.created_by_user_id == ids["user"]
    assert draft.created_at == NOW and draft.expires_at == NOW + timedelta(hours=24)
    assert draft.extracted_json is None and draft.model_name is None and draft.error_code is None

    assert _files(directory) == [draft.image_path]
    path = image_file(directory, draft.image_path)
    assert path.parent == directory
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert Image.open(path).format == "JPEG"
    # The stored name is random: 32 hex digits, nothing taken from the upload.
    assert len(draft.image_path) == 36 and draft.image_path.endswith(".jpg")
    assert int(draft.image_path[:32], 16) >= 0


def test_two_uploads_of_the_same_photo_get_different_names(db):
    engine, ids, directory = db
    first, second = _create(engine, ids, directory), _create(engine, ids, directory)
    assert _draft(engine, first).image_path != _draft(engine, second).image_path
    assert len(_files(directory)) == 2


@pytest.mark.parametrize("case,code", [("player_too_early", "not_open_yet"), ("cancelled", "cancelled"),
                                       ("played", "played"), ("no_matchup", "no_matchup"),
                                       ("missing", "not_found")])
def test_create_draft_refuses_a_week_that_cannot_take_a_card(db, case, code):
    engine, ids, directory = db
    week_id, today, actor = ids["week"], TODAY, Actor(ids["user"], True)
    if case == "player_too_early":
        today, actor = date(2026, 8, 26), Actor(ids["user"], False, ids["golfers"][0])
    elif case == "no_matchup":
        week_id = ids["weeks"][1]
    elif case == "missing":
        week_id = 999999
    else:
        with Session(engine) as session:
            session.get(Week, week_id).status = case
            session.commit()
    with pytest.raises(ScoreEntryError) as caught:
        _create(engine, ids, directory, week_id=week_id, today=today, actor=actor)
    assert caught.value.code == code
    assert _count(engine) == 0 and _files(directory) == []


def test_a_draft_can_be_started_after_the_play_date(db):
    engine, ids, directory = db
    for today in (date(2026, 8, 28), date(2026, 10, 1)):
        draft_id = _create(engine, ids, directory, today=today)
        assert _draft(engine, draft_id).status == "extracting"
    player = Actor(ids["user"], False, ids["golfers"][0])
    assert _draft(engine, _create(engine, ids, directory, today=date(2026, 8, 28), actor=player)) is not None


def test_create_draft_refuses_a_bad_image_without_writing(db):
    engine, ids, directory = db
    with pytest.raises(ImageRefused) as caught:
        _create(engine, ids, directory, raw=b"not an image at all")
    assert caught.value.code == "unsupported_type"
    assert _count(engine) == 0 and _files(directory) == []


def test_the_week_is_checked_before_the_image(db):
    engine, ids, directory = db
    with pytest.raises(ScoreEntryError):
        _create(engine, ids, directory, week_id=ids["weeks"][1], raw=b"not an image")


def test_a_failed_row_insert_removes_the_photo(db, monkeypatch):
    engine, ids, directory = db
    with Session(engine) as session:
        def fail():
            raise RuntimeError("synthetic commit failure")
        monkeypatch.setattr(session, "commit", fail)
        with pytest.raises(RuntimeError):
            create_draft(session, week_id=ids["week"], user_id=ids["user"], raw=_jpeg(), today=TODAY,
                         clock=clock, directory=directory, actor=Actor(ids["user"], True))
    assert _count(engine) == 0 and _files(directory) == []


def test_create_manual_draft_is_ready_with_no_photo(db):
    engine, ids, directory = db
    with Session(engine) as session:
        draft_id = create_manual_draft(session, week_id=ids["week"], user_id=ids["user"], today=TODAY,
                                       clock=clock, actor=Actor(ids["user"], True)).id
    draft = _draft(engine, draft_id)
    assert draft.status == "ready" and draft.image_path is None and not draft.extracted_json
    assert draft.expires_at == NOW + timedelta(hours=24)
    assert draft_card(draft) is None
    assert _files(directory) == []
    with Session(engine) as session, pytest.raises(ScoreEntryError):
        create_manual_draft(session, week_id=ids["weeks"][1], user_id=ids["user"], today=TODAY, clock=clock,
                            actor=Actor(ids["user"], True))


# --- run_extraction -----------------------------------------------------------------------


def test_extraction_stores_the_reading_and_the_model_name(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    provider = FakeProvider()
    run_extraction(_factory(engine), draft_id, provider, directory=directory)

    draft = _draft(engine, draft_id)
    assert draft.status == "ready" and draft.error_code is None
    assert draft.model_name == "example-model:1b"
    assert draft_card(draft) == CARD
    ((image, hole_count),) = provider.calls
    assert hole_count == 9 and Image.open(io.BytesIO(image)).format == "JPEG"
    assert _files(directory) == [draft.image_path]  # the photo stays for the review page


@pytest.mark.parametrize("error,code", [
    (ProviderUnavailable(), "provider_unavailable"),
    (ProviderUnavailable("provider_http_status"), "provider_http_status"),
    (ProviderTimeout(), "provider_timeout"),
    (ProviderOutputError("output_not_json"), "output_not_json"),
])
def test_a_provider_error_fails_the_draft_with_its_code(db, error, code):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    run_extraction(_factory(engine), draft_id, FakeProvider(error), directory=directory)
    draft = _draft(engine, draft_id)
    assert (draft.status, draft.error_code) == ("failed", code)
    assert draft.extracted_json is None and draft.model_name is None


def test_an_unexpected_exception_in_the_provider_does_not_escape(db, caplog):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    with caplog.at_level(logging.DEBUG):
        run_extraction(_factory(engine), draft_id, FakeProvider(RuntimeError("SECRET-MODEL-TEXT")),
                       directory=directory)
    draft = _draft(engine, draft_id)
    assert (draft.status, draft.error_code) == ("failed", "extraction_failed")
    assert "SECRET-MODEL-TEXT" not in caplog.text


def test_a_draft_discarded_during_the_read_stays_discarded(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    provider = FakeProvider()

    def discard():
        with Session(engine) as session:
            assert discard_draft(session, draft_id, directory=directory)

    provider.during_call = discard
    run_extraction(_factory(engine), draft_id, provider, directory=directory)
    assert _count(engine) == 0 and _files(directory) == []


def test_a_draft_failed_during_the_read_is_not_overwritten_by_a_late_result(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    provider = FakeProvider()

    def interrupt():
        with Session(engine) as session:
            assert mark_interrupted(session) == 1

    provider.during_call = interrupt
    run_extraction(_factory(engine), draft_id, provider, directory=directory)
    draft = _draft(engine, draft_id)
    assert (draft.status, draft.error_code) == ("failed", "interrupted")
    assert draft.extracted_json is None and draft.model_name is None


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_a_draft_that_is_not_extracting_is_left_alone(db, status):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    with Session(engine) as session:
        session.get(ScorecardDraft, draft_id).status = status
        session.commit()
    provider = FakeProvider()
    run_extraction(_factory(engine), draft_id, provider, directory=directory)
    run_extraction(_factory(engine), 999999, provider, directory=directory)
    assert provider.calls == [] and _draft(engine, draft_id).status == status


def test_a_missing_photo_fails_the_draft_without_calling_the_provider(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    image_file(directory, _draft(engine, draft_id).image_path).unlink()
    provider = FakeProvider()
    run_extraction(_factory(engine), draft_id, provider, directory=directory)
    assert provider.calls == []
    assert (_draft(engine, draft_id).status, _draft(engine, draft_id).error_code) == ("failed", "draft_unreadable")


def test_a_week_with_no_holes_fails_the_draft_without_calling_the_provider(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    with Session(engine) as session:
        session.execute(delete(HoleYardage))
        session.execute(delete(Hole))
        session.commit()
    provider = FakeProvider()
    run_extraction(_factory(engine), draft_id, provider, directory=directory)
    assert provider.calls == [] and _draft(engine, draft_id).error_code == "draft_unreadable"


def test_a_database_that_cannot_be_opened_does_not_escape(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)

    def broken():
        raise RuntimeError("synthetic database failure")

    run_extraction(broken, draft_id, FakeProvider(), directory=directory)
    assert _draft(engine, draft_id).status == "extracting"


def test_storing_the_result_is_retried_when_the_writer_is_busy(db, monkeypatch):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    monkeypatch.setattr(scorecard_drafts.time, "sleep", lambda seconds: None)
    opened = {"count": 0}

    def flaky():
        opened["count"] += 1
        if opened["count"] == 2:  # the first attempt to store the result
            raise RuntimeError("synthetic busy writer")
        return Session(bind=engine)

    run_extraction(flaky, draft_id, FakeProvider(), directory=directory)
    assert _draft(engine, draft_id).status == "ready" and opened["count"] == 3


def test_no_hole_scores_are_written_by_a_draft(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    run_extraction(_factory(engine), draft_id, FakeProvider(), directory=directory)
    with Session(engine) as session:
        assert session.scalar(select(func.count(HoleScore.id))) == 0


# --- start_extraction ---------------------------------------------------------------------


def test_start_extraction_runs_the_job_on_the_executor(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    state = _State(engine, directory, FakeProvider())
    assert start_extraction(state, draft_id) is True
    assert state.scorecard_executor.submitted == 1
    assert _draft(engine, draft_id).status == "ready"


def test_start_extraction_on_a_real_single_worker_thread(db):
    engine, ids, directory = db
    first, second = _create(engine, ids, directory), _create(engine, ids, directory)
    state = _State(engine, directory, FakeProvider())
    state.scorecard_executor = ThreadPoolExecutor(max_workers=1)
    request_thread = threading.get_ident()
    worker_threads = []
    state.vision_provider.during_call = lambda: worker_threads.append(threading.get_ident())
    assert start_extraction(state, first) and start_extraction(state, second)
    state.scorecard_executor.shutdown(wait=True)
    assert _draft(engine, first).status == "ready" and _draft(engine, second).status == "ready"
    # Never on the request thread, and one worker for both reads.
    assert len(worker_threads) == 2 and request_thread not in worker_threads
    assert len(set(worker_threads)) == 1


def test_with_no_provider_the_draft_becomes_manual_and_nothing_is_called(db):
    engine, ids, directory = db
    draft_id = _create(engine, ids, directory)
    state = _State(engine, directory, None)
    assert start_extraction(state, draft_id) is False
    assert state.scorecard_executor.submitted == 0
    draft = _draft(engine, draft_id)
    assert draft.status == "ready" and draft.image_path is None and draft_card(draft) is None
    assert _files(directory) == []


# --- reading and deleting -----------------------------------------------------------------


def test_get_and_list_open_drafts_show_every_admins_drafts_own_first(db):
    engine, ids, directory = db
    with Session(engine) as session:
        other = User(username="other@example.test", email="other@example.test", display_name="Other",
                     password_hash="x")
        session.add(other)
        session.commit()
        other_id = other.id
    mine_old = _create(engine, ids, directory)
    theirs = _create(engine, ids, directory, user_id=other_id,
                     clock=lambda: NOW + timedelta(minutes=5))
    mine_new = _create(engine, ids, directory, clock=lambda: NOW + timedelta(minutes=10))
    with Session(engine) as session:
        assert get_draft(session, mine_old).id == mine_old and get_draft(session, 999999) is None
        assert [d.id for d in list_open_drafts(session, ids["user"])] == [mine_new, mine_old, theirs]
        assert [d.id for d in list_open_drafts(session, other_id)] == [theirs, mine_new, mine_old]


def test_delete_draft_removes_photo_then_row_and_leaves_the_commit_to_the_caller(db):
    engine, ids, directory = db
    kept, gone = _create(engine, ids, directory), _create(engine, ids, directory)
    with Session(engine) as session:
        delete_draft(session, session.get(ScorecardDraft, gone), directory=directory)
        assert session.get(ScorecardDraft, gone) is None
        session.rollback()  # the caller owns the transaction: the row is back, the photo is not
    assert _draft(engine, gone) is not None and len(_files(directory)) == 1
    with Session(engine) as session:
        # A photo that is already missing is not an error.
        delete_draft(session, session.get(ScorecardDraft, gone), directory=directory)
        session.commit()
    assert _draft(engine, gone) is None and _draft(engine, kept) is not None
    assert _files(directory) == [_draft(engine, kept).image_path]


def test_discard_draft_removes_photo_and_row_and_tolerates_a_missing_photo(db):
    engine, ids, directory = db
    first, second = _create(engine, ids, directory), _create(engine, ids, directory)
    image_file(directory, _draft(engine, second).image_path).unlink()
    with Session(engine) as session:
        assert discard_draft(session, first, directory=directory) is True
        assert discard_draft(session, second, directory=directory) is True
        assert discard_draft(session, first, directory=directory) is False
    assert _count(engine) == 0 and _files(directory) == []


def test_purge_removes_a_24_hour_old_draft_and_keeps_a_23_hour_old_one(db):
    engine, ids, directory = db
    old = _create(engine, ids, directory, clock=lambda: NOW - timedelta(hours=24))
    young = _create(engine, ids, directory, clock=lambda: NOW - timedelta(hours=23))
    manual_old = None
    with Session(engine) as session:
        manual_old = create_manual_draft(session, week_id=ids["week"], user_id=ids["user"], today=TODAY,
                                         clock=lambda: NOW - timedelta(hours=30),
                                         actor=Actor(ids["user"], True)).id
    young_image = _draft(engine, young).image_path
    with Session(engine) as session:
        assert purge_expired(session, NOW, directory=directory) == 2
    assert _draft(engine, old) is None and _draft(engine, manual_old) is None
    assert _draft(engine, young) is not None and _files(directory) == [young_image]
    with Session(engine) as session:
        assert purge_expired(session, NOW, directory=directory) == 0


def test_purge_removes_an_old_stray_photo_and_keeps_a_new_one(db):
    engine, ids, directory = db
    referenced = _draft(engine, _create(engine, ids, directory)).image_path
    old_stray, new_stray = directory / ("a" * 32 + ".jpg"), directory / ("b" * 32 + ".jpg")
    for path in (old_stray, new_stray):
        path.write_bytes(b"stray")
    (directory / "subdirectory").mkdir()
    stale = (NOW - timedelta(hours=24, minutes=1)).timestamp()
    fresh = (NOW - timedelta(hours=23)).timestamp()
    os.utime(old_stray, (stale, stale))
    os.utime(new_stray, (fresh, fresh))
    os.utime(directory / referenced, (stale, stale))  # old, but a draft still references it
    with Session(engine) as session:
        assert purge_expired(session, NOW, directory=directory) == 0
    assert _files(directory) == sorted([referenced, new_stray.name, "subdirectory"])


def test_a_photo_that_cannot_be_removed_never_keeps_expired_text_alive(db, caplog):
    engine, ids, directory = db
    stuck = _create(engine, ids, directory, clock=lambda: NOW - timedelta(hours=30))
    other = _create(engine, ids, directory, clock=lambda: NOW - timedelta(hours=30))
    run_extraction(_factory(engine), stuck, FakeProvider(), directory=directory)
    assert _draft(engine, stuck).extracted_json
    old = (NOW - timedelta(hours=30)).timestamp()
    for name in _files(directory):
        os.utime(directory / name, (old, old))
    os.chmod(directory, 0o500)  # nothing in the directory can be unlinked
    try:
        with caplog.at_level(logging.WARNING), Session(engine) as session:
            assert purge_expired(session, NOW, directory=directory) == 2
        # Both rows, and the names and scores one of them held, are gone even though no photo could be.
        assert _draft(engine, stuck) is None and _draft(engine, other) is None
        assert len(_files(directory)) == 2
        assert "scorecard photos could not be removed count=" in caplog.text
        assert str(directory) not in caplog.text
    finally:
        os.chmod(directory, 0o700)
    # Once the directory is writable again the stray sweep removes what was left behind.
    with Session(engine) as session:
        assert purge_expired(session, NOW, directory=directory) == 0
    assert _files(directory) == []


def test_purge_with_no_directory_yet_is_harmless(db):
    engine, ids, directory = db
    with Session(engine) as session:
        assert purge_expired(session, NOW, directory=directory) == 0
    assert not directory.exists()


def test_mark_interrupted_fails_extracting_drafts_and_leaves_ready_ones(db):
    engine, ids, directory = db
    extracting = _create(engine, ids, directory)
    ready = _create(engine, ids, directory)
    run_extraction(_factory(engine), ready, FakeProvider(), directory=directory)
    with Session(engine) as session:
        assert mark_interrupted(session) == 1
        assert mark_interrupted(session) == 0
    assert (_draft(engine, extracting).status, _draft(engine, extracting).error_code) == ("failed", "interrupted")
    assert (_draft(engine, ready).status, _draft(engine, ready).error_code) == ("ready", None)


def test_deleting_a_week_removes_its_drafts(db):
    engine, ids, directory = db
    with Session(engine) as session:
        week = Week(season_id=ids["season"], index=9, play_date=date(2026, 10, 22), nine="front",
                    week_type="match", status="scheduled")
        session.add(week)
        session.flush()
        session.add(ScorecardDraft(week_id=week.id, created_by_user_id=ids["user"], status="ready",
                                   created_at=NOW, expires_at=NOW + timedelta(hours=24)))
        session.commit()
        assert session.scalar(select(func.count(ScorecardDraft.id))) == 1
        session.delete(week)
        session.commit()
    assert _count(engine) == 0


# --- logging ------------------------------------------------------------------------------


def test_no_log_record_carries_image_bytes_a_name_or_a_score(db, caplog):
    engine, ids, directory = db
    with caplog.at_level(logging.DEBUG):
        ready = _create(engine, ids, directory)
        run_extraction(_factory(engine), ready, FakeProvider(), directory=directory)
        failed = _create(engine, ids, directory)
        run_extraction(_factory(engine), failed, FakeProvider(ProviderOutputError("output_shape")),
                       directory=directory)
        start_extraction(_State(engine, directory, None), _create(engine, ids, directory))
        with Session(engine) as session:
            discard_draft(session, failed, directory=directory)
            mark_interrupted(session)
            purge_expired(session, NOW + timedelta(hours=25), directory=directory)
    assert [r for r in caplog.records if r.name == scorecard_drafts.logger.name]  # the work is logged...
    text = caplog.text
    # ...with IDs, statuses, durations and codes only.
    for forbidden in (NAME, "Zzyzx", "JFIF", "[4, 5, 6", "43", "example-model", str(directory)):
        assert forbidden not in text
    assert "status=ready" in text and "code=output_shape" in text and "duration_ms=" in text


# --- application wiring -------------------------------------------------------------------


def _settings(tmp_path) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path / 'app.db'}", session_secret="synthetic-test-secret",
                    seed_course=False)


def test_startup_sets_the_directory_and_a_single_worker_and_shuts_it_down(tmp_path, monkeypatch):
    managed = tmp_path / "data" / ".env"
    monkeypatch.setenv("MANAGED_ENV_PATH", str(managed))
    app = create_app(settings=_settings(tmp_path))
    assert app.state.scorecard_dir is None and app.state.scorecard_executor is None
    with TestClient(app) as client:
        assert client.app.state.scorecard_dir == managed.parent / "scorecards"
        executor = client.app.state.scorecard_executor
        assert executor._max_workers == 1
        assert client.get("/readyz").status_code == 200
        assert not (managed.parent / "scorecards").exists()  # created on first use, not at startup
    assert executor._shutdown and app.state.scorecard_executor is None


def test_an_assigned_executor_and_directory_are_kept(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    inline, directory = InlineExecutor(), tmp_path / "elsewhere"
    app.state.scorecard_executor, app.state.scorecard_dir = inline, directory
    with TestClient(app) as client:
        assert client.app.state.scorecard_executor is inline
        assert client.app.state.scorecard_dir == directory
    assert app.state.scorecard_executor is inline


def test_startup_fails_interrupted_drafts_and_purges_expired_ones(tmp_path):
    settings, directory = _settings(tmp_path), tmp_path / "scorecards"
    # First run: the application migrates an empty database, then drafts are left behind.
    with TestClient(create_app(settings=settings)) as client:
        engine = client.app.state.engine
        with Session(engine) as session:
            ids = seed_fixture(session)
            create_generated(session, ids, week_index=0)
            user = User(username="admin@example.test", email="admin@example.test", display_name="Admin",
                        password_hash="x")
            session.add(user)
            session.commit()
            ids = {**ids, "user": user.id, "week": ids["weeks"][0]}
        interrupted = _create(engine, ids, directory, clock=lambda: NOW - timedelta(hours=1))
        expired = _create(engine, ids, directory, clock=lambda: NOW - timedelta(hours=25))
        ready = _create(engine, ids, directory, clock=lambda: NOW - timedelta(hours=2))
        run_extraction(_factory(engine), ready, FakeProvider(), directory=directory)
        assert len(_files(directory)) == 3

    # Second run: the same database and directory, as after a container restart.
    app = create_app(settings=settings)
    app.state.scorecard_dir, app.state.scorecard_utc_now = directory, clock
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        engine = client.app.state.engine
        draft = _draft(engine, interrupted)
        assert (draft.status, draft.error_code) == ("failed", "interrupted")
        assert _draft(engine, expired) is None
        assert _draft(engine, ready).status == "ready"
        assert _files(directory) == sorted([draft.image_path, _draft(engine, ready).image_path])


def test_a_cleanup_failure_at_startup_never_stops_the_application(tmp_path, monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(app_module, "purge_expired", explode)
    with caplog.at_level(logging.WARNING), TestClient(create_app(settings=_settings(tmp_path))) as client:
        assert client.get("/readyz").status_code == 200
        assert client.get("/healthz").status_code == 200
    assert "scorecard draft cleanup failed at startup" in caplog.text
    assert "synthetic cleanup failure" not in caplog.text
