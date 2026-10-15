# Task Tracker — Chatbot

Reconstruido el 17 de agosto de 2026 a partir del código, no del plan original.
La aplicación **ya está construida**: este documento no es una lista de trabajo
pendiente, es el inventario de lo que existe y dónde está.

**Regla al marcar:** un `[x]` exige un fichero que se pueda abrir. Lo que no se
ha podido localizar en el árbol queda en `[ ]` con su nota, aunque el diseño
original lo diera por hecho.

Leyenda: `[x]` hecho y verificado en código · `[~]` en curso · `[ ]` sin hacer ·
`[!]` bloqueado · `[≠]` hecho, pero en otro sitio o de otra forma que la que
describe [`user_journeys.md`](user_journeys.md).

> El propio `user_journeys.md` avisa en su cabecera de que describe la intención,
> no el árbol de hoy. Por eso hay tantos `[≠]`: no son fallos, son el diseño que
> se movió durante la construcción.

**Cómo leer las rutas de la columna Evidencia.** Van abreviadas, con la base
implícita del área: `api/…`, `services/…`, `tasks/…`, `agents/…`, `core/…`,
`models/…` y `providers/…` cuelgan de `backend/app/`; `pages/…`, `components/…`
y `hooks/…`, de `frontend/src/`. Cuando una fila cita varios ficheros de la
misma carpeta, solo el primero lleva la carpeta delante. En las filas `[≠]`, la
ruta que aparece **tachada por el texto** ("no existe `providers/notifications/`")
es precisamente la que el diseño prometía y el código no tiene.

---

## Infrastructure Tasks

| # | Tarea | Estado | Evidencia |
|---|---|---|---|
| IT-01 | Docker Compose + servicios base | `[x]` | `docker-compose.yml`, `docker-compose.easypanel.yml`, `backend/Dockerfile`, `frontend/Dockerfile`. Siete servicios: app, worker, beat, db (pgvector/pgvector:pg16), redis, frontend, mcp |
| IT-02 | Esqueleto FastAPI | `[x]` | `backend/app/main.py`, `core/config.py`, `core/logging.py`, `db/session.py`, `api/health.py` |
| IT-03 | Alembic + migración inicial | `[x]` | `backend/alembic.ini`, `app/db/migrations/versions/` — **55 revisiones**, cabeza `0054_channel_secrets_encrypted`. La CI comprueba ida y vuelta |
| IT-04 | Auth JWT + RBAC | `[x]` | `core/security.py`, `api/auth.py`, `api/deps.py`. Roles `admin` y `cliente` |
| IT-05 | Esqueleto Celery | `[x]` | `app/tasks/__init__.py` con el `beat_schedule`, `tasks/ping.py`. 17 tareas registradas |
| IT-06 | Esqueleto frontend | `[x]` | `frontend/src/router.tsx`, `components/ProtectedRoute.tsx`, `components/Layout.tsx`. El admin aterriza en la Home; el operador, en el inbox |
| IT-07 | Cliente HTTP centralizado | `[x]` | `frontend/src/services/api.ts` y nueve servicios por área |
| IT-08 | Servicio cifrado credenciales | `[≠]` | Existe, pero repartido: `core/encryption.py`, `core/encrypted_type.py` (tipo `EncryptedText`), `services/credentials.py`, `services/channel_secrets.py`. No hay `credentials_service.py` |
| IT-09 | Tests automáticos críticos | `[x]` | `backend/tests/` — **136 ficheros** `test_*.py` (a 15 oct 2026) |
| IT-10 | Seed con datos demo | `[x]` | `app/scripts/seed.py`, lanzado por `backend/entrypoint.sh` al arrancar el contenedor |
| IT-11 | Audit log automático | `[x]` | `services/audit.py`, tabla `audit_log`, panel en `pages/admin/AuditPage.tsx` |
| IT-12 | EasyPanel + healthchecks | `[x]` | `DEPLOY_EASYPANEL.md`, `docker-compose.easypanel.yml`, `deployment/README.md`, `scripts/smoke.sh`, `api/health.py` |

**12 de 12.** Ninguna pendiente.

---

## User Journeys

### Mensajería y agente

