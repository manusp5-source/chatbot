# PROJECT — Chatbot

> Este fichero lo inyecta el hook `session-start.ps1` al abrir cada sesión en
> este directorio. Es el estado, no la documentación: lo que hay que saber para
> retomar. El manual es [`CLAUDE.md`](CLAUDE.md); el mapa técnico,
> [`design/design_summary.md`](design/design_summary.md).

## Qué es

Atención al cliente automatizada con un agente de IA para clínicas, más el panel
donde un humano ve y contesta lo mismo que ve el bot. Cinco canales: WhatsApp,
Instagram DM, correo, chat web y voz.

**Es el producto que se vende.** Un cliente por instalación.

## Estado — 17 de agosto de 2026

| | |
|---|---|
| Fase | Construido y en mantenimiento. No se planifica desde cero |
| Rama | `feat/factoria-artefactos` |
| Última tarea | Oleada 2 de FactorIA: reconstruir los artefactos del método a partir del código |
| Pruebas | 122 ficheros en `backend/tests/` |
| CI | `.github/workflows/ci.yml` — pruebas, migraciones ida y vuelta, tipos y build del panel, tres imágenes Docker |
| Migraciones | 55 revisiones, cabeza `0054_channel_secrets_encrypted` |

## Bloqueadores

| Qué | Detalle |
|---|---|
| **Sin remoto** | El repositorio no tiene copia fuera de esta máquina. Es el riesgo mayor y se salda en la **Oleada 0** del plan del paraguas |
| **La CI no basta por sí sola** | Se dispara con el push a `main` y EasyPanel despliega ese mismo push: cuando se pone roja, el contenedor roto ya va camino de producción. Faltan dos cosas a mano — protección de rama en GitHub y despliegue por webhook en EasyPanel. Paso a paso en `DEPLOY_EASYPANEL.md` §7 |
| **Guardarraíl clínico sin evals** | Vive en `services/agent_guardrails.py` y en el prompt. Convertirlo en casos de prueba de `limites` es la **Oleada 5** |

## Siguiente

1. Cerrar la Oleada 2: verificación con `/review` sobre este proyecto.
2. Oleada 0 del paraguas: remoto privado y copia fuera de la máquina.
3. Oleada 5: arnés del agente con la skill `entrenando-agentes`.

**No toca a este proyecto** la absorción de la Calculadora (Oleada 3, es del CRM)
ni la retirada de `UnicornIA-CRM` (Oleada 4).

## Dónde está cada cosa

| Necesitas | Fichero |
|---|---|
| Cómo se trabaja aquí, convenciones, avisos | [`CLAUDE.md`](CLAUDE.md) |
| Mapa de arquitectura y comandos de verificación | [`design/design_summary.md`](design/design_summary.md) |
| Qué existe y dónde | [`implementation/task_tracker.md`](implementation/task_tracker.md) |
| Qué entra y qué no | [`planning/scope.md`](planning/scope.md) |
| Decisiones y por qué | [`DECISIONS.md`](DECISIONS.md) |
| Lo aprendido a base de tropezar | [`KNOWLEDGE.md`](KNOWLEDGE.md) |
| Datos personales y seguridad | [`SECURITY.md`](SECURITY.md) |
| Desplegar | [`DEPLOY_EASYPANEL.md`](DEPLOY_EASYPANEL.md) |
