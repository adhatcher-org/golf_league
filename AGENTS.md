## Roles, authority and capabilities

Execution, testing and validation run from the separate orchestrator repository as Claude Code
agents. The mechanical steps — assignment packets, candidate commits, verbatim records, bounded
retries and task completion — are performed by its `gl` helper, never by an agent.

1. **Coordinator (the `/gl-run` skill in the main Claude session):** selects the next eligible task, delegates exploration, implementation and
   validation, and routes the validator's defects back for repair. It does not implement, verify,
   commit or edit task status itself; `gl complete` records completion only after a recorded PASS
   on the committed revision.
2. **gl-explorer:** reads this repository and writes a short factual brief before implementation.
   It changes nothing.
3. **gl-engineer:** implements exactly one assigned GL ID from its packet, runs its acceptance
   commands, and reports evidence. It does not commit, edit Obsidian task status, mark itself done,
   choose another task or launch another implementer. Include this boundary in every handoff.
4. **gl-validator:** independently verifies the committed candidate without changing source, tests,
   task notes or Git state, and returns exactly one verdict — PASS, FAIL or BLOCKED — with specific
   defects. It judges whether the result is correct enough for what depends on it, not style.
5. **gl-milestone-tester:** once every task in a milestone is complete, writes and runs milestone
   tests under `tests/milestones/<M>/` against that milestone's exit criteria. It does not change
   application code.
6. **gl-architect and gl-planner:** planning agents. The architect reviews the Plan notes and may
   edit only those; the planner audits evidence and breaks down the next milestone without
   changing anything.

All of these run on the Claude subscription. Nothing in this workflow calls a model API.

Use the current runtime's available delegation tools and actual models. A role name is not proof
that a given model or tool can be launched. Disclose material model/runtime adaptations. Do not
silently change provider settings or claim an execution that did not happen. If the caller requires
an exact unavailable model/runtime, report that blocker before dependent execution.

Completion state lives only in the Obsidian task files and their records. Nothing in this
repository — including this file — is evidence that a task or milestone is complete.

## 7. Verify milestone completion

- After completing all tasks in a milestone:
  - Check that every task in the milestone has its required evidence
  - Run the milestone test gate; `gl milestone-status` records its result in Obsidian
  - Confirm that all dependencies for subsequent milestones are satisfied
  - Only then proceed to assign tasks from the next milestone
  - A milestone closes only when every owning task has its required evidence

## 8. Assign the next task

- Only after the verified task update is saved and read back, refresh dependency state and select
  the next eligible task.
- Start a fresh implementer assignment with the updated baseline and minimal context.
- Continue automatically within the original authorized run scope; do not stop
  after each successful task to ask whether to continue.
- Report concise progress at task transitions.

Stop when the authorized scope is complete, the user stops the run, no eligible work remains,
or a required external action lacks authorization. Summarize verified/recorded tasks, unfinished
work, exact blockers and the next eligible task. Do not infer that an MVP implementation run also
authorizes PR publication, production deployment, DNS changes or real email. Honor such permissions
if already granted in the session.

## Bootstrap and milestone exceptions

- GL-00 can receive a documentation-only verification PASS before test tooling exists; explicitly
  record the guide's exception, not a fake make check result. M0 aggregate evidence follows.
- GL-01/02/60/70 are coordinated scaffold units. If a required check awaits a prerequisite
  interface within that group, record the partial result as unchecked/in progress, independently
  verify the completed portion, and assign only the documented next scaffold prerequisite.
  This is the guide's narrow bootstrap exception to ordinary completed-dependency progression,
  not permission to mark a partial task done. Once tooling exists, revisit partial units for
  full verification and task closure before leaving M0. Do not require future milestone tests
  to exist for M0 foundation checks.
- GL-35 can complete as the internal generator without exposing a public partial-write endpoint;
  GL-36 owns atomic public generation plus snapshots. Do not reintroduce their dependency cycle.
- A milestone closes only when every owning task has its required evidence. Task-level PASS is
  not PR approval, merge evidence, live deployment or model benchmark evidence. Milestone and
  external gates remain those in the live guide and current user authorization.
