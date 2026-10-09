"""Admin-managed configuration: the R-DEPLOYMENT allowlist and its file.

Exactly eight keys may be changed from the application (`MANAGED_KEYS`).
They live in one dotenv-style file, located by the environment-only
`MANAGED_ENV_PATH` (default `./data/.env`, inside the mounted data
directory). A value in that file outranks the container environment, which
outranks the code default; `golf_league.config.Settings` wires that order.

Every managed setting is restart-required: the running process reads the
file once, at startup, and the application never restarts itself.

The same file may also carry the deploy-time keys in `DEPLOY_FILE_KEYS`
(for example `SESSION_SECRET` on an Unraid data directory). Those rank
below the container environment, are never editable or displayed, and the
writer preserves their lines untouched.

This module is standard library plus the pure domain email check. It never
evaluates the file as shell, never expands variables, and never writes a key
outside the allowlist. Writes are atomic (temporary file in the
same directory, fsync, `os.replace`), mode 0600, and keep every line the
application does not own. A failed write leaves the previous file intact.
"""

import ipaddress
import logging
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from golf_league.domain.identity import is_valid_email

logger = logging.getLogger(__name__)

DEFAULT_MANAGED_ENV_PATH = "./data/.env"
MANAGED_FILE_MODE = 0o600


@dataclass(frozen=True)
class ManagedKey:
    """One allowlisted setting: its file key, `Settings` field and rules."""

    name: str
    field: str
    label: str
    kind: str  # "host", "port", "text", "secret", "email" or "choice"
    required: bool = False
    choices: tuple[str, ...] = ()
    max_length: int = 254
    help: str = ""

    @property
    def is_secret(self) -> bool:
        return self.kind == "secret"


