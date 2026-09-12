"""CrewAI Task factories for each phase of the workflow."""

from __future__ import annotations

from crewai import Agent, Task

from orchestrator.milestones import MILESTONE_TITLES, GLTask
from orchestrator.settings import EXECUTION_GUIDE, VAULT_PLAN_DIR, VAULT_TASKS_DIR

PLANNING = f"{VAULT_PLAN_DIR}/Planning.md"
MVP_PLAN = f"{VAULT_PLAN_DIR}/MVP Build Plan.md"


def architect_plan_review(agent: Agent, board_summary: str) -> Task:
    """Phase A: review the Plan notes and correct real architectural drift."""
    return Task(
        description=(
            f"Use the Obsidian MCP server to read '{PLANNING}' and '{MVP_PLAN}', plus "
            f"'{VAULT_PLAN_DIR}/Fall 2026 Season Data.md' and "
            f"'{VAULT_PLAN_DIR}/Wyandot Course Data.md' for source data, and the live "
            f"'{EXECUTION_GUIDE}'.\n\n"
            f"Current completion state read from the vault:\n{board_summary}\n\n"
            "1. Identify architectural drift, internal contradiction, or an omission that "
            "would mislead an implementer. The MVP Build Plan controls scope; Planning is "
            "background, including scoring requirements the MVP deliberately defers.\n"
            "2. Apply only corrections you can justify from the sources. Write them back to "
            "the Plan notes through the Obsidian MCP server, preserving surrounding content "
            "and existing structure.\n"
            "3. Do not expand scope, add release-2 features, or edit the plan merely to match "
            "whatever the repository currently contains.\n"
            "4. Leave anything that needs a human product decision unedited and report it as "
            "an open question instead."
        ),
        expected_output=(
            "A report with three sections: CHANGES APPLIED (note path, what changed, why, and "
            "the MCP write result for each), OPEN QUESTIONS (items needing a human decision, "
            "with the conflicting sources named), and ARCHITECTURAL RISKS for the next "
            "milestone. If nothing needed changing, say so explicitly and state what you "
            "checked. Never report a write you did not perform."
        ),
        agent=agent,
    )


def senior_reconciliation(agent: Agent, board_summary: str, repo_summary: str) -> Task:
    """Phase B: reconcile the MVP Build Plan against shipped code and the task notes."""
    return Task(
        description=(
            f"Read '{MVP_PLAN}' and the live '{EXECUTION_GUIDE}' through the Obsidian MCP "
            f"server, and every note under '{VAULT_TASKS_DIR}/'.\n\n"
            f"Vault completion state:\n{board_summary}\n\n"
            f"Repository state:\n{repo_summary}\n\n"
            "Using the repository tools, inspect what has actually been built.\n"
            "1. For every GL ID already marked [x], state whether the repository really "
            "contains the work and its evidence. Name any ID marked complete whose evidence "
            "you cannot find; do not re-mark it yourself.\n"
            "2. For the next milestone's open IDs, confirm technical prerequisites exist and "
            "break each one into concrete implementation steps: exact files to read, exact "
            "paths to write, the contracts and named negative cases, and the test command.\n"
            "3. Name every decision ID from the register that each open task depends on, and "
            "flag any that is still unresolved as a blocker rather than guessing.\n"
            "4. Run the repository's own checks to establish the current baseline. Report the "
            "real exit codes."
        ),
        expected_output=(
            "A reconciliation report with: EVIDENCE AUDIT (per completed ID: found / not "
            "found, with paths), BASELINE (commands run and their actual exit codes), "
            "TASK BREAKDOWNS (per open ID in the next milestone: read list, write list, "
            "contracts, negative cases, test command), and BLOCKERS (decision IDs that must "
            "be answered by a human before the affected ID can start)."
        ),
        agent=agent,
    )


