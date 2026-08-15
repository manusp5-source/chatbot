---
description: Comienza la ejecucion autonoma del plan aprobado. Verifica requisitos y construye.
---

# Start Autonomous Execution

> **Working files.** `docs/project_memory.md`, `implementation/task_tracker.md`
> and `docs/work_log.md` are YOUR running notes for this project. The delivered
> package does not ship them: they belong to whoever builds on it. If one is
> missing, create it from what you can read in the repo and carry on. Never stop
> because a tracking file is not there.

The user has approved the plan. Verify readiness and begin building.

## Pre-flight Check

Before writing any code, verify these exist and are populated:

- [ ] implementation/user_journeys.md — IT tasks and UJ journeys with acceptance criteria
- [ ] implementation/task_tracker.md — all items listed with status pending, milestones marked
- [ ] design/data_model.md — entities defined
- [ ] design/api_contracts.md — endpoints defined
- [ ] design/ui_wireframes.md — navigation and layouts defined
- [ ] design/design_summary.md — compact project reference generated
- [ ] docs/project_memory.md — shows planning complete

If anything is missing, complete it first.

## Execution Order

1. Complete Infrastructure Tasks (IT) first, in tracker order
2. Then User Journeys (UJ) in tracker order, grouped by milestone
3. After each item: update task_tracker.md + work_log.md
4. After each milestone: run /review
5. After all items: demo data verification + final security audit + deployment preparation

## Working on each item

Follow the Workflow in CLAUDE.md for Infrastructure Tasks and User Journeys. Do not duplicate it here.

If a journey is too large for a single session:
- Split into verifiable sub-steps within the journey definition
- Complete each sub-step fully before ending the session
- Update project_memory.md noting which sub-steps are done
- Journey stays `in_progress` until all sub-steps complete

## Stop and ask the user when:

- A requirement is ambiguous and you can't assume reasonably
- A design decision contradicts another
- An integration doesn't work as expected
- Something is significantly more complex than planned
- A security concern requires architectural change
- Scope needs to change

## Completion

When all items show completed:

1. Final /review for unreviewed items
2. Create seed script with realistic demo data for all entities
3. Verify every page renders correctly with demo data — no broken layouts, no empty states
4. Verify empty states render gracefully — clear demo data and check
5. Holistic security audit (auth, secrets, injection, API exposure, deployment hardening)
6. Deployment preparation (Docker, deployment_guide.md)
7. Update project_memory.md: phase = "Delivered"
8. Present completion summary to user