| # | Journey | Estado | Evidencia |
|---|---|---|---|
| UJ-01 | Webhook entrante | `[≠]` | `api/webhooks.py`. No es solo YCloud: hay selector de proveedor de WhatsApp (YCloud y Meta) más Instagram, Gmail, chat web y voz |
| UJ-02 | El agente procesa y responde | `[x]` | `agents/orchestrator.py`, `agents/tools/registry.py`, `providers/llm/`, `tasks/process_message.py` |
| UJ-03 | Buffer de ráfaga | `[x]` | `services/message_buffer.py` (Redis), `tasks/drain_buffer.py`. La voz va sin buffer: es síncrona |
| UJ-04 | Memoria conversacional | `[x]` | `services/conversation.py`, `services/rolling_summary.py` — además de la ventana de N mensajes hay resumen acumulado |
| UJ-15 | Derivación humana | `[x]` | `agents/tools/human_handoff.py`. Cambia el estado de la conversación y el bot se calla |
| UJ-16 | Aviso al equipo al derivar | `[≠]` | En `agents/tools/human_handoff.py` y `services/security_alerts.py`, con la configuración en `core/config.py` y `api/admin.py`. No existe `providers/notifications/` |
| UJ-17 | Respuesta manual del operador | `[x]` | `api/conversations.py`, `services/channel_sender.py`. Ventana de 24 h contemplada |
| UJ-18 | Transcripción de audio | `[x]` | `tasks/transcribe_audio.py`, `providers/transcription/`, `services/audio_processor.py`. La transcripción se guarda cifrada |

### Base de conocimiento

| # | Journey | Estado | Evidencia |
|---|---|---|---|
| UJ-05 | Subir documento (PDF/DOCX/TXT/MD) | `[x]` | `api/knowledge_base.py`, `services/kb_indexer.py`, `tasks/index_document.py` |
| UJ-06 | Subir documento (CSV/XLSX) | `[x]` | `services/kb_indexer.py`, mismo camino de indexado |
| UJ-07 | Búsqueda de prueba | `[x]` | `api/knowledge_base.py`, `pages/client/KnowledgeBase.tsx`. Búsqueda híbrida: vector + texto completo |

### CRM e inbox

| # | Journey | Estado | Evidencia |
|---|---|---|---|
| UJ-08 | Auto-crear contacto | `[x]` | `services/conversation.py`, `services/contact_merge.py`. Busca primero por BSUID y después por teléfono |
| UJ-09 | CRUD manual de contactos | `[x]` | `api/contacts.py`, `pages/client/Contacts.tsx`, `pages/client/ContactDetail.tsx` |
| UJ-10 | CRUD de etiquetas | `[x]` | `api/tags.py`, `components/TagPicker.tsx` |
| UJ-11 | Listado del inbox | `[x]` | `api/conversations.py`, `pages/client/Inbox.tsx` |
| UJ-12 | Vista de chat | `[x]` | `components/MessageBubble.tsx`, `components/MediaBubble.tsx` |
| UJ-13 | Tiempo real | `[x]` | `hooks/useInboxSocket.ts` sobre el bus de `core/events.py`. En el frontend es un hook, no `services/websocket.ts` |
| UJ-14 | Ficha lateral de contacto | `[x]` | `pages/client/ContactDetail.tsx`, `components/EstadoSelect.tsx`, `components/TagPicker.tsx` |
| UJ-19 | Audio con reproductor y transcripción | `[x]` | `components/MediaBubble.tsx:120` — `<audio controls>` |
| UJ-26 | Resumen por correo al cerrar | `[x]` | `api/conversations.py:1342` `close_conversation`, con la bandera `enviar_resumen_email` (línea 1365) |

### Panel de administración

