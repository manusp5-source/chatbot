# Infrastructure Tasks & User Journeys — Chatbot

> **Aviso.** Este documento es del diseño inicial: describe la intención, no
> el árbol de ficheros de hoy. Varias piezas acabaron en otro sitio (por
> ejemplo, todo lo de administración vive en `backend/app/api/admin.py`).
> Para saber dónde está algo, manda el código.

## Infrastructure Tasks

### IT-01: Docker Compose + servicios base

- **Purpose:** Infraestructura corriendo localmente y en EasyPanel (app, worker, beat, db, redis).
- **Components:** `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`, healthchecks.
- **Acceptance:** `docker compose up` levanta todos los servicios sin errores. `docker compose ps` los muestra "healthy".
- **Security:** Postgres con password de `.env`. Puertos internos no expuestos al host salvo `app` (8000) y `frontend` (5173).

### IT-02: Esqueleto FastAPI

- **Purpose:** Servidor HTTP arrancando con endpoint `/health`.
- **Components:** `backend/app/main.py`, `core/config.py` (pydantic-settings), `core/logging.py` (structlog), `db/session.py` (async engine SQLAlchemy).
- **Acceptance:** `GET /health` devuelve 200 con `{ status: "ok", db: "ok" }`.

### IT-03: Alembic + migración inicial

- **Purpose:** Esquema BD completo + función `match_chunks` SQL.
- **Components:** `alembic.ini`, `backend/app/db/migrations/`, primera migración con todas las tablas + extension vector.
- **Acceptance:** `alembic upgrade head` aplica todas las migraciones desde DB vacía. Todas las tablas y la función existen.

### IT-04: Auth JWT + RBAC

- **Purpose:** Login, emisión token, decoders, guards por rol.
- **Components:** `core/security.py`, `api/auth.py`, dependencias FastAPI `get_current_user`, `require_role(...)`.
- **Acceptance:** Login con seed admin funciona. Endpoint protegido sin token → 401. Con rol incorrecto → 403.
- **Security:** bcrypt cost 12. JWT con expiración. Rate limit en `/auth/login`.

### IT-05: Esqueleto Celery

- **Purpose:** Workers y beat corriendo, conectados a Redis.
- **Components:** `backend/app/tasks/__init__.py` (Celery app), worker dockerfile, beat schedule básica.
- **Acceptance:** Tarea `ping()` se encola desde un endpoint test y el worker la procesa, log visible.

### IT-06: Esqueleto frontend

- **Purpose:** App React arrancando con login + rutas vacías por rol.
- **Components:** `frontend/` con Vite + React + TS + Tailwind + shadcn/ui + React Router + Zustand.
- **Acceptance:** `npm run dev` levanta el frontend. Login funcional contra backend. Redirige según rol.

### IT-07: Cliente HTTP centralizado

- **Purpose:** Llamadas API uniformes, refresh token, manejo errores.
- **Components:** `frontend/src/services/api.ts`, interceptors, store auth.

### IT-08: Servicio cifrado credenciales

- **Purpose:** Cifrar/descifrar valores en tabla `credentials` con Fernet.
- **Components:** `backend/app/services/credentials_service.py`, integración con `core/config.py` (lee `ENCRYPTION_KEY`).
- **Acceptance:** Endpoint admin guarda credencial, valor en DB es bytea cifrado, lectura devuelve plaintext.

### IT-09: Tests automáticos críticos

- **Purpose:** Cobertura mínima en código sensible.
- **Components:** `tests/` con suites para auth, agente (mock OpenAI), kb_search, webhooks (firma HMAC).
- **Acceptance:** `pytest` pasa todos los tests, cobertura ≥70% en módulos críticos.

### IT-10: Seed con datos demo realistas

- **Purpose:** Despliegue limpio tiene usuarios, prompt, etiquetas, ejemplo conversación.
- **Components:** `backend/scripts/seed.py` ejecutable vía `docker compose exec app python -m app.scripts.seed`.

### IT-11: Audit log automático

- **Purpose:** Registrar cambios sensibles sin tener que llamar manualmente.
- **Components:** Decorador / middleware que captura mutaciones en endpoints admin.

### IT-12: EasyPanel + healthchecks

- **Purpose:** App lista para producción en EasyPanel.
- **Components:** Templates/manifests EasyPanel, healthchecks Docker, instrucciones `deployment/easypanel/README.md`.

---

## User Journeys

### UJ-01: Webhook YCloud entrante

- **Description:** YCloud envía webhook con mensaje. La app valida firma, persiste mensaje crudo, encola tarea de procesamiento.
- **Backend:** `api/webhooks.py POST /webhooks/ycloud`, `services/webhook_validator.py`, modelos `messages`, tarea Celery `process_message`.
- **Frontend:** N/A.
- **Acceptance:** Webhook real desde YCloud llega, mensaje queda en DB en estado pre-procesado, tarea encolada.
- **Security:** Validación HMAC obligatoria. Rate limit por IP. 401 si firma inválida.

