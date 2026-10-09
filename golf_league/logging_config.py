import logging
import re
import sys

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


class _RedactingFormatter(logging.Formatter):
    """Format a record with the standard layout, then redact the text."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    def format(self, record) -> str:
        return redact(super().format(record))


class ApplicationLogHandler(logging.StreamHandler):
    """Redacting stdout handler; resolves `sys.stdout` at emit time."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(_RedactingFormatter())

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass


def apply_log_level(level: str) -> None:
    """Apply the managed `LOG_LEVEL` to the application's own loggers.

    Called once at startup (LOG_LEVEL is restart-required). Installs one
    redacting stdout handler on the `golf_league` logger, so application
    records are visible at the chosen level under uvicorn, which configures
    only its own loggers. Unknown names fall back to INFO.
    """
    numeric = logging.getLevelName(str(level).upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    app_logger = logging.getLogger("golf_league")
    app_logger.setLevel(numeric)
    if not any(isinstance(handler, ApplicationLogHandler) for handler in app_logger.handlers):
        app_logger.addHandler(ApplicationLogHandler())


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
