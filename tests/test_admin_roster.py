"""Route-level tests for the admin roster forms."""

import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from golf_league.models import Course, Golfer, TeeSet, User
from golf_league.services.auth import hash_password

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]*)"')
RENDERED_TEE_RE = re.compile(r'name="rendered_tee_set_id" value="([^"]*)"')
HANDICAP_VALUE_RE = re.compile(r'name="handicap_strokes" value="([^"]*)"')


def _extract_csrf(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, f"no csrf_token field found in: {html!r}"
    return match.group(1)


def _extract_rendered_tee(html: str) -> str:
    match = RENDERED_TEE_RE.search(html)
    assert match, f"no rendered_tee_set_id field found in: {html!r}"
    return match.group(1)


def _extract_handicap_value(html: str) -> str:
    match = HANDICAP_VALUE_RE.search(html)
    assert match, f"no handicap_strokes field found in: {html!r}"
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


def _deer_tee_set_id(client):
    session = Session(bind=client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        tee_set = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
        return tee_set.id
    finally:
        session.close()


def _snake_tee_set_id(client):
    session = Session(bind=client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        tee_set = session.query(TeeSet).filter_by(course_id=course.id, name="Snake").one()
        return tee_set.id
    finally:
        session.close()


def test_admin_creates_a_golfer_through_the_form(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)

    new_page = admin_client.get("/admin/golfers/new")
    assert new_page.status_code == 200
    csrf_token = _extract_csrf(new_page.text)

    response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "Ann",
            "last_name": "Diaz",
            "email": "ann.diaz@example.test",
            "phone": "555-0100",
            "default_tee_set_id": str(tee_id),
            "handicap_strokes": "12",
            "notes": "Left-handed",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    golfer_id = response.headers["location"].split("/")[3]

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, int(golfer_id))
        assert golfer.first_name == "Ann"
        assert golfer.last_name == "Diaz"
        assert golfer.email == "ann.diaz@example.test"
        assert golfer.default_tee_set_id == tee_id
        assert golfer.handicap_strokes == 12
        assert golfer.handicap_status == "ok"
    finally:
        session.close()

    list_page = admin_client.get("/admin/golfers")
    assert list_page.status_code == 200
    assert "Ann Diaz" in list_page.text
    assert "ok" in list_page.text


def test_creating_a_golfer_with_only_the_three_required_fields_succeeds(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)

    new_page = admin_client.get("/admin/golfers/new")
    csrf_token = _extract_csrf(new_page.text)

    response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "Min",
            "last_name": "Imal",
            "email": "",
            "phone": "",
            "default_tee_set_id": str(tee_id),
            "handicap_strokes": "",
            "notes": "",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    golfer_id = response.headers["location"].split("/")[3]

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, int(golfer_id))
        assert golfer.email is None
        assert golfer.phone is None
        assert golfer.handicap_strokes is None
        assert golfer.notes is None
        assert golfer.handicap_status == "needs_contact"
    finally:
        session.close()


def test_post_without_csrf_is_rejected_with_403(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)

    wrong_token_response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "No",
            "last_name": "Csrf",
            "default_tee_set_id": str(tee_id),
            "csrf_token": "bogus",
        },
    )
    assert wrong_token_response.status_code == 403

    absent_token_response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "No",
            "last_name": "Csrf",
            "default_tee_set_id": str(tee_id),
        },
    )
    assert absent_token_response.status_code == 403


def test_anonymous_gets_401_and_verified_non_admin_gets_403(client):
    anon_response = client.get("/admin/golfers")
    assert anon_response.status_code == 401

    _make_user(client, email="player@example.test", password="s3cret-pw!", verified=True, is_admin=False)
    _login(client, "player@example.test", "s3cret-pw!")
    non_admin_response = client.get("/admin/golfers")
    assert non_admin_response.status_code == 403


def test_unknown_golfer_id_is_404(client):
    admin_client = _admin_client(client)
    response = admin_client.get("/admin/golfers/999999/edit")
    assert response.status_code == 404


def test_duplicate_email_re_renders_422_not_500(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)

    first_page = admin_client.get("/admin/golfers/new")
    first_csrf = _extract_csrf(first_page.text)
    first_response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "First",
            "last_name": "Golfer",
            "email": "dup@example.test",
            "default_tee_set_id": str(tee_id),
            "csrf_token": first_csrf,
        },
        follow_redirects=False,
    )
    assert first_response.status_code == 303

    second_page = admin_client.get("/admin/golfers/new")
    second_csrf = _extract_csrf(second_page.text)
    second_response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "Second",
            "last_name": "Golfer",
            "email": "Dup@Example.test ",
            "default_tee_set_id": str(tee_id),
            "csrf_token": second_csrf,
        },
    )
    assert second_response.status_code == 422
    assert "<form" in second_response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.query(Golfer).count() == 1
    finally:
        session.close()


