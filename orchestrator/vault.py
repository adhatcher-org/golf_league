"""Deterministic reads of Obsidian task state.

Agents write task notes through the Obsidian MCP server. The driver reads the
same notes straight off disk so completion state is parsed, not narrated: an
agent claiming "I checked the box" is never trusted without this read-back.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from orchestrator.milestones import TASKS, Board
from orchestrator.settings import VAULT_PLAN_DIR, VAULT_TASKS_DIR

DEFAULT_VAULT = Path(os.getenv("OBSIDIAN_VAULT", Path.home() / "Obsidian"))

#: Matches `- [x] Complete GL-03.` with any leading indent and either case of x.
_CHECKBOX = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s*Complete\s+(?P<id>GL-\d+)\b")


class VaultError(RuntimeError):
    """Raised when the vault is missing or a required note cannot be read."""


def vault_root() -> Path:
    root = DEFAULT_VAULT
    if not root.is_dir():
        raise VaultError(f"Obsidian vault not found at {root}. Set OBSIDIAN_VAULT.")
    return root


def note_path(relative: str) -> Path:
    return vault_root() / relative


def read_note(relative: str) -> str:
    path = note_path(relative)
    if not path.is_file():
        raise VaultError(f"Note not found: {relative}")
    return path.read_text(encoding="utf-8")


def plan_notes() -> dict[str, str]:
    """Every note under the Plan folder, keyed by vault-relative path."""
    base = note_path(VAULT_PLAN_DIR)
    return {
        f"{VAULT_PLAN_DIR}/{p.name}": p.read_text(encoding="utf-8")
        for p in sorted(base.glob("*.md"))
    }


def read_board() -> Board:
    """Parse `- [ ] Complete GL-NN.` lines across every task note."""
    done: set[str] = set()
    seen: set[str] = set()
    for note in sorted({t.note for t in TASKS}):
        for line in read_note(f"{VAULT_TASKS_DIR}/{note}").splitlines():
            match = _CHECKBOX.match(line)
            if not match:
                continue
            gl_id = match.group("id")
            seen.add(gl_id)
            if match.group("mark").lower() == "x":
                done.add(gl_id)
    missing = {t.gl_id for t in TASKS} - seen
    if missing:
        raise VaultError(
            "Task notes are missing completion checkboxes for: "
            + ", ".join(sorted(missing))
            + ". The registry and the vault have drifted; reconcile before running."
        )
    return Board(done=done)


def checkbox_state(gl_id: str, note: str) -> bool | None:
    """Current mark for one ID, or None when the line is absent."""
    for line in read_note(f"{VAULT_TASKS_DIR}/{note}").splitlines():
        match = _CHECKBOX.match(line)
        if match and match.group("id") == gl_id:
            return match.group("mark").lower() == "x"
    return None
