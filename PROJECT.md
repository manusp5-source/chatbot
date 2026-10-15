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
| Rama | `feat/gaps-entrevista` (trabajo de cierre de gaps; sin mergear) |
| Última tarea | Cierre de gaps del 14-15 oct 2026 (ver `implementation/task_tracker.md`, sección «Trabajo de cierre de gaps») |
| Pruebas | 134 ficheros en `backend/tests/` (a 15 oct 2026) |
| CI | `.github/workflows/ci.yml` — pruebas, migraciones ida y vuelta, tipos y build del panel, tres imágenes Docker |
| Migraciones | 55 revisiones, cabeza `0054_channel_secrets_encrypted` |

## Bloqueadores

| Qué | Detalle |
|---|---|
| **Remoto sin proteger** | `origin` existe (`github.com/manusp5-source/chatbot`), pero no hay protección de rama ni se ha comprobado que cada rama de trabajo esté subida. Lo sin commitear sigue sin estar a salvo: el hook `checkpoint.ps1` lo manda al stash |
| **La CI no basta por sí sola** | Se dispara con el push a `main` y EasyPanel despliega ese mismo push: cuando se pone roja, el contenedor roto ya va camino de producción. Faltan dos cosas a mano — protección de rama en GitHub y despliegue por webhook en EasyPanel. Paso a paso en `DEPLOY_EASYPANEL.md` §7 |
| **Guardarraíl clínico: reglas + capa LLM opcional** | Vive en `agents/guardarrail_clinico.py` (reglas, el suelo) y `agents/guardarrail_clinico_llm.py` (capa 2, apagada por defecto, solo añade derivaciones). Con evals de `limites` y multi-turno en `arnes/` y en `tests/test_guardarrail_clinico*.py`. Sin cubrir: idiomas distintos de ES/EN/CA básicos y el seguimiento a dos turnos |

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
