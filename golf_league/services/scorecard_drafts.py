"""Scorecard drafts: an upload held for review, then deleted (R-SCORES).

A draft is not data. The photo and its row exist only until the card is
submitted, discarded or 24 hours old; nothing here writes `hole_scores`.
Extraction runs on a background worker, never on a request thread, and the
model call holds no database transaction.

Log lines carry draft IDs, statuses, durations and codes only: never a name, a
score, model output or image data. This module imports no `fastapi`
(enforced by `tests/test_architecture.py`).
"""

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.scorecard import Actor
from golf_league.models import ScorecardDraft
from golf_league.services.matchups import Clock
from golf_league.services.scorecard_image import prepare_image
from golf_league.services.scorecard_vision import (
    ExtractedCard,
    ExtractedPlayer,
    ProviderError,
    VisionProvider,
)
from golf_league.services.scores import week_for_entry, week_holes

logger = logging.getLogger(__name__)

DRAFT_LIFETIME = timedelta(hours=24)
SCORECARD_DIRNAME = "scorecards"
_IMAGE_NAME_RE = re.compile(r"[0-9a-f]{32}\.jpg")
# How often the background job retries storing its result when the single
# SQLite writer is busy.
_STORE_ATTEMPTS = 3

SessionFactory = Callable[[], Session]


# --- the scorecard directory ------------------------------------------------


def scorecard_directory(managed_env_path: str | os.PathLike[str]) -> Path:
    """`<data dir>/scorecards`: the data directory is the managed file's parent."""
    return Path(managed_env_path).parent / SCORECARD_DIRNAME


def _ensure_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)  # mkdir's mode is subject to the umask


def image_file(directory: Path, image_path: str | None) -> Path | None:
    """The photo's path, or None for a manual draft or a name this module did not write."""
    if not image_path or not _IMAGE_NAME_RE.fullmatch(image_path):
        return None
    return directory / image_path


def _write_image(directory: Path, image: bytes) -> str:
    _ensure_directory(directory)
    name = uuid.uuid4().hex + ".jpg"
    descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(image)
    return name


def _remove_image(directory: Path, image_path: str | None) -> None:
    path = image_file(directory, image_path)
    if path is not None:
        path.unlink(missing_ok=True)


# --- creating ----------------------------------------------------------------


def create_draft(session: Session, *, week_id: int, user_id: int, raw: bytes, today: date,
                 clock: Clock, directory: Path, actor: Actor) -> ScorecardDraft:
    """Store a prepared photo and an `extracting` draft; commits.

    Raises `ScoreEntryError` for a week that cannot take a card and
    `ImageRefused` for a refused upload, in that order, before anything is
    written. If the row cannot be saved the photo is removed.
    """
    week_for_entry(session, week_id, today, actor)
    image = prepare_image(raw)
    name = _write_image(directory, image)
    try:
        draft = _new_draft(week_id, user_id, clock, status="extracting", image_path=name)
        session.add(draft)
        session.commit()
    except BaseException:
        session.rollback()
        _remove_image(directory, name)
        raise
    logger.info("scorecard draft created id=%d status=extracting", draft.id)
    return draft


def create_manual_draft(session: Session, *, week_id: int, user_id: int, today: date,
                        clock: Clock, actor: Actor) -> ScorecardDraft:
    """A `ready` draft with no photo and nothing extracted; commits."""
    week_for_entry(session, week_id, today, actor)
    draft = _new_draft(week_id, user_id, clock, status="ready", image_path=None)
    session.add(draft)
    session.commit()
    logger.info("scorecard draft created id=%d status=ready manual=1", draft.id)
    return draft


def _new_draft(week_id: int, user_id: int, clock: Clock, *, status: str,
               image_path: str | None) -> ScorecardDraft:
    now = clock()
    return ScorecardDraft(week_id=week_id, created_by_user_id=user_id, status=status,
                          image_path=image_path, created_at=now, expires_at=now + DRAFT_LIFETIME)


# --- extraction --------------------------------------------------------------


def card_to_json(card: ExtractedCard) -> str:
    """The structured reading only; never the model's raw text."""
    return json.dumps({
        "players": [
            {"name": player.name, "scores": list(player.scores), "uncertain": list(player.uncertain),
             "handicap_written": player.handicap_written, "out_total": player.out_total}
            for player in card.players
        ],
        "holes_header": list(card.holes_header),
    })


def draft_card(draft: ScorecardDraft) -> ExtractedCard | None:
    """The stored reading, or None for a manual draft or one not yet read."""
    if not draft.extracted_json:
        return None
    data = json.loads(draft.extracted_json)
    return ExtractedCard(
        players=tuple(
            ExtractedPlayer(name=player["name"], scores=tuple(player["scores"]),
                            uncertain=tuple(player["uncertain"]),
                            handicap_written=player["handicap_written"], out_total=player["out_total"])
            for player in data["players"]
        ),
        model_name=draft.model_name or "",
        holes_header=tuple(data["holes_header"]),
    )


def run_extraction(session_factory: SessionFactory, draft_id: int, provider: VisionProvider, *,
                   directory: Path) -> None:
    """The background job: read one draft's photo and store the result.

    Never raises. The provider call holds no session and no transaction, so a
    slow model cannot block the database's single writer.
    """
    started = time.monotonic()
    status, code = "failed", "extraction_failed"
    card: ExtractedCard | None = None
    try:
        with session_factory() as session:
            draft = session.get(ScorecardDraft, draft_id)
            if draft is None or draft.status != "extracting":
                return
            path = image_file(directory, draft.image_path)
            hole_count = len(week_holes(session, draft.week_id))
        if path is None or not path.is_file() or hole_count == 0:
            code = "draft_unreadable"
        else:
            card = provider.read_scorecard(path.read_bytes(), hole_count=hole_count)
            status, code = "ready", ""
    except ProviderError as error:
        code = error.code
    except Exception as error:  # a job on a worker thread must not raise
        logger.warning("scorecard extraction error id=%d type=%s", draft_id, type(error).__name__)
    try:
        stored = _store_result(session_factory, draft_id, status, code, card)
    except Exception as error:
        logger.warning("scorecard extraction not stored id=%d type=%s", draft_id, type(error).__name__)
        return
    logger.info("scorecard extraction id=%d status=%s code=%s duration_ms=%d stored=%d",
                draft_id, status, code or "-", int((time.monotonic() - started) * 1000), stored)


