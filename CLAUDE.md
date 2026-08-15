# CLAUDE.md — Chatbot

Manual de trabajo de este repositorio. Si acabas de abrir la carpeta, esto es lo
primero que hay que leer. La app **ya está construida**: aquí no se planifica
nada desde cero, se mantiene y se amplía.

---

## Qué es

Atención al cliente automatizada con un agente de IA, más el panel para que un
humano vea y conteste lo mismo que ve el bot.

- El cliente final escribe por **WhatsApp, Instagram DM, correo, chat web** o
  llama por **teléfono** (voz).
- Un agente de IA responde. Consulta la **base de conocimiento** del negocio
  (documentos subidos al panel, buscados por significado), da de alta o
  actualiza la **ficha del contacto** y, si hace falta, **deriva a un humano**.
- El operador trabaja en un **inbox** en tiempo real: lee la conversación,
  entra a contestar cuando quiere y el bot se aparta.
- Hay un **CRM** propio (contactos, notas, etiquetas, actividad) y **envíos
  masivos** por WhatsApp.

Es un producto **de un cliente por instalación**: se clona el repositorio, se
configura desde el panel y se despliega en el servidor de ese cliente. No hay
nada compartido entre clientes.

---

## Stack

| Capa | Qué |
|---|---|
| Backend | Python 3.12, FastAPI (async), SQLAlchemy 2, Pydantic v2 |
| Tareas en segundo plano | Celery 5 + Redis 7 (worker y beat) |
| Base de datos | PostgreSQL 16 con **pgvector** (búsqueda por significado) |
| Migraciones | Alembic |
| Panel | React 18 + TypeScript + Vite + Tailwind + Zustand + React Router |
| Empaquetado | Docker Compose (backend, worker, beat, db, redis, panel, MCP) |
| Pruebas | pytest (+ pytest-asyncio); en el panel, `tsc` y build de Vite |

Los proveedores externos (LLM, WhatsApp, transcripción, correo, voz) están
detrás de interfaces en `backend/app/providers/`. No se llama a ninguna API de
fuera directamente desde un endpoint.

---

## Mapa de carpetas

```
backend/                 API, agente, tareas y modelo de datos
  app/
    api/                 endpoints FastAPI (uno por área: contacts, conversations, admin…)
    agents/
      orchestrator.py    el agente que atiende a los clientes
      tools/             sus herramientas (kb_search, contact_lookup, contact_upsert,
                         human_handoff, calendar, schedule_config, registry)
      internal/          el agente interno del operador (ver "Conceptos")
    core/                config, cifrado, seguridad, Redis, bus de eventos, límites
    db/
      session.py         motor y sesión
      migrations/        Alembic (env.py, script.py.mako)
        versions/        las migraciones, NNNN_slug.py
    models/              tablas SQLAlchemy (una por fichero)
    providers/           whatsapp, llm, embeddings, transcription, voice, instagram,
                         gmail, email
    schemas/             esquemas Pydantic compartidos (auth, common)
    scripts/seed.py      datos iniciales: admin, agentes de plantilla, etiquetas
    services/            la lógica de negocio (clasificador, difusiones, fusión de
                         fichas, borrado RGPD, copias, presupuesto…)
    tasks/               tareas Celery + calendario de `beat`
    main.py              monta la app y registra los routers
  tests/                 LAS PRUEBAS ESTÁN AQUÍ (123 ficheros), no en la raíz
  alembic.ini
  requirements.txt / requirements-dev.txt
  entrypoint.sh          espera a Postgres, migra y siembra al arrancar el contenedor

frontend/src/
  pages/admin/           configuración: conexiones, agentes, clasificador, usuarios,
                         difusiones, copias, precios, auditoría, salud, logs
  pages/client/          día a día: Inbox, Contacts, ContactDetail, Calls, KnowledgeBase
  components/  hooks/  services/  store/  types/  lib/
  router.tsx             todas las rutas del panel

mcp-server/              servidor MCP que expone la agent-api a asistentes externos
design/                  documentación de diseño (resumen, modelo de datos, API, UI)
deployment/              apunta a la guía de despliegue; no contiene ficheros propios
scripts/smoke.sh         comprobación rápida de un despliegue
.github/workflows/ci.yml revisión automática
.claude/commands/        comandos de Claude Code de este proyecto
```

