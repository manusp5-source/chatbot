# Resumen de diseño — Chatbot

Referencia compacta. Para trabajar en el código, la fuente principal es
[`CLAUDE.md`](../CLAUDE.md); esto es el mapa de arquitectura que hay debajo.

**Comprobado contra el código el 2026-08-12.**

## Stack

- **Backend:** Python 3.12 + FastAPI (async) + SQLAlchemy 2 + Alembic + Pydantic v2 + Celery 5 + Redis 7
- **Panel:** React 18 + TypeScript + Vite + Tailwind + Zustand + React Router v6
- **Base de datos:** PostgreSQL 16 + pgvector. Claves primarias uuid. 38 tablas.
  Cabeza de migraciones: `0054_channel_secrets_encrypted` (`alembic heads` lo confirma)
- **Sesión:** JWT (HS256) + bcrypt. Roles `admin` y `cliente`

## Canales

WhatsApp, chat web, voz (Retell), Instagram DM y correo (Gmail / SMTP).

- Cada `Channel` tiene `type`, `enabled`, `config` (JSONB) y `agent_id → Agent`.
  Los secretos de la config van cifrados desde `0054`, no en plano.
- Salida: `services/channel_sender.py` despacha según `conv.canal`.
- Qué agente atiende cada canal: `services/runtime_config.py`
  (`Channel.agent_id → Agent`, con caída a `agent_config` si no hay nada).
- Pausa del bot, global o por canal: `services/agent_pause.py` (Redis). Los
  cinco canales están cubiertos.

## Backend, por carpetas

- `api/` — auth, conversations (REST + WebSocket), contacts, tags,
  knowledge_base, learning, admin, webhooks, voice, webchat, oauth, uploads,
  push, backups, agent_api, health
- `agents/orchestrator.py` + `agents/tools/` — kb_search, contact_lookup,
  contact_upsert, human_handoff, calendar, schedule_config, registry
- `agents/internal/` — agente interno del operador (service, agent, tools,
  prompts, budget)
- `providers/` — llm (openai, anthropic, gemini), whatsapp (ycloud, meta +
  selector), voice (retell), instagram, gmail, email (resend, smtp),
  transcription, embeddings
- `services/` — clasificador, difusiones, fusión de fichas, borrado RGPD,
  buffer de mensajes, indexador de la base de conocimiento, presupuesto,
  moderación, copias de seguridad, trazas, precios de modelos, autoaprendizaje
- `tasks/` — process_message, drain_buffer, index_document, transcribe_audio,
  outbound_send, poll_gmail, backup_db, purge_*, detect_*_gaps, refresh_prices,
  web_push_send, notify_pending_check, ping. El `beat_schedule` está en
  `tasks/__init__.py`

## Panel, por carpetas

- `pages/client/` — Inbox, Contacts, ContactDetail, Calls, KnowledgeBase
- `pages/admin/` — AdminHome, ConnectionsPage, AgentsPage, ClassifierPage,
  OutboundPage, UsersPage, BackupsPage, LLMProvidersPanel, ModelPricesPanel,
  AgentTokensPanel, LearningPage, AuditPage, HealthPage, BlocklistPage,
  SystemLogs, FlowDashboard, AgentDashboard, ConfigPage, SettingsPage,
  InternalAgentSettingsPage, InternalAgentPromptPage
- Rutas en `src/router.tsx`

## Agentes

Tres conceptos distintos:

- `agents` — los que atienden a clientes. Campos: `name`, `prompt_system`,
  `model_name`, `kind`, `tools_enabled` (JSONB), `knowledge_base_filter`
  (JSONB). Se enlazan 1:1 con un `Channel`. Dos tipos: **`text`** (WhatsApp,
  Instagram, correo, web) y **`voice`** (llamadas). Canal y agente tienen que
  coincidir de tipo. La instalación siembra uno de cada, "Agente de Texto" y
  "Agente de Voz", con el prompt a medio rellenar.
- `internal_agent_config` — el agente interno del operador. Fila única, sin
  histórico, con su presupuesto y su límite diario.
- `agent_config` — el singleton antiguo. Sigue ahí como red de seguridad: el
  runtime lo usa solo si no hay agente asignado al canal.

Los prompts viven en la base de datos y se editan desde el panel, no en
ficheros. `agent_prompt_history` guarda las versiones anteriores.

## Tablas principales

