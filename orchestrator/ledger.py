"""Append-only run record, so a stopped run can be inspected and resumed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNS_DIR = Path(".runs")


class Ledger:
    def __init__(self, root: Path | None = None) -> None:
        base = (root or Path.cwd()) / RUNS_DIR
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = base / f"run-{stamp}.jsonl"

    def record(self, event: str, **fields: Any) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