En la raíz: `docker-compose.yml` (local), `docker-compose.easypanel.yml`
(producción), `.env.example` (las 12 del despliegue, lo que importa EasyPanel),
`.env.desarrollo.example` (local),
`README.md`, `SECURITY.md`, `DEPLOY_EASYPANEL.md`.

---

## Levantar el entorno

Todo con Docker. No hace falta instalar Python ni Node para que la app funcione.

```bash
cp .env.desarrollo.example .env

# Las tres claves obligatorias, en el .env:
openssl rand -hex 24                  # POSTGRES_PASSWORD
openssl rand -base64 48               # JWT_SECRET
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # ENCRYPTION_KEY

docker compose up -d
curl http://localhost:8000/health     # espera ~30 s a que arranque
open http://localhost:5173            # panel
```

Entras con `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` del `.env`.

Detalles que ahorran tiempo:

- **Postgres y Redis no publican puerto al ordenador.** Solo se ven desde
  dentro de la red de Compose. Si quieres una base de datos accesible desde
  fuera (para pruebas), levanta una aparte: ver "Pruebas".
- **Las migraciones y los datos iniciales no se lanzan a mano**: los ejecuta
  `backend/entrypoint.sh` cada vez que arranca el contenedor de la API.
- **El campo `worker` de `/health` no cambia el código de respuesta.** Aunque
  ponga `caido`, la respuesta es 200. Está hecho a propósito.
- El panel en desarrollo se sirve ya compilado en el puerto 5173. Para trabajar
  en el frontend con recarga en caliente: `cd frontend && npm install && npm run dev`.

---

## Pruebas

Las pruebas del backend viven en **`backend/tests/`**. Se lanzan **desde
`backend/`**:

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Sin una base de datos a mano se saltan las que la necesitan (unas 290) y fallan
cuatro que van a buscar una credencial a la base de datos. Para pasarlas todas,
levanta un Postgres con pgvector aparte y apunta `DATABASE_URL` a él:

```bash
docker run -d --name chatbot-test-db \
  -e POSTGRES_USER=chatbot -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
  -p 5466:5432 pgvector/pgvector:pg16

cd backend
export DATABASE_URL="postgresql+asyncpg://chatbot:test@127.0.0.1:5466/test"
export ENCRYPTION_KEY="<la misma clave Fernet del .env>"
.venv/bin/alembic upgrade head
.venv/bin/pytest -q
```

> Usa un puerto que esté libre. Si ya tienes otro Postgres en el ordenador,
> Docker no siempre avisa del choque y acabas hablando con el equivocado: el
> síntoma es `FATAL: database "test" does not exist`.

La imagen de producción **no lleva pytest dentro**. No intentes lanzarlos con
`docker compose exec app pytest`.

Del panel:

```bash
cd frontend
npm ci
npx tsc --noEmit -p tsconfig.json    # tipos
npm run build                        # build
```

`.github/workflows/ci.yml` ejecuta en cada subida: las pruebas del backend
contra un Postgres real, las migraciones en los dos sentidos, los tipos y el
build del panel, y la construcción de las tres imágenes Docker. El linter
(`ruff`) sale en el informe pero **no bloquea**.

---

## Migraciones

Cada revisión es un fichero en `backend/app/db/migrations/versions/`. La
convención es **`NNNN_slug.py`**, con el número correlativo y un resumen corto
en español: `0053_contact_bsuid.py`, `0054_channel_secrets_encrypted.py`. El
identificador de la revisión es exactamente el nombre del fichero sin `.py`.
(Las cuatro primeras arrastran un nombre antiguo con fecha delante; no las
renombres, ya están aplicadas en instalaciones reales.)

Para saber por dónde va la cabeza actual, sin necesidad de base de datos:

```bash
cd backend
alembic heads          # → 0054_channel_secrets_encrypted (head)
```

Crear una migración nueva:

```bash
cd backend
alembic revision -m "resumen corto" --rev-id 0055_mi_cambio
```

`--rev-id` no es opcional aquí: es lo que fija el nombre del fichero y el
identificador. Alembic rellena solo el `down_revision` con la cabeza anterior
(la que te acaba de dar `alembic heads`); compruébalo antes de escribir nada.
Si sale vacío o apunta a otra cosa, ponlo a mano.

Aplicarla:

```bash
cd backend
export ENCRYPTION_KEY=...   # obligatorio: hay migraciones que cifran datos
alembic upgrade head
alembic downgrade -1        # la marcha atrás también tiene que funcionar
```

