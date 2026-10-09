"""GL-71: the deployment templates keep the documented safety properties.

Text checks on deploy/compose.example.yml and deploy/swag.example.conf, plus
the uvicorn proxy-header behaviour docs/deployment.md relies on: forwarded
headers are honoured only from the trusted SWAG address, and a client cannot
choose its own address by sending X-Forwarded-For.
"""

import asyncio
import re
from pathlib import Path

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "deploy" / "compose.example.yml").read_text(encoding="utf-8")
SWAG = (ROOT / "deploy" / "swag.example.conf").read_text(encoding="utf-8")


def test_compose_trusts_only_the_swag_address_for_forwarded_headers():
    assert re.search(r"^\s+FORWARDED_ALLOW_IPS: \$\{SWAG_IP:\?", COMPOSE, re.MULTILINE)
    assert not re.search(r"FORWARDED_ALLOW_IPS:\s*[\"']?\*", COMPOSE)


def test_compose_mounts_the_data_directory_and_points_managed_file_into_it():
    assert re.search(r"^\s+- \$\{GOLF_LEAGUE_DATA_DIR:\?[^}]*\}:/app/data$", COMPOSE, re.MULTILINE)
    assert "MANAGED_ENV_PATH: /app/data/.env" in COMPOSE
    assert "/app/data/.env:" not in COMPOSE  # never a single-file mount


def test_compose_publishes_no_host_port_and_pins_the_image():
    assert "ports:" not in COMPOSE
    assert re.search(r"^\s+image: \$\{GOLF_LEAGUE_IMAGE:\?", COMPOSE, re.MULTILINE)
    assert ":latest" not in COMPOSE


def test_compose_holds_placeholders_not_secrets():
    assert re.search(r"SESSION_SECRET: \$\{SESSION_SECRET:\?", COMPOSE)
    assert "ADMIN_PASSWORD: ${ADMIN_PASSWORD:-}" in COMPOSE
    for line in COMPOSE.splitlines():
        if re.match(r"\s+(SMTP_PASSWORD|SESSION_SECRET|ADMIN_PASSWORD):", line):
            assert "${" in line, line


def test_swag_template_appends_client_address_and_marks_cookies_secure():
    assert "include /config/nginx/proxy.conf;" in SWAG
    assert "proxy_cookie_flags ~ secure;" in SWAG
    assert "set $upstream_port 8000;" in SWAG
    assert not re.search(r"^\s*proxy_set_header\s+X-Forwarded-For", SWAG, re.MULTILINE)


async def _resolved_client(peer: str, headers: list[tuple[bytes, bytes]]) -> tuple[str, str]:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]
        seen["scheme"] = scope["scheme"]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts="172.31.250.2")
    scope = {"type": "http", "scheme": "http", "client": (peer, 40000), "headers": headers}
    await middleware(scope, None, None)
    return seen["client"], seen["scheme"]


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("172.31.250.2", b"198.51.100.66, 203.0.113.7", ("203.0.113.7", "https")),
    ("172.31.250.2", b"203.0.113.7", ("203.0.113.7", "https")),
    ("172.31.250.3", b"198.51.100.99", ("172.31.250.3", "http")),
])
def test_forwarded_headers_count_only_from_the_trusted_proxy(peer, forwarded, expected):
    headers = [(b"x-forwarded-for", forwarded), (b"x-forwarded-proto", b"https")]
    assert asyncio.run(_resolved_client(peer, headers)) == expected