def test_invalid_handicap_re_renders_422_and_writes_nothing(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)

    page = admin_client.get("/admin/golfers/new")
    csrf_token = _extract_csrf(page.text)
    response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "Bad",
            "last_name": "Handicap",
            "default_tee_set_id": str(tee_id),
            "handicap_strokes": "3.5",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 422
    assert "<form" in response.text
    assert "handicap_strokes" in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.query(Golfer).count() == 0
    finally:
        session.close()


def test_a_name_containing_markup_is_escaped_in_the_list(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)

    page = admin_client.get("/admin/golfers/new")
    csrf_token = _extract_csrf(page.text)
    response = admin_client.post(
        "/admin/golfers/new",
        data={
            "first_name": "<script>alert(1)</script>",
            "last_name": "Golfer",
            "default_tee_set_id": str(tee_id),
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    list_page = admin_client.get("/admin/golfers")
    assert list_page.status_code == 200
    assert "<script>alert(1)</script>" not in list_page.text
    assert "&lt;script&gt;" in list_page.text


def _create_golfer(admin_client, tee_id, **overrides):
    page = admin_client.get("/admin/golfers/new")
    csrf_token = _extract_csrf(page.text)
    data = {
        "first_name": "Edit",
        "last_name": "Target",
        "email": "",
        "phone": "",
        "default_tee_set_id": str(tee_id),
        "handicap_strokes": "",
        "notes": "",
        "csrf_token": csrf_token,
    }
    data.update(overrides)
    response = admin_client.post("/admin/golfers/new", data=data, follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].split("/")[3])


def test_admin_edits_a_golfer_and_updates_its_handicap_and_status(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)
    golfer_id = _create_golfer(admin_client, tee_id)

    edit_page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    assert edit_page.status_code == 200
    edit_csrf = _extract_csrf(edit_page.text)

    response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "email": "edit.target@example.test",
            "phone": "",
            "default_tee_set_id": str(tee_id),
            "handicap_strokes": "0",
            "notes": "",
            "rendered_tee_set_id": str(tee_id),
            "csrf_token": edit_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, golfer_id)
        assert golfer.email == "edit.target@example.test"
        assert golfer.handicap_strokes == 0
        assert golfer.handicap_status == "ok"
    finally:
        session.close()

    reloaded = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    assert 'value="0"' in reloaded.text


def test_editing_a_golfer_with_invalid_handicap_re_renders_422_and_changes_nothing(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)
    golfer_id = _create_golfer(admin_client, tee_id, handicap_strokes="4")

    edit_page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)

    response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "email": "",
            "phone": "",
            "default_tee_set_id": str(tee_id),
            "handicap_strokes": "3.5",
            "notes": "",
            "rendered_tee_set_id": str(tee_id),
            "csrf_token": edit_csrf,
        },
    )
    assert response.status_code == 422
    assert "<form" in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, golfer_id)
        assert golfer.handicap_strokes == 4
    finally:
        session.close()


def test_edit_post_without_csrf_is_rejected_with_403(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)
    golfer_id = _create_golfer(admin_client, tee_id)

    response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "default_tee_set_id": str(tee_id),
            "csrf_token": "bogus",
        },
    )
    assert response.status_code == 403


def test_update_of_unknown_golfer_is_404(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)

    page = admin_client.get("/admin/golfers/new")
    csrf_token = _extract_csrf(page.text)
    response = admin_client.post(
        "/admin/golfers/999999/edit",
        data={
            "first_name": "Ghost",
            "last_name": "Golfer",
            "default_tee_set_id": str(tee_id),
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 404


def test_changing_the_tee_re_renders_once_with_a_blank_handicap(client):
    admin_client = _admin_client(client)
    deer_id = _deer_tee_set_id(admin_client)
    snake_id = _snake_tee_set_id(admin_client)
    golfer_id = _create_golfer(
        admin_client, deer_id, email="tee.change@example.test", handicap_strokes="12"
    )

    edit_page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    rendered_tee = _extract_rendered_tee(edit_page.text)
    assert rendered_tee == str(deer_id)

    response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "email": "tee.change@example.test",
            "phone": "",
            "default_tee_set_id": str(snake_id),
            "handicap_strokes": "12",
            "notes": "",
            "rendered_tee_set_id": rendered_tee,
            "csrf_token": edit_csrf,
        },
    )
    assert response.status_code == 422
    assert "<form" in response.text
    assert "handicap_strokes" in response.text

    # The new tee is selected, rendered_tee_set_id now names it, and the
    # old handicap number does not reappear in the field.
    assert f'value="{snake_id}" selected' in response.text
    assert _extract_rendered_tee(response.text) == str(snake_id)
    assert _extract_handicap_value(response.text) == ""
    assert "12" not in _extract_handicap_value(response.text)

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, golfer_id)
        assert golfer.default_tee_set_id == deer_id
        assert golfer.handicap_strokes == 12
    finally:
        session.close()