| # | Journey | Estado | Evidencia |
|---|---|---|---|
| UJ-20 | CRUD de usuarios | `[≠]` | `api/admin.py` + `pages/admin/UsersPage.tsx`. Todo el admin vive en **un** fichero, no en `api/admin/users.py` |
| UJ-21 | Credenciales cifradas | `[x]` | `api/admin.py`, `services/credentials.py`, `pages/admin/ConnectionsPage.tsx`, `components/CredentialFields.tsx` |
| UJ-22 | Editor de prompt con versiones | `[≠]` | `pages/admin/AgentsPage.tsx` y tabla `agent_prompt_history`. Es por agente, no un prompt único: hay agentes de texto y de voz |
| UJ-23 | Configuración del modelo | `[x]` | `pages/admin/ConfigPage.tsx`, `pages/admin/LLMProvidersPanel.tsx`, `pages/admin/ModelPricesPanel.tsx` |
| UJ-24 | Auditoría | `[x]` | `api/admin.py`, `pages/admin/AuditPage.tsx` |
| UJ-25 | Salud del sistema | `[x]` | `api/health.py`, `pages/admin/HealthPage.tsx`, `pages/admin/SystemLogs.tsx` |
| UJ-27 | Dashboard con KPIs | `[≠]` | `services/dashboard.py`, `GET /admin/dashboard/summary` (`services/admin.ts:148`), pintado en `pages/admin/AdminHome.tsx`. Quedó del **lado admin**, no como `/dashboard` del cliente |

**27 de 27**, seis de ellas `[≠]`.

---

## Fuera del plan original — construido después

Nada de esto aparece en `user_journeys.md`. Está en producción y es la mitad del
valor de la aplicación hoy.

| Bloque | Evidencia |
|---|---|
| **Clasificador de entrada** | `services/classifier.py`, `classifier_rules.py`, `classifier_config.py`, `pages/admin/ClassifierPage.tsx` |
| **Difusiones masivas** | `tasks/outbound_send.py`, `pages/admin/OutboundPage.tsx`, con bajas (`outbound_optout`) |
| **Agente interno del operador** | `agents/internal/`, `components/InternalAgentWidget.tsx`, con presupuesto propio y blindaje anti-inyección |
| **Autoaprendizaje** | `api/learning.py`, `services/learned_rules.py`, `tasks/detect_faq_gaps.py`, `tasks/detect_correction_gaps.py`, `pages/admin/LearningPage.tsx` |
| **Canal de voz** | `api/voice.py`, `providers/voice/`, `pages/client/Calls.tsx` |
| **Instagram y Gmail** | `providers/instagram/`, `providers/gmail/`, `services/gmail_ingest.py`, `gmail_quarantine.py`, `gmail_retention.py` |
| **Copias de seguridad** | `api/backups.py`, `services/backups.py`, `tasks/backup_db.py`, `pages/admin/BackupsPage.tsx` |
| **Coste y tope de gasto** | `services/usage_tracker.py`, `llm_pricing.py`, `tasks/refresh_prices.py`, `pages/admin/AgentTokensPanel.tsx` |
| **Traza del agente** | `services/trace_logger.py`, `components/ConversationTracePanel.tsx`, `pages/admin/FlowDashboard.tsx` |
| **RGPD** | `services/data_erasure.py`, exportación de ficha, purgas periódicas (`purge_audios`, `purge_emails`, `purge_internal_logs`) |
| **Servidor MCP** | `mcp-server/`, expone la agent-api a asistentes externos |
| **Notificaciones push** | `api/push.py`, `services/web_push.py`, `tasks/web_push_send.py` |

---

## Pendiente de verdad

| # | Qué | Por qué |
|---|---|---|
| IT-13 | Arnés del agente conversacional | **Hecho en lo determinista** (`arnes/`, 27 casos; guardarraíl en `agents/guardarrail_clinico.py`). Siguen sin ejecutarse contra la instalación real con LLM los casos F2, F3 y T2, y quedan 5 funcionales/tono en rojo. Ver `arnes/PROGRESO.md` |
| IT-14 | Remoto de git | `origin` existe. Falta comprobar que cada rama de trabajo esté subida y la protección de `main` (decisión de Manuel, `DEPLOY_EASYPANEL.md` §7) |

---

## Trabajo de cierre de gaps — 14 oct 2026 (rama `feat/gaps-entrevista`)