### UJ-02: Agente IA procesa y responde

- **Description:** Worker Celery construye contexto, llama a OpenAI con tools, envía respuesta por WhatsApp.
- **Backend:** `agents/orchestrator.py`, `agents/tools/*`, `providers/llm/openai_client.py`, `providers/whatsapp/ycloud.py`.
- **Frontend:** N/A.
- **Acceptance:** Mensaje "hola" → bot responde con saludo en <15s. Tool `consultar_kb` puede invocarse con éxito.
- **Tests:** Mock OpenAI, simular invocación de tools, validar prompt enviado.

### UJ-03: Buffer Redis 5s

- **Description:** Si llegan varios mensajes del mismo número en ráfaga, se acumulan antes de procesarse.
- **Backend:** `services/message_buffer.py` con Redis (clave por teléfono, TTL 5s configurable).
- **Acceptance:** Tres mensajes seguidos del mismo número en <5s → el bot responde una sola vez al texto combinado.

### UJ-04: Memoria conversacional

- **Description:** El agente tiene contexto de los últimos N mensajes (default 20) de esa conversación.
- **Backend:** Query a `messages` filtrada por `conversation_id`, ordenada por fecha, limitada a `context_window`.
- **Acceptance:** Mensaje 21 sigue teniendo contexto desde el mensaje 1 si entra dentro del límite. Mensaje 100 solo tiene 20 últimos.

### UJ-05: Subir documento KB (PDF/DOCX/TXT/MD)

- **Description:** Cliente sube documento, se chunkea y se indexa en background.
- **Backend:** `api/knowledge_base.py POST /kb/documents`, `services/kb_indexer.py`, tarea Celery `index_document`, libs: `pypdf`, `python-docx`.
- **Frontend:** Página KB con uploader drag-drop, lista con estados.
- **Acceptance:** Subir un PDF → aparece "Procesando…", al terminar "Indexado" con nº chunks. Búsqueda RAG recupera contenido del PDF.

### UJ-06: Subir documento KB (CSV/XLSX)

- **Description:** Cada fila se convierte en un chunk con formato "Columna1: valor | Columna2: valor".
- **Backend:** Extensión de `kb_indexer.py` con `pandas` para CSV/XLSX.
- **Acceptance:** Subir CSV de catálogo → cada fila = un chunk. Búsqueda por nombre de producto devuelve la fila correcta.

### UJ-07: Búsqueda de prueba KB

- **Description:** Cliente prueba qué responde el RAG ante una pregunta antes de que lo haga el bot.
- **Backend:** `api/knowledge_base.py POST /kb/search`.
- **Frontend:** Panel collapsible en página KB con input + resultados.
- **Acceptance:** Pregunta devuelve top 5 chunks con similarity score y referencia al documento.

### UJ-08: Auto-crear contacto desde mensaje

- **Description:** Cuando entra un mensaje de un número desconocido, se crea automáticamente un contacto.
- **Backend:** En `services/conversation.py` antes de procesar el mensaje. Upsert por `telefono`.
- **Acceptance:** Primer mensaje de un número nuevo → contacto existe en DB con `estado=contacto`, `origen=whatsapp`.

### UJ-09: CRUD manual contactos

- **Description:** Cliente puede crear, editar, ver y borrar contactos.
- **Backend:** `api/contacts.py` CRUD completo.
- **Frontend:** Página `/contacts` con tabla + modal/drawer ficha.
- **Acceptance:** Crear contacto manualmente, editarlo, asignarle etiquetas, verlo en inbox.

### UJ-10: CRUD etiquetas

- **Description:** Cliente gestiona etiquetas con nombre y color, las asigna a contactos.
- **Backend:** `api/tags.py`, `api/contacts.py` (`POST/DELETE /contacts/{id}/tags/{tag_id}`).
- **Frontend:** Página `/tags` + selector en ficha contacto.
- **Acceptance:** Crear etiqueta "VIP" rojo, asignar a 3 contactos, filtrar inbox por "VIP" → solo se ven esos.

### UJ-11: Listado inbox conversaciones

- **Description:** Lista con filtros y paginación.
- **Backend:** `GET /conversations` con filtros.
- **Frontend:** Columna izquierda inbox.
- **Acceptance:** Filtrar por "humano" muestra solo las derivadas. Paginar funciona. Buscar por nombre filtra.

### UJ-12: Vista chat WhatsApp Web

