"""GL-80: the Ollama vision client against a local `http.server` thread.

Everything is synthetic: the "image" is a few made-up bytes, names are invented, and the
server listens on 127.0.0.1 only. No real provider is ever called.
"""

import base64
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from golf_league.services.scorecard_vision import (
    ExtractedCard,
    ExtractedPlayer,
    OllamaVisionProvider,
    ProviderOutputError,
    ProviderTimeout,
    ProviderUnavailable,
    build_prompt,
    parse_card,
)

IMAGE = b"\xff\xd8synthetic-image-bytes\xff\xd9"
MODEL = "example-model:1b"
NAME = "Zzyzx Quibble"
SCORES = [4, 5, 6, 3, 4, None, 5, 7, 4]
UNCERTAIN = [False] * 5 + [True] + [False] * 3


def _reply(player: dict | None = None, **extra) -> dict:
    body = {
        "players": [player or {"name": NAME, "scores": SCORES, "uncertain": UNCERTAIN}],
    }
    body.update(extra)
    return body


def _envelope(content) -> dict:
    return {"model": MODEL, "message": {"role": "assistant",
                                         "content": content if isinstance(content, str)
                                         else json.dumps(content)}}


class _Server:
    def __init__(self):
        self.status = 200
        self.body: bytes = b"{}"
        self.delay = 0.0
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                owner.requests.append({"path": self.path, "json": json.loads(raw)})
                if owner.delay:
                    time.sleep(owner.delay)
                try:
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(owner.body)))
                    self.end_headers()
                    self.wfile.write(owner.body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def reply(self, content, status: int = 200):
        self.status = status
        self.body = json.dumps(_envelope(content)).encode()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    srv = _Server()
    yield srv
    srv.close()


def _provider(server, **kwargs) -> OllamaVisionProvider:
    return OllamaVisionProvider(server.url, MODEL, 4096, **kwargs)


def _read(server, **kwargs):
    return _provider(server, **kwargs).read_scorecard(IMAGE, hole_count=9)


# --- happy paths -------------------------------------------------------------------------


def test_valid_reply_parses_and_passes_the_model_name_through(server):
    server.reply(_reply())
    card = _read(server)
    assert card == ExtractedCard(
        players=(ExtractedPlayer(NAME, tuple(SCORES), tuple(UNCERTAIN)),),
        model_name=MODEL,
        holes_header=(),
    )


def test_fenced_reply_parses(server):
    server.reply("```json\n" + json.dumps(_reply()) + "\n```")
    assert _read(server).players[0].name == NAME
    server.reply("```\n" + json.dumps(_reply()) + "\n```")
    assert _read(server).players[0].scores == tuple(SCORES)


def test_new_fields_parse(server):
    player = {"name": "Aaron", "handicap_written": 8, "scores": SCORES, "uncertain": UNCERTAIN,
              "out_total": 43}
    server.reply(_reply(player, holes_header=list(range(1, 10))))
    card = _read(server)
    assert card.players[0].handicap_written == 8
    assert card.players[0].out_total == 43
    assert card.holes_header == tuple(range(1, 10))


def test_reply_missing_the_new_fields_still_parses(server):
    server.reply(_reply())
    card = _read(server)
    assert card.players[0].handicap_written is None
    assert card.players[0].out_total is None
    assert card.holes_header == ()


def test_holes_header_with_out_keeps_only_integers(server):
    server.reply(_reply(holes_header=[1, 2, 3, 4, 5, 6, 7, 8, 9, "Out"]))
    assert _read(server).holes_header == tuple(range(1, 10))


def test_header_is_not_checked_against_hole_count():
    card = parse_card(json.dumps(_reply(holes_header=[10, 11, 12])), hole_count=9, model_name="m")
    assert card.holes_header == (10, 11, 12)


def test_integer_valued_floats_are_coerced_and_out_of_range_scores_pass_through():
    player = {"name": "A", "scores": [4.0, 99, 0, -1, None, 5, 5, 5, 5], "uncertain": [False] * 9,
              "out_total": 40.0, "handicap_written": "eight"}
    card = parse_card(json.dumps(_reply(player)), hole_count=9, model_name="m")
    assert card.players[0].scores == (4, 99, 0, -1, None, 5, 5, 5, 5)
    assert card.players[0].out_total == 40
    assert card.players[0].handicap_written is None


def test_zero_players_is_allowed():
    assert parse_card('{"players": []}', hole_count=9, model_name="m").players == ()


# --- the request ----------------------------------------------------------------------------


def test_request_carries_model_context_stream_and_base64_image(server):
    server.reply(_reply())
    _read(server)
    (request,) = server.requests
    assert request["path"] == "/api/chat"
    body = request["json"]
    assert body["model"] == MODEL
    assert body["stream"] is False and body["think"] is False
    assert body["options"] == {"num_ctx": 4096, "temperature": 0}
    (message,) = body["messages"]
    assert message["role"] == "user"
    assert base64.b64decode(message["images"][0]) == IMAGE
    assert message["content"] == build_prompt(9)
    scores = body["format"]["properties"]["players"]["items"]["properties"]["scores"]
    assert scores["minItems"] == scores["maxItems"] == 9


def test_base_url_with_a_trailing_slash_posts_to_api_chat(server):
    server.reply(_reply())
    OllamaVisionProvider(server.url + "/", MODEL, 4096).read_scorecard(IMAGE, hole_count=9)
    assert server.requests[0]["path"] == "/api/chat"


def test_prompt_wording():
    prompt = build_prompt(9)
    assert "holes 1 to 9" in prompt
    assert "null" in prompt and "uncertain" in prompt and "JSON only" in prompt
    assert "handicap" in prompt.lower() and "Par" in prompt and "Out" in prompt
    assert "holes 1 to 5" in build_prompt(5) and "9" not in build_prompt(5).replace("\n", "")


# --- error mapping ---------------------------------------------------------------------------


@pytest.mark.parametrize("content,code", [
    ({"players": [{"name": "A", "scores": [1, 2], "uncertain": [False, False]}]}, "output_length"),
    ({"players": [{"name": "A", "scores": SCORES, "uncertain": [False]}]}, "output_length"),
    ("this is not json", "output_not_json"),
    ("", "output_not_json"),
    ({"nope": []}, "output_shape"),
    ([1, 2, 3], "output_shape"),
    ({"players": "x"}, "output_shape"),
    ({"players": ["x"]}, "output_shape"),
    ({"players": [{"name": 5, "scores": SCORES, "uncertain": UNCERTAIN}]}, "output_shape"),
    ({"players": [{"name": "A", "scores": "x", "uncertain": UNCERTAIN}]}, "output_shape"),
    ({"players": [{"name": "A", "scores": SCORES, "uncertain": ["no"] * 9}]}, "output_shape"),
    ({"players": [{"name": "A", "scores": ["x"] * 9, "uncertain": UNCERTAIN}]}, "output_bad_score"),
    ({"players": [{"name": "A", "scores": [4.5] * 9, "uncertain": UNCERTAIN}]}, "output_bad_score"),
    ({"players": [{"name": "A", "scores": [True] * 9, "uncertain": UNCERTAIN}]}, "output_bad_score"),
    ({"players": [{"name": "A", "scores": SCORES, "uncertain": UNCERTAIN}] * 9},
     "output_too_many_players"),
])
def test_bad_model_output_is_an_output_error(server, content, code):
    server.reply(content)
    with pytest.raises(ProviderOutputError) as raised:
        _read(server)
    assert raised.value.code == code
    assert str(raised.value) == code  # the message is only the stable code


def test_bad_envelope_is_an_output_error(server):
    for body in (b"not json", b'{"message": {}}', b'{"message": {"content": 5}}', b"[]"):
        server.body = body
        with pytest.raises(ProviderOutputError):
            _read(server)


def test_http_500_is_unavailable(server):
    server.reply(_reply(), status=500)
    with pytest.raises(ProviderUnavailable) as raised:
        _read(server)
    assert raised.value.code == "provider_http_status"


def test_slow_server_is_a_timeout(server):
    server.reply(_reply())
    server.delay = 1.0
    started = time.monotonic()
    with pytest.raises(ProviderTimeout) as raised:
        _read(server, total_timeout=0.2)
    assert raised.value.code == "provider_timeout"
    assert time.monotonic() - started < 0.9


def test_refused_connection_is_unavailable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    provider = OllamaVisionProvider(f"http://127.0.0.1:{port}", MODEL, 4096, connect_timeout=1.0)
    with pytest.raises(ProviderUnavailable) as raised:
        provider.read_scorecard(IMAGE, hole_count=9)
    assert raised.value.code == "provider_unavailable"


def test_unresolvable_host_is_unavailable():
    provider = OllamaVisionProvider("http://no-such-host.invalid", MODEL, 4096, connect_timeout=1.0)
    with pytest.raises(ProviderUnavailable):
        provider.read_scorecard(IMAGE, hole_count=9)


def test_bad_url_is_unavailable_without_a_network_call():
    for url in ("ftp://x", "http://", "http://host:notaport"):
        with pytest.raises(ProviderUnavailable):
            OllamaVisionProvider(url, MODEL, 4096).read_scorecard(IMAGE, hole_count=9)


def test_non_http_garbage_reply_is_unavailable():
    """A server that answers with something that is not HTTP is a transport failure."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"garbage\r\n\r\n")
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        provider = OllamaVisionProvider(f"http://127.0.0.1:{listener.getsockname()[1]}", MODEL,
                                        4096, connect_timeout=1.0, total_timeout=2.0)
        with pytest.raises(ProviderUnavailable):
            provider.read_scorecard(IMAGE, hole_count=9)
    finally:
        listener.close()
        thread.join(timeout=2)


# --- logging ------------------------------------------------------------------------------------


@pytest.mark.parametrize("failing", [False, True])
def test_no_log_record_carries_image_names_scores_or_model_text(server, caplog, failing):
    leaked = "SECRET-MODEL-TEXT"
    if failing:
        server.reply(leaked + " not json")
    else:
        server.reply(_reply())
    with caplog.at_level(logging.DEBUG):
        try:
            _read(server)
        except ProviderOutputError:
            pass
    assert caplog.records  # the call is logged...
    text = caplog.text
    # ...but only provider, model, duration and code.
    for forbidden in (NAME, "Zzyzx", base64.b64encode(IMAGE).decode(), "synthetic-image", leaked,
                      "[4, 5, 6", "127.0.0.1"):
        assert forbidden not in text
    assert MODEL in text and "duration_ms=" in text


def test_url_credentials_are_never_logged(caplog):
    provider = OllamaVisionProvider("http://user:pw-secret@127.0.0.1:1", MODEL, 4096,
                                    connect_timeout=1.0)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderUnavailable):
        provider.read_scorecard(IMAGE, hole_count=9)
    assert "pw-secret" not in caplog.text and "user:" not in caplog.text
