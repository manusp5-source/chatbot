# Chatbot

App de atención al cliente con agente IA por WhatsApp, Instagram DM, correo, chat web y voz. Trae base de conocimiento propia (RAG), CRM nativo, inbox tipo WhatsApp Web con derivación a humano y envíos masivos. Producto reutilizable de un cliente por instalación: se clona, se configura por cliente y se despliega.

> **Antes de desplegar a producción:** lee [`SECURITY.md`](./SECURITY.md). Resume las defensas activas, el modelo de confianza y el checklist obligatorio.

## Para qué sirve

Atención al cliente completa: consultas, información de servicios, audios, derivación a una persona del equipo y agenda de citas por Google Calendar. Sirve para cualquier negocio que atienda por mensajes; el prompt del agente y la base de conocimiento son los que lo hacen tuyo.

## Stack

- **Backend:** Python 3.12 + FastAPI + Celery + Redis
- **Base de datos:** PostgreSQL 16 + pgvector (memoria conversacional, CRM, RAG)
- **LLM:** OpenAI, Anthropic o Gemini, detrás de la interfaz `LLMProvider`. Se elige en el panel
- **Embeddings:** OpenAI `text-embedding-3-small`
- **Transcripción de audio:** OpenAI Whisper
- **WhatsApp:** YCloud o Meta directamente, detrás de `WhatsAppProvider`
- **Otros canales:** Instagram DM (Meta), voz (Retell), correo (Gmail o SMTP), chat web
- **Correo saliente:** Resend o SMTP
- **Avisos al operador:** web push a la PWA y el registro en vivo del panel. No hay integración con Telegram ni con Slack
- **Frontend:** React + TypeScript (panel de administración y panel de trabajo, con inbox en tiempo real por WebSocket)
- **Despliegue:** Docker → EasyPanel

## Estructura

| Ruta | Contenido |
|---|---|
| `CLAUDE.md` | Manual de trabajo: stack, entorno, pruebas, migraciones y convenciones. **Empieza por aquí.** |
| `SECURITY.md` | Defensas activas, modelo de confianza y checklist previo a producción |
| `DEPLOY_EASYPANEL.md` | Guía de instalación en el servidor del cliente |
| `backend/` | Código Python: API, agente, tareas y modelo de datos |
| `backend/tests/` | Las pruebas automáticas (pytest) |
| `backend/app/db/migrations/versions/` | Las migraciones de base de datos |
| `frontend/` | Código React del panel |
| `mcp-server/` | Servidor MCP que expone la agent-api a asistentes externos |
| `design/` | Documentación de diseño: resumen, modelo de datos, API, pantallas |
| `deployment/` | Remite a la guía de despliegue; no contiene ficheros propios |
| `scripts/smoke.sh` | Comprobación rápida de un despliegue |
| `.claude/commands/` | Comandos de Claude Code de este proyecto |
| `docker-compose.yml` · `.env.desarrollo.example` | Desarrollo local |
| `.env.example` | Las 12 variables del despliegue (es lo que importa EasyPanel) |
| `docker-compose.easypanel.yml` | Producción |

## Quick start (desarrollo local)

```bash
# 1. Generar secrets locales (o copia .env.desarrollo.example y edítalo)
cp .env.desarrollo.example .env

# 2. Generar las claves obligatorias (en el .env)
#    Son los MISMOS comandos que usa DEPLOY_EASYPANEL.md § 0: no hay dos formas
#    de generar la clave de cifrado, solo esta.
openssl rand -hex 24                  # → POSTGRES_PASSWORD (hex, no base64)
openssl rand -base64 48               # → JWT_SECRET
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # → ENCRYPTION_KEY

# 3. Levantar todo (postgres + redis + app + worker + beat + panel + servidor MCP)
docker compose up -d

# 4. Comprobar (espera ~30s a que arranque)
curl http://localhost:8000/health
# → {"status":"ok","db":"ok","redis":"ok","worker":"ok","worker_last_seen_seconds":12}
open http://localhost:5173            # → panel de login
```

> **Sobre el campo `worker` de `/health`:** dice si el worker de tareas sigue
> vivo, pero **no** cambia el código de respuesta: aunque ponga `caido`, la
> respuesta sigue siendo 200. Es a propósito — ese endpoint es el latido del
> contenedor de la API, y reiniciar la API no resucita al worker. Valores:
> `ok`, `caido` (lleva más de 15 min sin dar señales), `sin_latido` (aún no ha
> dado ninguna: normal el primer par de minutos tras desplegar) y
> `desconocido` (Redis caído, así que no hay forma de saberlo).

Credenciales iniciales: las que hayas puesto en `INITIAL_ADMIN_EMAIL` e
`INITIAL_ADMIN_PASSWORD` de tu `.env`.

En `/admin/connections` configura las claves API reales (YCloud, OpenAI…) y copia al panel de cada proveedor las URLs de webhook que verás en la tarjeta de su canal. Ese es el primer paso después del login.

El segundo: en `/admin/agent/agents`, **rellenar el prompt de los dos agentes que siembra la instalación** (Agente de Texto y Agente de Voz). Vienen con una plantilla —las reglas de comportamiento están escritas y funcionan— pero los datos del negocio van marcados con `[[ RELLENAR: … ]]` y hay que completarlos antes de abrir ningún canal. El checklist de Inicio lo recuerda hasta que no quede ni un marcador en ningún agente activo.

