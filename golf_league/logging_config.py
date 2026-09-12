import re


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
    token_pattern = r'(token|secret|password|api_key|bearer)[=:]?[^\s]+'
    value = re.sub(token_pattern, "[REDACTED]", value, flags=re.IGNORECASE)

    # Redact complete URLs with query strings
    url_pattern = r'https?://[^\s?]+\?[^\s]+'
    value = re.sub(url_pattern, "[REDACTED]", value)

    return value
