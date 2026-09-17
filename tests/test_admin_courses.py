"""Route-level tests for the admin course/tee/rating forms."""

import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from golf_league.models import User
from golf_league.services.auth import hash_password

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]*)"')


def _extract_csrf(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, f"no csrf_token field found in: {html!r}"
    return match.group(1)


def _make_user(client, *, email, password, verified=True, is_admin=False):
    session = Session(bind=client.app.state.engine)
    try:
        user = User(
            username=email,
            email=email,
            display_name="Test User",
            password_hash=hash_password(password),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None,
            is_admin=is_admin,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id
    finally:
        session.close()


def _login(client, email, password):
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _admin_client(client, email="admin@example.test", password="s3cret-pw!"):
    _make_user(client, email=email, password=password, verified=True, is_admin=True)
    _login(client, email, password)
    return client


def test_admin_creates_a_course_then_both_tee_sets_through_the_forms(empty_client):
    client = _admin_client(empty_client)

    new_course_page = client.get("/admin/courses/new")
    assert new_course_page.status_code == 200
    csrf_token = _extract_csrf(new_course_page.text)

    create_response = client.post(
        "/admin/courses/new",
        data={
            "name": "Test Golf Club",
            "city": "",
            "state": "",
            "website": "https://example.test",
            "total_holes": "18",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    course_id = create_response.headers["location"].split("/")[3]

    tee_specs = [
        {"name": "Deer", "color_label": "White", "total_yards": "5707", "sort_order": "1",
         "front_rating": "33.9", "front_slope": "114", "front_par": "36",
         "back_rating": "34.0", "back_slope": "111", "back_par": "36",
         "full_rating": "67.9", "full_slope": "113", "full_par": "72"},
        {"name": "Snake", "color_label": "Gold", "total_yards": "4967", "sort_order": "2",
         "front_rating": "31.9", "front_slope": "107", "front_par": "36",
         "back_rating": "31.9", "back_slope": "110", "back_par": "36",
         "full_rating": "63.8", "full_slope": "109", "full_par": "72"},
    ]

    for spec in tee_specs:
        tee_new_page = client.get(f"/admin/courses/{course_id}/tees/new")
        assert tee_new_page.status_code == 200
        tee_csrf = _extract_csrf(tee_new_page.text)
        data = dict(spec)
        data["gender"] = "men"
        data["csrf_token"] = tee_csrf
        response = client.post(f"/admin/courses/{course_id}/tees/new", data=data, follow_redirects=False)
        assert response.status_code == 303, response.text

    edit_page = client.get(f"/admin/courses/{course_id}/edit")
    assert "Deer" in edit_page.text
    assert "Snake" in edit_page.text

    session = Session(bind=client.app.state.engine)
    try:
        from golf_league.models import TeeRating, TeeSet

        tee_sets = session.query(TeeSet).filter_by(course_id=int(course_id)).all()
        assert len(tee_sets) == 2
        totals = {t.name: t.total_yards for t in tee_sets}
        assert totals["Deer"] == 5707
        assert totals["Snake"] == 4967
        ratings = (
            session.query(TeeRating)
            .join(TeeSet, TeeRating.tee_set_id == TeeSet.id)
            .filter(TeeSet.course_id == int(course_id))
            .all()
        )
        assert len(ratings) == 6
    finally:
        session.close()


def test_post_without_csrf_is_rejected_with_403(empty_client):
    client = _admin_client(empty_client)
    response = client.post(
        "/admin/courses/new",
        data={"name": "No CSRF Club", "total_holes": "18", "csrf_token": "bogus"},
    )
    assert response.status_code == 403


def test_anonymous_gets_401_and_verified_non_admin_gets_403(empty_client):
    client = empty_client
    anon_response = client.get("/admin/courses")
    assert anon_response.status_code == 401

    _make_user(client, email="player@example.test", password="s3cret-pw!", verified=True, is_admin=False)
    _login(client, "player@example.test", "s3cret-pw!")
    non_admin_response = client.get("/admin/courses")
    assert non_admin_response.status_code == 403


def test_unknown_course_id_is_404(empty_client):
    client = _admin_client(empty_client)
    response = client.get("/admin/courses/999999/edit")
    assert response.status_code == 404


def test_tee_id_from_another_course_is_404(empty_client):
    client = _admin_client(empty_client)

    def _create_course(name):
        page = client.get("/admin/courses/new")
        csrf = _extract_csrf(page.text)
        resp = client.post(
            "/admin/courses/new",
            data={"name": name, "total_holes": "18", "csrf_token": csrf},
            follow_redirects=False,
        )
        return resp.headers["location"].split("/")[3]

    course_a = _create_course("Course A")
    course_b = _create_course("Course B")

    tee_page = client.get(f"/admin/courses/{course_a}/tees/new")
    csrf = _extract_csrf(tee_page.text)
    data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "33.9", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": csrf,
    }
    resp = client.post(f"/admin/courses/{course_a}/tees/new", data=data, follow_redirects=False)
    tee_set_id = resp.headers["location"].rstrip("/").split("/")[-1]
    # location is /admin/courses/{course_a}/edit; fetch tee id from db instead
    session = Session(bind=client.app.state.engine)
    try:
        from golf_league.models import TeeSet

        tee_set = session.query(TeeSet).filter_by(course_id=int(course_a), name="Deer").one()
        tee_set_id = tee_set.id
    finally:
        session.close()

    response = client.get(f"/admin/courses/{course_b}/tees/{tee_set_id}/edit")
    assert response.status_code == 404


def test_invalid_rating_re_renders_422_and_writes_nothing(empty_client):
    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Invalid Rating Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
    tee_csrf = _extract_csrf(tee_page.text)
    data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "abc", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": tee_csrf,
    }
    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422

    session = Session(bind=client.app.state.engine)
    try:
        from golf_league.models import TeeSet

        assert session.query(TeeSet).filter_by(course_id=int(course_id)).count() == 0
    finally:
        session.close()


def test_incomplete_scope_set_re_renders_422_and_writes_nothing(empty_client):
    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Incomplete Scope Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
    tee_csrf = _extract_csrf(tee_page.text)
    data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "33.9", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        # full scope missing entirely
        "csrf_token": tee_csrf,
    }
    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422

    session = Session(bind=client.app.state.engine)
    try:
        from golf_league.models import TeeSet

        assert session.query(TeeSet).filter_by(course_id=int(course_id)).count() == 0
    finally:
        session.close()


def test_duplicate_tee_name_and_gender_re_renders_422_not_500(empty_client):
    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Dup Tee Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    def _post_tee():
        tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
        tee_csrf = _extract_csrf(tee_page.text)
        data = {
            "name": "Deer", "color_label": "White", "gender": "men",
            "total_yards": "5707", "sort_order": "1",
            "front_rating": "33.9", "front_slope": "114", "front_par": "36",
            "back_rating": "34.0", "back_slope": "111", "back_par": "36",
            "full_rating": "67.9", "full_slope": "113", "full_par": "72",
            "csrf_token": tee_csrf,
        }
        return client.post(f"/admin/courses/{course_id}/tees/new", data=data, follow_redirects=False)

    first = _post_tee()
    assert first.status_code == 303
    second = _post_tee()
    assert second.status_code == 422


def test_seeded_client_starts_with_wyandot_present(client):
    admin_client = _admin_client(client)
    response = admin_client.get("/admin/courses")
    assert response.status_code == 200
    assert "Wyandot Golf Club" in response.text
