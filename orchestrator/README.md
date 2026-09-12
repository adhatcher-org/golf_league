# Golf League orchestrator

Drives the Coordinator → Architect → Senior Engineer → cline-engineer workflow over
the Obsidian vault, using LiteLLM-routed models.

## Run

```bash
cp .env.example .env   # fill in LITELLM_API_KEY and OBSIDIAN_MCP_TOKEN
set -a; source .env; set +a

uv run python workflow.py --phase status              # vault state only, no model calls
uv run python workflow.py --phase architect           # Phase A
uv run python workflow.py --phase senior              # Phase B
uv run python workflow.py --phase execute --max-tasks 1
uv run python workflow.py --phase all --max-tasks 3
```

`--implementer packet` writes the handoff packet to `handoffs/<GL-ID>.md` and stops,
for execution in Cline's Act mode. `--implementer crew` (the default) runs the
cline-engineer agent on `qwen3-coder-30b` through the same LiteLLM proxy.

## Phases

| Phase | Agent | Does |
|---|---|---|
| A | Architect (`claude-sonnet-5-cloud-plan`) | Reads the Plan notes, corrects real architectural drift, reports changes and open questions |
| B | Senior Engineer (`claude-sonnet-5-cloud-plan`) | Audits evidence for completed IDs, establishes the baseline, breaks the next milestone into implementation steps |
| C | Coordinator (`claude-sonnet-5-cloud-plan`) | One GL ID at a time: assign → implement → verify → record → milestone gate |

## Who may do what

Authority follows `AGENTS.md`, enforced by per-role MCP tool filters rather than
by prompt wording alone.

| Agent | Vault tools | Workspace tools |
|---|---|---|
| Coordinator | read + `vault_patch` / `vault_append` | none |
| Architect | read + patch/append + `vault_write` | none |
| Senior Engineer | read only | read, list, run commands |
| cline-engineer | read only | read, list, write, run commands |

`vault_delete`, `vault_move`, `vault_copy` and `command_execute` are blocked for every
agent. The implementer cannot touch Obsidian task status, so it cannot mark itself done.

## Trust boundaries

- Completion state is parsed from the vault on disk (`vault.read_board`), never taken
  from an agent's narration.
- After the Coordinator patches a checkbox, the driver reads the line back off disk. A
  `VERDICT: PASS` with an unflipped checkbox is reported as a warning, not a completion.
- Workspace commands run against an allowlist (`make`, `uv`, `ruff`, `pytest`, `alembic`,
  `python`, `git`) with `git push`/`commit`/`reset` and network fetches blocked.
- `GL-72` is authorization-gated and is skipped unless a human authorizes it in session.

## Ordering

`milestones.py` encodes the guide's dependency table, including the D1 exception that
stages `GL-20` (course/tee foundation) before `GL-10`, since golfers need a real tee FK.
Milestone gating is strict otherwise: no task from M(n+1) starts until M(n) closes.

Every run appends a JSONL record to `.runs/`.
