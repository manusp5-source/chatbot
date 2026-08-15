# API para agentes externos

API pensada para que los asistentes del operador monitoricen la app, revisen/editen
la base de conocimiento y mejoren los prompts de los agentes. **Pásale este documento
a tu asistente** junto con el token y la URL base.

## Autenticación

1. Crea un token en el panel: **Conexiones → Tokens de agente** (elige nombre y ámbitos).
   El token (`agt_…`) se muestra **una sola vez**.
2. Envía cada petición con la cabecera:

```
Authorization: Bearer agt_XXXXXXXX
```

- Base URL: `https://<tu-dominio>/api/v1/agent-api`
- Rate limit: 120 peticiones/minuto por token (HTTP 429 al superarlo).
- Token revocable al instante desde el panel; toda escritura queda auditada.

## Ámbitos

| Ámbito | Da acceso a |
|---|---|
| `monitor:read` | `GET /monitor/overview`, `GET /monitor/llm-costs`, `GET /monitor/channels`, `GET /monitor/health` |
| `interactions:read` | `GET /interactions`, `GET /interactions/{id}/metadata` |
| `kb:read` | `GET /kb/documents`, `GET /kb/documents/{id}`, `POST /kb/search` |
| `kb:write` | `POST /kb/documents`, `PUT /kb/documents/{id}` |
| `prompts:write` | `GET /agents`, `PUT /agents/{id}/prompt` |

Esta API **no** expone contenido de conversaciones, contactos, credenciales ni
configuración: el ámbito máximo posible es métricas agregadas + metadata de
conversaciones + KB + prompts. No existe un `health:read` aparte: la salud
operativa (BD/Redis/colas/canales) es la misma clase de riesgo que las métricas
agregadas (cero PII, cero contenido) y va dentro de `monitor:read`.

## Endpoints

### Monitorización

```
GET /monitor/overview?range=today|7d|30d|90d
```
Devuelve agregados: conversaciones por estado/canal, handoffs, volumen de mensajes
por rol, coste LLM del rango y canales pausados. Sin contenido de mensajes.

```
GET /monitor/llm-costs?range=24h|7d|30d|month
```
Coste/uso LLM con la MISMA query que el dashboard admin (los números cuadran con
el panel): totales, desglose por agente (`by_agent`), por modelo (`by_model`) y
serie por día (`series`).

```
GET /monitor/channels
```
Estado operativo por canal: pausado/activo, configurado/habilitado, agente
asignado y última entrada de cliente (`last_inbound_at`, proxy del último
webhook). Incluye `recent_errors` del runtime, saneados (solo ts/level/event/canal).

```
GET /monitor/health
```
Salud del backend: `{status, db, redis, queues: {celery_backlog,
outbound_jobs_pending}}`.

### Interacciones (solo metadata, sin PII)

```
GET /interactions?status=bot|humano|cerrada&canal=whatsapp|web|instagram_dm|email|retell_voice&contact_id=<uuid>&limit=20
GET /interactions/{id}/metadata
```
`contact_id` acota la lista a las conversaciones de un contacto concreto. Sin
él hay que paginar, y las conversaciones sin actividad (`last_activity_at`
nulo) quedan siempre al final del orden.

Metadata operativa de conversaciones: id, canal, estado, `created_at`,
`last_activity_at`, `seconds_in_status`, `assigned` (bool) y el contacto SOLO
como UUID (`contact_id`). El detalle añade `ended_at`, `derivada_a_humano_at`,
`archived`/`quarantined` y conteo de mensajes por rol. **Nunca** devuelve texto
de mensajes ni nombre/teléfono/email del contacto.

### Base de conocimiento

```
GET  /kb/documents                     → lista (id, nombre, formato, editable…)
GET  /kb/documents/{id}                → contenido completo (texto)
POST /kb/search      {"query": "...", "top_k": 5}
POST /kb/documents   {"titulo": "...", "contenido": "..."}     (crea .txt indexado)
PUT  /kb/documents/{id} {"contenido": "..."}                   (solo txt/md)
```

Notas para el asistente:
- Antes de editar, **lee el documento** (`GET /kb/documents/{id}`): el `PUT`
  reemplaza el contenido ENTERO, no hace parches.
- Cada edición guarda la versión anterior (el operador puede restaurar desde el panel)
  y reindexa automáticamente (chunks + embeddings).
- Los formatos binarios (pdf/docx/xlsx) se leen pero no se editan (HTTP 409):
  propón al operador crear un documento de texto nuevo si hace falta corregirlos.

### Prompts de agentes

```
GET /agents                            → agentes con su prompt_system actual
PUT /agents/{id}/prompt {"prompt_system": "..."}
```

Notas para el asistente:
- El prompt anterior queda en el historial (restaurable desde Agentes en el panel).
- Las reglas de seguridad del sistema (anti-inyección, transparencia IA) **no forman
  parte del prompt**: están fijas en código y no se pueden alterar por esta vía.
- Cambios de prompt afectan al bot que habla con clientes **al instante**: haz cambios
  incrementales y avisa al operador de qué cambiaste y por qué.

## Ejemplos

```bash
BASE="https://<tu-dominio>/api/v1/agent-api"
TOKEN="agt_XXXX"

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/monitor/overview?range=7d"

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/kb/documents"

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "horario de atención"}' "$BASE/kb/search"

curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"contenido": "Atendemos de 9 a 20h de lunes a viernes."}' \
  "$BASE/kb/documents/<document_id>"
```

## Errores

| Código | Significado |
|---|---|
| 401 | Token ausente, inválido, revocado o caducado |
| 403 | El token no tiene el ámbito necesario |
| 404 | Recurso no encontrado |
| 409 | Documento no editable (binario) |
| 429 | Rate limit del token superado |
