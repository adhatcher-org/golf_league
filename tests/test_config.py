"""Explicit origin validation does not depend on inbound request headers."""

import pytest
from pydantic import ValidationError

from golf_league.config import Settings


def test_default_external_origin():
    assert Settings(session_secret="synthetic").external_base_url == "https://golfleague.aaronhatcher.com"


@pytest.mark.parametrize("origin,expected", [("https://example.test/", "https://example.test"), ("http://localhost:8080", "http://localhost:8080"), ("https://[::1]:443/", "https://[::1]:443")])
def test_custom_external_origin(origin, expected):
    assert Settings(session_secret="synthetic", external_base_url=origin).external_base_url == expected


@pytest.mark.parametrize("origin", ["", "/relative", "//example.test", "ftp://example.test", "https://", "https://user@example.test", "https://user:password@example.test", "https://example.test/path", "https://example.test/?a=1", "https://example.test/#part", "https://example.test?", "https://example.test#", "https://example.test:bad", "https://example.test:70000", "https://example.test:0", "https://example.test\\evil", "https://example.test\n", "https://exa mple.test"])
def test_invalid_external_origins(origin):
    with pytest.raises(ValidationError):
        Settings(session_secret="synthetic", external_base_url=origin)