def _store_result(session_factory: SessionFactory, draft_id: int, status: str, code: str,
                  card: ExtractedCard | None) -> bool:
    """Write the outcome unless the draft was discarded or purged meanwhile."""
    for attempt in range(_STORE_ATTEMPTS):
        try:
            with session_factory() as session:
                draft = session.get(ScorecardDraft, draft_id)
                if draft is None or draft.status != "extracting":
                    return False
                draft.status = status
                draft.error_code = code or None
                if card is not None:
                    draft.extracted_json = card_to_json(card)
                    draft.model_name = card.model_name
                session.commit()
                return True
        except Exception:
            if attempt == _STORE_ATTEMPTS - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    return False


def _session_factory(app_state: Any) -> SessionFactory:
    engine = app_state.engine
    return lambda: Session(bind=engine, autoflush=False)


def start_extraction(app_state: Any, draft_id: int) -> bool:
    """Queue the draft on the single background worker; True when queued.

    With no provider configured nothing is called: the draft becomes a manual
    draft (its photo is removed) and False is returned.
    """
    factory = _session_factory(app_state)
    directory: Path = app_state.scorecard_dir
    provider = getattr(app_state, "vision_provider", None)
    if provider is None:
        with factory() as session:
            draft = session.get(ScorecardDraft, draft_id)
            if draft is not None and draft.status == "extracting":
                _remove_image(directory, draft.image_path)
                draft.status, draft.image_path = "ready", None
                session.commit()
        logger.info("scorecard draft id=%d status=ready manual=1 reason=not_configured", draft_id)
        return False
    app_state.scorecard_executor.submit(run_extraction, factory, draft_id, provider, directory=directory)
    return True


# --- reading and deleting ----------------------------------------------------


def get_draft(session: Session, draft_id: int) -> ScorecardDraft | None:
    return session.get(ScorecardDraft, draft_id)


def list_open_drafts(session: Session, user_id: int) -> list[ScorecardDraft]:
    """Every open draft, the given admin's own first, then newest first."""
    drafts = session.scalars(select(ScorecardDraft)).all()
    return sorted(drafts, key=lambda d: (d.created_by_user_id != user_id, -d.created_at.timestamp(), -d.id))


def delete_draft(session: Session, draft: ScorecardDraft, *, directory: Path) -> None:
    """Remove the photo, then the row. Does not commit: the caller owns the transaction."""
    _remove_image(directory, draft.image_path)
    session.delete(draft)
    session.flush()


def discard_draft(session: Session, draft_id: int, *, directory: Path) -> bool:
    """Delete a draft and its photo; commits. False when there is no such draft."""
    draft = session.get(ScorecardDraft, draft_id)
    if draft is None:
        return False
    delete_draft(session, draft, directory=directory)
    session.commit()
    logger.info("scorecard draft discarded id=%d", draft_id)
    return True


def _sweep_stray_photos(session: Session, now: datetime, directory: Path) -> int:
    """Remove unreferenced photos older than 24 hours; return how many could not be removed."""
    if not directory.is_dir():
        return 0
    referenced = set(session.scalars(
        select(ScorecardDraft.image_path).where(ScorecardDraft.image_path.is_not(None))
    ))
    cutoff = (now - DRAFT_LIFETIME).timestamp()
    stuck = 0
    for entry in directory.iterdir():
        try:
            if entry.is_symlink() or not entry.is_file() or entry.name in referenced:
                continue
            if entry.stat().st_mtime <= cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            stuck += 1
    return stuck


def purge_expired(session: Session, now: datetime, *, directory: Path) -> int:
    """Delete every draft that has expired, and every stray photo; commits.

    A stray photo is a file in the scorecard directory that no draft references
    and that is more than 24 hours old. Each draft is deleted in its own
    transaction, and a photo that cannot be removed never keeps a row (and its
    extracted text) alive: the row goes, and the photo is left for the stray
    sweep to retry. Returns the number of drafts deleted.
    """
    expired_ids = list(session.scalars(select(ScorecardDraft.id).where(ScorecardDraft.expires_at <= now)))
    purged = stuck = 0
    for draft_id in expired_ids:
        draft = session.get(ScorecardDraft, draft_id)
        if draft is None:
            continue
        try:
            _remove_image(directory, draft.image_path)
        except OSError:
            stuck += 1
        session.delete(draft)
        session.commit()
        purged += 1
    stuck += _sweep_stray_photos(session, now, directory)
    if purged:
        logger.info("scorecard drafts purged count=%d", purged)
    if stuck:
        logger.warning("scorecard photos could not be removed count=%d", stuck)
    return purged


def mark_interrupted(session: Session) -> int:
    """Fail every draft left `extracting` by a stopped process; commits."""
    drafts = session.scalars(select(ScorecardDraft).where(ScorecardDraft.status == "extracting")).all()
    for draft in drafts:
        draft.status, draft.error_code = "failed", "interrupted"
    session.commit()
    if drafts:
        logger.info("scorecard drafts interrupted count=%d", len(drafts))
    return len(drafts)