`ENCRYPTION_KEY` hace falta aunque tu migración no cifre nada, porque en el
camino se ejecutan las que sí lo hacen. Sin ella el comando muere con
`Fernet key must be 32 url-safe base64-encoded bytes`.

**`--autogenerate` no sirve en este repositorio.** Comprobado sobre una base de
datos exactamente al día: propone borrar cuatro tablas (`classifier_config`,
`agent_prompt_history`, `llm_model_price`, `llm_usage_log`) y catorce índices
que están perfectamente. Dos motivos:

1. Hay modelos que no se importan en `backend/app/models/__init__.py`, así que
   Alembic no los ve y cree que la tabla sobra.
2. Muchos índices se crean con SQL a mano (parciales, GIN de texto completo,
   IVFFlat de pgvector, índices sobre expresiones) y Alembic no los reconoce.

Si aun así lo usas para orientarte, **borra todo lo que no sea tu cambio** antes
de guardar. Lo normal aquí es escribir el `upgrade()` y el `downgrade()` a mano.

Reglas:

- Una migración aplicada **no se edita nunca**. Se corrige con otra encima.
- Toda migración lleva `downgrade()` de verdad. La CI lo comprueba.
- La cabecera del fichero explica **por qué** se hace, no solo qué. Mira
  `0053_contact_bsuid.py` como referencia de lo que se espera.

---

## Cómo añadir un campo a la ficha de contacto

El ejemplo de verdad, porque el contacto es lo que más manos toca. La tabla
`contacts` tiene hoy 20 columnas. Lista de sitios, en orden:

| # | Fichero | Qué |
|---|---|---|
| 1 | `backend/app/db/migrations/versions/NNNN_slug.py` | la columna nueva, con `downgrade()` |
| 2 | `backend/app/models/contact.py` | la columna en el modelo `Contact` |
| 3 | `backend/app/api/contacts.py` | los esquemas `ContactOut`, `ContactCreate` y `ContactUpdate` están **dentro de este fichero**, no en `backend/app/schemas/` |
| 4 | `backend/app/api/contacts.py` → `create_contact` | alta |
| 5 | `backend/app/api/contacts.py` → `update_contact` | edición |
| 6 | `backend/app/api/contacts.py` → `_CSV_COLUMNS` | la cabecera del CSV: **la misma lista sirve para importar y para exportar** |
| 7 | `backend/app/api/contacts.py` → `export_contacts` | escribir la celda |
| 8 | `backend/app/api/contacts.py` → `import_contacts` | leer la celda y aplicarla al crear y al actualizar |
| 9 | `backend/app/services/contact_merge.py` → `CAMPOS_RELLENABLES` | si al fusionar dos fichas el campo debe heredarse del duplicado |
| 10 | `backend/app/api/contacts.py` → `export_contact_data` | la exportación RGPD de una ficha (JSON) |
| 11 | `frontend/src/types/index.ts` → `interface Contact` | el tipo |
| 12 | `frontend/src/pages/client/ContactDetail.tsx` | el formulario de la ficha |
| 13 | `frontend/src/pages/client/Contacts.tsx` | solo si el campo sale en la tabla o en los filtros |

Y según el caso:

- Si el agente tiene que poder rellenarlo solo:
  `backend/app/agents/tools/contact_upsert.py` y `contact_lookup.py`.
- Si el campo se busca desde la lista: el filtro está en `list_contacts`, en
  `backend/app/api/contacts.py`.
- El borrado RGPD (`backend/app/services/data_erasure.py`) borra la fila entera,
  así que normalmente no hay que tocarlo.

**Aviso sobre los campos cifrados.** Tres columnas de `contacts` usan el tipo
`EncryptedText` (`backend/app/core/encrypted_type.py`): `nif`, `direccion` y
`notas_internas`. En Postgres son `bytea`: el contenido está cifrado con Fernet
y **la base de datos no puede mirarlo**. No hay hash de búsqueda. Consecuencias:

- Nada de `WHERE`, `LIKE`, `ORDER BY`, `GROUP BY`, `UNIQUE` ni índices sobre
  esas columnas. No dan error de sintaxis: simplemente no encuentran nada.
- Filtrar por ellas obliga a traerse las filas y comparar en Python. En una
  lista de contactos eso no escala; piénsalo dos veces antes de cifrar un campo
  por el que la gente va a querer buscar.
