"""Scorecard photo reading: provider settings helpers and the Ollama vision client.

R-OCR-PROVIDER permits one AI provider module, for reading scorecard photos, and
nothing else. Everything here is deliberately defensive:

* `Settings` only holds plain strings for the five keys; these pure helpers
  interpret them, so a bad value is a "not configured" state and never stops
  startup.
* The model's reply is data, not trusted. It is parsed and shape-checked, and
  anything unexpected is a `ProviderOutputError` with a short stable code.
* Nothing here logs a URL, an image, a name, a score or model output. Log lines
  carry the provider, the model name, a duration and an error code only.

This module imports no `fastapi` (enforced by `tests/test_architecture.py`).
"""

import base64
import http.client
import json
import logging
import re
import time
import zoneinfo
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_NUM_CTX = 32768
MIN_NUM_CTX = 2048
MAX_NUM_CTX = 131072
DEFAULT_LEAGUE_TIMEZONE = "America/Chicago"
MAX_PLAYERS = 8
# Limits on what a reply may make the app store and render. A name is cut to
# the roster's own name length; a number beyond this is not a score, a total or
# a hole number, whatever the model meant.
MAX_NAME_LENGTH = 80
MAX_NUMBER = 999
MAX_HEADER_ENTRIES = 24
# A reply larger than this is not a scorecard reading.
_MAX_REPLY_BYTES = 4 * 1024 * 1024

_timezone_warning_logged = False


# --- settings helpers (pure; each takes the settings object) -----------------


def analysis_provider(settings: Any) -> str:
    """The provider name: stripped and lowercased; empty means `ollama`."""
    value = str(settings.analysis_provider or "").strip().lower()
    return value or "ollama"


def vision_num_ctx(settings: Any) -> int | None:
    """The context size: empty gives 32768; ASCII digits from 2048 to 131072, else None."""
    value = str(settings.ollama_vision_num_ctx or "").strip()
    if not value:
        return DEFAULT_NUM_CTX
    if not (value.isascii() and value.isdigit()) or len(value) > 7:
        return None
    number = int(value)
    return number if MIN_NUM_CTX <= number <= MAX_NUM_CTX else None


def league_timezone_name(settings: Any) -> str:
    """The configured IANA zone, else `America/Chicago` (one warning per process)."""
    global _timezone_warning_logged
    value = str(settings.league_timezone or "").strip()
    try:
        zoneinfo.ZoneInfo(value)
    except Exception:  # empty, malformed, missing or unreadable zone data
        if not _timezone_warning_logged:
            _timezone_warning_logged = True
            logger.warning("LEAGUE_TIMEZONE is not a valid timezone; using %s", DEFAULT_LEAGUE_TIMEZONE)
        return DEFAULT_LEAGUE_TIMEZONE
    return value


def _valid_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a port that is not a number in range
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def photo_entry_configured(settings: Any) -> bool:
    """True only for a usable Ollama or OpenAI configuration."""
    provider = analysis_provider(settings)
    if provider == "ollama":
        return (
            _valid_http_url(str(settings.ollama_url or "").strip())
            and bool(str(settings.ollama_vision_model or "").strip())
            and vision_num_ctx(settings) is not None
        )
    elif provider == "openai":
        return (
            bool(str(settings.openai_api_key or "").strip())
            and bool(str(settings.openai_model or "").strip())
            and _valid_http_url(str(settings.openai_base_url or "").strip())
        )
    return False


# --- interface ---------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedPlayer:
    name: str
    scores: tuple[int | None, ...]  # length == hole_count; None where unreadable or blank
    uncertain: tuple[bool, ...]  # same length; True where the model flagged the cell
    handicap_written: int | None = None  # the handicap handwritten beside the name
    out_total: int | None = None  # the player's handwritten `Out` total


@dataclass(frozen=True)
class ExtractedCard:
    players: tuple[ExtractedPlayer, ...]  # 0 to 8 rows; the review step enforces 2 to 4
    model_name: str
    # Integers printed in the hole-number row, whatever they are (unchecked here).
    holes_header: tuple[int, ...] = ()


class VisionProvider(Protocol):
    def read_scorecard(self, image_jpeg: bytes, *, hole_count: int) -> ExtractedCard:
        """Read one prepared JPEG; raise a provider error, never return partial data."""
        ...


class ProviderError(Exception):
    """Base error. The message is the stable code: no model text, no image data."""

    code = "provider_error"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class ProviderTimeout(ProviderError):
    code = "provider_timeout"


class ProviderOutputError(ProviderError):
    code = "provider_output_invalid"


# --- prompt and schema --------------------------------------------------------

