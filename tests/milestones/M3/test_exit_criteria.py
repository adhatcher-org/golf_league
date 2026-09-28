"""M3 exit criteria: seeded and manually-administered course data.

The gate uses fresh SQLite files and only the published HTTP forms.  The
Wyandot fixture is intentionally repeated here instead of imported from the
seed service, so a changed seed cannot make its own acceptance check pass.
"""

import ast
import html
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.domain.handicap import course_handicap
from golf_league.migrations import current_revision
from golf_league.models import Course, Hole, HoleYardage, TeeRating, TeeSet, User
from golf_league.services.auth import hash_password

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APPLICATION_DIR = _REPO_ROOT / "golf_league"
_HANDICAP_HELPER = _APPLICATION_DIR / "domain" / "handicap.py"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]*)"')
_TEXTAREA_RE = re.compile(r'<textarea[^>]*id="grid"[^>]*>(.*?)</textarea>', re.DOTALL)
_ADMIN_EMAIL = "m3-admin@example.test"
_ADMIN_PASSWORD = "m3-admin-password-1"

# number, nine, par, SI-18, SI-9, Deer/White yards, Snake/Gold yards.
_WYANDOT_HOLES = (
    (1, "front", 5, 9, 5, 498, 425), (2, "front", 4, 6, 3, 332, 307),
    (3, "front", 4, 5, 2, 338, 308), (4, "front", 4, 12, 7, 310, 283),
    (5, "front", 3, 15, 8, 135, 95), (6, "front", 5, 18, 9, 425, 380),
    (7, "front", 4, 7, 4, 353, 310), (8, "front", 3, 11, 6, 128, 110),
    (9, "front", 4, 4, 1, 385, 315), (10, "back", 5, 16, 8, 423, 342),
    (11, "back", 4, 14, 7, 262, 256), (12, "back", 4, 3, 3, 385, 322),
    (13, "back", 3, 13, 6, 139, 129), (14, "back", 4, 10, 5, 284, 262),
    (15, "back", 3, 8, 4, 160, 120), (16, "back", 4, 1, 1, 354, 310),
    (17, "back", 5, 17, 9, 419, 375), (18, "back", 4, 2, 2, 377, 318),
)


def _head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _csrf(page: str) -> str:
    match = _CSRF_RE.search(page)
    assert match, "expected CSRF token in rendered form"
    return match.group(1)


@contextmanager
def _session(client: TestClient) -> Iterator[Session]:
    session = Session(bind=client.app.state.engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A real default boot on a missing SQLite file and bootstrap admin."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", _ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PASSWORD)
    database_url = f"sqlite:///{tmp_path / 'm3-seeded.db'}"
    settings = Settings(
        database_url=database_url,
        session_secret="m3-milestone-test-secret-not-a-real-secret",
    )
    with TestClient(create_app(settings=settings)) as client:
        assert current_revision(database_url) == _head_revision()
        yield client


@pytest.fixture
def unseeded_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A fresh app whose course data can only come from the admin forms."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", _ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PASSWORD)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'm3-unseeded.db'}",
        session_secret="m3-milestone-test-secret-not-a-real-secret",
        seed_course=False,
    )
    with TestClient(create_app(settings=settings)) as client:
        yield client


