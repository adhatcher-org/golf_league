"""Route-level tests for CSV roster import staging (GL-11)."""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import _extract_csrf
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.roster import derive_handicap_status
from golf_league.domain.roster_import import decode_warnings
from golf_league.models import Golfer, RosterImportBatch, RosterImportRow, TeeSet, User
from golf_league.services.auth import hash_password

FIXTURES = Path(__file__).parent / "fixtures" / "roster"


def _fixture_upload(name: str, field_name: str = "files") -> tuple:
    content = (FIXTURES / name).read_bytes()
    return (field_name, (name, io.BytesIO(content), "text/csv"))


def _regular_csv(rows: list[list[str]]) -> bytes:
    """Build a `summer_regular` CSV in memory from `[name, status, gold, white, email, phone]` rows."""
    import csv as csv_module

    buf = io.StringIO()
    writer = csv_module.writer(buf)
    writer.writerow(["Name", "Status", "Gold HC", "White HC", "Email", "Phone #"])
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _sub_csv(rows: list[list[str]]) -> bytes:
    """Build a `summer_sub` CSV in memory from `[first, last, phone, email, hc]` rows."""
    import csv as csv_module

    buf = io.StringIO()
    writer = csv_module.writer(buf)
    writer.writerow(["First Name", "Last Name", "Phone", "Email", "2025 HC"])
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _course_id(client) -> str:
    from golf_league.models import Course

    session = Session(bind=client.app.state.engine)
    try:
        course = session.execute(select(Course)).scalars().first()
        return str(course.id)
    finally:
        session.close()


