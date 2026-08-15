# System Architecture — Chatbot

> **Aviso.** Este documento es del diseño inicial: describe la intención, no
> el árbol de ficheros de hoy. Varias piezas acabaron en otro sitio (por
> ejemplo, todo lo de administración vive en `backend/app/api/admin.py`).
> Para saber dónde está algo, manda el código.

## Overview

App single-tenant para atención al cliente vía WhatsApp con agente IA, RAG, CRM e inbox tiempo real. Desplegada como conjunto de contenedores Docker en EasyPanel sobre un VPS por cliente.

```
┌─────────────────────────────────────────────────────────────────┐
│                          EasyPanel (VPS)                         │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │   FastAPI    │  │   Celery     │  │   Postgres   │           │
│  │  + WebSocket │  │   workers    │  │  + pgvector  │           │
│  │   (app)      │  │   (worker)   │  │     (db)     │           │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘           │
│         │                  │                                     │
│         └──────────┬───────┘                                     │
│                    │                                             │
│              ┌─────▼─────┐    ┌──────────┐                       │
│              │   Redis   │    │  Celery  │                       │
│              │           │    │   beat   │                       │
│              └───────────┘    └──────────┘                       │
└─────────────────────────────────────────────────────────────────┘
        ▲                                  │
        │ webhook YCloud                   │ outbound
        ▼                                  ▼
   WhatsApp                       OpenAI (LLM + embeddings
   (vía YCloud)                   + Whisper), Resend,
                                  Telegram, Slack
```

## Components

### Backend — Python + FastAPI

- **api/** — endpoints REST + WebSocket por dominio (auth, conversations, contacts, kb, admin, webhooks)
- **agents/** — orchestrator del agente IA + tools (kb_search, contact_lookup, contact_upsert, human_handoff)
- **providers/** — adaptadores externos: `whatsapp/`, `llm/`, `transcription/`, `embeddings/`, `notifications/`, `email/`. Cada uno con interfaz base + implementación concreta.
- **services/** — lógica de negocio: message_buffer, conversation, kb_indexer, audio_processor
- **models/** — SQLAlchemy ORM
- **schemas/** — Pydantic (request/response)
- **db/** — engine, sesión, migraciones Alembic
- **core/** — config, security, logging, eventos internos (pub/sub para WebSocket)
- **tasks/** — Celery tasks: process_message, index_document, transcribe_audio, send_email_summary

### Frontend — React

- **pages/admin/** — usuarios, credenciales, prompts, audit, salud
- **pages/client/** — inbox, contactos, base de conocimiento, dashboard
- **components/** — ChatView, MessageBubble, AudioPlayer, TagPicker, ContactCard
- **services/api.ts** — cliente REST
- **services/websocket.ts** — cliente WebSocket
- Routing con React Router. Estado con Zustand o Context. Estilos con Tailwind.

### Base de datos — PostgreSQL 16 + pgvector

- Una sola DB para todo: memoria conversacional, CRM, RAG.
- Extension `vector` activada en migración inicial.
- Función SQL `match_chunks(query_embedding, threshold, limit)` para búsqueda semántica.
- Migraciones gestionadas con Alembic (`backend/app/db/migrations/`).

### Autenticación

- JWT (HS256) con bcrypt para hash de passwords.
- Tokens con expiración (default 24h) refrescables.
- Roles `admin` / `cliente` con guard a nivel router.
- Audit log de accesos sensibles.

### Cola de tareas — Celery + Redis

- Broker Redis (DB 1)
- Backend Redis (DB 2)
- Buffer mensajes Redis (DB 0)
- Workers escalables horizontal (cada uno procesa N tareas en paralelo)
- Celery beat para tareas programadas (cierre conversaciones inactivas, recordatorios derivación humana)

### WebSocket

- Endpoint `/ws/inbox` con autenticación JWT.
- Suscripción por canal (cliente recibe solo sus conversaciones).
- Bus interno publish/subscribe en `core/events.py` que conecta procesamiento Celery → WebSocket.

## Credential Level Mapping

- **Level 1 (.env)** — secretos de infraestructura:
  - `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`
  - `JWT_SECRET`, `ENCRYPTION_KEY`
  - `INITIAL_ADMIN_EMAIL`, `INITIAL_ADMIN_PASSWORD` (solo para seed)

- **Level 2 (deployment config)** — variables que dependen del entorno:
  - `APP_ENV`, `APP_BASE_URL`, `FRONTEND_BASE_URL`
  - `AUDIO_STORAGE_PATH`, defaults del agente (modelo, temperatura, buffer)

- **Level 3 (admin panel, cifradas en DB)** — claves de proveedores:
  - YCloud (api key, webhook secret, phone number)
  - OpenAI (api key)
  - Resend (api key, from email)
  - Telegram (bot token, chat id)
  - Slack (webhook url)

## Integration Points

| Servicio | Uso | Tipo |
|---|---|---|
| YCloud | WhatsApp Business API: webhook entrante, envío mensajes, descarga audios | HTTP REST |
| OpenAI | LLM (GPT-5.4 mini con tool use), embeddings, transcripción Whisper | HTTP REST (SDK oficial) |
| Resend | Email transaccional (resumen al cierre) | HTTP REST |
| Telegram | Notificación equipo en derivación humana | HTTP Bot API |
| Slack | Notificación equipo en derivación humana | Webhook |

Todos los proveedores se acceden a través de su interfaz abstracta en `providers/` para permitir cambios futuros con un solo adaptador.