- Si se pierde o se cambia `ENCRYPTION_KEY`, esas columnas quedan ilegibles
  para siempre. El tipo devuelve `None` en vez de romper la fila, así que el
  dato desaparece en silencio.
- `telefono`, `email` y `nombre` están **en claro** a propósito, porque la app
  los necesita para buscar y para la clave única. Es una decisión consciente,
  explicada en la cabecera de `encrypted_type.py`.

---

## Conceptos que aparecen por todas partes

**El clasificador.** Filtro de entrada. Antes de que el mensaje llegue al
agente, decide si es basura (spam, boletines, autorrespuestas) y lo aparta.
Trabaja en dos pasos: primero reglas duras que no cuestan nada (remitente,
dominio, texto en el asunto) y solo si ninguna dispara, una llamada al LLM.
Ante cualquier error deja pasar el mensaje: perder un cliente real es peor que
tragarse un spam. `backend/app/services/classifier.py`, `classifier_rules.py`,
`classifier_config.py`; panel en `frontend/src/pages/admin/ClassifierPage.tsx`
y `frontend/src/pages/admin/classifier/`.

**BSUID.** Identificador que Meta asigna a cada persona dentro de un negocio de
WhatsApp (*Business-Scoped User ID*). Desde 2026 Meta puede no mandar el
teléfono, pero el BSUID lo manda siempre y no cambia aunque la persona cambie de
número. Se guarda en `contacts.wa_user_id` (único, índice parcial). Al llegar un
mensaje se busca primero por BSUID y después por teléfono; si aparecen dos
fichas de la misma persona, se fusionan. `backend/app/models/contact.py`,
migración `0053_contact_bsuid.py`, prueba `backend/tests/test_contacto_bsuid.py`.

**Difusiones.** El envío masivo por WhatsApp: una plantilla aprobada por Meta se
manda a una lista de destinatarios. Va en segundo plano, de uno en uno y con
pausas aleatorias entre envíos, para no despertar a los antispam de Meta. Antes
de cada envío comprueba las bajas (`outbound_optout`, permanentes). Se sigue en
directo desde el panel y se puede cancelar. `backend/app/models/outbound_job.py`,
`backend/app/tasks/outbound_send.py`,
`frontend/src/pages/admin/OutboundPage.tsx`.