PROMPT_TEMPLATE = (
    "This image is a photo of a golf scorecard for nine holes (holes 1 to {hole_count}). "
    "Read only the handwritten player rows. For each player give the printed or written name "
    "and the strokes for holes 1 to {hole_count}, in order, left to right. "
    "A number written next to the name is the player's handicap: put it in handicap_written "
    "and leave it out of the name and the scores. "
    "Give the player's handwritten Out total in out_total, or null. "
    "Ignore the printed yardage rows, the printed row labelled Handicap (that is each hole's "
    "stroke index, not the player's), the Par row, and every column after Out, "
    "including the net score. "
    "Give holes_header as the numbers printed in the hole-number row. "
    "A card may have one or two filled rows out of four; do not invent players. "
    "For any cell that is blank or cannot be read, use null and set uncertain to true for "
    "that cell; uncertain is false otherwise. "
    "scores and uncertain must each have exactly {hole_count} entries. "
    "Return JSON only, with no other text, in this shape: "
    '{{"players": [{{"name": "", "handicap_written": null, "scores": [], "uncertain": [], '
    '"out_total": null}}], "holes_header": []}}'
)


def build_prompt(hole_count: int) -> str:
    return PROMPT_TEMPLATE.format(hole_count=hole_count)


def _response_schema(hole_count: int) -> dict[str, Any]:
    nullable_int = {"type": ["integer", "null"]}
    return {
        "type": "object",
        "properties": {
            "players": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "handicap_written": nullable_int,
                        "scores": {
                            "type": "array",
                            "items": nullable_int,
                            "minItems": hole_count,
                            "maxItems": hole_count,
                        },
                        "uncertain": {
                            "type": "array",
                            "items": {"type": "boolean"},
                            "minItems": hole_count,
                            "maxItems": hole_count,
                        },
                        "out_total": nullable_int,
                    },
                    "required": ["name", "scores", "uncertain"],
                },
            },
            "holes_header": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["players"],
    }


# --- parsing -------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```$", re.DOTALL)


def _integer_or_none(value: Any) -> int | None:
    """An int for a small int or integer-valued float; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and abs(value) <= MAX_NUMBER:
        return value
    return None


def _score_cell(value: Any) -> int | None:
    if value is None:
        return None
    number = _integer_or_none(value)
    if number is None:
        raise ProviderOutputError("output_bad_score")
    return number


def _parse_player(raw: Any, hole_count: int) -> ExtractedPlayer:
    if not isinstance(raw, dict):
        raise ProviderOutputError("output_shape")
    name = raw.get("name")
    scores = raw.get("scores")
    uncertain = raw.get("uncertain")
    if not isinstance(name, str):
        raise ProviderOutputError("output_shape")
    if not isinstance(scores, list) or not isinstance(uncertain, list):
        raise ProviderOutputError("output_shape")
    if len(scores) != hole_count or len(uncertain) != hole_count:
        raise ProviderOutputError("output_length")
    if not all(isinstance(flag, bool) for flag in uncertain):
        raise ProviderOutputError("output_shape")
    return ExtractedPlayer(
        name=name.strip()[:MAX_NAME_LENGTH].strip(),
        scores=tuple(_score_cell(cell) for cell in scores),
        uncertain=tuple(uncertain),
        # Optional, untrusted extras: anything unusable is simply absent.
        handicap_written=_integer_or_none(raw.get("handicap_written")),
        out_total=_integer_or_none(raw.get("out_total")),
    )


def parse_card(content: str, *, hole_count: int, model_name: str) -> ExtractedCard:
    """Turn the model's reply text into an `ExtractedCard` or raise `ProviderOutputError`."""
    text = content.strip() if isinstance(content, str) else ""
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ProviderOutputError("output_not_json") from None
    if not isinstance(data, dict):
        raise ProviderOutputError("output_shape")
    players = data.get("players")
    if not isinstance(players, list):
        raise ProviderOutputError("output_shape")
    if len(players) > MAX_PLAYERS:
        raise ProviderOutputError("output_too_many_players")
    header = data.get("holes_header")
    holes_header = (
        tuple(
            item for item in header[:MAX_HEADER_ENTRIES]
            if isinstance(item, int) and not isinstance(item, bool) and abs(item) <= MAX_NUMBER
        )
        if isinstance(header, list)
        else ()
    )
    return ExtractedCard(
        players=tuple(_parse_player(player, hole_count) for player in players),
        model_name=model_name,
        holes_header=holes_header,
    )


# --- Ollama client ---------------------------------------------------------------


