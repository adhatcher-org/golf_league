"""Pure tests for `golf_league.domain.roster_import` — no database."""

from golf_league.domain.roster_import import (
    ROSTER_HEADERS,
    decode_warnings,
    detect_header_set,
    email_is_wellformed,
    encode_warnings,
    likely_reversed,
    near_miss_domain,
    normalize_email_cell,
    parse_handicap_cell,
    phones_equal,
    resolve_status,
    sanitize_display_name,
    selected_handicap,
    split_regular_name,
)


def test_regular_and_sub_header_sets_are_detected():
    assert detect_header_set(list(ROSTER_HEADERS)) == "roster"
    assert detect_header_set([f" {h} " for h in ROSTER_HEADERS]) == "roster"


def test_an_unsupported_header_set_is_not_detected():
    assert detect_header_set(["Full Name", "Team", "Handicap", "Email"]) is None
    assert detect_header_set(list(ROSTER_HEADERS)[:-1]) is None
    assert detect_header_set([h.lower() for h in ROSTER_HEADERS]) is None


def test_regular_name_splits_on_the_first_comma_only():
    assert split_regular_name("de Vries, Jo Ann") == ("Jo Ann", "de Vries")
    assert split_regular_name("Smith, Jo, Ann") == ("Jo, Ann", "Smith")


def test_a_name_with_no_comma_or_an_empty_side_does_not_split():
    assert split_regular_name("NoComma") is None
    assert split_regular_name(", First") is None
    assert split_regular_name("Last, ") is None
    assert split_regular_name(",") is None


def test_tee_resolves_supported_labels_and_rejects_blank_and_unknown():
    assert resolve_status("Blue") == "Blue"
    assert resolve_status("Gold") == "Gold"
    assert resolve_status(" White ") == "White"
    assert resolve_status(" gold ") is None
    assert resolve_status("WHITE") is None
    assert resolve_status("") is None
    assert resolve_status("   ") is None
    assert resolve_status("Silver") is None


def test_handicap_grammar_accepts_blank_zero_and_negative_and_rejects_decimals_and_unicode_digits():
    assert parse_handicap_cell("") == (None, True)
    assert parse_handicap_cell("   ") == (None, True)
    assert parse_handicap_cell("0") == (0, True)
    assert parse_handicap_cell("-5") == (-5, True)
    assert parse_handicap_cell("3.5") == (None, False)
    assert parse_handicap_cell("1e3") == (None, False)
    assert parse_handicap_cell("NaN") == (None, False)
    assert parse_handicap_cell("+2") == (None, False)
    assert parse_handicap_cell("\u00b2") == (None, False)


def test_minus_zero_parses_as_zero():
    assert parse_handicap_cell("-0") == (0, True)


def test_email_normalization_reports_trimmed_and_lowercased_warnings():
    value, warnings = normalize_email_cell(" Ann@Example.TEST ")
    assert value == "ann@example.test"
    assert set(warnings) == {"email_whitespace_trimmed", "email_lowercased"}

    value, warnings = normalize_email_cell("ann@example.test")
    assert value == "ann@example.test"
    assert warnings == []

    value, warnings = normalize_email_cell(" ann@example.test")
    assert value == "ann@example.test"
    assert warnings == ["email_whitespace_trimmed"]


def test_a_blank_email_cell_normalizes_to_none_with_no_warnings():
    assert normalize_email_cell("") == (None, [])
    assert normalize_email_cell("   ") == (None, [])


def test_malformed_addresses_are_not_wellformed():
    assert email_is_wellformed("ann@example.test") is True
    assert email_is_wellformed("ann example.test") is False
    assert email_is_wellformed("annexample.test") is False
    assert email_is_wellformed("ann@@example.test") is False
    assert email_is_wellformed("@example.test") is False
    assert email_is_wellformed("ann@.test") is False
    assert email_is_wellformed("ann@example.") is False
    assert email_is_wellformed("ann@examplecom") is False


def test_a_listed_domain_gets_no_near_miss_warning():
    assert near_miss_domain("gmail.com") is None
    assert near_miss_domain("GMAIL.COM") is None


def test_near_miss_domain_suggests_icloud_for_icould():
    assert near_miss_domain("icould.com") == "icloud.com"


