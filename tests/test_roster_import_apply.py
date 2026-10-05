"""GL-12 route/service acceptance tests for staged roster import apply."""

import io

from conftest import _extract_csrf
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_admin_imports import (
    _batch_id_from,
    _course_id,
    _new_page_csrf,
    _regular_csv,
    _upload,
)

from golf_league.models import Golfer, RosterImportBatch, RosterImportRow


def _row_and_batch(client, batch_id, *, email=None):
    session = Session(bind=client.app.state.engine)
    try:
        batch = session.get(RosterImportBatch, batch_id)
        query = select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        if email is not None:
            query = query.where(RosterImportRow.email == email)
        row = session.execute(query).scalars().first()
        return batch.version, row.id, row.version
    finally:
        session.close()


def _edit(client, batch_id, row_id, batch_version, row_version, **overrides):
    data = {
        "csrf_token": _extract_csrf(client.get(f"/admin/roster/imports/{batch_id}").text),
        "batch_version": str(batch_version), "row_version": str(row_version),
        "action": "edit", "first_name": "Ann", "last_name": "Example",
        "email": "ann.example@example.test", "phone": "555-0100", "tee_label": "Gold",
        "handicap_gold": "", "handicap_white": "", "handicap_single": "5",
        "included": "true",
    }
    data.update(overrides)
    return client.post(f"/admin/roster/imports/{batch_id}/rows/{row_id}/edit", data=data, follow_redirects=False)


def test_corrected_invalid_handicap_applies_and_preserves_provenance(admin_client):
    csv_bytes = _regular_csv([["Example, Ann", "Gold", "bad", "6", "ann.example@example.test", "555-0100"]])
    _, csrf = _new_page_csrf(admin_client)
    batch_id = _batch_id_from(_upload(admin_client, csrf, course_id=_course_id(admin_client), files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))]))
    batch_version, row_id, row_version = _row_and_batch(admin_client, batch_id)
    assert _edit(admin_client, batch_id, row_id, batch_version, row_version).status_code == 303
    batch_version, _, _ = _row_and_batch(admin_client, batch_id)
    response = admin_client.post(f"/admin/roster/imports/{batch_id}/apply", data={"csrf_token": _extract_csrf(admin_client.get(f"/admin/roster/imports/{batch_id}").text), "batch_version": batch_version}, follow_redirects=False)
    assert response.status_code == 303
    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.get(RosterImportRow, row_id)
        assert row.raw_line.endswith("555-0100")
        assert row.handicap_single == 5
        assert session.execute(select(Golfer)).scalar_one().handicap_strokes == 5
        assert session.get(RosterImportBatch, batch_id).created_count == 1
    finally:
        session.close()


def test_stale_edit_is_conflict_and_discard_keeps_rows(admin_client):
    _, csrf = _new_page_csrf(admin_client)
    batch_id = _batch_id_from(_upload(admin_client, csrf, course_id=_course_id(admin_client), files=[("files", ("r.csv", io.BytesIO(_regular_csv([["Example, Ann", "Gold", "5", "6", "ann.example@example.test", ""]])), "text/csv"))]))
    batch_version, row_id, row_version = _row_and_batch(admin_client, batch_id)
    assert _edit(admin_client, batch_id, row_id, batch_version, row_version).status_code == 303
    assert _edit(admin_client, batch_id, row_id, batch_version, row_version).status_code == 409
    fresh_version, _, _ = _row_and_batch(admin_client, batch_id)
    response = admin_client.post(f"/admin/roster/imports/{batch_id}/discard", data={"csrf_token": _extract_csrf(admin_client.get(f"/admin/roster/imports/{batch_id}").text), "batch_version": fresh_version}, follow_redirects=False)
    assert response.status_code == 303
    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.get(RosterImportBatch, batch_id).state == "discarded"
        assert session.get(RosterImportRow, row_id) is not None
    finally:
        session.close()


def test_edit_rejects_a_row_from_another_batch_with_not_found(admin_client):
    _, csrf = _new_page_csrf(admin_client)
    first_batch = _batch_id_from(_upload(admin_client, csrf, course_id=_course_id(admin_client), files=[("files", ("first.csv", io.BytesIO(_regular_csv([["First, Ann", "Gold", "5", "6", "first@example.test", ""]])), "text/csv"))]))
    _, csrf = _new_page_csrf(admin_client)
    second_batch = _batch_id_from(_upload(admin_client, csrf, course_id=_course_id(admin_client), files=[("files", ("second.csv", io.BytesIO(_regular_csv([["Second, Bea", "Gold", "5", "6", "second@example.test", ""]])), "text/csv"))]))
    first_version, row_id, row_version = _row_and_batch(admin_client, first_batch)
    response = _edit(admin_client, second_batch, row_id, first_version, row_version)
    assert response.status_code == 404


def test_included_error_rolls_back_and_excluded_error_is_skipped(admin_client):
    csv_bytes = _regular_csv([
        ["Good, Ann", "Gold", "5", "6", "good@example.test", ""],
        ["Bad, Bea", "Gold", "bad", "6", "bad@example.test", ""],
    ])
    _, csrf = _new_page_csrf(admin_client)
    batch_id = _batch_id_from(_upload(admin_client, csrf, course_id=_course_id(admin_client), files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))]))
    version, _, _ = _row_and_batch(admin_client, batch_id, email="good@example.test")
    apply_data = {"csrf_token": _extract_csrf(admin_client.get(f"/admin/roster/imports/{batch_id}").text), "batch_version": version}
    assert admin_client.post(f"/admin/roster/imports/{batch_id}/apply", data=apply_data).status_code == 422
    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(Golfer)).scalars().all() == []
        bad = session.execute(select(RosterImportRow).where(RosterImportRow.email == "bad@example.test")).scalar_one()
        batch = session.get(RosterImportBatch, batch_id)
    finally:
        session.close()
    assert _edit(admin_client, batch_id, bad.id, batch.version, bad.version, included=False).status_code == 303
    version, _, _ = _row_and_batch(admin_client, batch_id, email="good@example.test")
    assert admin_client.post(f"/admin/roster/imports/{batch_id}/apply", data={"csrf_token": _extract_csrf(admin_client.get(f"/admin/roster/imports/{batch_id}").text), "batch_version": version}, follow_redirects=False).status_code == 303
