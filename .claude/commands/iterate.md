---
description: Modo iteracion post-entrega. Para aplicar cambios, mejoras o corregir bugs.
---

# Iteration Mode

> **Working files.** `docs/project_memory.md`, `implementation/task_tracker.md`
> and `docs/work_log.md` are YOUR running notes for this project. The delivered
> package does not ship them: they belong to whoever builds on it. If one is
> missing, create it from what you can read in the repo and carry on. Never stop
> because a tracking file is not there.

The project has been delivered. The user is requesting changes.

## Before any change:

1. Read the user's request carefully in the conversation
2. Read docs/project_memory.md for current state
3. Read implementation/task_tracker.md and user_journeys.md for context
4. Classify: bug fix, enhancement, new feature, or planning gap

## Bug fix:

Identify root cause → assess regression risk → fix → test the fix → verify affected journey still works end-to-end → update work_log.md

## Planning gap:

Identify what was missed → define as new UJ or add to existing → add to user_journeys.md and task_tracker.md → implement following standard workflow → no user approval needed (already in scope)

## Enhancement:

Identify affected journey → state changes needed (backend/frontend/both) → assess regression risk → update design docs if needed → implement → verify end-to-end → update work_log.md

## New feature:

Define as new IT or UJ with acceptance criteria → add to user_journeys.md and task_tracker.md → check impact on existing journeys → present scope to user for approval → implement following standard workflow

## Always:

- Apply security checklist to all changed code
- Verify existing functionality still works
- Update task_tracker.md, work_log.md, project_memory.md
- Update design docs if schema, endpoints, or UI changed
- Update design_summary.md if the change affects module structure, entity relationships, or key patterns

## Never:

- Change existing behavior without documenting why
- Skip regression verification
- Modify DB schema without updating data_model.md
- Add endpoints without updating api_contracts.md