## Verificación end-to-end (Fase 1)

| # | Check | Cómo |
|---|---|---|
| 1 | Despliegue limpio | `docker compose up` arranca todos los servicios sin errores |
| 2 | Migraciones | Tablas creadas + función `match_chunks` + seed admin/prompt/tags |
| 3 | Login | Email + password → JWT → panel |
| 4 | Inbox tiempo real | Abrir dos pestañas; cambios se reflejan vía WebSocket |
| 5 | KB | Subir PDF → indexar → buscar → ver chunks |
| 6 | Webhook YCloud | POST a `/api/v1/webhooks/ycloud` con payload de prueba |
| 7 | Agente | Mensaje real → bot responde con tool use (RAG + CRM) |
| 8 | Derivación humana | Bot llama `derivar_humano` → status `humano` + aviso por web push y en el registro del panel |
| 9 | Audio | Mensaje voz WhatsApp → transcrito y procesado |
| 10 | Operador responde | Caja respuesta en inbox → llega a WhatsApp del usuario |
| 11 | Ventana 24h | Mensaje fuera de ventana → UI bloquea con aviso |
| 12 | Tests | Los ejecuta sola la revisión automática en cada cambio (ver abajo). La imagen de producción NO trae pytest dentro: para lanzarlos a mano, `pip install -r backend/requirements-dev.txt` en un entorno virtual y `pytest -q` desde `backend/` |

## Revisión automática (CI)

Cada cambio que se sube a GitHub pasa por `.github/workflows/ci.yml`: las más de
1.100 pruebas del backend contra una base de datos real, las migraciones en los
dos sentidos (subir y deshacer), el build y los tipos del panel, y la
construcción de las tres imágenes Docker (backend, panel y servidor MCP). El
linter (`ruff`) se ejecuta en modo informativo:
sale en el informe pero no bloquea, porque el repositorio arrastra más de 600
avisos y ponerlo a bloquear dejaría la revisión en rojo permanente.

> **Que la revisión esté en verde no impide desplegar algo roto por sí solo.**
> Hacen falta dos ajustes manuales —protección de la rama en GitHub y apagar el
> auto-despliegue en EasyPanel— explicados paso a paso en
> [`DEPLOY_EASYPANEL.md` § 7.b](./DEPLOY_EASYPANEL.md).

## Trabajar en el código con Claude Code

Abre esta carpeta con Claude Code. Lee `CLAUDE.md` solo: ahí está el mapa de
carpetas, cómo levantar el entorno, cómo pasar las pruebas, cómo crear una
migración y las convenciones del proyecto.

Hay tres comandos propios en `.claude/commands/`:

| Comando | Cuándo |
|---|---|
| `/session-start` | Al empezar a trabajar: reconstruye el contexto y dice por dónde ibas |
| `/iterate` | Para hacer un cambio: arreglo, mejora o función nueva, de punta a punta |
| `/review` | Al terminar un cambio: revisión independiente antes de darlo por bueno |

Los tres escriben el avance en `docs/bitacora.md`, que crean ellos mismos la
primera vez.

## Distribución del producto

Cada cliente recibe su propia instancia en su propio servidor. No hay
infraestructura central compartida.

> **La guía de instalación es [`DEPLOY_EASYPANEL.md`](DEPLOY_EASYPANEL.md), y se
> sigue entera.** Estos cuatro puntos son el mapa, no el manual.

1. Clonar el repositorio en el VPS del cliente.
2. `cp .env.example .env` y rellenarlo: son las 12 variables del despliegue.
   Si quieres ver TODAS las opcionales con su valor por defecto, mira el anexo
   de `DEPLOY_EASYPANEL.md`. **No** uses `.env.desarrollo.example`: ese es
   para local y trae `APP_ENV=development`, que apaga todas las
   comprobaciones de arranque (ver más abajo).
3. Desplegar con **`docker-compose.easypanel.yml`**, no con `docker-compose.yml`.
   El de EasyPanel fija `APP_ENV=production` y exige las variables obligatorias
   con `${VAR:?}`: si falta alguna, el despliegue para y dice cuál.
4. Cargar la documentación del cliente en la Base de Conocimiento desde el panel.

### Por qué importa no equivocarse de fichero

Con `APP_ENV=development`, que es lo que trae `.env.desarrollo.example`:

- **La sesión se cae sola.** Si `JWT_SECRET` sigue siendo el de fábrica, cada
  proceso genera uno aleatorio: en cuanto el contenedor se reinicia, a todo el
  mundo lo echa del panel sin explicación.
- **No hay red de seguridad contra los secretos de juguete.** En producción,
  `validate_production()` (`backend/app/core/config.py`) aborta el arranque si
  quedan el `JWT_SECRET` por defecto, el admin de ejemplo o URLs a localhost.
  Fuera de producción no comprueba nada, así que una instalación abierta a
  internet puede quedarse con el usuario y la contraseña de ejemplo, que están
  escritos en este mismo repositorio.
- **El panel no habla con la API.** El `CORS_ALLOWED_ORIGINS` de ejemplo apunta
  a localhost, y el compose de desarrollo publica los puertos sin SSL.