MANAGED_KEYS: tuple[ManagedKey, ...] = (
    ManagedKey("SMTP_HOST", "smtp_host", "SMTP host", "host", max_length=253,
               help="Host name or IP address of the mail relay. Leave blank to keep mail in memory."),
    ManagedKey("SMTP_PORT", "smtp_port", "SMTP port", "port", required=True,
               help="A whole number from 1 to 65535. The relay normally uses 587."),
    ManagedKey("SMTP_USERNAME", "smtp_username", "SMTP username", "text",
               help="Leave blank when the relay needs no login."),
    ManagedKey("SMTP_PASSWORD", "smtp_password", "SMTP password", "secret", max_length=1024,
               help="Never shown. Leave blank to keep the stored password."),
    ManagedKey("SMTP_FROM_EMAIL", "smtp_from_email", "Sender address", "email", required=True),
    ManagedKey("SMTP_FROM_NAME", "smtp_from_name", "Sender name", "text", required=True, max_length=120),
    ManagedKey("SMTP_TLS_MODE", "smtp_tls_mode", "SMTP security", "choice", required=True,
               choices=("starttls", "ssl", "none"),
               help="starttls upgrades a plain connection; ssl connects over TLS; none sends unencrypted."),
    ManagedKey("LOG_LEVEL", "log_level", "Log level", "choice", required=True,
               choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
)

MANAGED_KEYS_BY_NAME: dict[str, ManagedKey] = {key.name: key for key in MANAGED_KEYS}

# Deploy-time keys the same file may also carry, as a fallback below the
# container environment (environment wins; code default last). Not editable
# from the application, never displayed, and never rewritten by the form.
# MANAGED_ENV_PATH itself is environment-only and is not in this set.
DEPLOY_FILE_KEYS: dict[str, str | None] = {
    "SESSION_SECRET": "session_secret",
    "DATABASE_URL": "database_url",
    "EXTERNAL_BASE_URL": "external_base_url",
    "MAX_USERS": "max_users",
    "EMAIL_VERIFICATION_REQUIRED": "email_verification_required",
    # First-boot only; read by `bootstrap_admin`, not a `Settings` field.
    "ADMIN_EMAIL": None,
    "ADMIN_PASSWORD": None,
}

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?$")
_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

# One writer at a time inside this (single-worker) process.
_WRITE_LOCK = threading.Lock()


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _validate_host(key: ManagedKey, value: str) -> tuple[str, str | None]:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not _HOST_RE.match(value):
            return "", f"{key.label} must be a host name or IP address."
    return value, None


def _validate_port(key: ManagedKey, value: str) -> tuple[str, str | None]:
    if not value.isdecimal() or len(value) > 5 or not 1 <= int(value) <= 65535:
        return "", f"{key.label} must be a whole number from 1 to 65535."
    return str(int(value)), None


def _validate_email(key: ManagedKey, value: str) -> tuple[str, str | None]:
    if not is_valid_email(value):
        return "", f"{key.label} must be an email address."
    return value, None


def _validate_choice(key: ManagedKey, value: str) -> tuple[str, str | None]:
    normalized = value.upper() if key.name == "LOG_LEVEL" else value.lower()
    if normalized not in key.choices:
        return "", f"{key.label} must be one of: {', '.join(key.choices)}."
    return normalized, None


_KIND_VALIDATORS = {
    "host": _validate_host,
    "port": _validate_port,
    "email": _validate_email,
    "choice": _validate_choice,
}


def validate_value(key: ManagedKey, raw: str) -> tuple[str, str | None]:
    """Return `(normalized_value, error)`; `error` is None when valid.

    Never raises. Error messages never echo the submitted value.
    """
    if not isinstance(raw, str):
        return "", f"{key.label} is not valid."
    if _has_control_characters(raw):
        return "", f"{key.label} must be a single line of text."
    value = raw if key.is_secret else raw.strip()
    if len(value) > key.max_length:
        return "", f"{key.label} must be at most {key.max_length} characters."
    if not value:
        return "", (f"{key.label} is required." if key.required else None)
    validator = _KIND_VALIDATORS.get(key.kind)
    return validator(key, value) if validator else (value, None)


def _decode(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r'\\(["\\])', r"\1", value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _encode(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"\n'


def _read_assignments(path: Path | str) -> dict[str, str]:
    """Parse every `KEY=value` line of the file; the last assignment wins.

    Values are decoded literally (quotes removed, no expansion, no shell).
    A missing file is empty; an unreadable file is logged and treated as
    empty, so it can never stop the application starting.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        logger.warning("managed configuration file could not be read; using environment values")
        return {}
    assignments: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT_RE.match(line)
        if match is not None:
            assignments[match.group(1)] = _decode(match.group(2))
    return assignments


def read_managed_values(path: Path | str) -> dict[str, str]:
    """Return the valid allowlisted (managed) values stored in the file.

    A key outside the allowlist and an invalid value are ignored (an invalid
    value is logged by key name only), so a hand-edited file can never stop
    the application starting.
    """
    values: dict[str, str] = {}
    for name, raw in _read_assignments(path).items():
        key = MANAGED_KEYS_BY_NAME.get(name)
        if key is None:
            continue
        normalized, error = validate_value(key, raw)
        if error is not None:
            logger.warning("managed configuration value ignored: %s", key.name)
            continue
        values[key.name] = normalized
    return values


def read_deploy_values(path: Path | str) -> dict[str, str]:
    """Return the deploy-time fallback values stored in the same file.

    Only `DEPLOY_FILE_KEYS` are returned. They rank *below* the container
    environment (the caller applies that order) and are never editable from
    the application. An empty value counts as unset. Values are returned
    raw for `Settings` to validate and are never logged.
    """
    return {
        name: value
        for name, value in _read_assignments(path).items()
        if name in DEPLOY_FILE_KEYS and value.strip()
    }


def _check_updates(updates: Mapping[str, str]) -> None:
    for name, value in updates.items():
        key = MANAGED_KEYS_BY_NAME.get(name)
        if key is None:
            raise ValueError("not a managed configuration key")
        normalized, error = validate_value(key, value)
        if error is not None or normalized != value:
            raise ValueError("managed configuration value is not valid")


def _merge(existing: str, updates: Mapping[str, str]) -> str:
    """Replace managed assignments in place, keep every other line verbatim."""
    lines: list[str] = []
    written: set[str] = set()
    for line in existing.splitlines(keepends=True):
        match = _ASSIGNMENT_RE.match(line)
        name = match.group(1) if match else None
        if name in updates:
            if name not in written:
                lines.append(_encode(name, updates[name]))
                written.add(name)
            continue
        lines.append(line if line.endswith("\n") else line + "\n")
    lines.extend(_encode(name, value) for name, value in updates.items() if name not in written)
    return "".join(lines)


def write_managed_values(path: Path | str, updates: Mapping[str, str]) -> None:
    """Atomically store `updates` in the managed file.

    Every key must be allowlisted and every value must already be valid;
    otherwise `ValueError` is raised before the file is touched. Lines the
    application does not own are kept verbatim; the first assignment of an
    updated key is replaced in place and later duplicates are dropped; new
    keys are appended. On any failure the temporary file is removed, the
    previous file is left unchanged, and the exception propagates.
    """
    _check_updates(updates)
    target = Path(path)
    with _WRITE_LOCK:
        try:
            existing = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""

        content = _merge(existing, updates)
        fd, temp_name = tempfile.mkstemp(prefix=".managed-env.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), MANAGED_FILE_MODE)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself; best effort where the platform refuses."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