def test_near_miss_domain_prefers_the_single_domain_at_the_smallest_distance():
    assert near_miss_domain("mne.com") == "me.com"


def test_near_miss_domain_returns_none_on_a_tie_at_the_smallest_distance():
    assert near_miss_domain("mse.com") is None


def test_near_miss_domain_returns_none_when_nothing_is_within_distance_two():
    # The task specification's own worked example for this branch,
    # "mail.com", is actually distance 1 from the listed "gmail.com" (a
    # single leading-character insertion) and so is a real near-miss under
    # the specified optimal-string-alignment algorithm, not a case of
    # "nothing within distance two". "protonmail.com" is verified (see the
    # implementation report's SPEC CONFLICTS) to sit at distance >= 4 from
    # every `KNOWN_DOMAINS` entry, which is what this test name requires.
    assert near_miss_domain("protonmail.com") is None


def test_likely_reversed_needs_another_row_with_a_different_email():
    rows = [
        (1, "Taylor", "Reed", "taylor.reed@example.test"),
        (2, "Reed", "Other", "reed.other@example.test"),
    ]
    # Row 2's first name ("Reed") matches row 1's last name ("Reed"), with
    # different emails, so row 2 is the one flagged.
    assert likely_reversed(rows) == {2}

    same_email_rows = [
        (1, "Taylor", "Reed", "same@example.test"),
        (2, "Reed", "Other", "same@example.test"),
    ]
    assert likely_reversed(same_email_rows) == set()


def test_a_shared_surname_alone_produces_no_warning():
    rows = [
        (1, "Amy", "Sameson", "amy.sameson@example.test"),
        (2, "Ben", "Sameson", "ben.sameson@example.test"),
    ]
    assert likely_reversed(rows) == set()


def test_selected_handicap_follows_the_tee_label_for_regulars_and_the_single_column_for_subs():
    assert (
        selected_handicap(
            source_role="summer_regular",
            tee_label="Gold",
            handicap_gold=5,
            handicap_white=9,
            handicap_single=None,
        )
        == 5
    )
    assert (
        selected_handicap(
            source_role="summer_regular",
            tee_label="White",
            handicap_gold=5,
            handicap_white=9,
            handicap_single=None,
        )
        == 9
    )
    assert (
        selected_handicap(
            source_role="summer_regular",
            tee_label=None,
            handicap_gold=5,
            handicap_white=9,
            handicap_single=None,
        )
        is None
    )
    assert (
        selected_handicap(
            source_role="summer_sub",
            tee_label=None,
            handicap_gold=None,
            handicap_white=None,
            handicap_single=3,
        )
        == 3
    )
    # Neither header set's role: no persisted row can carry this value —
    # `detect_header_set` only ever returns these two roles or None, and
    # None is rejected before a row is built — but the pure function
    # still defines a safe fallback for a caller that passes anything
    # else directly.
    assert (
        selected_handicap(
            source_role="unexpected",
            tee_label=None,
            handicap_gold=None,
            handicap_white=None,
            handicap_single=None,
        )
        is None
    )


def test_phones_equal_ignores_formatting_but_not_absence():
    assert phones_equal("(555) 010-2000", "5550102000") is True
    assert phones_equal(None, None) is True
    assert phones_equal(None, "5550102000") is False
    assert phones_equal("5550102000", None) is False
    assert phones_equal("555-0100", "555-0101") is False


def test_sanitize_display_name_strips_separators_and_control_characters():
    assert sanitize_display_name("C:\\Users\\admin\\roster.csv") == "roster.csv"
    assert sanitize_display_name("/tmp/roster.csv") == "roster.csv"
    assert sanitize_display_name("ro\x00ster.csv") == "roster.csv"
    assert sanitize_display_name("   ") == "upload.csv"
    assert sanitize_display_name("") == "upload.csv"
    assert sanitize_display_name("a" * 300) == "a" * 255


def test_warnings_round_trip_through_encode_and_decode():
    codes = ["email_lowercased", "handicap_defaulted_zero", "email_lowercased"]
    encoded = encode_warnings(codes)
    assert encoded == "handicap_defaulted_zero,email_lowercased"
    assert decode_warnings(encoded) == ["handicap_defaulted_zero", "email_lowercased"]
    assert decode_warnings("") == []
    assert encode_warnings([]) == ""
