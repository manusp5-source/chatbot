---
description: Inicializa un nuevo proyecto desde la plantilla. Activa el modo planificacion.
---

# Project Initialization — Planning Mode

You are initializing a new project from this template.

CLAUDE.md is already loaded and governs your behavior. Do not repeat its rules — follow them.

Complete Planning Mode by following these steps in order:

1. **Understand**: Read the project description (START_PROJECT_PROMPT.md or conversation). Analyze purpose, users, roles, integrations, constraints.

2. **Ask**: Identify blocking questions and assumptions. Write to planning/questions.md. Wait for user answers on blocking items.

3. **Requirements & Scope**: Define functional/non-functional requirements (planning/requirements.md), scope boundaries (planning/scope.md), and risks (planning/risks.md).

4. **Design**: Create design docs with priority-based depth:
   - HIGH detail: data_model.md, api_contracts.md (read before every journey)
   - MEDIUM detail: ui_wireframes.md, architecture.md (read occasionally)
   - LIGHT detail: style_guide.md, stack_selection.md
   - Include credential level mapping (Level 1/2/3) in architecture.md
   - Write nfr.md in docs/

5. **Decompose**: Break all work into Infrastructure Tasks (IT) and User Journeys (UJ) in implementation/user_journeys.md. Group related UJs into milestones (3-6 related journeys that form a functional unit). Populate implementation/task_tracker.md with milestone boundaries.

6. **Skills & MCPs**: Do not skip this step. Do not write "None selected" without exploring first. Search MCP registries and skill registries for tools matching the project stack. List available skills/MCPs in the environment. Install and verify selected ones. Document in skills/inventory.md and mcps/inventory.md.

7. **Decisions**: Record all significant decisions in docs/decision_log.md.

8. **Memory & Summary**: Update docs/project_memory.md with current state. Generate design/design_summary.md — a compact (~50 line) reference with stack, module map, entity list, key patterns, and credential map.

9. **Present**: Show structured summary with IT list, UJ list (grouped by milestone, in execution order), estimated sessions, open questions, top risks. Wait for user approval.

**Quality check before presenting:**
- Every feature maps to an IT or UJ
- Every UJ has a frontend page in the navigation
- API contracts cover every journey's needs
- Data model covers every journey's storage
- Admin panel has dedicated journeys
- Every credential is assigned a level

**If planning needs multiple sessions:** Complete current step fully, update project_memory.md with progress, resume with /session-start next session.

No code until user approves.
