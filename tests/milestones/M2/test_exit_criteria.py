"""M2 exit criteria: fresh boot through staged import and private roster.

These deliberately exercise the published HTTP boundary with a temporary,
fresh SQLite database.  The CSV data is synthetic and inline: the gate does
not depend on a real roster or on ordinary test fixtures.
"""

import csv
import io
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.migrations import current_revision
from golf_league.models import Course, Golfer, RosterImportBatch, RosterImportRow, User

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]*)"')
_ADMIN_EMAIL = "m2-admin@example.test"
_ADMIN_PASSWORD = "m2-admin-password-1"
_PLAYER_EMAIL = "player@example.test"
_PLAYER_PASSWORD = "m2-player-password-1"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _csrf(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match, "expected a CSRF token in the rendered form"
    return match.group(1)


def _regular_csv(rows: list[list[str]]) -> bytes:
    """Return a minimal, valid summer-regular roster export."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Status", "Gold HC", "White HC", "Email", "Phone #"])
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


@contextmanager
def _session(client: TestClient) -> Iterator[Session]:
    session = Session(bind=client.app.state.engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def m2_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Boot the real app on a brand-new database with its first admin."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", _ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PASSWORD)
    database_url = f"sqlite:///{tmp_path / 'm2.db'}"
    settings = Settings(
        database_url=database_url,
        session_secret="m2-milestone-test-secret-not-a-real-secret",
    )
    with TestClient(create_app(settings=settings)) as client:
        assert current_revision(database_url) == _head_revision()
        yield client


def _login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _course_id(client: TestClient) -> int:
    with _session(client) as session:
        return session.scalar(select(Course.id))


def _upload_regular(client: TestClient, rows: list[list[str]], *, name: str = "m2.csv") -> int:
    form = client.get("/admin/roster/imports/new")
    response = client.post(
        "/admin/roster/imports/new",
        data={"course_id": str(_course_id(client)), "csrf_token": _csrf(form.text)},
        files={"files": (name, _regular_csv(rows), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def _batch(client: TestClient, batch_id: int) -> RosterImportBatch:
    with _session(client) as session:
        batch = session.get(RosterImportBatch, batch_id)
        assert batch is not None
        session.expunge(batch)
        return batch


def _row(client: TestClient, batch_id: int, email: str) -> RosterImportRow:
    with _session(client) as session:
        row = session.scalar(
            select(RosterImportRow).where(
                RosterImportRow.batch_id == batch_id,
                RosterImportRow.email == email,
            )
        )
        assert row is not None
        session.expunge(row)
        return row


def _apply(client: TestClient, batch_id: int, batch_version: int):
    # Once a batch is applied its review page deliberately has no mutable
    # form, so obtain the same session-bound token from the import form.
    page = client.get("/admin/roster/imports/new")
    return client.post(
        f"/admin/roster/imports/{batch_id}/apply",
        data={"csrf_token": _csrf(page.text), "batch_version": str(batch_version)},
        follow_redirects=False,
    )


def _edit(
    client: TestClient,
    batch_id: int,
    row: RosterImportRow,
    batch_version: int,
    *,
    phone: str,
    tee_label: str,
    handicap_gold: str,
    handicap_white: str = "6",
    update_opt_in: bool = False,
):
    page = client.get(f"/admin/roster/imports/{batch_id}")
    data = {
        "csrf_token": _csrf(page.text),
        "batch_version": str(batch_version),
        "row_version": str(row.version),
        "action": "edit",
        "first_name": row.first_name or "",
        "last_name": row.last_name or "",
        "email": row.email or "",
        "phone": phone,
        "tee_label": tee_label,
        "handicap_gold": handicap_gold,
        "handicap_white": handicap_white,
        "handicap_single": "",
        "included": "true",
    }
    if update_opt_in:
        data["update_opt_in"] = "true"
    return client.post(
        f"/admin/roster/imports/{batch_id}/rows/{row.id}/edit",
        data=data,
        follow_redirects=False,
    )


def _register_verify_and_login(client: TestClient) -> None:
    client.cookies.clear()
    form = client.get("/register")
    registered = client.post(
        "/register",
        data={
            "email": "  PLAYER@EXAMPLE.TEST  ",
            "password": _PLAYER_PASSWORD,
            "csrf_token": _csrf(form.text),
        },
    )
    assert registered.status_code == 200
    assert "If that address is on the league roster" in registered.text
    sent = client.app.state.email_sender.sent
    assert len(sent) == 1
    token = sent[-1]["body"].rsplit("/verify/", 1)[1]
    assert client.get(f"/verify/{token}").status_code == 200
    _login(client, _PLAYER_EMAIL, _PLAYER_PASSWORD)


def test_fresh_boot_admin_import_registration_and_private_roster(m2_app: TestClient) -> None:
    """Fresh boot reaches the complete M2 admin-to-player happy path."""
    with _session(m2_app) as session:
        admin = session.scalar(select(User).where(User.email == _ADMIN_EMAIL))
        assert admin is not None and admin.is_admin and admin.email_verified_at is not None

    _login(m2_app, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    batch_id = _upload_regular(
        m2_app,
        [["Player, Pat", "Gold", "5", "6", "  PLAYER@EXAMPLE.TEST  ", "555-0100"]],
        name="initial-roster.csv",
    )
    staged = _batch(m2_app, batch_id)
    staged_row = _row(m2_app, batch_id, _PLAYER_EMAIL)
    assert staged.is_initial is True and staged.row_count == 1
    assert staged_row.source_role == "summer_regular"
    assert staged_row.source_file == "initial-roster.csv" and staged_row.source_row == 1
    assert staged_row.raw_line.endswith("555-0100")
    assert _apply(m2_app, batch_id, staged.version).status_code == 303

    with _session(m2_app) as session:
        batch = session.get(RosterImportBatch, batch_id)
        golfer = session.scalar(select(Golfer).where(Golfer.email == _PLAYER_EMAIL))
        assert batch is not None and (batch.created_count, batch.updated_count) == (1, 0)
        assert golfer is not None
        assert golfer.default_tee_set.color_label == "Gold"
        assert golfer.handicap_strokes == 5 and golfer.handicap_status == "ok"

    _register_verify_and_login(m2_app)
    roster = m2_app.get("/roster")
    assert roster.status_code == 200 and "Pat Player" in roster.text
    for private_text in (
        _PLAYER_EMAIL,
        "555-0100",
        "Player, Pat",
        "/admin/",
        "/verify/",
        "token",
    ):
        assert private_text.lower() not in roster.text.lower()


def test_hard_error_is_atomic_and_protected_updates_require_opt_in(m2_app: TestClient) -> None:
    """A correction is required before apply; protected changes need consent."""
    _login(m2_app, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    batch_id = _upload_regular(
        m2_app,
        [
            ["Good, Player", "Gold", "5", "6", "good@example.test", "555-0100"],
            ["Bad, Player", "Gold", "not-a-number", "6", "bad@example.test", "555-0200"],
        ],
    )
    staged = _batch(m2_app, batch_id)
    assert _apply(m2_app, batch_id, staged.version).status_code == 422
    with _session(m2_app) as session:
        assert session.scalar(select(func.count()).select_from(Golfer)) == 0

    bad_row = _row(m2_app, batch_id, "bad@example.test")
    current = _batch(m2_app, batch_id)
    assert _edit(
        m2_app, batch_id, bad_row, current.version,
        phone="555-0200", tee_label="Gold", handicap_gold="7",
    ).status_code == 303
    assert _apply(m2_app, batch_id, _batch(m2_app, batch_id).version).status_code == 303

    protected_batch = _upload_regular(
        m2_app,
        [["Good, Player", "White", "9", "10", "good@example.test", "555-9999"]],
        name="changed-private-fields.csv",
    )
    protected = _batch(m2_app, protected_batch)
    assert _apply(m2_app, protected_batch, protected.version).status_code == 422
    with _session(m2_app) as session:
        golfer = session.scalar(select(Golfer).where(Golfer.email == "good@example.test"))
        assert golfer is not None
        assert golfer.phone == "555-0100" and golfer.default_tee_set.color_label == "Gold"
        assert golfer.handicap_strokes == 5

    row = _row(m2_app, protected_batch, "good@example.test")
    assert _edit(
        m2_app, protected_batch, row, _batch(m2_app, protected_batch).version,
        phone="555-9999", tee_label="White", handicap_gold="9", handicap_white="10",
        update_opt_in=True,
    ).status_code == 303
    assert _apply(m2_app, protected_batch, _batch(m2_app, protected_batch).version).status_code == 303
    with _session(m2_app) as session:
        golfer = session.scalar(select(Golfer).where(Golfer.email == "good@example.test"))
        assert golfer is not None
        assert golfer.phone == "555-9999" and golfer.default_tee_set.color_label == "White"
        assert golfer.handicap_strokes == 10


def test_idempotency_and_admin_boundaries_hold_after_a_real_import(m2_app: TestClient) -> None:
    """Repeated data does not duplicate roster rows, and scopes stay enforced."""
    _login(m2_app, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    initial = _upload_regular(
        m2_app,
        [["Player, Pat", "Gold", "5", "6", _PLAYER_EMAIL, "555-0100"]],
    )
    assert _apply(m2_app, initial, _batch(m2_app, initial).version).status_code == 303

    identical = _upload_regular(
        m2_app,
        [["Player, Pat", "Gold", "5", "6", _PLAYER_EMAIL, "555-0100"]],
        name="identical.csv",
    )
    version = _batch(m2_app, identical).version
    assert _apply(m2_app, identical, version).status_code == 303
    assert _apply(m2_app, identical, version).status_code == 409
    with _session(m2_app) as session:
        batch = session.get(RosterImportBatch, identical)
        assert batch is not None and (batch.created_count, batch.unchanged_count) == (0, 1)
        assert session.scalar(select(func.count()).select_from(Golfer)) == 1

    other = _upload_regular(
        m2_app,
        [["Other, Player", "Gold", "5", "6", "other@example.test", "555-0200"]],
        name="other.csv",
    )
    row = _row(m2_app, initial, _PLAYER_EMAIL)
    page = m2_app.get(f"/admin/roster/imports/{other}")
    cross_batch = m2_app.post(
        f"/admin/roster/imports/{other}/rows/{row.id}/edit",
        data={
            "csrf_token": _csrf(page.text),
            "batch_version": str(_batch(m2_app, other).version),
            "row_version": str(row.version),
            "action": "edit",
            "first_name": "Player",
            "last_name": "Pat",
            "email": _PLAYER_EMAIL,
            "phone": "555-0100",
            "tee_label": "Gold",
            "handicap_gold": "5",
            "handicap_white": "6",
            "handicap_single": "",
            "included": "true",
        },
    )
    assert cross_batch.status_code == 404

    _register_verify_and_login(m2_app)
    assert m2_app.get("/admin/roster/imports/new").status_code == 403
    m2_app.cookies.clear()
    assert m2_app.get("/admin/roster/imports/new").status_code == 401