def implementation(agent: Agent, task: GLTask, packet: str) -> Task:
    """Phase C step 1: implement exactly one GL ID."""
    return Task(
        description=(
            f"Implement {task.gl_id} and nothing else. Your assignment packet:\n\n"
            f"{packet}\n\n"
            "Read the named files with read_repo_file before writing. Make the code changes "
            "with write_repo_file. Then run the task command and `make check` with "
            "run_repo_command and report their real exit codes."
        ),
        expected_output=(
            f"An implementation report for {task.gl_id}: CHANGED FILES (repo-relative paths), "
            "COMMANDS (each command with its actual exit code and the relevant output), and "
            "STATUS (implemented / blocked). If blocked, name the missing decision. Do not "
            "claim the task is complete and do not touch Obsidian task status."
        ),
        agent=agent,
        context=[],
    )


def verification(agent: Agent, task: GLTask, implementation_task: Task) -> Task:
    """Phase C step 2: independent verification, no source changes."""
    return Task(
        description=(
            f"Independently verify the candidate for {task.gl_id} against its acceptance "
            f"cases in '{task.note_path}' and the contracts in '{EXECUTION_GUIDE}'.\n\n"
            "Read the changed files and run the repository's checks yourself. Change no "
            "source, tests, task notes or Git state. Treat the implementer's report as a "
            "claim to check, not as evidence. A command that does not exist is not a passed "
            "check. Confirm the work stays inside this ID's scope and adds no prohibited "
            "feature."
        ),
        expected_output=(
            "A verdict line that is exactly one of 'VERDICT: PASS', 'VERDICT: FAIL' or "
            "'VERDICT: BLOCKED', followed by EVIDENCE (commands run with real exit codes, "
            "files inspected), ACCEPTANCE CASES (each case: met / not met), and for FAIL or "
            "BLOCKED the specific defect or missing decision."
        ),
        agent=agent,
        context=[implementation_task],
    )


def record_completion(agent: Agent, task: GLTask, verification_task: Task) -> Task:
    """Phase C step 3: the Coordinator, and only the Coordinator, flips the checkbox."""
    return Task(
        description=(
            f"The verifier returned a verdict for {task.gl_id}.\n\n"
            "If and only if the verdict is PASS: use the Obsidian MCP server to patch "
            f"'{task.note_path}', changing the single line '- [ ] Complete {task.gl_id}.' to "
            f"'- [x] Complete {task.gl_id}.'. Change nothing else in that note. Then append "
            "the completion record for this ID to its section: status, changed paths, "
            "decision IDs, the test command with its exit result, and any remaining "
            "limitation.\n\n"
            "If the verdict is FAIL or BLOCKED: leave the checkbox unchecked, record the "
            "defect or missing decision in the task note, and state the remediation route."
        ),
        expected_output=(
            f"A line 'RECORDED: yes' or 'RECORDED: no' for {task.gl_id}, the MCP write result "
            "verbatim, and the reason. Never report a write the MCP server did not confirm."
        ),
        agent=agent,
        context=[verification_task],
    )


def milestone_gate(agent: Agent, milestone: str, board_summary: str) -> Task:
    """Phase C step 4: close a milestone only when every owning task has evidence."""
    title = MILESTONE_TITLES.get(milestone, milestone)
    return Task(
        description=(
            f"Every task in milestone {milestone} ({title}) now reads as complete in the "
            f"vault.\n\n{board_summary}\n\n"
            "Before any task from the next milestone is assigned:\n"
            "1. Confirm through the Obsidian MCP server that each owning task in this "
            "milestone carries its required evidence, not just a checked box.\n"
            "2. Confirm the dependencies the next milestone needs are satisfied.\n"
            "3. Record the milestone status in the task notes.\n"
            "Do not re-verify by your own inspection what an independent verifier must "
            "establish, and do not close the milestone if any evidence is missing."
        ),
        expected_output=(
            f"'MILESTONE {milestone}: CLOSED' or 'MILESTONE {milestone}: OPEN', the per-task "
            "evidence check, what was recorded in Obsidian, and the next milestone's first "
            "eligible task."
        ),
        agent=agent,
    )
