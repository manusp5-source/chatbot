# API Contracts — Chatbot

## Base URL

`/api/v1`

## Autenticación

JWT en header `Authorization: Bearer <token>`. Tokens emitidos por `/auth/login`. Expiración default 24h.

Endpoints públicos (sin auth): `/auth/login`, `/health`, `/webhooks/ycloud`.

---

## Auth

### POST `/auth/login`
**Auth:** público
**Request:** `{ email, password }`
**Response 200:** `{ access_token, token_type: "bearer", user: { id, email, role, nombre } }`
**Errores:** 401 credenciales inválidas

### POST `/auth/logout`
**Auth:** required
**Response 200:** `{ ok: true }`

### GET `/auth/me`
**Auth:** required
**Response 200:** `{ id, email, role, nombre }`

---

## Webhooks (proveedores)

### POST `/webhooks/ycloud`
**Auth:** validación HMAC firma
**Request:** payload YCloud (texto, audio, status updates)
**Response 200:** `{ ok: true }` inmediato (procesamiento async vía Celery)
**Errores:** 401 firma inválida, 400 payload malformado

---

## Conversations (rol cliente)

### GET `/conversations`
**Query params:** `status`, `tag_id`, `unread`, `search`, `page`, `page_size`
**Response 200:** `{ items: [...], total, page, page_size }`

### GET `/conversations/{id}`
**Response 200:** conversación con contact + últimos mensajes

### GET `/conversations/{id}/messages`
**Query params:** `before`, `limit`
**Response 200:** `{ items: [...] }` (paginación cursor)

### POST `/conversations/{id}/messages`
Enviar mensaje manual del operador.
**Request:** `{ contenido: string }`
**Response 200:** mensaje creado
**Errores:** 400 fuera de ventana 24h, 409 conversación cerrada

### POST `/conversations/{id}/take-over`
Operador toma control (cambia status a `humano`).
**Response 200:** conversación actualizada

### POST `/conversations/{id}/return-to-bot`
**Response 200:** conversación actualizada

### POST `/conversations/{id}/close`
**Request:** `{ enviar_resumen_email?: boolean }`
**Response 200:** conversación cerrada (+ email enviado si aplica)

### POST `/conversations/{id}/mark-read`
**Response 200:** `{ ok: true }`

### WebSocket `/ws/inbox`
**Auth:** JWT en query param o header inicial
**Eventos emitidos:**
- `message.new` — `{ conversation_id, message }`
- `conversation.updated` — `{ conversation_id, status, last_message_at, ... }`
- `conversation.assigned` — `{ conversation_id, user_id }`

---

## Contacts (rol cliente)

### GET `/contacts`
**Query params:** `search`, `tag_id`, `estado`, `page`, `page_size`

### GET `/contacts/{id}`

### POST `/contacts`
**Request:** `{ telefono, email?, nombre?, estado?, servicio_interes?, notas_internas? }`

### PATCH `/contacts/{id}`
**Request:** campos editables parcial

### DELETE `/contacts/{id}`

### POST `/contacts/{id}/tags`
**Request:** `{ tag_id }`

### DELETE `/contacts/{id}/tags/{tag_id}`

---

## Tags (rol cliente)

### GET `/tags`
### POST `/tags` — `{ nombre, color }`
### PATCH `/tags/{id}`
### DELETE `/tags/{id}`

---

## Knowledge Base (rol cliente)

### GET `/kb/documents`
### POST `/kb/documents` (multipart/form-data) — sube archivo
**Response 200:** `{ id, nombre, formato, status: "procesando" }`

### DELETE `/kb/documents/{id}` — borra documento y sus chunks

### POST `/kb/search` — búsqueda de prueba
**Request:** `{ query: string, top_k?: number }`
**Response 200:** `{ results: [{ chunk_id, document_id, contenido, similarity, metadata }] }`

---

## Dashboard (rol cliente)

### GET `/dashboard/summary`
**Query params:** `range` = `7d` | `30d` | `90d`
**Response 200:** conversaciones totales, leads, tasa derivación, tiempo medio respuesta, top etiquetas

---

## Admin

### Users

- `GET /admin/users` — listar
- `POST /admin/users` — `{ email, password, role, nombre }`
- `PATCH /admin/users/{id}` — `{ role?, nombre?, activo? }`
- `POST /admin/users/{id}/reset-password`
- `DELETE /admin/users/{id}`

### Credentials

- `GET /admin/credentials` — listar (valores enmascarados: `sk-...***...abc`)
- `PUT /admin/credentials/{key}` — `{ value }` (se cifra con Fernet antes de persistir)
- `POST /admin/credentials/{key}/test` — prueba conexión, devuelve `{ ok, message }`

### Agent Config

- `GET /admin/agent/config` — versión activa
- `GET /admin/agent/config/history` — historial de versiones
- `POST /admin/agent/config` — crear nueva versión (no activa por defecto)
- `POST /admin/agent/config/{id}/activate` — activa una versión

### Audit Log

- `GET /admin/audit` — filtros: `user_id`, `action`, `entity`, `from`, `to`, `page`

### Health

- `GET /health` — público, status básico
- `GET /admin/health` — auth, detalle: DB, Redis, Celery workers, últimos errores

---

## Error Format

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "Conversación no encontrada",
    "details": { "id": "..." }
  }
}
```

Códigos comunes: `INVALID_CREDENTIALS`, `FORBIDDEN`, `RESOURCE_NOT_FOUND`, `VALIDATION_ERROR`, `RATE_LIMITED`, `WHATSAPP_24H_WINDOW_EXPIRED`, `INTERNAL_ERROR`.

## Rate Limiting

- `/auth/login`: 5 req/min por IP
- `/webhooks/ycloud`: 100 req/min por IP (YCloud tiene IPs conocidas)
- Resto endpoints autenticados: 300 req/min por usuario

## Paginación

Default `page=1`, `page_size=20`. Máximo `page_size=100`. Respuesta incluye `total`, `page`, `page_size`.
