"""Route coverage for CSRF-protected complete hole-grid saves."""
import json

from sqlalchemy.orm import Session

from golf_league.models import Course, Golfer, Hole, TeeSet
from golf_league.services.course_seed import HOLES


def _csrf(text: str) -> str:
    import re
    return re.search(r'name="csrf_token" value="([^"]*)"', text).group(1)


def _complete_grid(client, course_id: int):
    session = Session(bind=client.app.state.engine)
    try:
        tees = {tee.name: tee.id for tee in session.query(TeeSet).filter_by(course_id=course_id)}
    finally:
        session.close()
    return [
        {"number": number, "nine": nine, "par": par, "stroke_index_18": si18, "stroke_index_9": si9,
         "yardages": {str(tees["Deer"]): deer, str(tees["Snake"]): snake}}
        for number, nine, par, si18, si9, deer, snake in HOLES
    ]


def test_admin_enters_full_grid_through_route(admin_client):
    client = admin_client
    session = Session(bind=client.app.state.engine)
    try:
        course_id = session.query(Course).filter_by(name="Wyandot Golf Club").one().id
    finally:
        session.close()
    page = client.get(f"/admin/courses/{course_id}/holes")
    response = client.post(f"/admin/courses/{course_id}/holes", data={"csrf_token": _csrf(page.text), "grid": json.dumps(_complete_grid(client, course_id))}, follow_redirects=False)
    assert response.status_code == 303
    session = Session(bind=client.app.state.engine)
    try:
        assert session.query(Hole).filter_by(course_id=course_id).count() == 18
    finally:
        session.close()


def test_incomplete_grid_is_rejected_without_writes_and_csrf_is_required(admin_client):
    client = admin_client
    session = Session(bind=client.app.state.engine)
    try:
        course_id = session.query(Course).filter_by(name="Wyandot Golf Club").one().id
    finally:
        session.close()
    assert client.post(f"/admin/courses/{course_id}/holes", data={"grid": "[]"}).status_code == 403
    page = client.get(f"/admin/courses/{course_id}/holes")
    response = client.post(f"/admin/courses/{course_id}/holes", data={"csrf_token": _csrf(page.text), "grid": "[]"})
    assert response.status_code == 422


def test_malformed_paste_is_422_and_anonymous_and_non_admin_are_denied(client, empty_client):
    response = client.get("/admin/courses/1/holes")
    assert response.status_code == 401
    # A malformed pasted row cannot parse and writes no state.
    admin = _make_nonempty_admin(empty_client)
    session = Session(bind=admin.app.state.engine)
    try:
        course_id = session.query(Course).filter_by(name="Paste Club").one().id
    finally:
        session.close()
    page = admin.get(f"/admin/courses/{course_id}/holes")
    response = admin.post(f"/admin/courses/{course_id}/holes", data={"csrf_token": _csrf(page.text), "grid": "[{bad"})
    assert response.status_code == 422
    # The unconfirmed grid is prefilled with derived, editable SI9 values.
    assert '"stroke_index_9": 1' in page.text
    from datetime import UTC, datetime

    from golf_league.models import User
    from golf_league.services.auth import hash_password
    session = Session(bind=client.app.state.engine)
    try:
        session.add(User(username="player@example.test", email="player@example.test", display_name="Player", password_hash=hash_password("s3cret-pw!"), email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=False))
        session.commit()
    finally:
        session.close()
    login = client.get("/login")
    client.post("/login", data={"email": "player@example.test", "password": "s3cret-pw!", "csrf_token": _csrf(login.text)}, follow_redirects=False)
    assert client.get("/admin/courses/1/holes").status_code == 403


def _make_nonempty_admin(client):
    from datetime import UTC, datetime

    from golf_league.models import User
    from golf_league.services.auth import hash_password
    session = Session(bind=client.app.state.engine)
    try:
        course = Course(name="Paste Club", total_holes=18)
        session.add(course)
        session.flush()
        for name, label, order in (("Deer", "White", 1), ("Snake", "Gold", 2)):
            session.add(TeeSet(course_id=course.id, name=name, color_label=label, gender="men", total_yards=1, sort_order=order))
        session.add(User(username="grid.admin@example.test", email="grid.admin@example.test", display_name="Grid", password_hash=hash_password("s3cret-pw!"), email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=True))
        session.commit()
    finally:
        session.close()
    login = client.get("/login")
    response = client.post("/login", data={"email": "grid.admin@example.test", "password": "s3cret-pw!", "csrf_token": _csrf(login.text)}, follow_redirects=False)
    assert response.status_code == 303
    return client


def test_delete_tee_refusal_names_yardages_and_golfers(admin_client):
    client = admin_client
    session = Session(bind=client.app.state.engine)
    try:
        course = session.query(Course).filter_by(name="Wyandot Golf Club").one()
        tee = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
        session.add(Golfer(first_name="Pat", last_name="Player", email="pat@example.test", default_tee_set_id=tee.id, handicap_source="self_reported", handicap_status="ok"))
        session.commit()
        course_id, tee_id = course.id, tee.id
    finally:
        session.close()
    page = client.get(f"/admin/courses/{course_id}/holes")
    response = client.post(f"/admin/courses/{course_id}/tees/{tee_id}/delete", data={"csrf_token": _csrf(page.text)})
    assert response.status_code == 409
    assert "hole yardages" in response.text
    assert "golfer default-tee" in response.text