Cada fila nace de un intent (`planning/intent-002.md` a `intent-004.md`) o de una decisión
(`DECISIONS.md` DEC-14 a DEC-17). Columna `Review ✓`: la pasada 1 de `/review` (revisor sonnet + crítico opus,
subagentes distintos) dio **«arreglar primero»**; los hallazgos sostenidos están arreglados con su eval
nuevo (ver `docs/work_log.md`, «## Review»). La pasada 2 (15 oct) dio «avanzar» con las salvedades del work_log; las filas siguen en `REVIEW` hasta que Manuel acepte y se mezcle la rama, y G2a no pasa a `DONE` sin ejecutar F2 con LLM real.

| ID | Qué | Skill | Model | Eval ✓ | Review ✓ | Estado |
|---|---|---|---|---|---|---|
| G1 | Aviso de IA en el primer mensaje de texto (cierra L9-B) | ninguna | codex | `test_aviso_ia_primera_interaccion.py`, `test_aviso_ia_bd.py` (Postgres real; incluye el puente de derivación) + L9 sin `xfail` | pasada 1 «arreglar primero» → arreglado; pasada 2 (15 oct) «avanzar» con salvedades | `REVIEW` |
| G2a | Regla 7 de `SECURITY_GUARD` (anti-sustitución) | ninguna | codex | Prueba del prompt ensamblado, ejecutada. **F2 ejecutado con LLM real el 15 oct: no estable** (26 tiradas, 4 redacciones; T2 pasa 7/7, F3 bloqueado); ver DEC-21 | pasada 1 «arreglar primero» → arreglado; pasada 2 (15 oct) «avanzar» con salvedades | `REVIEW` |
| G2b | Verificador de importes en la salida + `consultar_kb` vacío explícito | ninguna | sonnet (por orden expresa de Manuel, no Codex) | `test_output_verifier.py`, en rojo y en verde (ampliado tras la review). **No atrapa F2 literal** (DEC-15) | pasada 1 «arreglar primero» → arreglado; pasada 2 (15 oct) «avanzar» con salvedades | `REVIEW` |
| G3 | Guardarraíl: cobertura, seguimiento sin pronombre y capa LLM opcional | ninguna | sonnet (por orden expresa de Manuel, no Codex) | `test_guardarrail_clinico_cobertura.py` y `_capa_llm.py`. **Capa LLM medida con el modelo real el 15 oct: 36/36 paráfrasis, 1/36 falso positivo, 9/9 inyecciones resistidas** (muestra pequeña, sin latencia ni coste) | pasada 1 «arreglar primero» → arreglado; pasada 2 (15 oct) «avanzar» con salvedades | `REVIEW` |
| G5 | KPIs de calidad (`GET /dashboard/quality`) | ninguna | sonnet | `test_quality_kpis.py` contra Postgres real | pasada 1 «arreglar primero» → arreglado; pasada 2 (15 oct) «avanzar» con salvedades | `REVIEW` |
| G6 | Escalado SLA y aviso de fuera de horario (ambos opt-in) | ninguna | sonnet | `test_handoff_sla.py` y `test_ajustes_derivacion.py` (GET/PUT `/admin/settings/handoff`) contra Postgres real | pasada 1 «arreglar primero» → arreglado; pasada 2 (15 oct) «avanzar» con salvedades | `REVIEW` |
| G7 | DPIA técnica (borrador) en `../legal/dpia-chatbot-borrador.md` | ninguna | sonnet | n/a (documento): rutas citadas comprobadas | pasada 1 (14 oct): arreglar primero → arreglado; pasada 2 pendiente | `REVIEW` |
| G8 | Filtro determinista de instrucciones dirigidas al asistente en los fragmentos de la KB (T2B, DEC-22) | ninguna | sonnet (por orden expresa de Manuel, no Codex) | `test_kb_sanitizer.py` en rojo y en verde; **T2B contra LLM real: 4/4 fallaban sin filtro (también con regla de prompt reforzada) y 4/4 pasan con él**, juez ciego | **no ha pasado `/review`**: se añadió después de las dos pasadas | `REVIEW` |

**Sin hacer, a propósito:** ejecutar F2/T2/F3 contra la app real (necesita claves de LLM y gasta
dinero), router y agentes por dominio (DEC-17), streaming de voz, equipos y skills de handoff,
cifrado de `Message.contenido`, CI bloqueando el despliegue.
