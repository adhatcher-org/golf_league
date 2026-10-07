import re

_TOKEN_PATH = re.compile(r"(/(?:join|set-password|reset|verify)/)([^/?\s\"'<>]+)", re.IGNORECASE)


class AccessPathRedactionFilter:
    """Redact secrets from uvicorn's formatted access arguments only."""

    def filter(self, record) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(redact(item) if isinstance(item, str) else item for item in args)
        elif isinstance(args, dict):
            record.args = {key: redact(value) if isinstance(value, str) else value for key, value in args.items()}
        return True


def install_access_log_filter() -> None:
    """Install the access filter once, including for the default imported app."""
    import logging

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AccessPathRedactionFilter) for item in access_logger.filters):
        access_logger.addFilter(AccessPathRedactionFilter())


def configure_logging(debug: bool = False) -> None:
    """Configure logging for the application."""
    import logging
    import sys

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )


def redact(value: str) -> str:
    """Redact sensitive information from log values."""
    # Redact email addresses
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    value = re.sub(email_pattern, "[REDACTED]", value)

    # Redact tokens (common patterns)
    token_pattern = r'\b(token|secret|password|api_key)\b\s*[=:]\s*[^\s]+'
    value = re.sub(token_pattern, "[REDACTED]", value, flags=re.IGNORECASE)
    value = re.sub(r"\bbearer\s+[^\s]+", "Bearer [REDACTED]", value, flags=re.IGNORECASE)

    # Redact complete URLs with query strings
    url_pattern = r'https?://[^\s?]+\?[^\s]+'
    value = re.sub(url_pattern, "[REDACTED]", value)

    # Keep route names useful in diagnostics while masking their opaque
    # credential segment. Query strings can carry arbitrary secrets too.
    value = _TOKEN_PATH.sub(r"\1[REDACTED]", value)
    value = re.sub(r"\?[^\s\"'<>]*", "?[REDACTED]", value)

    return value
