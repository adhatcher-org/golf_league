"""Workspace tools for the implementer and verifier agents.

Every path is confined to the repository root and every command is checked
against an allowlist, so an agent cannot reach the vault, the host, or the
network through these tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from crewai.tools import tool

_REPO_ROOT = Path.cwd()

MAX_READ_BYTES = 200_000
COMMAND_TIMEOUT = 900

#: First token of a command must appear here. `make check` and the task-specific
#: pytest invocations from the execution guide are the intended surface.
ALLOWED_COMMANDS = ("make", "uv", "ruff", "pytest", "alembic", "python", "git")

#: Subcommands that mutate history or publish are never run by an agent.
FORBIDDEN_FRAGMENTS = (
    "git push",
    "git commit",
    "git reset",
    "git checkout",
    "rm -rf",
    "curl",
    "wget",
    "docker push",
)


def set_repo_root(path: Path) -> None:
    global _REPO_ROOT
    _REPO_ROOT = path.resolve()


def _resolve(relative: str) -> Path:
    candidate = (_REPO_ROOT / relative).resolve()
    if candidate != _REPO_ROOT and _REPO_ROOT not in candidate.parents:
        raise ValueError(f"Path escapes the repository root: {relative}")
    return candidate


@tool("read_repo_file")
def read_repo_file(path: str) -> str:
    """Read a UTF-8 file from the golf_league repository. `path` is repo-relative."""
    try:
        target = _resolve(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not target.is_file():
        return f"ERROR: no such file: {path}"
    data = target.read_text(encoding="utf-8", errors="replace")
    if len(data) > MAX_READ_BYTES:
        return data[:MAX_READ_BYTES] + f"\n...[truncated at {MAX_READ_BYTES} bytes]"
    return data


@tool("list_repo_files")
def list_repo_files(pattern: str = "**/*.py") -> str:
    """List repository files matching a glob pattern (e.g. 'golf_league/**/*.py')."""
    skip = (".venv", "__pycache__", ".git", ".ruff_cache", "node_modules")
    matches = [
        str(p.relative_to(_REPO_ROOT))
        for p in sorted(_REPO_ROOT.glob(pattern))
        if p.is_file() and not any(part in skip for part in p.parts)
    ]
    return "\n".join(matches[:500]) or "(no matches)"


@tool("write_repo_file")
def write_repo_file(path: str, content: str) -> str:
    """Create or overwrite a repository file with `content`. `path` is repo-relative."""
    try:
        target = _resolve(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {path} ({len(content)} bytes)"


@tool("run_repo_command")
def run_repo_command(command: str) -> str:
    """Run an allowlisted build/test command in the repository root.

    Allowed first tokens: make, uv, ruff, pytest, alembic, python, git.
    Returns the exit code with combined stdout/stderr; a nonzero exit is a real
    failure and must be reported as such, never summarized as success.
    """
    stripped = command.strip()
    head = stripped.split()[0] if stripped else ""
    if head not in ALLOWED_COMMANDS:
        return f"ERROR: command not allowed: {head!r}. Allowed: {', '.join(ALLOWED_COMMANDS)}"
    for fragment in FORBIDDEN_FRAGMENTS:
        if fragment in stripped:
            return f"ERROR: forbidden command fragment: {fragment!r}"
    try:
        proc = subprocess.run(
            stripped,
            shell=True,
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {COMMAND_TIMEOUT}s: {stripped}"
    output = (proc.stdout + proc.stderr)[-20_000:]
    return f"$ {stripped}\nexit={proc.returncode}\n{output}"


IMPLEMENTER_TOOLS = [read_repo_file, list_repo_files, write_repo_file, run_repo_command]
#: The verifier inspects and runs checks but never edits source (AGENTS.md role 3).
VERIFIER_TOOLS = [read_repo_file, list_repo_files, run_repo_command]
