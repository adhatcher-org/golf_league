"""Handoff packet rendering.

One packet per GL ID, using the exact template in 00-Execution-Guide.md. The
same text is given to the in-process cline-engineer agent and written to
`handoffs/<GL-ID>.md` so the packet can instead be pasted into Cline's Act mode
against the identical Obsidian MCP server.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from orchestrator.milestones import BY_ID, GLTask
from orchestrator.settings import EXECUTION_GUIDE

HANDOFF_DIR = Path("handoffs")

_BOUNDARIES = """\
Boundaries for this assignment:
- Implement this ID only. Do not start, plan or touch another GL ID.
- Do not edit Obsidian task status. The Coordinator records completion after
  independent verification; marking yourself done is out of scope.
- Do not launch another implementer.
- Do not invent answers to blocked decisions and do not add release-2 features.
- Never add scores, rounds, results, standings, scoring rulesets, automatic
  draft or pairings, handicap recomputation, or AI/provider modules.
- Synthetic fixtures only: no real roster or contact data in source, tests,
  logs, output or screenshots."""


def render(
    task: GLTask,
    *,
    dependencies: str,
    decisions: str,
    read_list: str,
    write_list: str,
    contracts: str,
    command: str,
) -> str:
    """Render the guide's handoff block, plus the workflow boundaries."""
    return f"""\
Task: {task.gl_id} only.
Source: {task.note_path}, section "{task.gl_id}"; plus the live {EXECUTION_GUIDE}.
Milestone: {task.milestone}.
Dependencies: {dependencies}
Resolved decisions: {decisions}
Read: {read_list}
Write: {write_list}

Implement the stated contracts and named negative cases:
{contracts}

{_BOUNDARIES}

Run `{command}`, then `make check`. Report actual output and changed files.
If blocked, stop this task, name the missing decision, and keep it unchecked."""


def default_packet(task: GLTask, *, completed: set[str], baseline: str) -> str:
    """A packet built from registry facts alone, before senior-engineer enrichment."""
    deps = ", ".join(task.depends_on) or "none"
    satisfied = "all complete" if all(d in completed for d in task.depends_on) else "INCOMPLETE"
    return render(
        task,
        dependencies=f"{deps} ({satisfied}); baseline {baseline}",
        decisions="See the decision register in the execution guide; escalate anything still open.",
        read_list=f"{task.note_path}, {EXECUTION_GUIDE}, AGENTS.md, and only the dependency code named above.",
        write_list="Only the paths named in the task note, plus the migration, tests and docs this task requires.",
        contracts=task.notes or "As stated in the task note's acceptance cases.",
        command="uv run pytest -q",
    )


def write_packet(task: GLTask, body: str, root: Path | None = None) -> Path:
    """Persist a packet for Cline Act mode and for the run record."""
    base = (root or Path.cwd()) / HANDOFF_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{task.gl_id}.md"
    header = (
        f"<!-- Generated {date.today().isoformat()} by orchestrator; "
        f"milestone {task.milestone}. Paste into Cline Act mode or let the "
        f"cline-engineer agent consume it. -->\n\n"
    )
    path.write_text(header + body + "\n", encoding="utf-8")
    return path


def packet_for(gl_id: str, *, completed: set[str], baseline: str) -> str:
    return default_packet(BY_ID[gl_id], completed=completed, baseline=baseline)