- **Description:** Burbujas, agrupación por día, scroll infinito hacia mensajes antiguos.
- **Backend:** `GET /conversations/{id}/messages` con cursor pagination.
- **Frontend:** Componente `ChatView` con `MessageBubble`.
- **Acceptance:** Conversación con 200 mensajes carga progresivamente al scrollear arriba.

### UJ-13: WebSocket inbox tiempo real

- **Description:** Nuevos mensajes aparecen en el inbox sin recargar.
- **Backend:** `WebSocket /ws/inbox` + bus eventos en `core/events.py`. Cada `process_message` publica evento.
- **Frontend:** `services/websocket.ts` con reconnect, store reactivo.
- **Acceptance:** Dos pestañas abiertas en `/inbox` → mensaje nuevo se ve en ambas <1s.

### UJ-14: Ficha contacto lateral

- **Description:** Panel derecho con datos, etiquetas, notas, botones de acción.
- **Frontend:** Componente `ContactCard`.
- **Acceptance:** Cambiar etiqueta o nota se persiste con autosave. Botones cambian estado conversación.

### UJ-15: Derivación humana

- **Description:** El agente invoca tool `derivar_humano`. Conversación pasa a `humano`. Bot deja de responder.
- **Backend:** Tool en `agents/tools/human_handoff.py`. Cambia `conversations.status`.
- **Acceptance:** Pedir al bot "quiero hablar con una persona" → tool invocado, status cambia, próximo mensaje NO procesado por bot.

### UJ-16: Notificación Telegram + Slack

- **Description:** Al derivar, notificar al equipo por canales configurados.
- **Backend:** `providers/notifications/telegram.py`, `slack.py`, hook en `human_handoff`.
- **Acceptance:** Tras derivación, llega aviso a Telegram con resumen + link al inbox.

### UJ-17: Respuesta manual operador

- **Description:** Operador responde desde el inbox. Texto va a WhatsApp.
- **Backend:** `POST /conversations/{id}/messages`. Valida ventana 24h.
- **Frontend:** Caja respuesta abajo del chat. Disabled si fuera de ventana.
- **Acceptance:** Operador escribe y envía → mensaje llega a WhatsApp del cliente. Si >24h, botón desactivado con aviso.

### UJ-18: Transcripción audio

- **Description:** Audio de WhatsApp se descarga y transcribe con Whisper.
- **Backend:** Tarea Celery `transcribe_audio`. Guarda audio en volumen + transcripción en `messages.audio_transcript`.
- **Acceptance:** Mensaje audio entra → en <10s la transcripción está en DB. Agente usa la transcripción como texto.

### UJ-19: Reproductor audio + transcripción en inbox

- **Description:** En el chat, los audios tienen play + transcripción visible.
- **Frontend:** Componente `AudioPlayer`.
- **Acceptance:** Audio se reproduce inline. Transcripción se muestra debajo (colapsable).

### UJ-20: Admin CRUD usuarios

- **Description:** Admin gestiona usuarios y roles.
- **Backend:** `api/admin/users.py`.
- **Frontend:** Página `/admin/users`.

### UJ-21: Admin credenciales cifradas

- **Description:** Editar claves de proveedores. Cifrado al guardar. Test conexión.
- **Backend:** `api/admin/credentials.py` usando `services/credentials_service.py`.
- **Frontend:** Página `/admin/credentials`.
- **Acceptance:** Guardar key OpenAI → en DB está cifrada. "Probar conexión" devuelve ok/error real.

### UJ-22: Admin editor prompt versionado

- **Description:** Editor texto + historial versiones + activar.
- **Backend:** `api/admin/agent.py` (config + history).
- **Frontend:** Página `/admin/agent/prompt`.

### UJ-23: Admin configuración modelo

- **Description:** Form con modelo, temperatura, buffer, etc.
- **Backend:** `POST /admin/agent/config`.
- **Frontend:** Página `/admin/agent/config`.

### UJ-24: Admin audit log

- **Description:** Tabla filtrable con cambios sensibles.
- **Backend:** `GET /admin/audit` con filtros.
- **Frontend:** Página `/admin/audit`.

### UJ-25: Admin salud del sistema

- **Description:** Estado servicios, métricas, últimos errores.
- **Backend:** `GET /admin/health`.
- **Frontend:** Página `/admin/health`.

### UJ-26: Email resumen al cerrar conversación

- **Description:** Botón "Cerrar y enviar resumen" en inbox → email vía Resend.
- **Backend:** `POST /conversations/{id}/close` con flag. `providers/email/resend_client.py`.
- **Acceptance:** Cerrar conversación con flag → llega email al contacto con resumen generado.

### UJ-27: Dashboard cliente

- **Description:** KPIs + gráficos según rango.
- **Backend:** `GET /dashboard/summary`.
- **Frontend:** Página `/dashboard` con cards + Recharts o similar.
