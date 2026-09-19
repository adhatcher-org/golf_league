"""Domain and service level tests for the roster."""

import pytest

from golf_league.domain.roster import (
    derive_handicap_status,
    normalize_email,
    normalize_name,
    normalize_phone,
    validate_handicap_strokes,
)
from golf_league.models import Golfer
from golf_league.services.roster import (
    RosterValidationError,
    create_golfer,
    get_golfer,
    update_golfer,
)


def _tee_set_id(course, name="Deer"):
    return next(t for t in course.tee_sets if t.name == name).id


def test_golfer_persists_and_reads_back_with_its_tee(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    created = create_golfer(
        session,
        first_name="Ann",
        last_name="Diaz",
        default_tee_set_id=tee_id,
        email="ann.diaz@example.test",
        handicap_strokes="5",
    )

    fetched = get_golfer(session, created.id)
    assert fetched is not None
    assert fetched.first_name == "Ann"
    assert fetched.last_name == "Diaz"
    assert fetched.default_tee_set_id == tee_id
    assert fetched.handicap_strokes == 5


def test_two_golfers_may_both_have_no_email(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    first = create_golfer(
        session, first_name="No", last_name="Email1", default_tee_set_id=tee_id
    )
    second = create_golfer(
        session, first_name="No", last_name="Email2", default_tee_set_id=tee_id
    )

    assert first.email is None
    assert second.email is None
    assert first.id != second.id


def test_same_normalized_email_cannot_create_two_golfers(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    create_golfer(
        session,
        first_name="Ann",
        last_name="X",
        default_tee_set_id=tee_id,
        email="Ann@X.test ",
    )

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session,
            first_name="Ann",
            last_name="Y",
            default_tee_set_id=tee_id,
            email="ann@x.test",
        )
    assert "email" in exc_info.value.errors


def test_blank_handicap_is_null_and_zero_is_scratch(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    blank = create_golfer(
        session,
        first_name="Blank",
        last_name="Handicap",
        default_tee_set_id=tee_id,
        handicap_strokes="",
    )
    scratch = create_golfer(
        session,
        first_name="Scratch",
        last_name="Golfer",
        default_tee_set_id=tee_id,
        handicap_strokes="0",
    )

    assert blank.handicap_strokes is None
    assert scratch.handicap_strokes == 0
    assert blank.handicap_strokes != scratch.handicap_strokes


def test_negative_handicap_survives_storage_and_read_back(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    created = create_golfer(
        session,
        first_name="Plus",
        last_name="Handicap",
        default_tee_set_id=tee_id,
        handicap_strokes="-3",
    )

    fetched = get_golfer(session, created.id)
    assert fetched.handicap_strokes == -3


def test_no_handicap_range_is_enforced(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    low = create_golfer(
        session,
        first_name="Low",
        last_name="Handicap",
        default_tee_set_id=tee_id,
        handicap_strokes="-20",
    )
    high = create_golfer(
        session,
        first_name="High",
        last_name="Handicap",
        default_tee_set_id=tee_id,
        handicap_strokes="54",
    )

    assert low.handicap_strokes == -20
    assert high.handicap_strokes == 54


@pytest.mark.parametrize("bad_value", ["3.5", "3.0", "+3", "abc", "1e3"])
def test_non_integer_handicap_is_rejected(session, wyandot_course, bad_value):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session,
            first_name="Bad",
            last_name="Handicap",
            default_tee_set_id=tee_id,
            handicap_strokes=bad_value,
        )
    assert "handicap_strokes" in exc_info.value.errors
    assert session.query(Golfer).count() == 0


def test_a_non_decimal_unicode_digit_is_rejected_without_raising(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session,
            first_name="Super",
            last_name="Script",
            default_tee_set_id=tee_id,
            handicap_strokes="²",
        )
    assert "handicap_strokes" in exc_info.value.errors


def test_handicap_status_is_needs_contact_when_email_is_absent():
    status = derive_handicap_status(email=None, handicap_strokes=10)
    assert status == "needs_contact"


def test_handicap_status_is_needs_entry_when_strokes_are_absent_and_email_is_present():
    status = derive_handicap_status(email="a@b.test", handicap_strokes=None)
    assert status == "needs_entry"


def test_handicap_status_is_ok_when_both_are_present():
    status = derive_handicap_status(email="a@b.test", handicap_strokes=0)
    assert status == "ok"


def test_phone_is_stored_exactly_as_entered(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    created = create_golfer(
        session,
        first_name="Phone",
        last_name="Golfer",
        default_tee_set_id=tee_id,
        phone=" (614) 555-0100 ext. 2 ",
    )

    assert created.phone == "(614) 555-0100 ext. 2"
    assert normalize_phone(" (614) 555-0100 ext. 2 ") == "(614) 555-0100 ext. 2"


def test_name_whitespace_is_collapsed_but_case_is_kept():
    assert normalize_name("  de   Vries  ") == "de Vries"
    assert normalize_name("  McLeod ") == "McLeod"


def test_unknown_tee_set_id_is_a_validation_error_not_a_crash(session, wyandot_course):
    wyandot_course()

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session,
            first_name="No",
            last_name="Tee",
            default_tee_set_id=999999,
        )
    assert "default_tee_set_id" in exc_info.value.errors


def test_changing_the_tee_without_supplying_a_handicap_is_rejected(session, golfer, wyandot_course):
    course = wyandot_course()
    other_tee_id = _tee_set_id(course, "Snake")
    assert other_tee_id != golfer.default_tee_set_id

    with pytest.raises(RosterValidationError) as exc_info:
        update_golfer(
            session,
            golfer.id,
            first_name=golfer.first_name,
            last_name=golfer.last_name,
            default_tee_set_id=other_tee_id,
        )
    assert "handicap_strokes" in exc_info.value.errors

    unchanged = get_golfer(session, golfer.id)
    assert unchanged.default_tee_set_id == golfer.default_tee_set_id
    assert unchanged.handicap_strokes == golfer.handicap_strokes


def test_changing_the_tee_with_an_explicit_blank_handicap_sets_null(session, golfer, wyandot_course):
    course = wyandot_course()
    other_tee_id = _tee_set_id(course, "Snake")
    assert other_tee_id != golfer.default_tee_set_id

    updated = update_golfer(
        session,
        golfer.id,
        first_name=golfer.first_name,
        last_name=golfer.last_name,
        default_tee_set_id=other_tee_id,
        handicap_strokes="",
    )

    assert updated.default_tee_set_id == other_tee_id
    assert updated.handicap_strokes is None


def test_a_failed_update_rolls_back_and_changes_nothing(session, golfer):
    original_first_name = golfer.first_name
    original_tee = golfer.default_tee_set_id
    original_strokes = golfer.handicap_strokes

    with pytest.raises(RosterValidationError):
        update_golfer(
            session,
            golfer.id,
            first_name="Changed",
            last_name=golfer.last_name,
            default_tee_set_id=golfer.default_tee_set_id,
            handicap_strokes="not-a-number",
        )

    unchanged = get_golfer(session, golfer.id)
    assert unchanged.first_name == original_first_name
    assert unchanged.default_tee_set_id == original_tee
    assert unchanged.handicap_strokes == original_strokes


def test_normalize_email_blank_and_whitespace_only_are_none():
    assert normalize_email(None) is None
    assert normalize_email("") is None
    assert normalize_email("   ") is None
    assert normalize_email(" Ann@X.test ") == "ann@x.test"


def test_normalize_phone_blank_is_none():
    assert normalize_phone(None) is None
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None


def test_validate_handicap_strokes_accepts_absent_and_zero():
    assert validate_handicap_strokes(None) is None
    assert validate_handicap_strokes("") is None
    assert validate_handicap_strokes("   ") is None
    assert validate_handicap_strokes("0") is None
    assert validate_handicap_strokes(0) is None
    assert validate_handicap_strokes("-3") is None


def test_update_a_golfer_without_changing_the_tee_does_not_require_handicap(session, golfer):
    updated = update_golfer(
        session,
        golfer.id,
        first_name="Renamed",
        last_name=golfer.last_name,
        default_tee_set_id=golfer.default_tee_set_id,
    )
    assert updated.first_name == "Renamed"
    assert updated.handicap_strokes == golfer.handicap_strokes


def test_create_golfer_accepts_a_real_int_handicap(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    created = create_golfer(
        session,
        first_name="Int",
        last_name="Handicap",
        default_tee_set_id=tee_id,
        handicap_strokes=7,
    )
    assert created.handicap_strokes == 7


def test_missing_first_or_last_name_is_a_validation_error(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session, first_name="", last_name="Golfer", default_tee_set_id=tee_id
        )
    assert "first_name" in exc_info.value.errors

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session, first_name="Golfer", last_name="   ", default_tee_set_id=tee_id
        )
    assert "last_name" in exc_info.value.errors


def test_missing_and_non_numeric_tee_set_id_are_validation_errors(session, wyandot_course):
    wyandot_course()

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session, first_name="No", last_name="Tee", default_tee_set_id=""
        )
    assert "default_tee_set_id" in exc_info.value.errors

    with pytest.raises(RosterValidationError) as exc_info:
        create_golfer(
            session, first_name="Bad", last_name="Tee", default_tee_set_id="abc"
        )
    assert "default_tee_set_id" in exc_info.value.errors


def test_normalize_name_of_none_is_empty_string():
    assert normalize_name(None) == ""


def test_validate_handicap_strokes_rejects_a_bool():
    assert validate_handicap_strokes(True) is not None
    assert validate_handicap_strokes(False) is not None


def test_updating_to_an_unknown_tee_set_is_a_validation_error(session, golfer):
    with pytest.raises(RosterValidationError) as exc_info:
        update_golfer(
            session,
            golfer.id,
            first_name=golfer.first_name,
            last_name=golfer.last_name,
            default_tee_set_id=999999,
            handicap_strokes=golfer.handicap_strokes,
        )
    assert "default_tee_set_id" in exc_info.value.errors


def test_updating_to_a_duplicate_email_re_raises_and_rolls_back(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)

    create_golfer(
        session,
        first_name="First",
        last_name="Golfer",
        default_tee_set_id=tee_id,
        email="first@example.test",
    )
    second = create_golfer(
        session,
        first_name="Second",
        last_name="Golfer",
        default_tee_set_id=tee_id,
        email="second@example.test",
    )

    with pytest.raises(RosterValidationError) as exc_info:
        update_golfer(
            session,
            second.id,
            first_name=second.first_name,
            last_name=second.last_name,
            default_tee_set_id=tee_id,
            email="first@example.test",
            handicap_strokes=second.handicap_strokes,
        )
    assert "email" in exc_info.value.errors


def test_update_of_unknown_golfer_returns_none(session, wyandot_course):
    course = wyandot_course()
    tee_id = _tee_set_id(course)
    result = update_golfer(
        session,
        999999,
        first_name="Nobody",
        last_name="Home",
        default_tee_set_id=tee_id,
        handicap_strokes="",
    )
    assert result is None
