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
| IT-09 | Tests automáticos críticos | `[x]` | `backend/tests/` — **122 ficheros** `test_*.py` |
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
| IT-13 | Arnés del agente conversacional | El guardarraíl clínico vive hoy en `services/agent_guardrails.py` y en el prompt. Falta convertirlo en casos de prueba de la categoría `limites`. Es la **Oleada 5** (skill `entrenando-agentes`), no se hace aquí |
| IT-14 | Remoto de git | El repositorio no tiene copia fuera de esta máquina. **Oleada 0** |