`users` · `credentials` (cifradas) · `channels` · `external_apis` ·
`agents` / `agent_config` / `internal_agent_config` / `agent_prompt_history` /
`agent_tokens` · `contacts` (teléfono único, BSUID único parcial, `in_crm`) —
N:N con `tags` vía `contact_tags` — más `contact_note` y `contact_activity` ·
`conversations` (`canal` + `status` bot/humano/cerrada) — 1:N `messages` ·
`documents` / `document_versions` — 1:N `chunks` (embedding vector(1536)) ·
`kb_edit_proposals` · `knowledge_gaps` · `learned_rules` · `agent_corrections` ·
`classifier_config` / `classifier_rules` · `outbound_job` /
`outbound_job_recipient` / `outbound_optout` · `llm_providers` /
`llm_model_price` / `llm_usage_log` · `backup_settings` / `backup_runs` ·
`audit_log` · `agent_trace_event` · `app_settings` · `push_subscription` ·
`whatsapp_template_var`

El detalle columna a columna está en [`data_model.md`](data_model.md), que es un
resumen: la verdad es el modelo en `backend/app/models/`.

## Patrones

- **Interfaz por proveedor** (`LLMProvider`, `WhatsAppProvider`…). Ningún
  endpoint llama a una API de fuera directamente.
- **Tool use** del agente con esquemas Pydantic convertidos a JSON Schema.
- **Buffer en Redis de unos segundos** antes de procesar, para agrupar los
  mensajes que llegan seguidos. La voz va sin buffer, es síncrona.
- **WebSocket** sobre un bus interno de publicación/suscripción
  (`core/events.py`).
- **El estado de la conversación** decide si el bot responde o se calla.
- **Cifrado en reposo** con Fernet (`core/encrypted_type.py`) en notas internas,
  NIF, dirección, transcripciones y credenciales. No se puede filtrar por esas
  columnas en SQL.
- **Búsqueda híbrida** en la base de conocimiento: vector (pgvector) más texto
  completo en español. Funciones `match_chunks` y `match_chunks_hybrid`.

## Coste y observabilidad

- `usage_tracker` + `llm_pricing` → `llm_usage_log`. Los precios se refrescan
  con la tarea `refresh_prices` y se pueden editar en el panel.
- Tope mensual en dólares con pausa automática del agente. Cuenta los tokens y
  también los minutos de las llamadas de voz.
- `agent_trace_event`: traza de cada paso del agente, visible por conversación.
- `audit_log`: quién hizo qué en el panel.

## Verification Commands

Lo que hay que ejecutar para decir que algo funciona. Verificar es haber corrido
el comando y mirado la salida, no que el fichero exista.

**Levantar y comprobar que responde**

```bash
cp .env.desarrollo.example .env     # y rellenar las tres claves (ver CLAUDE.md)
docker compose up -d
curl http://localhost:8000/health   # 200; espera ~30 s al arranque
open http://localhost:5173          # panel
```

`/health` devuelve **200 aunque el worker esté caído**, a propósito: mira el
campo `worker` del cuerpo, no el código de estado.

**Pruebas del backend** (desde `backend/`, con una base pgvector aparte —
Postgres del Compose no publica puerto al host)

```bash
docker run -d --name chatbot-test-db -p 5466:5432 \
  -e POSTGRES_USER=chatbot -e POSTGRES_DB=test -e POSTGRES_HOST_AUTH_METHOD=trust \
  pgvector/pgvector:pg16

python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export DATABASE_URL="postgresql+asyncpg://chatbot@127.0.0.1:5466/test"
export ENCRYPTION_KEY="<clave Fernet>"     # obligatoria aunque no cifres nada
.venv/bin/alembic upgrade head
.venv/bin/pytest -q
```

Sin base de datos se saltan unas 290 pruebas y fallan cuatro. Usa un puerto
libre: si hay otro Postgres delante, el síntoma es
`FATAL: database "test" does not exist`.

**Migraciones, ida y vuelta** (lo que exige la CI)

```bash
.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head
```

**Panel**

```bash
cd frontend && npm ci && npx tsc --noEmit -p tsconfig.json && npm run build
```

**Todo junto**, tal como lo ejecuta `.github/workflows/ci.yml`: pruebas del
backend contra Postgres real, migraciones en los dos sentidos, tipos y build del
panel, y construcción de las tres imágenes Docker. `ruff` sale en el informe
pero **no bloquea**.

## Credenciales

- **En el `.env`:** `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`,
  `JWT_SECRET`, `ENCRYPTION_KEY`, `INITIAL_ADMIN_*`
- **En la configuración del despliegue:** `APP_ENV`, `APP_BASE_URL`,
  `FRONTEND_BASE_URL`, `AUDIO_STORAGE_PATH`, `DEFAULT_LLM_*`
- **En el panel, cifradas:** claves de OpenAI/Anthropic/Gemini, WhatsApp,
  Resend/SMTP, Telegram, Slack, Retell, Meta/Instagram y Google (Calendar y
  entrada por Gmail)
