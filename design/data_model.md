# Modelo de datos — Chatbot

> **Esto es un resumen, no un contrato.** La verdad son los modelos de
> `backend/app/models/` y las migraciones de
> `backend/app/db/migrations/versions/`. Si algo de aquí no cuadra con el
> código, manda el código.
>
> Documenta **10 de las 38 tablas**: las que se tocan a diario. El resto están
> listadas al final.
>
> **Comprobado contra el código** contra una base de datos migrada hasta
> `0054_channel_secrets_encrypted`.

## Cómo leer los tipos

- `timestamptz` es siempre `timestamp with time zone`.
- **`bytea` = columna cifrada** con Fernet (`backend/app/core/encrypted_type.py`).
  En Python se lee y se escribe como texto normal, pero **la base de datos no
  ve el contenido**: no se puede filtrar, ordenar ni indexar por ella. Van
  marcadas con **(cifrada)**.

## Tablas

### `users`

Usuarios del panel.

| Campo | Tipo | Constraints | Notas |
|---|---|---|---|
| id | uuid | PK | |
| email | varchar(255) | UNIQUE, NOT NULL | en minúsculas |
| password_hash | varchar(255) | NOT NULL | bcrypt |
| role | enum `user_role` | NOT NULL | `admin` \| `cliente` |
| nombre | varchar(120) | | |
| activo | boolean | NOT NULL, default true | |
| last_login | timestamptz | | |
| password_changed_at | timestamptz | | |
| created_at | timestamptz | NOT NULL, default now() | |
| updated_at | timestamptz | NOT NULL, default now() | |

### `credentials`

Claves de proveedores externos, cifradas. Se rellenan desde el panel.

| Campo | Tipo | Constraints | Notas |
|---|---|---|---|
| id | uuid | PK | |
| key | varchar(80) | UNIQUE, NOT NULL | ej. `openai_api_key` |
| value_encrypted | bytea | NOT NULL | **(cifrada)** |
| descripcion | text | | texto de ayuda del panel |
| updated_at | timestamptz | NOT NULL, default now() | |
| updated_by | uuid | FK users.id | |

### `contacts`

El CRM. **20 columnas.** Tres van cifradas.

| Campo | Tipo | Constraints | Notas |
|---|---|---|---|
| id | uuid | PK | |
| telefono | varchar(320) | UNIQUE, NOT NULL | es la llave de la ficha. WhatsApp en E.164 (`+34…`); los canales sin teléfono usan un identificador con prefijo: `ig:`, `web:`, `email:`, `wa:` |
| email | varchar(255) | | |
| nombre | varchar(120) | | |
| social_handle | varchar(80) | | usuario de Instagram |
| wa_user_id | varchar(128) | UNIQUE parcial | **BSUID** de WhatsApp; el índice es `WHERE wa_user_id IS NOT NULL` |
| wa_parent_user_id | varchar(128) | | cuenta padre de Meta, solo traza |
| estado | enum `contact_estado` | NOT NULL, default `contacto` | `contacto` \| `solicitud_presupuesto` \| `seguimiento` \| `cliente` \| `perdido` \| `no_cualifica` |
| origen | enum `contact_origen` | NOT NULL | `whatsapp` \| `web` \| `manual` \| `instagram` \| `email` |
| servicio_interes | varchar(200) | | |
| empresa | varchar(200) | | |
| cargo | varchar(120) | | |
| web | varchar(255) | | |
| nif | bytea | | **(cifrada)** |
| direccion | bytea | | **(cifrada)** |
| notas_internas | bytea | | **(cifrada)** |
| in_crm | boolean | NOT NULL, default true | si la ficha sale en el CRM o solo en el inbox |
| ultimo_mensaje_at | timestamptz | | |
| created_at | timestamptz | NOT NULL, default now() | |
| updated_at | timestamptz | NOT NULL, default now() | |

Índices: `ix_contacts_telefono`, `contacts_telefono_key` (único),
`ux_contacts_wa_user_id` (único parcial), `ix_contacts_estado`,
`ix_contacts_in_crm`, `ix_contacts_ultimo_mensaje_at`.

Para añadir un campo aquí hay que tocar trece sitios. La lista está en
[`../CLAUDE.md`](../CLAUDE.md), sección "Cómo añadir un campo a la ficha de
contacto".

### `tags` y `contact_tags`

Etiquetas configurables, relación N:M con contactos.

`tags`: `id` (uuid, PK), `nombre` (varchar(60), único), `color`
(varchar(7), hex `#RRGGBB`), `created_at`.

`contact_tags`: `contact_id` y `tag_id` (ambos FK con `ON DELETE CASCADE`) más
`created_at`. Clave primaria compuesta.

### `conversations`

Un hilo con un contacto por un canal. Ha crecido mucho: aquí van las columnas
que se usan siempre, y agrupadas las de cada función.

| Campo | Tipo | Constraints | Notas |
|---|---|---|---|
| id | uuid | PK | |
| contact_id | uuid | FK contacts.id, NOT NULL | |
| canal | enum `conversation_canal` | NOT NULL | `whatsapp` \| `web` \| `instagram_dm` \| `email` \| `retell_voice` |
| session_id | varchar(320) | NOT NULL | identificador del hilo en su canal |
| status | enum `conversation_status` | NOT NULL, default `bot` | `bot` \| `humano` \| `cerrada` |
| started_at | timestamptz | NOT NULL, default now() | |
| ended_at | timestamptz | | |
| last_message_at | timestamptz | | |
| derivada_a_humano_at | timestamptz | | |
| asignada_a | uuid | FK users.id | operador |
| archived_at | timestamptz | | |
| resumen | bytea | | **(cifrada)** — se genera al cerrar |
| rolling_summary | bytea | | **(cifrada)** — resumen que se va actualizando |
| rolling_summary_upto | int | NOT NULL, default 0 | hasta qué mensaje va el resumen |

