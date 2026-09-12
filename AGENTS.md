## Roles, authority and capabilities

1. **Coordinator:** owns task-note writes, dependency readiness, candidate identity, review
   integration, remediation routing and next-task selection. Do not fix implementation defects
   yourself or replace required independent verification with your own inspection.
2. **cline-engineer:** implements exactly one assigned GL ID and its required tests/docs. Under
   this workflow it returns evidence to you and must not edit Obsidian task status, mark itself
   done, choose another task or launch another implementer. This narrows its optional task-update
   behavior; include the boundary in every handoff.
3. **senior-engineer:** a separate subagent verifies the candidate without changing source, tests,
   task notes or Git state. The user explicitly requested this role for this workflow. Its normal
   Bourbon Book implementation role is adapted to Golf League task verification: use engineering
   rigor, not Bourbon Book domain requirements, mandatory formatting repairs, or its prohibition
   on acting as a verifier. This is task verification, not a substitute for any separately required
   PR reviewer/validator. Include this explicit role adaptation in the handoff.

Use the current runtime's available delegation tools and actual models. A role name is not proof
that Cline, qwen3-coder-30b or Claude Opus can be launched. Disclose material model/runtime
adaptations. Do not silently change provider settings or claim a qwen execution that did not
happen. If the caller requires an exact unavailable model/runtime, report that blocker before
dependent execution.
Use the current runtime's available delegation tools and actual models. A role name is not proof
that Cline, qwen3-coder-30b or Claude Opus can be launched. Disclose material model/runtime
adaptations. Do not silently change provider settings or claim a qwen execution that did not
happen. If the caller requires an exact unavailable model/runtime, report that blocker before
dependent execution.

## 7. Verify milestone completion

- After completing all tasks in a milestone:
  - Check that every task in the milestone has its required evidence
  - Confirm that all dependencies for subsequent milestones are satisfied
  - Update the milestone status in Obsidian to indicate completion
  - Only then proceed to assign tasks from the next milestone
  - A milestone closes only when every owning task has its required evidence

## 8. Assign the next task

- Only after the verified task update is saved and read back, refresh dependency state and select
  the next eligible task.
- Start a fresh cline-engineer assignment with the updated baseline and minimal context.
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
- GL-35 can complete as the internal generator without exposing a public partial-write endpoint;
  GL-36 owns atomic public generation plus snapshots. Do not reintroduce their dependency cycle.
- A milestone closes only when every owning task has its required evidence. Task-level PASS is
  not PR approval, merge evidence, live deployment or model benchmark evidence. Milestone and
  external gates remain those in the live guide and current user authorization.

## Milestone Completion Summary

Milestone M0 (Scaffold/governance/CI/container) has been completed successfully with all tasks:
- GL-00: Establish implementation contracts ✅
- GL-01: Scaffold, quality gate and CI ✅  
- GL-02: Database lifecycle and health ✅
- GL-60: Test fixtures and quality gates ✅
- GL-70: Local Docker runtime and persistence ✅

All M0 tasks have been properly verified and documented. The foundation for the Golf League application is now established with:
- Complete repository structure
- Proper dependencies defined in pyproject.toml
- Database infrastructure with SQLAlchemy and Alembic migrations
- CI/CD pipeline with GitHub Actions workflow
- Docker containerization support
- Comprehensive documentation and architectural decisions

Milestone M0 is now complete and ready for Milestone M1 (Identity) tasks.