def _upload(client, csrf_token, *, course_id, files):
    return client.post(
        "/admin/roster/imports/new",
        data={"course_id": course_id, "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )


def _new_page_csrf(client) -> tuple:
    page = client.get("/admin/roster/imports/new")
    return page, _extract_csrf(page.text)


def _make_other_user(client, *, email, password, is_admin=False):
    session = Session(bind=client.app.state.engine)
    try:
        user = User(
            username=email,
            email=email,
            display_name="Other",
            password_hash=hash_password(password),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None),
            is_admin=is_admin,
        )
        session.add(user)
        session.commit()
    finally:
        session.close()


def test_staging_a_regular_csv_creates_a_batch_and_rows_and_leaves_golfers_untouched(
    admin_client,
):
    session = Session(bind=admin_client.app.state.engine)
    golfers_before = session.execute(select(Golfer)).scalars().all()
    session.close()
    assert golfers_before == []

    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/admin/roster/imports/")

    session = Session(bind=admin_client.app.state.engine)
    try:
        batches = session.execute(select(RosterImportBatch)).scalars().all()
        assert len(batches) == 1
        rows = session.execute(select(RosterImportRow)).scalars().all()
        assert len(rows) == 2
        golfers_after = session.execute(select(Golfer)).scalars().all()
        assert golfers_after == []
    finally:
        session.close()


def test_staging_a_sub_csv_records_source_role_summer_sub_and_no_tee_label(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_blank_hc.csv")],
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = session.execute(select(RosterImportRow)).scalars().all()
        assert len(rows) == 3
        for row in rows:
            assert row.source_role == "summer_sub"
            assert row.tee_label is None
    finally:
        session.close()


def test_one_batch_may_hold_a_regular_file_and_a_sub_file(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[
            _fixture_upload("regular_two_rows.csv"),
            _fixture_upload("sub_blank_hc.csv"),
        ],
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        batches = session.execute(select(RosterImportBatch)).scalars().all()
        assert len(batches) == 1
        rows = (
            session.execute(
                select(RosterImportRow).order_by(RosterImportRow.position)
            )
            .scalars()
            .all()
        )
        assert [row.source_role for row in rows] == [
            "summer_regular", "summer_regular", "summer_sub", "summer_sub", "summer_sub",
        ]
        assert [row.position for row in rows] == [1, 2, 3, 4, 5]
    finally:
        session.close()


def test_the_upload_form_preselects_the_only_course(admin_client):
    page = admin_client.get("/admin/roster/imports/new")
    assert page.status_code == 200
    course_id = _course_id(admin_client)
    assert f'value="{course_id}" selected' in page.text


def test_the_batch_records_the_course_the_admin_a_ninety_day_expiry_and_zero_result_counters(
    admin_client,
):
    _, csrf_token = _new_page_csrf(admin_client)
    course_id = _course_id(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=course_id,
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        batch = session.execute(select(RosterImportBatch)).scalar_one()
        assert str(batch.course_id) == course_id
        assert batch.created_count == 0
        assert batch.updated_count == 0
        assert batch.unchanged_count == 0
        assert batch.skipped_count == 0
        assert batch.state == "staged"
        assert batch.applied_at is None
        delta = batch.expires_at - batch.created_at
        assert delta == timedelta(days=90)
    finally:
        session.close()


def test_a_quoted_comma_row_keeps_its_original_text_in_raw_line(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_quoted_comma.csv")],
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(select(RosterImportRow)).scalar_one()
        assert row.first_name == "Jo Ann"
        assert row.last_name == "de Vries"
        assert '"de Vries, Jo Ann"' in row.raw_line
    finally:
        session.close()


def test_an_upload_with_no_file_is_422_and_stages_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client, csrf_token, course_id=_course_id(admin_client), files=[]
    )
    assert response.status_code == 422
    assert "Choose at least one CSV file." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_a_file_with_headers_and_no_data_rows_is_422_and_stages_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("headers_only.csv")],
    )
    assert response.status_code == 422
    assert "headers_only.csv has no data rows." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_a_header_only_file_beside_a_full_one_is_422_and_stages_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[
            _fixture_upload("headers_only.csv"),
            _fixture_upload("regular_two_rows.csv"),
        ],
    )
    assert response.status_code == 422
    assert "headers_only.csv has no data rows." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
        assert session.execute(select(RosterImportRow)).scalars().all() == []
    finally:
        session.close()


def test_a_named_zero_byte_file_is_422_for_its_header_set(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("empty.csv", io.BytesIO(b""), "text/csv"))],
    )
    assert response.status_code == 422
    assert "Unrecognized column headers." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_a_row_blank_in_every_cell_is_skipped_and_not_staged(admin_client):
    csv_bytes = _regular_csv(
        [
            ["Example, Ann", "Gold", "5", "6", "ann.example@example.test", "555-0100"],
            ["", "", "", "", "", ""],
        ]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("regular.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = session.execute(select(RosterImportRow)).scalars().all()
        assert len(rows) == 1
        assert rows[0].position == 1
    finally:
        session.close()


def test_an_upload_over_two_megabytes_is_422_and_stages_nothing(admin_client):
    big_rows = [
        ["Example, Ann", "Gold", "5", "6", "ann.example@example.test", "x" * 100]
        for _ in range(20000)
    ]
    csv_bytes = _regular_csv(big_rows)
    assert len(csv_bytes) > 2 * 1024 * 1024

    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("big.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 422
    assert "Upload is larger than 2 MB." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_more_than_one_thousand_rows_is_422_and_stages_nothing(admin_client):
    rows = [
        ["Example, Ann", "Gold", "5", "6", f"ann{i}@example.test", "555-0100"]
        for i in range(1001)
    ]
    csv_bytes = _regular_csv(rows)

    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("many.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 422
    assert "More than 1000 rows in one upload." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_an_unsupported_header_set_is_422_and_stages_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("bad_headers.csv")],
    )
    assert response.status_code == 422
    assert "Unrecognized column headers." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_invalid_utf8_bytes_are_422_and_stage_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("bad_encoding.csv")],
    )
    assert response.status_code == 422
    assert "File is not valid UTF-8 text." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_a_course_without_exactly_one_mens_gold_and_one_mens_white_tee_is_422(
    empty_admin_client,
):
    from golf_league.models import Course

    session = Session(bind=empty_admin_client.app.state.engine)
    try:
        course = Course(name="No Tees Course", total_holes=18)
        session.add(course)
        session.commit()
        session.refresh(course)
        course_id = str(course.id)
    finally:
        session.close()

    page = empty_admin_client.get("/admin/roster/imports/new")
    csrf_token = _extract_csrf(page.text)
    response = _upload(
        empty_admin_client,
        csrf_token,
        course_id=course_id,
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 422
    # Jinja autoescapes the apostrophes in the rendered message.
    assert "course_id" in response.text
    assert "Course needs exactly one men" in response.text
    assert "Gold and one men" in response.text
    assert "White tee." in response.text


def test_a_rejected_upload_re_renders_the_form_without_the_admins_input(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("bad_headers.csv")],
    )
    assert response.status_code == 422
    # Nothing from the rejected file — its name or its contents — is echoed
    # back into the re-rendered form.
    assert "bad_headers.csv" not in response.text
    assert "nomatch.someone@example.test" not in response.text
    assert "Unrecognized column headers." in response.text