Del clasificador: `quarantined_at`, `quarantine_reason`, `spam_reviewed`.
Del canal de correo: `gmail_thread_id`, `subject`.
Del canal de voz: `call_duration_seconds`, `call_ended_reason`, `call_cost_usd`,
`call_cost_source`, `call_recording_url` **(cifrada)**.

### `messages`

| Campo | Tipo | Constraints | Notas |
|---|---|---|---|
| id | uuid | PK | |
| conversation_id | uuid | FK conversations.id ON DELETE CASCADE, NOT NULL | |
| rol | enum `message_role` | NOT NULL | `user` \| `assistant` \| `operator` \| `system` |
| contenido | text | | |
| audio_url | varchar(500) | | ruta del fichero de audio |
| audio_transcript | bytea | | **(cifrada)** — transcripción |
| metadata | jsonb | NOT NULL, default `{}` | tool_calls, id del mensaje en el proveedor… |
| created_at | timestamptz | NOT NULL, default now() | |
| leido_at | timestamptz | | para las no leídas |

Adjuntos: `media_type`, `media_url`, `media_mime`, `media_size`,
`media_filename`, `media_duration_seconds`, `media_purged_at`.

Índices que conviene conocer, porque **no se ven en el modelo** (se crean con
SQL en las migraciones y `--autogenerate` los da por borrados):

- `ix_messages_conv_created` — `(conversation_id, created_at)`
- `ix_messages_provider_message_id` — sobre `metadata ->> 'provider_message_id'`
- `ix_messages_unread` — parcial: `WHERE rol = 'user' AND leido_at IS NULL`

### `documents` y `chunks`

La base de conocimiento.

`documents`: `id`, `nombre` (varchar(255)), `formato` (enum: `pdf`, `docx`,
`txt`, `md`, `csv`, `xlsx`), `tamano_bytes`, `status` (enum: `procesando`,
`indexado`, `error`, `indexado_sin_semantica`), `error_msg`, `num_chunks`,
`storage_path`, `uploaded_at`, `uploaded_by` (FK users.id).

`chunks`: `id`, `document_id` (FK ON DELETE CASCADE), `contenido` (text),
`embedding` (`vector(1536)`, `text-embedding-3-small`), `metadata` (jsonb),
`created_at`.

Dos índices, uno por cada mitad de la búsqueda híbrida:

```sql
CREATE INDEX ix_chunks_embedding ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_chunks_fts_es  ON chunks USING gin (to_tsvector('spanish', contenido));
```

### `audit_log`

Quién hizo qué en el panel.

`id`, `user_id` (FK, nulo en acciones del sistema), `action` (varchar(80), ej.
`contact.data_erased`), `entity`, `entity_id`, `before` y `after` (jsonb), `ip`,
`user_agent`, `created_at`.

## Relaciones

- `contacts` 1—N `conversations` 1—N `messages`
- `contacts` N—N `tags` (vía `contact_tags`), 1—N `contact_note`, 1—N `contact_activity`
- `documents` 1—N `chunks`, 1—N `document_versions`
- `users` 1—N `audit_log`, `documents.uploaded_by`, `conversations.asignada_a`
- `channels` N—1 `agents`

## Búsqueda en la base de conocimiento

Dos funciones SQL, creadas por las migraciones:

- `match_chunks(query_embedding, match_threshold, match_count)` — solo vector.
- `match_chunks_hybrid(...)` — vector más texto completo en español, que es la
  que usa el agente.

Ninguna de las dos está en `backend/app/models/`. Se definen en las migraciones.

## Datos iniciales (`backend/app/scripts/seed.py`)

Los crea `backend/entrypoint.sh` al arrancar. Es idempotente: repetirlo no
duplica nada.

- El usuario administrador, con lo que haya en `INITIAL_ADMIN_*` del `.env`.
- Dos agentes de plantilla, "Agente de Texto" (`kind=text`) y "Agente de Voz"
  (`kind=voice`), con el prompt escrito y los datos del negocio marcados con
  `[[ RELLENAR: … ]]`.
- Etiquetas de ejemplo.

## Las otras 28 tablas

Sin detallar aquí. Cada una tiene su modelo en `backend/app/models/` y la
migración que la creó explica en la cabecera por qué existe.

Agentes: `agents`, `agent_config`, `agent_prompt_history`, `agent_tokens`,
`internal_agent_config`, `agent_trace_event`, `agent_corrections`.
Canales: `channels`, `external_apis`, `whatsapp_template_var`.
Clasificador: `classifier_config`, `classifier_rules`.
Difusiones: `outbound_job`, `outbound_job_recipient`, `outbound_optout`.
Base de conocimiento y aprendizaje: `document_versions`, `kb_edit_proposals`,
`knowledge_gaps`, `learned_rules`.
Coste: `llm_providers`, `llm_model_price`, `llm_usage_log`.
Copias: `backup_settings`, `backup_runs`.
Otras: `contact_note`, `contact_activity`, `app_settings`, `push_subscription`.

Aparte va `alembic_version`, que es la tabla de control de Alembic: guarda una
sola fila con el identificador de la última migración aplicada.