**El agente interno.** Otro agente, pero para el operador, no para los clientes.
Se le pregunta desde el panel ("qué pasó con este cliente", "propón una mejora
para la base de conocimiento") y responde con sus propias herramientas. Tiene
su presupuesto y su límite diario aparte, y un blindaje fijo en el código para
que no obedezca órdenes escondidas en los mensajes de los clientes que lee.
`backend/app/agents/internal/`, `backend/app/models/internal_agent_config.py`.

**Tipos de agente.** Los agentes que atienden a clientes son de dos clases:
`text` (WhatsApp, Instagram, correo, chat web) y `voice` (llamadas). No son
intercambiables: el canal y el agente tienen que coincidir de tipo. La
instalación siembra uno de cada, "Agente de Texto" y "Agente de Voz", con el
prompt a medio rellenar.

**Los prompts no están en el código.** Viven en la base de datos y se editan
desde el panel (Agentes). Buscarlos en un fichero es perder el tiempo. La única
parte escrita en código es el blindaje de seguridad del agente interno.

---

## Convenciones de la casa

**Idioma.** El código habla español.

- **Columnas y campos, en español**: `telefono`, `nombre`, `contenido`, `rol`,
  `estado`, `origen`, `servicio_interes`, `notas_internas`, `leido_at`. Los
  valores de los enum también: `contacto`, `cliente`, `no_cualifica`, `bot`,
  `humano`, `cerrada`, `procesando`, `indexado`.
- Los **nombres de tabla** están en inglés por herencia (`contacts`, `messages`,
  `conversations`). No los cambies; sigue el patrón que ya hay.
- **Comentarios y docstrings en español**, y explicando el *porqué*, no el qué.
  El código ya dice qué hace. Mira las cabeceras de las migraciones o de
  `backend/app/services/budget.py`.
- Los **textos del panel**, en español. Los nombres de componente React, en
  inglés y PascalCase (`ContactDetail.tsx`), como el resto del ecosistema.

**Pruebas.** Se llaman por lo que prueban, en español y en frase:
`test_derivacion_avisa_al_cliente.py`,
`test_contactos_csv_fusion_telefono.py`,
`test_sin_clave_lanza_en_vez_de_devolver_texto`. Nada de
`test_service_1`. El nombre tiene que decir qué se rompe si falla.

**Commits.** En español, describiendo el efecto para quien usa la app, no el
cambio técnico: "El panel deja de callarse cuando el envío falla".

**Estilo.** `ruff` con línea de 100 (`backend/pyproject.toml`). Sale en el
informe de la CI pero no bloquea; el repositorio arrastra avisos antiguos. No
los arregles en masa dentro de un cambio funcional: se mezcla todo y no hay
quien revise el diff.

**Nada de llamar a una API de fuera desde un endpoint.** Va detrás de su
interfaz en `backend/app/providers/`.

**Ninguna clave en el código.** Las credenciales de proveedores se guardan
cifradas y se configuran desde el panel (Conexiones). En el `.env` solo van las
tres de infraestructura: `POSTGRES_PASSWORD`, `JWT_SECRET`, `ENCRYPTION_KEY`.

---

## Si tocas X, acuérdate de Y

| Si tocas | Acuérdate de |
|---|---|
| una columna o un modelo | migración + esquema Pydantic + `frontend/src/types/index.ts` |
| un modelo **nuevo** | importarlo en `backend/app/models/__init__.py`, o Alembic no lo verá |
| un endpoint nuevo | registrar el router en `backend/app/main.py` |
| una pantalla nueva | añadir la ruta en `frontend/src/router.tsx` y el enlace en el menú |
| una tarea Celery nueva | importarla en `backend/app/tasks/__init__.py`; si es periódica, añadirla al `beat_schedule` de ese mismo fichero |
| una herramienta del agente | registrarla en `backend/app/agents/tools/registry.py` y activarla en `tools_enabled` del agente |
| una variable de entorno | `backend/app/core/config.py`, `.env.desarrollo.example`, `docker-compose.yml`, `docker-compose.easypanel.yml` y el anexo de `DEPLOY_EASYPANEL.md` (y `.env.example` solo si es obligatoria en el despliegue) |
| el modelo de datos | `design/data_model.md` y `design/design_summary.md` |
| un endpoint o su forma | `design/api_contracts.md` |
| algo del despliegue | `DEPLOY_EASYPANEL.md` |
| algo con datos personales | `SECURITY.md` |
| un campo del contacto | la tabla de la sección anterior, entera |

---

## Lo que no se toca a la ligera

- **`ENCRYPTION_KEY`.** Cambiarla deja ilegibles las notas internas, el NIF, la
  dirección, las transcripciones y las credenciales guardadas. No hay vuelta
  atrás y no avisa: los campos empiezan a salir vacíos.
- **Las migraciones ya aplicadas.** Nunca se editan.
- **`backend/app/core/config.py`, `validate_production()`.** Es lo que impide
  arrancar en producción con el administrador de ejemplo o con un `JWT_SECRET`
  de fábrica. Si te estorba, es que estás arrancando mal.
- **`backend/entrypoint.sh`.** Espera a Postgres, migra y siembra. Si falla el
  seed no hay usuario administrador y el panel se queda con un login que no
  acepta a nadie.
- **El comportamiento del clasificador ante un error.** Deja pasar el mensaje a
  propósito. Cerrarlo significa perder clientes reales en silencio.
- **El comportamiento del `/health`.** Devuelve 200 aunque el worker esté
  caído, para que el contenedor no entre en bucle de reinicios.
- **`docker-compose.easypanel.yml` y `.env.example`.** Son los de
  producción. El `docker-compose.yml` y el `.env.desarrollo.example` son de desarrollo y
  traen `APP_ENV=development`, que apaga las comprobaciones de arranque.

---

## Desplegar

La guía es **`DEPLOY_EASYPANEL.md`** y se sigue entera. No se resume aquí ni se
copia en ningún otro sitio. Antes de tocar producción, lee también
**`SECURITY.md`**.

## Bitácoras

> **Las bitácoras no vienen en el paquete.** `docs/project_memory.md`,
> `implementation/task_tracker.md` y `docs/work_log.md` son el cuaderno de
> quien trabaja sobre este proyecto, no del que lo construyó. Si un comando o
> una tabla de aquí abajo menciona uno de esos ficheros y no lo encuentras, no
> pasa nada: créalo con lo que puedas leer del repositorio y sigue.
