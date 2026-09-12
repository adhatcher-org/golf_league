"""Milestone and task registry, transcribed from 00-Execution-Guide.md.

The guide is the authority. This module encodes its dependency order so the
Coordinator can select eligible work without re-deriving it from prose each run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.settings import VAULT_TASKS_DIR

MILESTONE_TITLES = {
    "M0": "Scaffold, governance, CI, container",
    "M1": "Identity",
    "M2": "Roster, import, registration",
    "M3": "Course",
    "M4": "Season and teams",
    "M5": "Weeks, matchups, snapshots",
    "M6": "Player views",
    "M7": "Shared link, polish, config, deploy",
}

MILESTONE_ORDER = list(MILESTONE_TITLES)


@dataclass(frozen=True)
class GLTask:
    """One bounded work ID owned by exactly one task note."""

    gl_id: str
    milestone: str
    note: str
    depends_on: tuple[str, ...] = ()
    #: Guide exception: may start once its own prerequisites are met, without
    #: waiting for every earlier milestone to close (D1 stages GL-20 before GL-10).
    early: bool = False
    notes: str = ""

    @property
    def note_path(self) -> str:
        return f"{VAULT_TASKS_DIR}/{self.note}"

    @property
    def checkbox(self) -> str:
        return f"- [ ] Complete {self.gl_id}."


_SETUP = "01-Setup-and-Infrastructure.md"
_ROSTER = "02-Roster-Management.md"
_COURSE = "03-Course-Setup.md"
_SEASON = "04-Season-Management.md"
_USER = "05-User-Facing-Features.md"
_QUALITY = "06-Testing-and-Quality.md"
_DEPLOY = "07-Deployment.md"

TASKS: tuple[GLTask, ...] = (
    # M0 — coordinated scaffold units; bootstrap exception applies (guide §bootstrap).
    GLTask("GL-00", "M0", _SETUP, notes="Documentation-only verification allowed before tooling exists."),
    GLTask("GL-01", "M0", _SETUP, depends_on=("GL-00",)),
    GLTask("GL-02", "M0", _SETUP, depends_on=("GL-01",)),
    GLTask("GL-60", "M0", _QUALITY, depends_on=("GL-01",)),
    GLTask("GL-70", "M0", _DEPLOY, depends_on=("GL-01",)),
    # M1 — identity.
    GLTask("GL-03", "M1", _SETUP, depends_on=("GL-02",)),
    GLTask("GL-04", "M1", _SETUP, depends_on=("GL-03",)),
    # M3 GL-20 runs early: golfers need a real tee FK (D1).
    GLTask(
        "GL-20",
        "M3",
        _COURSE,
        depends_on=("GL-04",),
        early=True,
        notes="Runs after M1 and before GL-10. Idempotent missing-only Wyandot seed; never overwrites admin edits.",
    ),
    # M2 — roster, import, registration.
    GLTask("GL-10", "M2", _ROSTER, depends_on=("GL-04", "GL-20")),
    GLTask("GL-11", "M2", _ROSTER, depends_on=("GL-10",)),
    GLTask("GL-12", "M2", _ROSTER, depends_on=("GL-11",)),
    GLTask("GL-13", "M2", _ROSTER, depends_on=("GL-10",)),
    # M3 — remaining course work.
    GLTask("GL-21", "M3", _COURSE, depends_on=("GL-20",)),
    GLTask("GL-22", "M3", _COURSE, depends_on=("GL-21",)),
    # M4 — season and teams.
    GLTask("GL-30", "M4", _SEASON, depends_on=("GL-22",)),
    GLTask("GL-31", "M4", _SEASON, depends_on=("GL-30",)),
    GLTask("GL-32", "M4", _SEASON, depends_on=("GL-31",)),
    # M5 — weeks, matchups, snapshots.
    GLTask("GL-33", "M5", _SEASON, depends_on=("GL-32",)),
    GLTask("GL-34", "M5", _SEASON, depends_on=("GL-33",)),
    GLTask("GL-35", "M5", _SEASON, depends_on=("GL-34",)),
    GLTask("GL-36", "M5", _SEASON, depends_on=("GL-35",)),
    GLTask("GL-37", "M5", _SEASON, depends_on=("GL-34",), notes="Rain-date activation; never implemented as a cascade."),
    # M6 — player views.
    GLTask("GL-40", "M6", _USER, depends_on=("GL-36",)),
    GLTask("GL-41", "M6", _USER, depends_on=("GL-36",)),
    # M7 — shared link, polish, config, deploy.
    GLTask("GL-42", "M7", _USER, depends_on=("GL-40", "GL-41")),
    GLTask("GL-43", "M7", _USER, depends_on=("GL-42",)),
    GLTask("GL-44", "M7", _USER, depends_on=("GL-43",)),
    GLTask("GL-61", "M7", _QUALITY, depends_on=("GL-44",)),
    GLTask("GL-71", "M7", _DEPLOY, depends_on=("GL-44",)),
    GLTask(
        "GL-72",
        "M7",
        _DEPLOY,
        depends_on=("GL-71",),
        notes="Requires real evidence and explicit session authorization; never auto-completed.",
    ),
)

BY_ID = {t.gl_id: t for t in TASKS}

#: IDs that must never be completed without explicit user authorization in-session.
AUTHORIZATION_GATED = frozenset({"GL-72"})


@dataclass
class Board:
    """Completion state for every GL ID, as read back from Obsidian."""

    done: set[str] = field(default_factory=set)

    def is_done(self, gl_id: str) -> bool:
        return gl_id in self.done

    def milestone_complete(self, milestone: str) -> bool:
        return all(self.is_done(t.gl_id) for t in TASKS if t.milestone == milestone)

    def open_in_milestone(self, milestone: str) -> list[GLTask]:
        return [t for t in TASKS if t.milestone == milestone and not self.is_done(t.gl_id)]

    def current_milestone(self) -> str | None:
        """Lowest milestone that still has open work."""
        for m in MILESTONE_ORDER:
            if not self.milestone_complete(m):
                return m
        return None

    def blocking_milestones(self, task: GLTask) -> list[str]:
        """Earlier milestones that are still open and gate `task`."""
        index = MILESTONE_ORDER.index(task.milestone)
        return [m for m in MILESTONE_ORDER[:index] if not self.milestone_complete(m)]

    def eligible(self) -> list[GLTask]:
        """Open tasks whose dependencies and milestone gate are satisfied.

        Milestone gating holds strictly, except for tasks the guide marks `early`,
        which need only their own dependencies.
        """
        ready: list[GLTask] = []
        for task in TASKS:
            if self.is_done(task.gl_id):
                continue
            if not all(self.is_done(dep) for dep in task.depends_on):
                continue
            if not task.early and self.blocking_milestones(task):
                continue
            ready.append(task)
        return ready

    def next_task(self) -> GLTask | None:
        ready = self.eligible()
        return ready[0] if ready else None
