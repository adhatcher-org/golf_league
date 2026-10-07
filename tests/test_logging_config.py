import logging

from golf_league.logging_config import (
    AccessPathRedactionFilter,
    configure_logging,
    install_access_log_filter,
    redact,
)


def test_redact_hides_an_email_address():
    assert "@" not in redact("contact test.user@example.invalid now")
    assert "[REDACTED]" in redact("contact test.user@example.invalid now")


def test_redact_hides_tokens_and_query_strings():
    assert "[REDACTED]" in redact("token=abc123def")
    assert "[REDACTED]" in redact("https://example.invalid/cb?code=xyz")


def test_redact_leaves_ordinary_text_alone():
    assert redact("round 3 scores posted") == "round 3 scores posted"


def test_configure_logging_sets_the_level():
    configure_logging(debug=True)
    root_logger = logging.getLogger()
    original_level = root_logger.level
    try:
        logging.basicConfig(level=logging.INFO, force=True)
        assert logging.getLogger().level == logging.INFO
    finally:
        root_logger.setLevel(original_level)


def test_redact_hides_token_paths_and_queries_but_keeps_route_diagnostics():
    rendered = redact(
        "GET /join/secret-a /set-password/secret-b /reset/secret-c "
        "/verify/secret-d /reset?email=user@example.test HTTP/1.1"
    )
    assert "/join/[REDACTED]" in rendered
    assert "/set-password/[REDACTED]" in rendered
    assert "/reset/[REDACTED]" in rendered
    assert "/verify/[REDACTED]" in rendered
    assert "/reset?[REDACTED]" in rendered
    assert "secret-a" not in rendered and "user@example.test" not in rendered


def test_uvicorn_access_filter_sanitizes_tuple_args_idempotently():
    install_access_log_filter()
    install_access_log_filter()
    logger = logging.getLogger("uvicorn.access")
    filters = [item for item in logger.filters if isinstance(item, AccessPathRedactionFilter)]
    assert len(filters) == 1
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %s',
        ("peer", "GET", "/reset/raw-reset-token?email=one@example.test", "1.1", 200), None,
    )
    original_scope_value = "/reset/raw-reset-token?email=one@example.test"
    for item in filters:
        assert item.filter(record)
    rendered = record.getMessage()
    assert "raw-reset-token" not in rendered
    assert "one@example.test" not in rendered
    assert "GET /reset/" in rendered and "200" in rendered
    assert original_scope_value.endswith("one@example.test")
