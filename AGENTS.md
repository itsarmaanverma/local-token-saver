# Codex Continuation Instructions

This repository uses agent-mesh for sequential continuity between Codex and Claude. Work on exactly one checklist task at a time.

## Start every session

1. Set `MESH_PLATFORM=codex`.
2. Run `python C:/Users/armaa/agent-mesh/mesh.py resume --platform codex`.
3. Read `docs/PHASE_PROGRESS.md` and the newest `.agent-mesh/handoff.md` block addressed to `codex` or `any`.
4. Run `git status --short --branch` and inspect existing changes before editing.
5. Continue only the active task, or claim the first unchecked task if none is active.

## Editing and verification rules

- Coordination mode is sequential. Do not launch parallel agents or begin another checklist item.
- Claim the task and run `mesh check` for every planned file before editing.
- Keep code, tests, benchmarks, and documentation changes inside the current task's scope.
- Do not mark a task complete until targeted tests, the full suite, changed-file lint, and its benchmark pass.
- Baseline: 83 passed, 6 skipped, and one Windows containment failure. No phase may add failures; P02 must remove the known failure.
- Preserve unrelated changes and never rewrite shared history.

## Complete or pause a task

- Cross off the task in `docs/PHASE_PROGRESS.md`; add a brief summary, verification evidence, commit SHA, and two-line handoff.
- Run `mesh done <task-id>`, append a handoff to `any`, push the checkpoint, and stop.
- Do not start the next task without explicit user approval.
- If blocked or close to a usage cutoff, leave the working tree intact, record dirty files and the next command, append a handoff, and stop.