def test_the_second_submission_after_a_tee_change_saves(client):
    admin_client = _admin_client(client)
    deer_id = _deer_tee_set_id(admin_client)
    snake_id = _snake_tee_set_id(admin_client)
    golfer_id = _create_golfer(
        admin_client, deer_id, email="second.submit@example.test", handicap_strokes="12"
    )

    edit_page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    rendered_tee = _extract_rendered_tee(edit_page.text)

    first_response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "email": "second.submit@example.test",
            "phone": "",
            "default_tee_set_id": str(snake_id),
            "handicap_strokes": "12",
            "notes": "",
            "rendered_tee_set_id": rendered_tee,
            "csrf_token": edit_csrf,
        },
    )
    assert first_response.status_code == 422
    second_csrf = _extract_csrf(first_response.text)
    second_rendered_tee = _extract_rendered_tee(first_response.text)
    assert second_rendered_tee == str(snake_id)

    second_response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "email": "second.submit@example.test",
            "phone": "",
            "default_tee_set_id": str(snake_id),
            "handicap_strokes": "8",
            "notes": "",
            "rendered_tee_set_id": second_rendered_tee,
            "csrf_token": second_csrf,
        },
        follow_redirects=False,
    )
    assert second_response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, golfer_id)
        assert golfer.default_tee_set_id == snake_id
        assert golfer.handicap_strokes == 8
    finally:
        session.close()


def test_a_second_submission_may_clear_the_handicap_after_a_tee_change(client):
    admin_client = _admin_client(client)
    deer_id = _deer_tee_set_id(admin_client)
    snake_id = _snake_tee_set_id(admin_client)
    golfer_id = _create_golfer(
        admin_client, deer_id, email="clear.after.change@example.test", handicap_strokes="12"
    )

    edit_page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)
    rendered_tee = _extract_rendered_tee(edit_page.text)

    first_response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "email": "clear.after.change@example.test",
            "phone": "",
            "default_tee_set_id": str(snake_id),
            "handicap_strokes": "12",
            "notes": "",
            "rendered_tee_set_id": rendered_tee,
            "csrf_token": edit_csrf,
        },
    )
    assert first_response.status_code == 422
    second_csrf = _extract_csrf(first_response.text)
    second_rendered_tee = _extract_rendered_tee(first_response.text)

    second_response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "email": "clear.after.change@example.test",
            "phone": "",
            "default_tee_set_id": str(snake_id),
            "handicap_strokes": "",
            "notes": "",
            "rendered_tee_set_id": second_rendered_tee,
            "csrf_token": second_csrf,
        },
        follow_redirects=False,
    )
    assert second_response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, golfer_id)
        assert golfer.default_tee_set_id == snake_id
        assert golfer.handicap_strokes is None
        assert golfer.handicap_status == "needs_entry"
    finally:
        session.close()


def test_a_request_omitting_rendered_tee_set_id_cannot_skip_the_two_step(client):
    admin_client = _admin_client(client)
    tee_id = _deer_tee_set_id(admin_client)
    golfer_id = _create_golfer(admin_client, tee_id, handicap_strokes="6")

    edit_page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    edit_csrf = _extract_csrf(edit_page.text)

    # Same tee as before, but rendered_tee_set_id is entirely omitted from
    # the request — a hand-built bypass attempt.
    response = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Edit",
            "last_name": "Target",
            "default_tee_set_id": str(tee_id),
            "handicap_strokes": "9",
            "csrf_token": edit_csrf,
        },
    )
    assert response.status_code == 422
    assert "<form" in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        golfer = session.get(Golfer, golfer_id)
        assert golfer.default_tee_set_id == tee_id
        assert golfer.handicap_strokes == 6
    finally:
        session.close()