def _login(client: TestClient, email: str = _ADMIN_EMAIL, password: str = _ADMIN_PASSWORD) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _create_course(client: TestClient, name: str) -> int:
    page = client.get("/admin/courses/new")
    response = client.post(
        "/admin/courses/new",
        data={"name": name, "total_holes": "18", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("/")[3])


def _tee_payload(name: str, color: str, yards: int, order: int) -> dict[str, str]:
    return {
        "name": name, "color_label": color, "gender": "men",
        "total_yards": str(yards), "sort_order": str(order),
        "front_rating": "36.0", "front_slope": "113", "front_par": "36",
        "back_rating": "36.0", "back_slope": "113", "back_par": "36",
        "full_rating": "72.0", "full_slope": "113", "full_par": "72",
    }


def _create_tee(client: TestClient, course_id: int, payload: dict[str, str]) -> int:
    page = client.get(f"/admin/courses/{course_id}/tees/new")
    response = client.post(
        f"/admin/courses/{course_id}/tees/new",
        data={**payload, "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _session(client) as session:
        tee_id = session.scalar(
            select(TeeSet.id).where(
                TeeSet.course_id == course_id, TeeSet.name == payload["name"]
            )
        )
    assert tee_id is not None
    return tee_id


def _complete_grid(tee_ids: list[int]) -> list[dict[str, object]]:
    """Return a valid 18x2 grid whose nines sum to the supplied tee pars."""
    return [
        {
            "number": number,
            "nine": "front" if number <= 9 else "back",
            "par": 4,
            "stroke_index_18": number,
            "stroke_index_9": number if number <= 9 else number - 9,
            "yardages": {str(tee_ids[0]): 300 + number, str(tee_ids[1]): 250 + number},
        }
        for number in range(1, 19)
    ]


def _grid_from_page(page: str) -> list[dict[str, object]]:
    match = _TEXTAREA_RE.search(page)
    assert match, "expected complete grid JSON textarea"
    parsed = json.loads(html.unescape(match.group(1)))
    assert isinstance(parsed, list)
    return parsed


def _course_snapshot(client: TestClient, course_id: int) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with _session(client) as session:
        holes = tuple(
            session.execute(
                select(Hole).where(Hole.course_id == course_id).order_by(Hole.number)
            ).scalars()
        )
        hole_data = tuple(
            (hole.id, hole.number, hole.nine, hole.par, hole.stroke_index_18, hole.stroke_index_9)
            for hole in holes
        )
        yardage_data = tuple(
            session.execute(
                select(HoleYardage.hole_id, HoleYardage.tee_set_id, HoleYardage.yards)
                .join(Hole)
                .where(Hole.course_id == course_id)
                .order_by(HoleYardage.hole_id, HoleYardage.tee_set_id)
            )
        )
    return hole_data, yardage_data


def test_fresh_boot_seeds_exact_wyandot_data_and_rendered_grid(seeded_app: TestClient) -> None:
    """Default boot reaches head and exposes the complete known course card."""
    with _session(seeded_app) as session:
        admins = session.scalars(select(User).where(User.is_admin.is_(True))).all()
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert len(admins) == 1
        assert admins[0].email == _ADMIN_EMAIL and admins[0].email_verified_at is not None
        course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
        assert course is not None
        tee_sets = session.scalars(
            select(TeeSet).where(TeeSet.course_id == course.id).order_by(TeeSet.sort_order)
        ).all()
        assert [(tee.name, tee.color_label, tee.gender, tee.total_yards, tee.sort_order) for tee in tee_sets] == [
            ("Deer", "White", "men", 5707, 1), ("Snake", "Gold", "men", 4967, 2)
        ]
        ratings = {
            (tee.name, rating.scope): (rating.rating, rating.slope, rating.par)
            for tee in tee_sets
            for rating in session.scalars(select(TeeRating).where(TeeRating.tee_set_id == tee.id))
        }
        assert ratings == {
            ("Deer", "front"): (Decimal("33.9"), 114, 36),
            ("Deer", "back"): (Decimal("34.0"), 111, 36),
            ("Deer", "full"): (Decimal("67.9"), 113, 72),
            ("Snake", "front"): (Decimal("31.9"), 107, 36),
            ("Snake", "back"): (Decimal("31.9"), 110, 36),
            ("Snake", "full"): (Decimal("63.8"), 109, 72),
        }
        holes = session.scalars(
            select(Hole).where(Hole.course_id == course.id).order_by(Hole.number)
        ).all()
        assert len(holes) == 18
        assert session.scalar(
            select(func.count()).select_from(HoleYardage).join(Hole).where(Hole.course_id == course.id)
        ) == 36
        tee_ids = {tee.name: tee.id for tee in tee_sets}
        assert [
            (hole.number, hole.nine, hole.par, hole.stroke_index_18, hole.stroke_index_9,
             next(y.yards for y in hole.yardages if y.tee_set_id == tee_ids["Deer"]),
             next(y.yards for y in hole.yardages if y.tee_set_id == tee_ids["Snake"]))
            for hole in holes
        ] == list(_WYANDOT_HOLES)
        assert tuple(sum(row[index] for row in _WYANDOT_HOLES[:9]) for index in (5, 6)) == (2904, 2533)
        assert tuple(sum(row[index] for row in _WYANDOT_HOLES[9:]) for index in (5, 6)) == (2803, 2434)

    _login(seeded_app)
    assert seeded_app.get("/admin/courses").status_code == 200
    page = seeded_app.get(f"/admin/courses/{course.id}/holes")
    assert page.status_code == 200
    rendered = _grid_from_page(page.text)
    expected = [
        {
            "number": number, "nine": nine, "par": par,
            "stroke_index_18": si18, "stroke_index_9": si9,
            "yardages": {str(tee_ids["Deer"]): deer, str(tee_ids["Snake"]): snake},
        }
        for number, nine, par, si18, si9, deer, snake in _WYANDOT_HOLES
    ]
    assert rendered == expected


def test_unseeded_admin_forms_create_complete_durable_course(unseeded_app: TestClient) -> None:
    """The alternate fresh path builds both tees and all grid rows by forms."""
    _login(unseeded_app)
    course_id = _create_course(unseeded_app, "M3 Form Course")
    deer_id = _create_tee(unseeded_app, course_id, _tee_payload("Deer", "White", 5707, 1))
    snake_id = _create_tee(unseeded_app, course_id, _tee_payload("Snake", "Gold", 4967, 2))
    grid = _complete_grid([deer_id, snake_id])
    page = unseeded_app.get(f"/admin/courses/{course_id}/holes")
    response = unseeded_app.post(
        f"/admin/courses/{course_id}/holes",
        data={"csrf_token": _csrf(page.text), "grid": json.dumps(grid)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _session(unseeded_app) as session:
        holes = session.scalars(
            select(Hole).where(Hole.course_id == course_id).order_by(Hole.number)
        ).all()
        assert len(holes) == 18
        assert session.scalar(
            select(func.count()).select_from(HoleYardage).join(Hole).where(Hole.course_id == course_id)
        ) == 36
        assert (sum(hole.par for hole in holes[:9]), sum(hole.par for hole in holes[9:])) == (36, 36)
        assert sum(hole.par for hole in holes) == 72
        assert session.scalar(select(func.count()).select_from(TeeRating).join(TeeSet).where(TeeSet.course_id == course_id)) == 6

    reloaded = unseeded_app.get(f"/admin/courses/{course_id}/holes")
    assert reloaded.status_code == 200 and _grid_from_page(reloaded.text) == grid


def test_course_boundary_failures_are_authorized_scoped_and_atomic(seeded_app: TestClient) -> None:
    """Course mutations reject unauthorized, foreign, invalid, and partial grids."""
    with _session(seeded_app) as session:
        course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
        assert course is not None
        course_id = course.id
        deer_id = session.scalar(select(TeeSet.id).where(TeeSet.course_id == course_id, TeeSet.name == "Deer"))
        assert deer_id is not None

    assert seeded_app.get("/admin/courses").status_code == 401
    assert seeded_app.get(f"/admin/courses/{course_id}/holes").status_code == 401
    _login(seeded_app)
    absent_csrf = seeded_app.post(
        f"/admin/courses/{course_id}/holes", data={"grid": "[]"}, follow_redirects=False
    )
    assert absent_csrf.status_code == 403

    with _session(seeded_app) as session:
        player = User(
            username="m3-player@example.test", email="m3-player@example.test", display_name="M3 Player",
            password_hash=hash_password("m3-player-password-1"),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=False,
        )
        session.add(player)
        session.commit()
    seeded_app.cookies.clear()
    _login(seeded_app, "m3-player@example.test", "m3-player-password-1")
    assert seeded_app.get("/admin/courses").status_code == 403
    assert seeded_app.get(f"/admin/courses/{course_id}/holes").status_code == 403
    seeded_app.cookies.clear()
    _login(seeded_app)

    invalid_course_id = _create_course(seeded_app, "M3 Invalid Tee Course")
    for field, value in (("front_rating", "0"), ("front_rating", "-0.1"), ("front_slope", "²")):
        page = seeded_app.get(f"/admin/courses/{invalid_course_id}/tees/new")
        payload = _tee_payload("Rejected", "White", 5707, 1)
        payload[field] = value
        response = seeded_app.post(
            f"/admin/courses/{invalid_course_id}/tees/new",
            data={**payload, "csrf_token": _csrf(page.text)},
        )
        assert response.status_code == 422 and "<form" in response.text
        with _session(seeded_app) as session:
            assert session.scalar(select(func.count()).select_from(TeeSet).where(TeeSet.course_id == invalid_course_id)) == 0
            assert session.scalar(select(func.count()).select_from(TeeRating).join(TeeSet).where(TeeSet.course_id == invalid_course_id)) == 0

    page = seeded_app.get(f"/admin/courses/{course_id}/holes")
    current_grid = _grid_from_page(page.text)
    before = _course_snapshot(seeded_app, course_id)
    duplicate_index = json.loads(json.dumps(current_grid))
    duplicate_index[1]["stroke_index_18"] = duplicate_index[0]["stroke_index_18"]
    duplicate_response = seeded_app.post(
        f"/admin/courses/{course_id}/holes",
        data={"csrf_token": _csrf(page.text), "grid": json.dumps(duplicate_index)},
    )
    assert duplicate_response.status_code == 422
    assert _course_snapshot(seeded_app, course_id) == before

    foreign_course_id = _create_course(seeded_app, "M3 Foreign Tee Course")
    foreign_tee_id = _create_tee(
        seeded_app, foreign_course_id, _tee_payload("Foreign", "Blue", 5100, 1)
    )
    page = seeded_app.get(f"/admin/courses/{course_id}/holes")
    assert seeded_app.get(f"/admin/courses/{course_id}/tees/{foreign_tee_id}/edit").status_code == 404
    foreign_delete = seeded_app.post(
        f"/admin/courses/{course_id}/tees/{foreign_tee_id}/delete",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert foreign_delete.status_code == 404
    before = _course_snapshot(seeded_app, course_id)
    foreign_grid = _grid_from_page(page.text)
    for row in foreign_grid:
        yardages = row["yardages"]
        assert isinstance(yardages, dict)
        yardages[str(foreign_tee_id)] = yardages.pop(str(deer_id))
    foreign_response = seeded_app.post(
        f"/admin/courses/{course_id}/holes",
        data={"csrf_token": _csrf(page.text), "grid": json.dumps(foreign_grid)},
    )
    assert foreign_response.status_code == 422
    assert _course_snapshot(seeded_app, course_id) == before

    delete_page = seeded_app.get(f"/admin/courses/{course_id}/holes")
    delete_response = seeded_app.post(
        f"/admin/courses/{course_id}/tees/{deer_id}/delete",
        data={"csrf_token": _csrf(delete_page.text)},
        follow_redirects=False,
    )
    assert delete_response.status_code == 409
    assert "18 hole yardages" in delete_response.text
    with _session(seeded_app) as session:
        assert session.get(TeeSet, deer_id) is not None


def _resolved_import_target(node: ast.ImportFrom, module_path: Path) -> str | None:
    """Resolve an import-from target, rejecting relative imports above root."""
    if node.level == 0:
        return node.module
    try:
        relative_path = module_path.relative_to(_APPLICATION_DIR)
    except ValueError:
        return None
    package_parts = [_APPLICATION_DIR.name, *relative_path.parent.parts]
    remove_count = node.level - 1
    if remove_count >= len(package_parts):
        return None
    if remove_count:
        package_parts = package_parts[:-remove_count]
    if node.module:
        package_parts.extend(node.module.split("."))
    return ".".join(package_parts)


def _handicap_imports(module_path: Path) -> set[str]:
    """Return import forms in a live module that resolve to the GL-22 helper."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "golf_league.domain.handicap" for alias in node.names):
                found.add("golf_league.domain.handicap")
        elif isinstance(node, ast.ImportFrom):
            target = _resolved_import_target(node, module_path)
            if target == "golf_league.domain.handicap":
                found.add(target)
            elif target == "golf_league.domain" and any(alias.name == "handicap" for alias in node.names):
                found.add("golf_league.domain.handicap")
    return found


def test_gl22_math_and_live_application_isolation() -> None:
    """GL-22 stays exact, signed, float-safe, and unused by live app code."""
    assert course_handicap(Decimal("2.5"), Decimal("36"), 113, 36) == 3
    assert course_handicap(Decimal("-2.5"), Decimal("36"), 113, 36) == -2
    assert course_handicap(Decimal("-3"), Decimal("36"), 113, 36) == -3
    with pytest.raises(TypeError):
        course_handicap(2.5, Decimal("36"), 113, 36)

    for module_path in _APPLICATION_DIR.rglob("*.py"):
        if module_path == _HANDICAP_HELPER:
            continue
        assert not _handicap_imports(module_path), (
            f"{module_path.relative_to(_REPO_ROOT)} imports golf_league.domain.handicap"
        )