class OllamaVisionProvider:
    """Reads a scorecard through Ollama's `/api/chat`, standard library only."""

    def __init__(
        self,
        url: str,
        model: str,
        num_ctx: int,
        *,
        connect_timeout: float = 5.0,
        total_timeout: float = 180.0,
    ) -> None:
        self._url = url.strip()
        self._model = model.strip()
        self._num_ctx = num_ctx
        self._connect_timeout = connect_timeout
        self._total_timeout = total_timeout

    def _request_body(self, image_jpeg: bytes, hole_count: int) -> bytes:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": build_prompt(hole_count),
                    "images": [base64.b64encode(image_jpeg).decode("ascii")],
                }
            ],
            "stream": False,
            "think": False,
            "format": _response_schema(hole_count),
            "options": {"num_ctx": self._num_ctx, "temperature": 0},
        }
        return json.dumps(payload).encode("utf-8")

    def _post(self, body: bytes) -> bytes:
        """POST to `{url}/api/chat`; map every transport failure to a provider error."""
        try:
            parts = urlsplit(self._url)
            host = parts.hostname
            port = parts.port
        except ValueError:
            raise ProviderUnavailable("provider_bad_url") from None
        if parts.scheme not in ("http", "https") or not host:
            raise ProviderUnavailable("provider_bad_url")
        path = parts.path.rstrip("/") + "/api/chat"
        connection_class = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        conn = connection_class(host, port, timeout=self._connect_timeout)
        try:
            conn.connect()
            conn.sock.settimeout(self._total_timeout)
            conn.request(
                "POST", path, body=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response = conn.getresponse()
            status = response.status
            reply = response.read(_MAX_REPLY_BYTES + 1)
        except TimeoutError:  # includes the connect and read socket timeouts
            raise ProviderTimeout() from None
        except (OSError, http.client.HTTPException):
            raise ProviderUnavailable() from None
        finally:
            conn.close()
        if not 200 <= status < 300:
            raise ProviderUnavailable("provider_http_status")
        if len(reply) > _MAX_REPLY_BYTES:
            raise ProviderOutputError("output_too_large")
        return reply

    def read_scorecard(self, image_jpeg: bytes, *, hole_count: int) -> ExtractedCard:
        started = time.monotonic()
        try:
            reply = self._post(self._request_body(image_jpeg, hole_count))
            try:
                envelope = json.loads(reply)
                content = envelope["message"]["content"]
            except (ValueError, KeyError, TypeError, RecursionError):
                raise ProviderOutputError("output_envelope") from None
            card = parse_card(content, hole_count=hole_count, model_name=self._model)
        except ProviderError as error:
            logger.warning(
                "vision read failed provider=ollama model=%s duration_ms=%d code=%s",
                self._model, _elapsed_ms(started), error.code,
            )
            raise
        logger.info(
            "vision read ok provider=ollama model=%s duration_ms=%d",
            self._model, _elapsed_ms(started),
        )
        return card


class OpenAIVisionProvider:
    """Reads a scorecard through OpenAI's Chat Completions API, standard library only."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        connect_timeout: float = 5.0,
        total_timeout: float = 180.0,
    ) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._base_url = base_url.strip()
        self._connect_timeout = connect_timeout
        self._total_timeout = total_timeout

    def _request_body(self, image_jpeg: bytes, hole_count: int) -> bytes:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_prompt(hole_count)},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64.b64encode(image_jpeg).decode('ascii')}"
                            },
                        },
                    ],
                }
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        return json.dumps(payload).encode("utf-8")

    def _post(self, body: bytes) -> bytes:
        """POST to `{base_url}/chat/completions`; map every transport failure to a provider error."""
        try:
            parts = urlsplit(self._base_url)
            host = parts.hostname
            port = parts.port
        except ValueError:
            raise ProviderUnavailable("provider_bad_url") from None
        if parts.scheme not in ("http", "https") or not host:
            raise ProviderUnavailable("provider_bad_url")
        path = parts.path.rstrip("/") + "/chat/completions"
        connection_class = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        conn = connection_class(host, port, timeout=self._connect_timeout)
        try:
            conn.connect()
            conn.sock.settimeout(self._total_timeout)
            conn.request(
                "POST", path, body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            response = conn.getresponse()
            status = response.status
            reply = response.read(_MAX_REPLY_BYTES + 1)
        except TimeoutError:  # includes the connect and read socket timeouts
            raise ProviderTimeout() from None
        except (OSError, http.client.HTTPException):
            raise ProviderUnavailable() from None
        finally:
            conn.close()
        if not 200 <= status < 300:
            raise ProviderUnavailable("provider_http_status")
        if len(reply) > _MAX_REPLY_BYTES:
            raise ProviderOutputError("output_too_large")
        return reply

    def read_scorecard(self, image_jpeg: bytes, *, hole_count: int) -> ExtractedCard:
        started = time.monotonic()
        try:
            reply = self._post(self._request_body(image_jpeg, hole_count))
            try:
                envelope = json.loads(reply)
                content = envelope["choices"][0]["message"]["content"]
            except (ValueError, KeyError, TypeError, RecursionError, IndexError):
                raise ProviderOutputError("output_envelope") from None
            card = parse_card(content, hole_count=hole_count, model_name=self._model)
        except ProviderError as error:
            logger.warning(
                "vision read failed provider=openai model=%s duration_ms=%d code=%s",
                self._model, _elapsed_ms(started), error.code,
            )
            raise
        logger.info(
            "vision read ok provider=openai model=%s duration_ms=%d",
            self._model, _elapsed_ms(started),
        )
        return card


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
