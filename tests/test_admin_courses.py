"""Route-level tests for the admin course/tee/rating forms."""

import re
from datetime import UTC, datetime

import pytest
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


def test_admin_edits_a_tee_and_its_three_ratings_through_the_form(client):
    from decimal import Decimal

    from golf_league.models import Course, TeeRating, TeeSet

    admin_client = _admin_client(client)

    session = Session(bind=admin_client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        tee_set = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
        course_id, tee_id = course.id, tee_set.id
    finally:
        session.close()

    get_page = admin_client.get(f"/admin/courses/{course_id}/tees/{tee_id}/edit")
    assert get_page.status_code == 200
    assert 'value="67.9"' in get_page.text
    csrf = _extract_csrf(get_page.text)

    data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "33.9", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "68.2", "full_slope": "113", "full_par": "72",
        "csrf_token": csrf,
    }
    response = admin_client.post(
        f"/admin/courses/{course_id}/tees/{tee_id}/edit", data=data, follow_redirects=False
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        ratings = {
            r.scope: r
            for r in session.query(TeeRating).filter_by(tee_set_id=tee_id).all()
        }
        assert len(ratings) == 3
        assert ratings["full"].rating == Decimal("68.2")
        assert ratings["front"].rating == Decimal("33.9")
        assert ratings["back"].rating == Decimal("34.0")
    finally:
        session.close()


def test_edit_tee_with_invalid_rating_re_renders_422_and_changes_nothing(client):
    from decimal import Decimal

    from golf_league.models import Course, TeeRating, TeeSet

    admin_client = _admin_client(client)

    session = Session(bind=admin_client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        tee_set = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
        course_id, tee_id = course.id, tee_set.id
    finally:
        session.close()

    get_page = admin_client.get(f"/admin/courses/{course_id}/tees/{tee_id}/edit")
    csrf = _extract_csrf(get_page.text)

    data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "abc", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": csrf,
    }
    response = admin_client.post(f"/admin/courses/{course_id}/tees/{tee_id}/edit", data=data)
    assert response.status_code == 422

    session = Session(bind=admin_client.app.state.engine)
    try:
        ratings = {
            r.scope: r
            for r in session.query(TeeRating).filter_by(tee_set_id=tee_id).all()
        }
        assert ratings["front"].rating == Decimal("33.9")
        assert ratings["back"].rating == Decimal("34.0")
        assert ratings["full"].rating == Decimal("67.9")
    finally:
        session.close()


def test_edit_tee_to_a_duplicate_name_and_gender_re_renders_422(client):
    from golf_league.models import Course, TeeSet

    admin_client = _admin_client(client)

    session = Session(bind=admin_client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        snake = session.query(TeeSet).filter_by(course_id=course.id, name="Snake").one()
        course_id, tee_id = course.id, snake.id
    finally:
        session.close()

    get_page = admin_client.get(f"/admin/courses/{course_id}/tees/{tee_id}/edit")
    csrf = _extract_csrf(get_page.text)

    data = {
        "name": "Deer", "color_label": "Gold", "gender": "men",
        "total_yards": "4967", "sort_order": "2",
        "front_rating": "31.9", "front_slope": "107", "front_par": "36",
        "back_rating": "31.9", "back_slope": "110", "back_par": "36",
        "full_rating": "63.8", "full_slope": "109", "full_par": "72",
        "csrf_token": csrf,
    }
    response = admin_client.post(f"/admin/courses/{course_id}/tees/{tee_id}/edit", data=data)
    assert response.status_code == 422


def test_post_edit_tee_from_another_course_is_404_and_changes_nothing(empty_client):
    from golf_league.models import TeeRating, TeeSet

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
    client.post(f"/admin/courses/{course_a}/tees/new", data=data, follow_redirects=False)

    session = Session(bind=client.app.state.engine)
    try:
        tee_set = session.query(TeeSet).filter_by(course_id=int(course_a), name="Deer").one()
        tee_id = tee_set.id
    finally:
        session.close()

    edit_page = client.get(f"/admin/courses/{course_a}/tees/{tee_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    bad_data = dict(data)
    bad_data["csrf_token"] = edit_csrf
    bad_data["total_yards"] = "9999"
    response = client.post(
        f"/admin/courses/{course_b}/tees/{tee_id}/edit", data=bad_data, follow_redirects=False
    )
    assert response.status_code == 404

    session = Session(bind=client.app.state.engine)
    try:
        unchanged = session.get(TeeSet, tee_id)
        assert unchanged.total_yards == 5707
        assert unchanged.course_id == int(course_a)
        ratings = session.query(TeeRating).filter_by(tee_set_id=tee_id).all()
        assert len(ratings) == 3
    finally:
        session.close()


def test_admin_updates_a_course_through_the_form(empty_client):
    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Course To Edit", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    edit_page = client.get(f"/admin/courses/{course_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    response = client.post(
        f"/admin/courses/{course_id}/edit",
        data={
            "name": "Course To Edit",
            "city": "New City",
            "state": "",
            "website": "",
            "total_holes": "18",
            "csrf_token": edit_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    from golf_league.models import Course

    session = Session(bind=client.app.state.engine)
    try:
        course = session.get(Course, int(course_id))
        assert course.city == "New City"
    finally:
        session.close()

    edit_page2 = client.get(f"/admin/courses/{course_id}/edit")
    edit_csrf2 = _extract_csrf(edit_page2.text)
    bad_response = client.post(
        f"/admin/courses/{course_id}/edit",
        data={
            "name": "",
            "city": "New City",
            "state": "",
            "website": "",
            "total_holes": "18",
            "csrf_token": edit_csrf2,
        },
    )
    assert bad_response.status_code == 422
    assert "<form" in bad_response.text
    assert "Name is required." in bad_response.text


def test_create_course_with_invalid_input_re_renders_422_and_writes_nothing(empty_client):
    from golf_league.models import Course

    client = _admin_client(empty_client)

    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    empty_name_response = client.post(
        "/admin/courses/new",
        data={"name": "", "total_holes": "18", "csrf_token": csrf},
    )
    assert empty_name_response.status_code == 422
    assert "<form" in empty_name_response.text

    page2 = client.get("/admin/courses/new")
    csrf2 = _extract_csrf(page2.text)
    bad_holes_response = client.post(
        "/admin/courses/new",
        data={"name": "X", "total_holes": "abc", "csrf_token": csrf2},
    )
    assert bad_holes_response.status_code == 422
    assert "<form" in bad_holes_response.text

    page3 = client.get("/admin/courses/new")
    csrf3 = _extract_csrf(page3.text)
    first_response = client.post(
        "/admin/courses/new",
        data={"name": "Dup Club", "total_holes": "18", "csrf_token": csrf3},
        follow_redirects=False,
    )
    assert first_response.status_code == 303

    page4 = client.get("/admin/courses/new")
    csrf4 = _extract_csrf(page4.text)
    dup_response = client.post(
        "/admin/courses/new",
        data={"name": "Dup Club", "total_holes": "18", "csrf_token": csrf4},
    )
    assert dup_response.status_code == 422
    assert "<form" in dup_response.text

    session = Session(bind=client.app.state.engine)
    try:
        courses = session.query(Course).all()
        assert len(courses) == 1
        assert courses[0].name == "Dup Club"
    finally:
        session.close()


def test_update_course_to_a_duplicate_name_re_renders_422(empty_client):
    from golf_league.models import Course

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

    _create_course("Club A")
    course_b_id = _create_course("Club B")

    edit_page = client.get(f"/admin/courses/{course_b_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    response = client.post(
        f"/admin/courses/{course_b_id}/edit",
        data={
            "name": "Club A",
            "city": "",
            "state": "",
            "website": "",
            "total_holes": "18",
            "csrf_token": edit_csrf,
        },
    )
    assert response.status_code == 422

    session = Session(bind=client.app.state.engine)
    try:
        course_b = session.get(Course, int(course_b_id))
        assert course_b.name == "Club B"
    finally:
        session.close()


def test_post_new_tee_with_signaling_nan_rating_is_422_and_writes_nothing(empty_client):
    from golf_league.models import TeeSet

    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "sNaN Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
    tee_csrf = _extract_csrf(tee_page.text)
    data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "sNaN", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": tee_csrf,
    }
    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422

    session = Session(bind=client.app.state.engine)
    try:
        assert session.query(TeeSet).filter_by(course_id=int(course_id)).count() == 0
    finally:
        session.close()


def test_post_new_tee_with_overflowing_rating_is_422_and_writes_nothing(empty_client):
    from golf_league.models import TeeSet

    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Overflow Rating Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
    tee_csrf = _extract_csrf(tee_page.text)
    data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "1E+400", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": tee_csrf,
    }
    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422

    session = Session(bind=client.app.state.engine)
    try:
        assert session.query(TeeSet).filter_by(course_id=int(course_id)).count() == 0
    finally:
        session.close()


def test_seeded_client_starts_with_wyandot_present(client):
    admin_client = _admin_client(client)
    response = admin_client.get("/admin/courses")
    assert response.status_code == 200
    assert "Wyandot Golf Club" in response.text


@pytest.mark.parametrize("field", ["front_slope", "total_yards", "sort_order"])
def test_superscript_digit_in_a_tee_field_is_422_not_500(empty_client, field):
    from golf_league.models import TeeSet

    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Superscript Club", "total_holes": "18", "csrf_token": csrf},
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
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": tee_csrf,
    }
    data[field] = "²"
    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422
    assert "<form" in response.text

    session = Session(bind=client.app.state.engine)
    try:
        assert session.query(TeeSet).filter_by(course_id=int(course_id)).count() == 0
    finally:
        session.close()


def test_superscript_digit_in_total_holes_is_422_not_500(empty_client):
    from golf_league.models import Course

    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Superscript Holes Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    edit_page = client.get(f"/admin/courses/{course_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    response = client.post(
        f"/admin/courses/{course_id}/edit",
        data={
            "name": "Superscript Holes Club",
            "city": "",
            "state": "",
            "website": "",
            "total_holes": "²",
            "csrf_token": edit_csrf,
        },
    )
    assert response.status_code == 422
    assert "<form" in response.text

    session = Session(bind=client.app.state.engine)
    try:
        course = session.get(Course, int(course_id))
        assert course.total_holes == 18
    finally:
        session.close()


def test_post_new_tee_with_an_empty_name_re_renders_422(empty_client):
    from golf_league.models import TeeSet

    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Empty Name Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
    tee_csrf = _extract_csrf(tee_page.text)
    data = {
        "name": "", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "33.9", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": tee_csrf,
    }
    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422
    assert "<form" in response.text
    assert "Name is required." in response.text

    session = Session(bind=client.app.state.engine)
    try:
        assert session.query(TeeSet).filter_by(course_id=int(course_id)).count() == 0
    finally:
        session.close()


def test_post_new_tee_with_an_empty_color_label_re_renders_422(empty_client):
    from golf_league.models import TeeSet

    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": "Empty Color Club", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = create_resp.headers["location"].split("/")[3]

    tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
    tee_csrf = _extract_csrf(tee_page.text)
    data = {
        "name": "Deer", "color_label": "", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "33.9", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": tee_csrf,
    }
    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422
    assert "<form" in response.text
    assert "Color label is required." in response.text

    session = Session(bind=client.app.state.engine)
    try:
        assert session.query(TeeSet).filter_by(course_id=int(course_id)).count() == 0
    finally:
        session.close()


def test_post_edit_tee_with_an_empty_name_re_renders_422(client):
    from golf_league.models import Course, TeeSet

    admin_client = _admin_client(client)

    session = Session(bind=admin_client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        tee_set = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
        course_id, tee_id = course.id, tee_set.id
    finally:
        session.close()

    get_page = admin_client.get(f"/admin/courses/{course_id}/tees/{tee_id}/edit")
    csrf = _extract_csrf(get_page.text)

    data = {
        "name": "", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "33.9", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
        "csrf_token": csrf,
    }
    response = admin_client.post(f"/admin/courses/{course_id}/tees/{tee_id}/edit", data=data)
    assert response.status_code == 422
    assert "<form" in response.text
    assert "Name is required." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        unchanged = session.get(TeeSet, tee_id)
        assert unchanged.name == "Deer"
    finally:
        session.close()


@pytest.mark.parametrize("kind", ["create_course", "update_course", "create_tee", "update_tee"])
def test_post_with_an_absent_csrf_token_is_403(client, kind):
    from golf_league.models import Course, TeeSet

    admin_client = _admin_client(client)

    session = Session(bind=admin_client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        tee_set = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
        course_id, tee_id = course.id, tee_set.id
    finally:
        session.close()

    course_data = {
        "name": "Wyandot Golf Club", "city": "", "state": "", "website": "", "total_holes": "18",
    }
    tee_data = {
        "name": "Deer", "color_label": "White", "gender": "men",
        "total_yards": "5707", "sort_order": "1",
        "front_rating": "33.9", "front_slope": "114", "front_par": "36",
        "back_rating": "34.0", "back_slope": "111", "back_par": "36",
        "full_rating": "67.9", "full_slope": "113", "full_par": "72",
    }
    paths_and_data = {
        "create_course": ("/admin/courses/new", dict(course_data)),
        "update_course": (f"/admin/courses/{course_id}/edit", dict(course_data)),
        "create_tee": (f"/admin/courses/{course_id}/tees/new", dict(tee_data)),
        "update_tee": (f"/admin/courses/{course_id}/tees/{tee_id}/edit", dict(tee_data)),
    }
    path, data = paths_and_data[kind]
    assert "csrf_token" not in data

    response = admin_client.post(path, data=data, follow_redirects=False)
    assert response.status_code == 403


def _course_with_one_tee(client, course_name):
    """Create a course and a valid tee set through the forms; return both ids."""
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": course_name, "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = int(create_resp.headers["location"].split("/")[3])

    tee_page = client.get(f"/admin/courses/{course_id}/tees/new")
    tee_csrf = _extract_csrf(tee_page.text)
    tee_resp = client.post(
        f"/admin/courses/{course_id}/tees/new",
        data={
            "name": "Deer", "color_label": "White", "gender": "men",
            "total_yards": "5707", "sort_order": "1",
            "front_rating": "33.9", "front_slope": "114", "front_par": "36",
            "back_rating": "34.0", "back_slope": "111", "back_par": "36",
            "full_rating": "67.9", "full_slope": "113", "full_par": "72",
            "csrf_token": tee_csrf,
        },
        follow_redirects=False,
    )
    assert tee_resp.status_code == 303

    session = Session(bind=client.app.state.engine)
    try:
        from golf_league.models import TeeSet

        tee_set_id = session.query(TeeSet).filter_by(course_id=course_id).one().id
    finally:
        session.close()
    return course_id, tee_set_id


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [("front_rating", "0"), ("back_rating", "-33.9")],
)
def test_post_new_tee_with_a_non_positive_rating_re_renders_422(empty_client, field, bad_value):
    from golf_league.models import TeeSet

    client = _admin_client(empty_client)
    page = client.get("/admin/courses/new")
    csrf = _extract_csrf(page.text)
    create_resp = client.post(
        "/admin/courses/new",
        data={"name": f"Rating Bound Club {field}", "total_holes": "18", "csrf_token": csrf},
        follow_redirects=False,
    )
    course_id = int(create_resp.headers["location"].split("/")[3])

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
    data[field] = bad_value

    response = client.post(f"/admin/courses/{course_id}/tees/new", data=data)
    assert response.status_code == 422
    assert "<form" in response.text
    assert "Rating must be greater than zero." in response.text

    session = Session(bind=client.app.state.engine)
    try:
        assert session.query(TeeSet).filter_by(course_id=course_id).count() == 0
    finally:
        session.close()


def test_post_edit_tee_with_a_non_positive_rating_re_renders_422(empty_client):
    from decimal import Decimal

    from golf_league.models import TeeRating

    client = _admin_client(empty_client)
    course_id, tee_set_id = _course_with_one_tee(client, "Rating Bound Edit Club")

    edit_page = client.get(f"/admin/courses/{course_id}/tees/{tee_set_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    response = client.post(
        f"/admin/courses/{course_id}/tees/{tee_set_id}/edit",
        data={
            "name": "Deer", "color_label": "White", "gender": "men",
            "total_yards": "5707", "sort_order": "1",
            "front_rating": "0", "front_slope": "114", "front_par": "36",
            "back_rating": "34.0", "back_slope": "111", "back_par": "36",
            "full_rating": "67.9", "full_slope": "113", "full_par": "72",
            "csrf_token": edit_csrf,
        },
    )
    assert response.status_code == 422
    assert "<form" in response.text
    assert "Rating must be greater than zero." in response.text

    session = Session(bind=client.app.state.engine)
    try:
        stored = session.query(TeeRating).filter_by(tee_set_id=tee_set_id, scope="front").one()
        assert stored.rating == Decimal("33.9")
    finally:
        session.close()
