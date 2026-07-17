# Claude Continuation Instructions

This repository is a sequential, cross-platform remediation project. Work on exactly one task at a time.

## Start every session

1. Set `MESH_PLATFORM=claude`.
2. Run `python C:/Users/armaa/agent-mesh/mesh.py resume --platform claude`.
3. Read `docs/PHASE_PROGRESS.md` and the newest `.agent-mesh/handoff.md` block addressed to `claude` or `any`.
4. Run `git status --short --branch` and inspect any existing diff before editing.
5. Continue only the active task, or claim the first unchecked task if none is active.

## Editing and verification rules

- Coordination mode is sequential. Do not launch parallel agents or begin a second checklist item.
- Before editing, claim the task with agent-mesh and run `mesh check` for every planned file.
- Keep implementation, tests, benchmarks, and progress updates within the active task's scope.
- Do not mark a task complete unless targeted tests, the full suite, changed-file lint, and its benchmark pass.
- The known baseline is 83 passed, 6 skipped, and one Windows containment failure; no earlier phase may add failures, and P02 must remove the known failure.
- Never overwrite unrelated or pre-existing changes.

## Complete or pause a task

- Update the matching checklist entry in `docs/PHASE_PROGRESS.md` with a brief summary, verification evidence, and commit SHA.
- Add two concise handoff lines describing the next task and its first command.
- Run `mesh done <task-id>` and append a handoff to `any`; push the code and progress checkpoint.
- Stop after the push. Do not start the next task without explicit user approval.
- If blocked or near a usage cutoff, keep the current worktree intact, record dirty files and the next command, append a handoff, and stop.

