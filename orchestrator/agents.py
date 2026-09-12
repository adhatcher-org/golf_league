"""Agent definitions.

Roles, goals and boundaries come from AGENTS.md and the user's workflow spec.
The boundaries are written into each backstory because a role name alone does
not constrain a model.
"""

from __future__ import annotations

from crewai import Agent

from orchestrator.settings import (
    EXECUTION_GUIDE,
    OBSIDIAN_AUTHOR_TOOLS,
    OBSIDIAN_EDIT_TOOLS,
    OBSIDIAN_READ_TOOLS,
    Runtime,
)
from orchestrator.tools import IMPLEMENTER_TOOLS, VERIFIER_TOOLS

_GUIDE_REF = f"The live '{EXECUTION_GUIDE}' governs scope and precedence."

_HONESTY = (
    "Report actual tool output. Never claim a command ran, a test passed, or a note "
    "was written unless a tool result shows it. If you are blocked, say so and name "
    "the missing decision rather than inventing an answer."
)


def build_agents(runtime: Runtime) -> dict[str, Agent]:
    """Construct the four workflow agents bound to their models and MCP servers."""
    plan_llm = runtime.plan_llm
    act_llm = runtime.act_llm

    # Vault access is granted per role. Only the Coordinator can edit task notes,
    # only the Architect can author Plan notes, and the implementer is read-only
    # so it cannot mark itself done.
    read_only = runtime.obsidian_mcp(OBSIDIAN_READ_TOOLS)
    coordinator_vault = runtime.obsidian_mcp(OBSIDIAN_READ_TOOLS + OBSIDIAN_EDIT_TOOLS)
    architect_vault = runtime.obsidian_mcp(
        OBSIDIAN_READ_TOOLS + OBSIDIAN_EDIT_TOOLS + OBSIDIAN_AUTHOR_TOOLS
    )

    coordinator = Agent(
        role="Coordinator Agent",
        goal=(
            "Read '01 Projects/Golf League/Plan/Planning.md' and "
            "'01 Projects/Golf League/Plan/MVP Build Plan.md', inspect files under "
            "'01 Projects/Golf League/Tasks/', determine completed items ([x]), "
            "identify the next open task ([ ]), and assign execution."
        ),
        backstory=(
            "You orchestrate task execution and track progress across Obsidian vault notes. "
            "You own task-note writes, dependency readiness, candidate identity, review "
            "integration, remediation routing and next-task selection. You do not fix "
            "implementation defects yourself and you do not replace independent verification "
            "with your own inspection. You advance one milestone at a time: a milestone closes "
            "only when every owning task has its required evidence. "
            f"{_GUIDE_REF} {_HONESTY}"
        ),
        mcps=[coordinator_vault],
        llm=plan_llm,
        allow_delegation=False,
        verbose=True,
    )

    architect = Agent(
        role="Software Architect",
        goal="Review architectural requirements and clarify details for the active open task.",
        backstory=(
            "You safeguard system design integrity across session runs. You read the Plan notes "
            "and correct genuine architectural drift, contradiction or omission in them. You do "
            "not silently edit the source plan to match an implementation, you do not add "
            "release-2 features, and you record every change with its rationale. Architectural "
            "decisions are captured as ADRs, not as quiet edits. "
            f"{_GUIDE_REF} {_HONESTY}"
        ),
        mcps=[architect_vault],
        llm=plan_llm,
        allow_delegation=False,
        verbose=True,
    )

    senior_engineer = Agent(
        role="Senior Software Engineer",
        goal=(
            "Validate technical prerequisites and break down the open task into concrete "
            "implementation steps, then verify completed candidates without changing them."
        ),
        backstory=(
            "You turn architecture blueprints into actionable development tickets. Apply "
            "engineering rigor, not Bourbon Book domain requirements, mandatory formatting "
            "repairs, or its prohibition on acting as a verifier: this role is explicitly "
            "adapted to Golf League task verification. This is task verification, not a "
            "substitute for any separately required PR reviewer or validator. When verifying "
            "you change no source, tests, task notes or Git state; you read, run the repository's "
            "own checks, and return a single verdict of PASS, FAIL or BLOCKED with evidence. "
            f"{_GUIDE_REF} {_HONESTY}"
        ),
        mcps=[read_only],
        llm=plan_llm,
        tools=VERIFIER_TOOLS,
        allow_delegation=False,
        verbose=True,
    )

    cline_engineer = Agent(
        role="Junior Software Engineer",
        goal=(
            "Execute code changes in the workspace for exactly one assigned GL ID, including "
            "its required tests and docs, and return evidence of what actually ran."
        ),
        backstory=(
            "You write workstation code and keep project notes accurate. You implement exactly "
            "one assigned GL ID and its required tests and docs. Under this workflow you return "
            "evidence to the Coordinator and must not edit Obsidian task status, mark yourself "
            "done, choose another task, or launch another implementer. This narrows any optional "
            "task-update behavior you would otherwise have. Run the task command and then "
            "`make check`, and report the actual output and changed files. If blocked, stop the "
            "task, name the missing decision, and leave the task unchecked. "
            f"{_GUIDE_REF} {_HONESTY}"
        ),
        mcps=[read_only],
        llm=act_llm,
        tools=IMPLEMENTER_TOOLS,
        allow_delegation=False,
        verbose=True,
    )

    return {
        "coordinator": coordinator,
        "architect": architect,
        "senior_engineer": senior_engineer,
        "cline_engineer": cline_engineer,
    }