def test_a_file_name_with_directory_separators_and_control_characters_is_stored_as_display_text(
    admin_client,
):
    # `sanitize_display_name` itself (exercised directly, with a real
    # control character, in test_roster_import_parse.py) strips directory
    # separators and control characters. At the HTTP layer, the test
    # transport (httpx multipart encoding) percent-encodes a raw NUL byte
    # in a filename before it ever reaches the server, so this route-level
    # test proves the part an HTTP client can actually send: a path-like
    # filename is reduced to display text, never used as a path.
    csv_bytes = _regular_csv(
        [["Example, Ann", "Gold", "5", "6", "ann.example@example.test", "555-0100"]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[
            (
                "files",
                ("../../etc/roster.csv", io.BytesIO(csv_bytes), "text/csv"),
            )
        ],
    )
    assert response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        batch = session.execute(select(RosterImportBatch)).scalar_one()
        assert batch.source_display_name == "roster.csv"
        row = session.execute(select(RosterImportRow)).scalar_one()
        assert row.source_file == "roster.csv"
    finally:
        session.close()


def _batch_id_from(response) -> int:
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _make_golfer(
    client,
    *,
    email,
    first_name="Pat",
    last_name="Existing",
    phone="555-9999",
    tee_name="Deer",
    handicap_strokes=10,
) -> int:
    session = Session(bind=client.app.state.engine)
    try:
        tee_set = (
            session.execute(select(TeeSet).where(TeeSet.name == tee_name))
            .scalars()
            .first()
        )
        golfer = Golfer(
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            default_tee_set_id=tee_set.id,
            handicap_strokes=handicap_strokes,
            handicap_source="self_reported",
            handicap_status=derive_handicap_status(
                email=email, handicap_strokes=handicap_strokes
            ),
        )
        session.add(golfer)
        session.commit()
        session.refresh(golfer)
        return golfer.id
    finally:
        session.close()


def _consume_initial_batch(client) -> None:
    """Stage and mark-applied a throwaway batch so the next one is not initial."""
    _, csrf_token = _new_page_csrf(client)
    csv_bytes = _regular_csv(
        [["Consume, Initial", "Gold", "1", "1", "consume.initial@example.test", ""]]
    )
    response = _upload(
        client,
        csrf_token,
        course_id=_course_id(client),
        files=[("files", ("consume.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)
    session = Session(bind=client.app.state.engine)
    try:
        batch = session.get(RosterImportBatch, batch_id)
        batch.state = "applied"
        session.commit()
    finally:
        session.close()


def test_the_first_batch_is_initial_and_a_later_batch_is_not_once_one_is_applied(
    admin_client,
):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 303
    first_batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        first_batch = session.get(RosterImportBatch, first_batch_id)
        assert first_batch.is_initial is True
        first_batch.state = "applied"
        session.commit()
    finally:
        session.close()

    _, csrf_token = _new_page_csrf(admin_client)
    response2 = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_blank_hc.csv")],
    )
    assert response2.status_code == 303
    second_batch_id = _batch_id_from(response2)

    session = Session(bind=admin_client.app.state.engine)
    try:
        second_batch = session.get(RosterImportBatch, second_batch_id)
        assert second_batch.is_initial is False
    finally:
        session.close()


def test_a_row_whose_email_matches_a_golfer_with_a_different_phone_is_protected_field_changed(
    admin_client,
):
    _consume_initial_batch(admin_client)
    _make_golfer(
        admin_client,
        email="pat.existing@example.test",
        first_name="Pat",
        last_name="Existing",
        phone="555-9999",
        tee_name="Deer",
        handicap_strokes=10,
    )

    csv_bytes = _regular_csv(
        [["Existing, Pat", "White", "1", "10", "pat.existing@example.test", "555-1111"]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error == "protected_field_changed"
    finally:
        session.close()


def test_a_formatting_only_phone_difference_is_not_a_protected_field_change(admin_client):
    _consume_initial_batch(admin_client)
    _make_golfer(
        admin_client,
        email="pat.existing@example.test",
        first_name="Pat",
        last_name="Existing",
        phone="5550102000",
        tee_name="Deer",
        handicap_strokes=10,
    )

    csv_bytes = _regular_csv(
        [
            [
                "Existing, Pat", "White", "1", "10",
                "pat.existing@example.test", "(555) 010-2000",
            ]
        ]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error is None
    finally:
        session.close()


def test_a_blank_cell_is_never_a_protected_field_change(admin_client):
    _consume_initial_batch(admin_client)
    _make_golfer(
        admin_client,
        email="pat.existing@example.test",
        first_name="Pat",
        last_name="Existing",
        phone="555-9999",
        tee_name="Deer",
        handicap_strokes=10,
    )

    # A sub row has no tee column (tee_label always NULL) and this one
    # leaves phone and handicap blank too — none of the three may be
    # compared against the matched golfer's stored values.
    csv_bytes = _sub_csv([["Pat", "Existing", "", "pat.existing@example.test", ""]])
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("s.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error is None
    finally:
        session.close()


def test_every_row_sharing_a_duplicated_email_carries_email_duplicate_in_batch(
    admin_client,
):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_duplicate_email.csv")],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = (
            session.execute(
                select(RosterImportRow)
                .where(RosterImportRow.batch_id == batch_id)
                .order_by(RosterImportRow.position)
            )
            .scalars()
            .all()
        )
        assert [row.validation_error for row in rows] == [
            "email_duplicate_in_batch",
            "email_duplicate_in_batch",
        ]
    finally:
        session.close()


def test_a_sub_row_for_a_new_golfer_is_tee_required_for_new_golfer_outside_an_initial_batch(
    admin_client,
):
    _consume_initial_batch(admin_client)
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_near_miss_domain.csv")],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error == "tee_required_for_new_golfer"
    finally:
        session.close()


def test_an_initial_batch_records_tee_defaulted_white_and_handicap_defaulted_zero_instead(
    admin_client,
):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_blank_hc.csv")],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        batch = session.get(RosterImportBatch, batch_id)
        assert batch.is_initial is True
        row = session.execute(
            select(RosterImportRow).where(
                RosterImportRow.batch_id == batch_id,
                RosterImportRow.email == "cara.sub@example.test",
            )
        ).scalar_one()
        assert row.validation_error is None
        codes = set(decode_warnings(row.warnings))
        assert "handicap_defaulted_zero" in codes
        assert "tee_defaulted_white" in codes
    finally:
        session.close()


def test_a_landing_value_is_never_recorded_for_a_row_matching_an_existing_golfer(
    admin_client,
):
    _make_golfer(
        admin_client,
        email="nohc.golfer@example.test",
        first_name="No",
        last_name="Handicap",
        phone=None,
        tee_name="Deer",
        handicap_strokes=None,
    )

    csv_bytes = _sub_csv([["No", "Handicap", "", "nohc.golfer@example.test", ""]])
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("s.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        batch = session.get(RosterImportBatch, batch_id)
        assert batch.is_initial is True
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        codes = set(decode_warnings(row.warnings))
        assert "handicap_blank" in codes
        assert "handicap_defaulted_zero" not in codes
        assert "tee_defaulted_white" not in codes
        assert row.validation_error is None
    finally:
        session.close()


def test_a_row_with_a_hard_error_still_carries_its_warnings(admin_client):
    csv_bytes = _regular_csv(
        [["Bad, Combo", "Gold", "3.5", "", "combo.bad@icould.com", ""]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error == "handicap_invalid"
        assert "email_domain_near_miss" in decode_warnings(row.warnings)
    finally:
        session.close()


def test_every_staged_row_starts_included_and_not_opted_in(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalars().all()
        assert rows
        for row in rows:
            assert row.included is True
            assert row.update_opt_in is False
    finally:
        session.close()


def test_post_without_a_csrf_token_is_403_and_stages_nothing(admin_client):
    response = _upload(
        admin_client,
        "",
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 403

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()

    purge_response = admin_client.post(
        "/admin/roster/imports/purge", data={"csrf_token": ""}
    )
    assert purge_response.status_code == 403


def test_anonymous_is_401_and_a_verified_non_admin_is_403_on_every_import_route(client):
    assert client.get("/admin/roster/imports/new").status_code == 401
    assert (
        client.post(
            "/admin/roster/imports/new", data={"course_id": "1", "csrf_token": ""}
        ).status_code
        == 401
    )
    assert client.get("/admin/roster/imports/1").status_code == 401
    assert (
        client.post(
            "/admin/roster/imports/purge", data={"csrf_token": ""}
        ).status_code
        == 401
    )

    _make_other_user(client, email="member@example.test", password="s3cret-pw!")
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    login_response = client.post(
        "/login",
        data={
            "email": "member@example.test",
            "password": "s3cret-pw!",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    assert client.get("/admin/roster/imports/new").status_code == 403
    assert (
        client.post(
            "/admin/roster/imports/new", data={"course_id": "1", "csrf_token": ""}
        ).status_code
        == 403
    )
    assert client.get("/admin/roster/imports/1").status_code == 403
    assert (
        client.post(
            "/admin/roster/imports/purge", data={"csrf_token": ""}
        ).status_code
        == 403
    )


def test_an_unknown_batch_id_is_404(admin_client):
    response = admin_client.get("/admin/roster/imports/999999")
    assert response.status_code == 404


def test_the_review_page_shows_raw_line_and_escapes_markup_in_a_name(admin_client):
    csv_bytes = _regular_csv(
        [["<b>Script</b>, Name", "Gold", "5", "6", "script.name@example.test", ""]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    review = admin_client.get(response.headers["location"])
    assert review.status_code == 200
    assert "<b>Script</b>" not in review.text
    assert "&lt;b&gt;Script&lt;/b&gt;" in review.text
    assert "script.name@example.test" in review.text


def test_the_review_page_has_staged_review_controls(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 303
    review = admin_client.get(response.headers["location"])
    assert review.status_code == 200
    assert "/rows/" in review.text
    assert "/apply" in review.text
    assert "/discard" in review.text
    assert 'name="csrf_token"' in review.text


def test_purge_deletes_expired_batches_and_their_rows_and_keeps_the_rest(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    old_response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert old_response.status_code == 303
    old_batch_id = _batch_id_from(old_response)

    _, csrf_token = _new_page_csrf(admin_client)
    keep_response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_blank_hc.csv")],
    )
    assert keep_response.status_code == 303
    keep_batch_id = _batch_id_from(keep_response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        old_batch = session.get(RosterImportBatch, old_batch_id)
        old_batch.expires_at = old_batch.created_at - timedelta(days=1)
        session.commit()
    finally:
        session.close()

    _, csrf_token = _new_page_csrf(admin_client)
    purge_response = admin_client.post(
        "/admin/roster/imports/purge",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert purge_response.status_code == 303
    assert purge_response.headers["location"] == "/admin/roster/imports/new"

    session = Session(bind=admin_client.app.state.engine)
    try:
        remaining_batches = {
            b.id for b in session.execute(select(RosterImportBatch)).scalars().all()
        }
        assert remaining_batches == {keep_batch_id}
        remaining_rows = session.execute(select(RosterImportRow)).scalars().all()
        assert remaining_rows
        assert all(r.batch_id == keep_batch_id for r in remaining_rows)
    finally:
        session.close()


def test_no_email_phone_or_raw_line_appears_in_any_log_record_or_exception_message(
    admin_client, caplog
):
    with caplog.at_level("DEBUG"):
        _, csrf_token = _new_page_csrf(admin_client)
        response = _upload(
            admin_client,
            csrf_token,
            course_id=_course_id(admin_client),
            files=[_fixture_upload("regular_two_rows.csv")],
        )
        assert response.status_code == 303

        _, csrf_token = _new_page_csrf(admin_client)
        bad_response = _upload(
            admin_client,
            csrf_token,
            course_id=_course_id(admin_client),
            files=[_fixture_upload("bad_encoding.csv")],
        )
        assert bad_response.status_code == 422

    forbidden = [
        "ann.example@example.test", "bob.sample@example.test", "555-0100", "555-0101",
    ]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    for value in forbidden:
        assert value not in log_text


# --- Additional coverage: the fixtures above are all named in the spec's
# "Tests required" list, but the tests above never uploaded three of them
# (regular_bad_status.csv, regular_bad_numbers.csv, sub_missing_email.csv).
# The tests below use those three, plus a few more route-level branches
# (course_id validation, name_unparseable, each protected-field variant,
# the review page's near-miss suggestion, a reversed-name pair, and an
# empty purge) that the tests above did not reach.


def test_a_non_numeric_course_id_is_422_and_stages_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id="not-a-number",
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 422
    assert "Choose a course." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_an_unknown_numeric_course_id_is_422_and_stages_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id="999999",
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 422
    assert "Choose a course." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_regular_bad_status_csv_reports_status_blank_and_status_unknown(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_bad_status.csv")],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = (
            session.execute(
                select(RosterImportRow)
                .where(RosterImportRow.batch_id == batch_id)
                .order_by(RosterImportRow.position)
            )
            .scalars()
            .all()
        )
        assert [row.validation_error for row in rows] == [
            "status_blank",
            "status_unknown",
        ]
    finally:
        session.close()


def test_regular_bad_numbers_csv_reports_handicap_invalid_for_every_row(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_bad_numbers.csv")],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalars().all()
        assert len(rows) == 4
        assert all(row.validation_error == "handicap_invalid" for row in rows)
    finally:
        session.close()


def test_sub_missing_email_csv_reports_email_missing_and_email_malformed(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_missing_email.csv")],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = (
            session.execute(
                select(RosterImportRow)
                .where(RosterImportRow.batch_id == batch_id)
                .order_by(RosterImportRow.position)
            )
            .scalars()
            .all()
        )
        assert [row.validation_error for row in rows] == [
            "email_missing",
            "email_malformed",
        ]
    finally:
        session.close()


def test_a_name_that_fails_to_parse_reports_name_unparseable(admin_client):
    csv_bytes = _regular_csv(
        [["NoCommaHere", "Gold", "5", "6", "no.comma@example.test", ""]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error == "name_unparseable"
        assert row.first_name is None
        assert row.last_name is None
    finally:
        session.close()


def test_a_row_whose_email_matches_a_golfer_with_a_different_name_is_protected_field_changed(
    admin_client,
):
    _consume_initial_batch(admin_client)
    _make_golfer(
        admin_client,
        email="pat.existing@example.test",
        first_name="Pat",
        last_name="Existing",
        phone="555-9999",
        tee_name="Deer",
        handicap_strokes=10,
    )

    csv_bytes = _regular_csv(
        [["Existing, Patricia", "White", "1", "10", "pat.existing@example.test", "555-9999"]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error == "protected_field_changed"
    finally:
        session.close()


def test_a_row_whose_email_matches_a_golfer_with_a_different_tee_is_protected_field_changed(
    admin_client,
):
    _consume_initial_batch(admin_client)
    _make_golfer(
        admin_client,
        email="pat.existing@example.test",
        first_name="Pat",
        last_name="Existing",
        phone="555-9999",
        tee_name="Deer",  # White
        handicap_strokes=10,
    )

    # The row claims Gold (Snake) for a golfer whose tee on file is White
    # (Deer): the tee is a protected field, so this differs.
    csv_bytes = _regular_csv(
        [["Existing, Pat", "Gold", "10", "1", "pat.existing@example.test", "555-9999"]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error == "protected_field_changed"
    finally:
        session.close()


def test_a_row_whose_email_matches_a_golfer_with_a_different_handicap_is_protected_field_changed(
    admin_client,
):
    _consume_initial_batch(admin_client)
    _make_golfer(
        admin_client,
        email="pat.existing@example.test",
        first_name="Pat",
        last_name="Existing",
        phone="555-9999",
        tee_name="Deer",
        handicap_strokes=10,
    )

    csv_bytes = _regular_csv(
        [["Existing, Pat", "White", "1", "4", "pat.existing@example.test", "555-9999"]]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        row = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalar_one()
        assert row.validation_error == "protected_field_changed"
    finally:
        session.close()


def test_the_review_page_shows_a_near_miss_suggestion_for_a_flagged_email(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("sub_near_miss_domain.csv")],
    )
    assert response.status_code == 303
    review = admin_client.get(response.headers["location"])
    assert review.status_code == 200
    assert "email_domain_near_miss" in review.text
    assert "icloud.com" in review.text


def test_name_possibly_reversed_is_recorded_through_the_full_pipeline(admin_client):
    csv_bytes = _regular_csv(
        [
            ["Target, Reversed", "White", "0", "0", "target.reversed@example.test", ""],
            ["Reversed, Other", "Gold", "0", "0", "other.reversed@example.test", ""],
        ]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = (
            session.execute(
                select(RosterImportRow)
                .where(RosterImportRow.batch_id == batch_id)
                .order_by(RosterImportRow.position)
            )
            .scalars()
            .all()
        )
        # "Target, Reversed" has first name "Reversed", matching the other
        # row's last name "Reversed", with a different email.
        assert "name_possibly_reversed" in decode_warnings(rows[0].warnings)
        assert "name_possibly_reversed" not in decode_warnings(rows[1].warnings)
    finally:
        session.close()


def test_purge_with_nothing_expired_deletes_nothing(admin_client):
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[_fixture_upload("regular_two_rows.csv")],
    )
    assert response.status_code == 303

    _, csrf_token = _new_page_csrf(admin_client)
    purge_response = admin_client.post(
        "/admin/roster/imports/purge",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert purge_response.status_code == 303

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert len(session.execute(select(RosterImportBatch)).scalars().all()) == 1
        assert len(session.execute(select(RosterImportRow)).scalars().all()) == 2
    finally:
        session.close()


def test_revalidate_batch_is_a_no_op_for_an_unknown_batch_id(admin_client):
    from golf_league.services.roster_imports import revalidate_batch

    session = Session(bind=admin_client.app.state.engine)
    try:
        # No batch with this id exists; this must return quietly rather
        # than raise, since `revalidate_batch` documents itself as a
        # reusable function GL-12 calls again later against a batch id it
        # already has in hand.
        revalidate_batch(session, 999999)
    finally:
        session.close()


def test_revalidate_batch_is_a_no_op_for_a_batch_with_no_rows(admin_client):
    from golf_league.services.roster_imports import revalidate_batch

    course_id = int(_course_id(admin_client))
    session = Session(bind=admin_client.app.state.engine)
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        batch = RosterImportBatch(
            created_by_user_id=1,
            course_id=course_id,
            version=1,
            state="staged",
            is_initial=False,
            source_display_name="empty.csv",
            row_count=0,
            created_count=0,
            updated_count=0,
            unchanged_count=0,
            skipped_count=0,
            created_at=now,
            expires_at=now + timedelta(days=90),
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)
        # A batch that exists but has no rows: the loop body never runs,
        # and this must not raise.
        revalidate_batch(session, batch.id)
    finally:
        session.close()


def test_regular_rows_report_email_missing_email_malformed_and_phone_invalid(
    admin_client,
):
    csv_bytes = _regular_csv(
        [
            ["Blank, Email", "Gold", "5", "6", "", "555-0500"],
            ["Bad, Email", "Gold", "5", "6", "not-an-address", "555-0501"],
            [
                "Long, Phone", "White", "5", "6", "long.phone@example.test",
                "5" * 41,
            ],
        ]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("r.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = (
            session.execute(
                select(RosterImportRow)
                .where(RosterImportRow.batch_id == batch_id)
                .order_by(RosterImportRow.position)
            )
            .scalars()
            .all()
        )
        assert [row.validation_error for row in rows] == [
            "email_missing",
            "email_malformed",
            "phone_invalid",
        ]
    finally:
        session.close()


def test_sub_rows_report_name_unparseable_handicap_invalid_and_phone_invalid(
    admin_client,
):
    csv_bytes = _sub_csv(
        [
            ["", "Blank", "555-0600", "blank.first@example.test", "1"],
            ["Bad", "Handicap", "555-0601", "bad.handicap@example.test", "3.5"],
            ["Long", "Phone", "5" * 41, "long.phone.sub@example.test", "2"],
        ]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("s.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = (
            session.execute(
                select(RosterImportRow)
                .where(RosterImportRow.batch_id == batch_id)
                .order_by(RosterImportRow.position)
            )
            .scalars()
            .all()
        )
        assert [row.validation_error for row in rows] == [
            "name_unparseable",
            "handicap_invalid",
            "phone_invalid",
        ]
    finally:
        session.close()


def test_a_batch_of_entirely_blank_emails_stages_with_no_golfer_match_lookup(
    admin_client,
):
    # Every row's email is blank, so the service's internal golfer-lookup
    # has no address to query for (an empty `IN (...)` list is skipped
    # rather than sent to the database). This must behave identically to
    # any other batch from the outside: two hard-error rows, nothing
    # raised, nothing staged onto `golfers`.
    csv_bytes = _sub_csv(
        [
            ["Ann", "Blank", "555-0700", "", "1"],
            ["Bob", "Blank", "555-0701", "", "2"],
        ]
    )
    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("s.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 303
    batch_id = _batch_id_from(response)

    session = Session(bind=admin_client.app.state.engine)
    try:
        rows = session.execute(
            select(RosterImportRow).where(RosterImportRow.batch_id == batch_id)
        ).scalars().all()
        assert len(rows) == 2
        assert all(row.validation_error == "email_missing" for row in rows)
    finally:
        session.close()


def test_a_header_row_exceeding_the_csv_field_limit_is_unrecognized_column_headers(
    admin_client,
):
    # `csv.reader` raises `csv.Error` ("field larger than field limit")
    # for a single field beyond its configured limit; R-IMPORT-FILES
    # requires malformed CSV to be rejected the same way as an
    # unrecognized header set. This CSV is well under the 2 MB request
    # cap, so it reaches header parsing rather than being rejected for
    # size first.
    huge_field = "x" * 200000
    csv_bytes = f'"{huge_field}",Status,Gold HC,White HC,Email,Phone #\r\n'.encode()

    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("huge_header.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 422
    assert "Unrecognized column headers." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()


def test_a_data_row_exceeding_the_csv_field_limit_is_unrecognized_column_headers(
    admin_client,
):
    huge_field = "x" * 200000
    csv_bytes = (
        "Name,Status,Gold HC,White HC,Email,Phone #\r\n"
        f'"{huge_field}",Gold,5,6,huge.field@example.test,\r\n'
    ).encode()

    _, csrf_token = _new_page_csrf(admin_client)
    response = _upload(
        admin_client,
        csrf_token,
        course_id=_course_id(admin_client),
        files=[("files", ("huge_row.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert response.status_code == 422
    assert "Unrecognized column headers." in response.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        assert session.execute(select(RosterImportBatch)).scalars().all() == []
    finally:
        session.close()
