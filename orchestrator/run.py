"""Workflow driver.

Phase A  Architect reviews the Plan notes and reports to the Coordinator.
Phase B  Senior Engineer reconciles the MVP Build Plan against shipped code and
         the Tasks/ notes, and breaks the next milestone into implementation steps.
Phase C  Coordinator assigns one GL ID at a time to a cline-engineer, has the
         Senior Engineer verify it, records the result in Obsidian itself, and
         closes each milestone before starting the next.

Milestone order, dependencies and the bootstrap exception come from
'01 Projects/Golf League/Tasks/00-Execution-Guide.md'.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crewai import Crew, Process

from orchestrator import packets, tasks, tools, vault
from orchestrator.agents import build_agents
from orchestrator.ledger import Ledger
from orchestrator.milestones import (
    AUTHORIZATION_GATED,
    MILESTONE_TITLES,
    Board,
    GLTask,
)
from orchestrator.settings import ConfigError, Runtime


def _board_summary(board: Board) -> str:
    lines = []
    for milestone, title in MILESTONE_TITLES.items():
        open_ids = [t.gl_id for t in board.open_in_milestone(milestone)]
        state = "complete" if not open_ids else f"open: {', '.join(open_ids)}"
        lines.append(f"- {milestone} ({title}): {state}")
    return "\n".join(lines)


def _repo_summary(root: Path) -> str:
    packages = sorted(p.name for p in (root / "golf_league").glob("*.py")) if (root / "golf_league").is_dir() else []
    git = "git repository present" if (root / ".git").is_dir() else "NOT a git repository (no commit SHAs available for evidence)"
    return f"- root: {root}\n- {git}\n- golf_league modules: {', '.join(packages) or 'none'}"


def _run_crew(crew_tasks, agents_used, ledger: Ledger, label: str) -> str:
    crew = Crew(agents=agents_used, tasks=crew_tasks, process=Process.sequential, verbose=True)
    result = crew.kickoff()
    output = str(result)
    ledger.record("crew_finished", label=label, output=output[:8000])
    return output


def phase_architect(agents, board: Board, ledger: Ledger) -> str:
    print("\n=== Phase A — Architect: Plan review ===")
    task = tasks.architect_plan_review(agents["architect"], _board_summary(board))
    return _run_crew([task], [agents["architect"]], ledger, "architect_plan_review")


def phase_senior(agents, board: Board, repo_root: Path, ledger: Ledger) -> str:
    print("\n=== Phase B — Senior Engineer: reconciliation and task breakdown ===")
    task = tasks.senior_reconciliation(
        agents["senior_engineer"], _board_summary(board), _repo_summary(repo_root)
    )
    return _run_crew([task], [agents["senior_engineer"]], ledger, "senior_reconciliation")


def _execute_one(agents, task: GLTask, board: Board, breakdown: str, ledger: Ledger, *, implementer: str) -> bool:
    """Implement, verify and record one GL ID. Returns True when it is recorded done."""
    baseline = "current worktree" if not Path(".git").is_dir() else "current HEAD"
    packet = packets.default_packet(task, completed=board.done, baseline=baseline)
    if breakdown:
        packet += (
            "\n\nSenior Engineer breakdown for this milestone (authoritative over the "
            f"generic lists above where they conflict):\n{breakdown[:6000]}"
        )
    packet_path = packets.write_packet(task, packet)
    ledger.record("packet_written", gl_id=task.gl_id, path=str(packet_path))
    print(f"\n--- {task.gl_id} ({task.milestone}) — packet: {packet_path} ---")

    if implementer == "packet":
        print(
            f"implementer=packet: {task.gl_id} is staged for Cline Act mode. "
            "Run the packet in Cline, then re-run with --implementer crew or resume after "
            "the checkbox is recorded."
        )
        ledger.record("staged_for_cline", gl_id=task.gl_id)
        return False

    impl = tasks.implementation(agents["cline_engineer"], task, packet)
    verify = tasks.verification(agents["senior_engineer"], task, impl)
    record = tasks.record_completion(agents["coordinator"], task, verify)
    output = _run_crew(
        [impl, verify, record],
        [agents["cline_engineer"], agents["senior_engineer"], agents["coordinator"]],
        ledger,
        f"execute_{task.gl_id}",
    )

    # Trust the vault, not the narration: read the checkbox back off disk.
    recorded = vault.checkbox_state(task.gl_id, task.note)
    ledger.record("checkbox_readback", gl_id=task.gl_id, checked=recorded)
    if recorded:
        print(f"{task.gl_id}: recorded complete in {task.note}")
        return True
    print(f"{task.gl_id}: still unchecked in {task.note} — treating as not done.")
    if "VERDICT: PASS" in output:
        print(
            f"WARNING: verifier reported PASS for {task.gl_id} but the Obsidian checkbox was "
            "not updated. Check the MCP write result in the run ledger."
        )
    return False


def phase_execute(agents, repo_root: Path, ledger: Ledger, *, breakdown: str, implementer: str, max_tasks: int) -> None:
    print("\n=== Phase C — Coordinator: milestone execution ===")
    completed_here = 0
    while completed_here < max_tasks:
        board = vault.read_board()
        milestone = board.current_milestone()
        if milestone is None:
            print("Every milestone is complete. No eligible work remains.")
            return

        eligible = [t for t in board.eligible() if t.gl_id not in AUTHORIZATION_GATED]
        gated = [t.gl_id for t in board.eligible() if t.gl_id in AUTHORIZATION_GATED]
        if gated:
            print(f"Skipping authorization-gated tasks: {', '.join(gated)} (needs explicit user authorization).")
        if not eligible:
            blocked = [t.gl_id for t in board.open_in_milestone(milestone)]
            print(
                f"No eligible task. Milestone {milestone} still open: {', '.join(blocked) or 'none'}. "
                "Dependencies or authorization are blocking progress."
            )
            ledger.record("no_eligible_task", milestone=milestone, open_ids=blocked)
            return

        task = eligible[0]
        ledger.record("assigned", gl_id=task.gl_id, milestone=task.milestone)
        done = _execute_one(agents, task, board, breakdown, ledger, implementer=implementer)
        if not done:
            print(f"Stopping: {task.gl_id} did not reach a recorded completion.")
            return
        completed_here += 1

        board = vault.read_board()
        if board.milestone_complete(task.milestone):
            print(f"\n--- Milestone gate: {task.milestone} ---")
            gate = tasks.milestone_gate(agents["coordinator"], task.milestone, _board_summary(board))
            _run_crew([gate], [agents["coordinator"]], ledger, f"gate_{task.milestone}")

    print(f"Reached --max-tasks ({max_tasks}). Stopping.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Golf League multi-agent workflow driver.")
    parser.add_argument(
        "--phase",
        choices=("all", "architect", "senior", "execute", "status"),
        default="all",
        help="Which phase to run. 'status' only prints vault state.",
    )
    parser.add_argument(
        "--implementer",
        choices=("crew", "packet"),
        default="crew",
        help=(
            "'crew' runs the cline-engineer agent on qwen3-coder-30b through LiteLLM. "
            "'packet' writes the handoff packet and stops, for execution in Cline Act mode."
        ),
    )
    parser.add_argument("--max-tasks", type=int, default=1, help="Maximum GL IDs to complete in this run.")
    parser.add_argument("--repo-root", default=None, help="Repository root (defaults to cwd).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root or Path.cwd()).resolve()
    tools.set_repo_root(repo_root)

    board = vault.read_board()
    print("Vault state:")
    print(_board_summary(board))
    next_task = board.next_task()
    print(f"Next eligible: {next_task.gl_id if next_task else 'none'}")
    if args.phase == "status":
        return 0

    try:
        runtime = Runtime.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        runtime.verify_access()
    except ConfigError as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2

    print(f"Planning model: {runtime.plan_model} | Acting model: {runtime.act_model}")
    ledger = Ledger(repo_root)
    ledger.record("run_started", phase=args.phase, implementer=args.implementer, plan_model=runtime.plan_model, act_model=runtime.act_model)
    agents = build_agents(runtime)

    breakdown = ""
    if args.phase in ("all", "architect"):
        phase_architect(agents, board, ledger)
    if args.phase in ("all", "senior"):
        breakdown = phase_senior(agents, vault.read_board(), repo_root, ledger)
    if args.phase in ("all", "execute"):
        phase_execute(
            agents,
            repo_root,
            ledger,
            breakdown=breakdown,
            implementer=args.implementer,
            max_tasks=args.max_tasks,
        )

    print(f"\nRun ledger: {ledger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
