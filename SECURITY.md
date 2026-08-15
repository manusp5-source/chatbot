# Seguridad — Chatbot

Este documento describe el modelo de seguridad de la app, qué ataques mitiga
y qué responsabilidades quedan del lado del operador (tú). Vivo, actualizar
con cada cambio.

> **Si vas a desplegar HOY, ve a [§ 6 Checklist pre-producción](#6-checklist-pre-producción).**
> El paso a paso de la instalación está en [`DEPLOY_EASYPANEL.md`](./DEPLOY_EASYPANEL.md);
> aquí solo está lo que afecta a la seguridad.

---

## 1. Modelo de confianza

| Actor | Confianza | Por qué |
|---|---|---|
| **Admin** (rol `admin`) | Alta | Configura credenciales, modelo, prompts. Toda acción queda auditada. |
| **Operador** (rol `cliente`) | Media | Lee/responde conversaciones. Puede borrar contactos/etiquetas. No accede a `/admin/*`. **Ve toda la base de contactos**, ver aviso debajo. |
| **Usuario final de cualquier canal** | **Nula** | Cualquier persona del mundo con tu número, tu Instagram, tu web, tu teléfono o tu correo. Toda entrada se considera adversaria. |
| **Proveedores de canal** (YCloud, Meta, Retell, Google) | Alta | Cada webhook entrante se verifica antes de procesarlo. Detalle en [§ 2.3](#23-canales-de-entrada-quién-entra-y-qué-lo-protege). |
| **OpenAI / Anthropic / Gemini / Resend / Telegram / Slack** | Alta | Conexiones HTTPS salientes, sin entrada de datos. |
| **Cliente MCP con token de agente** | Media | Es un servicio expuesto a internet. Con ámbito de escritura puede reescribir el prompt del bot. Ver [§ 2.4](#24-servidor-mcp-y-tokens-de-agente). |
| **EasyPanel host** | Alta | Tiene acceso al volumen Postgres y a las env vars. |

La app es **single-tenant**: una instancia por cliente, sin aislamiento entre operadores dentro de la misma instancia.

### El rol `cliente` ve toda la base de contactos (decisión consciente)

Solo hay dos roles. `admin` guarda lo irreversible (exportar, borrar, importar,
credenciales, copias de seguridad). `cliente` es "operador de bandeja", y eso
incluye poder leer **todos** los contactos, todas las conversaciones, las notas
internas, los NIF, las direcciones y las transcripciones de las llamadas de
cualquier ficha. No puede exportarlo de una tacada, pero sí ir pasando páginas.

No es un fallo: es el modelo de permisos tal y como está escrito, y encaja con
el producto —una instalación por negocio, con un equipo pequeño que atiende a
sus propios clientes—. Lo que implica en la práctica:

- **Da de alta como `cliente` solo a quien ya podría ver esa información**
  trabajando en el negocio. No es un rol para colaboradores externos, becarios
  de un solo canal ni proveedores.
- Si necesitas que alguien vea únicamente su parte, hoy **no se puede**: haría
  falta un tercer rol limitado a su propia bandeja.
- Si el negocio trata datos de salud, financieros o de menores, tenlo en cuenta
  al repartir los accesos: quien entra al panel entra a todo el fichero.

Toda acción sigue quedando en el registro de auditoría con usuario, IP y
momento (`/admin/audit`), así que el acceso no es anónimo.

---

## 2. Capas de defensa

### 2.1 Autenticación / autorización del panel
- JWT HS256 firmado con `JWT_SECRET` (mínimo 32 caracteres, validado al arranque en producción).
- Cada JWT incluye `jti` único. Logout añade el token a una blocklist en Redis hasta su `exp`.
- bcrypt para passwords (límite 72 bytes; pwds más largas se rechazan, no se truncan).
- Rate-limit en `/auth/login`: 5/min · 30/h por IP del cliente real (`X-Forwarded-For`).
- `get_current_user` solo acepta token en cabecera `Authorization: Bearer ...`. El WebSocket recibe el token por query-string vía un helper separado.
- Admin no puede auto-desactivarse ni auto-degradarse el rol.

### 2.2 Credenciales de proveedor
- Almacenadas cifradas con Fernet (`ENCRYPTION_KEY`) en la tabla `credentials`.
- Lectura desde la BD vía `services.credentials.get_credential` con caché en memoria de 30s e invalidación al actualizar.
- Las claves de OpenAI/YCloud/Meta/Resend/Telegram/Slack **nunca** se leen de env vars en producción — solo desde la tabla cifrada.
- El admin las gestiona desde `/admin/connections` (pestaña **Servicios**).
- Las claves **por canal** de Instagram (`app_secret`, `page_access_token`,
  `verify_token`) y de Retell (`api_key`, `webhook_secret`) van igual de
  cifradas, pero en otra columna: `channels.credentials_encrypted`, desde la
  migración 0054. Antes vivían en `channels.config`, que es JSONB en plano: con
  el `app_secret` de Meta a la vista se pueden firmar webhooks falsos y saltarse
  la comprobación de firma. La lista exacta de claves que se cifran está en
  `services/channel_secrets.py:SECRET_KEYS`. Se leen con
  `services.channel_secrets.channel_config` y se escriben con
  `apply_channel_values`; nada más debe tocar esas claves.
- Excepción a propósito: la `api_key` del chat web **no** se cifra porque es
  pública por diseño — viaja en el HTML de la web del cliente. Lo que la protege
  es la lista de dominios permitidos, no el secreto. Cómo se crea ese canal y
  dónde se pone esa lista: [§ 2.3.d](#23d-chat-web-cómo-se-crea-y-dónde-va-la-lista-de-dominios).

### 2.3 Canales de entrada: quién entra y qué lo protege

La app tiene **seis** vías por las que entra tráfico de gente desconocida. Cada
una tiene su dirección, su secreto y su forma de comprobar que quien llama es
quien dice ser. Si un secreto no está puesto, la puerta se cierra (no se abre).

Las direcciones que hay que pegar en el panel de cada proveedor las tienes
también en la app: **Conexiones → Canales**, tarjeta del canal, botón de copiar.

| Canal | Dirección de entrada | Secreto que lo protege | Cómo se comprueba |
|---|---|---|---|
| **WhatsApp (YCloud)** | `POST /api/v1/webhooks/ycloud` | `ycloud_webhook_secret` (tabla `credentials`, cifrado) | HMAC-SHA256 sobre `timestamp.cuerpo`, cabecera `YCloud-Signature` (`t=…,s=…`), comparación en tiempo constante. Sin secreto → **401**. |
| **WhatsApp (API oficial de Meta)** | `GET` + `POST /api/v1/webhooks/whatsapp/meta` | `meta_wa_verify_token` (alta del webhook) y `meta_wa_app_secret` (firma), ambos en `credentials`, cifrados | GET: comparación en tiempo constante del token, si no cuadra **403**. POST: HMAC-SHA256 del cuerpo crudo, cabecera `X-Hub-Signature-256`. Sin `app_secret` → **401**. |
| **Instagram (DM)** | `GET` + `POST /api/v1/webhooks/instagram` | `verify_token` y `app_secret` del canal (`channels.credentials_encrypted`, cifrados) | Igual que Meta: token en el alta, HMAC-SHA256 del cuerpo crudo en cada aviso. Sin credenciales → **403** / **401**. |
| **Chat web (widget)** | `POST /api/v1/webchat/sessions`, `POST /messages`, `GET /history`, `WS /api/v1/webchat/ws/{id}` | `api_key` del canal — **pública a propósito**, va en el HTML — + token de sesión firmado con `JWT_SECRET` | No hay secreto que esconder: lo que decide quién puede usarlo es la **lista de dominios permitidos** (§ 2.3.d). La sesión se firma con HMAC-SHA256 y caduca. |
| **Voz (Retell)** | `WS /api/v1/voice/retell/llm-ws/{call_id}` y `POST /api/v1/voice/retell/webhook` | `webhook_secret` y `api_key` del canal (`channels.credentials_encrypted`, cifrados) | HMAC-SHA256 del cuerpo, cabecera `x-retell-signature`. El webhook además rechaza avisos con más de 5 minutos (anti-repetición). El WebSocket comprueba el `call_id` contra la API de Retell antes de atender. Sin credenciales → cierre `1008` / **401**. |
| **Correo (Gmail)** | **No hay dirección de entrada.** La app va a buscar el correo cada 2 minutos (tarea `poll_gmail`) | Token OAuth de Google, cifrado | No hay firma que comprobar porque nadie llama desde fuera: es la app la que llama a Google autenticada. Sin autorización → no lee nada y lo deja escrito en los registros. |

Además, en los tres webhooks de `/api/v1/webhooks/*`:

- Rate-limit **120 peticiones/minuto por IP** (`@limiter.limit("120/minute")` en
  `api/webhooks.py`), con contador compartido en Redis.
- Rechazo si `Content-Length > 1 MB`.
- Idempotencia por `provider_message_id` con guard atómico en Redis: si el
  proveedor reintenta, el mensaje no se procesa dos veces.

El chat web tiene sus propios límites, más bajos, porque su clave es pública:
10 sesiones/min, 30 mensajes/min, 60 consultas de historial/min y un tope de
2000 mensajes al día por instalación.

#### 2.3.d Chat web: cómo se crea y dónde va la lista de dominios

Este canal no existe hasta que lo creas. Se hace desde el panel:

1. **Conexiones → pestaña Servicios → tarjeta "Chat en tu web" → "Crear el chat web".**
   Eso genera la `api_key` y el trozo de código que pegas en tu web.
2. En esa misma tarjeta, **"Código y dominios"**, tienes el campo **Dominios
   permitidos**: uno por línea, acepta `ejemplo.com` y `*.ejemplo.com`.
   Guarda con "Guardar dominios".

**Si dejas la lista vacía, el chat funciona en cualquier web que pegue el
código.** No es un fallo, es el valor por defecto: la clave viaja en el HTML, así
que cualquiera puede copiarla del código fuente de tu página y montar el chat en
la suya. El agente contestará, y las llamadas al modelo las pagas tú. Rellena la
lista antes de publicar el widget.

La comprobación se hace contra la cabecera `Origin` (y `Referer` si no hay
`Origin`) en cada creación de sesión y en cada mensaje; si no encaja, **403**.
Una petición sin ninguna de las dos cabeceras también se rechaza, salvo que la
lista esté vacía.

Si la clave se te queda pegada en un sitio del que quieres sacarla, el botón de
rotarla está en la misma tarjeta: corta las sesiones abiertas y obliga a
actualizar el código en todas las webs donde esté puesto.

### 2.4 Servidor MCP y tokens de agente

El servicio `mcp` es la sexta pieza del despliegue y **es un servicio expuesto a
internet si le pones dominio**. Sirve para que un asistente (Claude Code, n8n u
otro cliente MCP) mire la instalación y trabaje sobre ella sin entrar al panel.

Lo que hay que tener claro antes de encenderlo:

- **El servidor MCP no guarda ninguna clave y no autentica nada.** Es un cliente
  HTTP fino: coge la cabecera `Authorization: Bearer …` que le manda el cliente y
  se la pasa tal cual al backend, a `/api/v1/agent-api`. Quien decide qué se
  puede hacer es el backend, a partir del token.
- **Los tokens de agente se crean en el panel**, en Conexiones → API / MCP.
  Empiezan por `agt_`, se enseñan **una sola vez** y en la base de datos solo
  queda su hash SHA-256. Si lo pierdes, se crea otro; no se recupera.
- Cada token lleva **ámbitos**. Estos son todos los que hay:

| Ámbito | Qué permite | Lectura / escritura |
|---|---|---|
| `monitor:read` | Estado, salud, canales y coste de modelo | Lectura |
| `interactions:read` | Listado de conversaciones y sus metadatos | Lectura |
| `kb:read` | Leer y buscar en la base de conocimiento | Lectura |
| `kb:write` | Crear y sobrescribir documentos de la base de conocimiento | **Escritura** |
| `prompts:write` | Listar agentes y **sobrescribir su prompt de sistema** | **Escritura** |

> **`prompts:write` es el ámbito peligroso.** Con él, quien tenga el token puede
> reescribir por completo lo que tu bot le dice a tus clientes, desde fuera y sin
> pasar por el panel. La capa fija de seguridad (§ 2.12) sigue por delante y no se
> puede quitar por ahí, pero todo lo demás sí: el tono, los precios que afirma,
> a quién deriva. Si el token se filtra, tu bot es de quien lo tenga.

**Cómo usarlo con cabeza:**

- Da a cada token **solo los ámbitos que necesite**. Para vigilar la instalación
  sobra con `monitor:read` e `interactions:read`. No repartas `prompts:write`
  "por si acaso".
- **Ponles caducidad** al crearlos. Si no la pones, el token no caduca nunca.
- **Si no vas a conectar ningún agente, no le pongas dominio al servicio `mcp`.**
  Sigue levantado, pero no se llega a él desde fuera. Es la opción por defecto
  recomendada.
- Revoca el token en cuanto deje de hacer falta. Cada escritura queda en
  `/admin/audit` con el token que la hizo.

Lo que sí trae de serie: 120 peticiones/minuto por token (HTTP 429 al pasarse) y
auditoría de todas las escrituras. Lo que **no** trae: lista blanca de IPs.
El token es lo único que separa a un desconocido de tu prompt.

### 2.5 SSRF en descarga de audios
- Allowlist de hosts (`*.ycloud.com`).
- HTTPS obligado, `follow_redirects=False`.
- Resolución DNS comprobada: rechazo si apunta a IP privada/loopback/link-local.

### 2.6 Subida de documentos (KB)
- Sólo admin.
- Filename saneado a basename, longitud máxima.
- Validación de magic bytes (`%PDF-`, `PK\x03\x04`, UTF-8 para texto plano).
- Tamaño máximo 25 MB.
- Storage path resuelto y validado para evitar traversal.

### 2.7 Servir audios
- Endpoint `/audios/{filename}` autenticado.
- Path resuelto con `Path.resolve()`, debe estar bajo `_AUDIO_ROOT`.
- Frontend descarga vía `fetch` con `Authorization` y crea blob URL (el `<audio>` no puede llevar headers).

### 2.8 Salida de servidor
- Sin Swagger/Redoc/openapi.json en producción.
- `/admin/health` y `/admin/credentials/{key}/test` devuelven mensajes genéricos; el detalle real va al log estructurado.
- Errores no controlados → `{"code":"INTERNAL_ERROR"}` sin stack.

### 2.9 Cabeceras HTTP (nginx)
- `Strict-Transport-Security`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` bloqueando cámara/micro/geolocalización.
- CSP estricta: `script-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`. `connect-src 'self' https: wss:` (se puede restringir a dominio fijo cuando se conozca en build-time).

### 2.10 Logging
- structlog JSON.
- Scrubber recursivo (dicts y listas) de teléfono, email, claves `sk-*`, Bearer tokens, JWTs.

### 2.11 Contenedor
- Backend corre como usuario `app` (uid 1000), no root.
- Directorios de datos (`/data/audios`, `/data/kb_docs`) propiedad de `app`.

### 2.12 Endurecimiento adicional
- **Capa de seguridad fija anti prompt-injection** antepuesta al prompt de TODOS
  los canales, no editable desde el panel (`runtime_config.SECURITY_GUARD`).
- **Rate-limit con storage Redis compartido** (límite real con N réplicas) e IP
  de cliente derivada del proxy de confianza (`TRUSTED_PROXY_COUNT`), no del
  primer `X-Forwarded-For` falsificable.
- **WebSocket de voz (Retell)** valida el `call_id` contra la API de Retell antes
  de atender (`RETELL_WS_VALIDATE`).
- **Fallback LLM**: la `base_url` del gateway se valida (https + rechazo de IP
  internas) → anti-SSRF/exfiltración.
- **Idempotencia de webhooks** con guard atómico Redis (evita doble proceso por
  reintentos del proveedor).
- **Outbound** respeta la blocklist; **secretos de canal** enmascarados en las
  respuestas de la API; **PII redactada** en las trazas del agente.
- `JWT_SECRET` por defecto inseguro → en no-producción se genera uno aleatorio;
  en producción `validate_production` aborta el arranque.
- Import CSV con tope de tamaño; export con neutralización de fórmulas + auditoría.

Pendiente conocido: el JWT del panel viaja en el navegador, no en una cookie
`httpOnly`.

---

## 3. Defensas contra abuso del agente

Valen para cualquier canal de entrada, no solo para WhatsApp: quien escribe al
bot es siempre alguien de fuera.

| Vector | Defensa | Archivo |
|---|---|---|
| Suplantación de contacto vía `crear_actualizar_contacto("telefono":"+34X")` | El teléfono se fuerza al de `ctx` (webhook), se ignora el de args, intento se notifica. | `agents/tools/contact_upsert.py` |
| Fuga PII vía `buscar_contacto(email="jefe@empresa.com")` | La tool no acepta parámetros, solo devuelve datos del propio contacto. | `agents/tools/contact_lookup.py` |
| Dump del KB | `top_k≤8`, query ≤500 chars, contenido truncado a 1200 chars. | `agents/tools/kb_search.py` |
| Spam al canal del equipo vía `derivar_humano` | Cooldown 10 min por conversación, idempotente si ya derivada/cerrada, motivo/resumen escapados HTML y truncados. | `agents/tools/human_handoff.py` |
| DoS económico OpenAI | 10 msg/min · 60 llamadas LLM/h por contacto. Mensajes truncados a 4000 chars antes del LLM. Audios > 8 MB rechazados. | `services/agent_guardrails.py`, `services/audio_processor.py` |
| Blocklist persistente | 5 infracciones de rate-limit en 10 min → bloqueo automático 24h. Visible y desbloqueable en `/admin/blocklist`. | `services/agent_guardrails.py`, UI `BlocklistPage.tsx` |
| Contenido tóxico/jailbreak | API de moderación de OpenAI (`omni-moderation-latest`) antes del LLM. Si se marca → no se contesta, pasa a humano, alerta al canal. **Solo funciona si hay `openai_api_key`**, aunque uses otro proveedor de IA: ver [§ 9](#9-proveedor-de-ia-y-moderación-de-contenido). | `services/moderation.py` |
| Prompt injection | (a) Capa fija de seguridad antepuesta al prompt por el sistema, no editable desde el panel. (b) Prompt plantilla de los agentes con reglas explícitas. (c) Defensa en código: las tools no obedecen al LLM si intenta saltarse el contrato. | `services/runtime_config.py:SECURITY_GUARD`, seed `PROMPT_PLANTILLA_TEXTO` / `PROMPT_PLANTILLA_VOZ`, todas las tools |
| Inyección HTML en alertas al equipo | `html.escape` en motivo/resumen antes de `notify_team` (Telegram usa parse_mode HTML). | `agents/tools/human_handoff.py`, `services/security_alerts.py` |
| Iteración runaway del agente | `MAX_ITERATIONS=5` en el orchestrator. | `agents/orchestrator.py` |

### Alertas de seguridad

`services/security_alerts.notify_security(kind, title, details, throttle_key)` envía al canal Telegram/Slack con prefix `[SEGURIDAD]`. Throttling 10 min por `(kind, throttle_key)` para no inundar.

Se dispara automáticamente en:

- `phone_override` — intento de suplantación de teléfono
- `audio_too_large` — audio rechazado por tamaño
- `llm_rate_limited` — rate-limit LLM excedido
- `moderation_flagged` — contenido marcado por moderación
- `phone_blocked` — contacto añadido a blocklist

Sin credenciales Telegram/Slack configuradas no llegan a ningún sitio — quedan solo en el log.

---

## 4. PII en reposo

| Campo | Cifrado | Motivo |
|---|---|---|
| `Message.audio_transcript` | Sí — Fernet (`EncryptedText`) | Voz del cliente, alta sensibilidad. |
| `Contact.notas_internas` | Sí — Fernet | Notas del operador, puede incluir datos médicos/sensibles. |
| `Conversation.resumen` | Sí — Fernet | Resumen humano de la conversación. |
| `Contact.telefono` | No — en claro | Clave de búsqueda + UNIQUE. Cifrarla rompe `WHERE telefono = ...`. |
| `Contact.email`, `Contact.nombre` | No — en claro | Necesarios para búsqueda LIKE en `/contacts`. |
| `Message.contenido` | No — en claro | Alto volumen, se renderiza en cada vista. |
| Credenciales de proveedor | Sí — Fernet | Cifrado obligatorio desde día 1. |

**Trade-off consciente**: cifrar a nivel campo `telefono/email/nombre` requiere HMAC determinista para búsqueda exacta + Fernet para almacenamiento. Es trabajo serio y rompe búsquedas LIKE.
La protección para esos campos depende de:
1. **Cifrado del volumen del disco** del proveedor (EasyPanel / VPS provider).
2. **Cifrado de las credenciales de BD** (`POSTGRES_PASSWORD` no se reutiliza).
3. **Acceso restringido al panel admin** (JWT + bcrypt + auditoría).

Si necesitas cifrado a nivel campo de teléfono/email, dejar como evolución futura.

---

## 5. Auditoría

`audit_log` registra:

- `auth.login.success`, `auth.login.failed`, `auth.logout`
- `user.created`, `user.updated`, `user.deleted`, `user.password_reset`
- `credential.created`, `credential.updated`
- `agent_config.activated`, `agent.prompt_update`
- `kb.note.created`, `kb.document.edited`
- `agent_token.created`, `agent_token.revoked`
- `phone.unblocked`

Cada entrada lleva `user_id`, `ip`, `user_agent`, `before/after` JSONB. Visible en `/admin/audit`.

Lo que hace un cliente MCP también queda aquí: las escrituras hechas con un token
de agente se registran sin `user_id` y con el token que las hizo dentro de
`after`. Si algún día te cambia el prompt del bot sin haberlo tocado tú, es donde
hay que mirar.

**Limitación**: no hay política de retención. Crece indefinido. Para fase 1 aceptable; cuando la tabla supere ~1M filas considerar archivado.

---

## 6. Checklist pre-producción

Esto es lo que hay que hacer **antes de abrir el chatbot a clientes reales**, una
vez, en una instalación nueva. El paso a paso de la instalación en sí está en
[`DEPLOY_EASYPANEL.md`](./DEPLOY_EASYPANEL.md); aquí solo está lo que, si te lo
saltas, deja la instalación abierta.

### 6.1 Genera tus secretos y no los metas en el repositorio

`JWT_SECRET`, `ENCRYPTION_KEY`, la contraseña de Postgres y la del administrador
inicial se generan una vez y van en las **variables de entorno del despliegue**,
nunca en un fichero que suba a git.

```bash
openssl rand -hex 24      # POSTGRES_PASSWORD (hex, no base64: viaja dentro de DATABASE_URL)
openssl rand -base64 48   # JWT_SECRET
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # ENCRYPTION_KEY
```

Los mismos valores van en `app`, `worker` y `beat`. Si no coinciden, unos
servicios no podrán descifrar lo que escriben los otros.

- **Si no lo haces:** con los valores de ejemplo, cualquiera que tenga este
  código puede firmarse un token de sesión válido y entrar al panel.
- **Guarda la `ENCRYPTION_KEY` en un gestor de contraseñas.** Cifra las
  credenciales de proveedor, las claves de los canales de Instagram y Retell, y
  las copias de seguridad. Si la pierdes, no se recupera nada de eso.
- Si algún día se te filtra alguno, rótalo. Cambiar `ENCRYPTION_KEY` obliga a
  volver a meter a mano todas las credenciales desde el panel.

### 6.2 Despliega con los ficheros de producción, no con los de desarrollo

| Producción | Desarrollo local — **no lo uses para desplegar** |
|---|---|
| `docker-compose.easypanel.yml` | `docker-compose.yml` |
| `.env.example` | `.env.desarrollo.example` |

- **Si te equivocas de fichero:** `.env.desarrollo.example` trae `APP_ENV=development`, y con
  eso `validate_production()` (`backend/app/core/config.py`) **no comprueba
  nada**. Arranca tan contento con el `JWT_SECRET` de fábrica, con URLs a
  localhost y con el usuario administrador de ejemplo que está escrito en este
  mismo repositorio. Resultado: una instalación abierta a internet en la que se
  entra con unas credenciales públicas.
- Comprueba después del despliegue que el servicio `app` tiene
  `APP_ENV=production` en su pestaña Environment. Es la comprobación de treinta
  segundos que evita el problema entero.

### 6.3 Deja que el primer arranque haga su trabajo

El `entrypoint.sh` del servicio `app` ejecuta, en este orden:

1. `alembic upgrade head` — crea el esquema y aplica las migraciones.
2. `python -m app.scripts.seed` — crea el usuario administrador inicial y siembra
   los dos agentes (**Agente de Texto** y **Agente de Voz**) con su prompt
   plantilla.

El seed es *create-only*: en una instalación que ya funciona no pisa nada de lo
que hayas escrito.

Si el arranque se para, léelo en los registros antes de tocar nada. Las causas
habituales son a propósito:

- `JWT_SECRET` vacío, por defecto o de menos de 32 caracteres.
- `ENCRYPTION_KEY` vacía o sin formato Fernet válido.
- `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` con los valores de ejemplo.
- `APP_BASE_URL` o `FRONTEND_BASE_URL` apuntando a `localhost`.

### 6.4 Entra, cambia la contraseña y pon tus claves

Primer login con `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD`.

1. **Cambia la contraseña del administrador** en `/admin/users`. La inicial ha
   pasado por las variables de entorno y por tus notas: no vale como definitiva.
2. Pon las claves en `/admin/connections`, pestaña **Servicios**. Se cifran con
   Fernet antes de guardarse. Qué pasa si falta cada una:
   - **Clave de IA** (OpenAI, Anthropic o Gemini) → el agente no contesta.
   - **`openai_api_key`** → además, **la moderación de contenido queda apagada**
     aunque tengas otro proveedor funcionando. Léete la [§ 9](#9-proveedor-de-ia-y-moderación-de-contenido)
     antes de decidir.
   - **`telegram_bot_token` + `telegram_chat_id`** (o `slack_webhook_url`) → las
     alertas de seguridad **no llegan a ningún sitio**, se quedan en el registro.
     Es lo que te avisa de que alguien está abusando del bot: ponlo.
   - **Claves del canal que vayas a usar** → sin ellas ese canal no opera.

### 6.5 Rellena el prompt de los dos agentes

Los agentes se siembran con una **plantilla**: las reglas de comportamiento están
escritas y funcionan, pero los datos del negocio vienen vacíos y marcados con
`[[ RELLENAR: … ]]`.

Ve a **`/admin/agent/agents`**, abre cada agente y complétalo. El detalle del qué
y el cómo está en [`DEPLOY_EASYPANEL.md` § 5.b](./DEPLOY_EASYPANEL.md#5b-personaliza-el-prompt-de-tus-agentes-obligatorio-antes-de-abrir).
Los prompts plantilla son `PROMPT_PLANTILLA_TEXTO` y `PROMPT_PLANTILLA_VOZ`, en
`backend/app/scripts/seed.py`.

**No copies ni pegues ningún bloque de reglas de seguridad.** La capa fija
(`services/runtime_config.py:SECURITY_GUARD`) la antepone el sistema a cada
llamada al modelo, en todos los canales, y no se puede quitar desde el panel.
Pegarla a mano en el prompt solo consigue duplicarla y gastar contexto.

- **Si no lo haces:** el agente contestará a tus clientes con los marcadores
  `[[ RELLENAR: … ]]` dentro del mensaje. El checklist de Inicio del panel no da
  el paso por hecho hasta que no queda ni un marcador en ningún agente activo.

### 6.6 Conecta y protege los canales que vayas a abrir

Solo los que vayas a usar. Para cada uno: crea el canal en **Conexiones**, pega
su clave, y copia la dirección del webhook al panel del proveedor. Las
direcciones, los secretos y la comprobación de firma de los seis canales están en
la tabla de la [§ 2.3](#23-canales-de-entrada-quién-entra-y-qué-lo-protege).

Dos que se olvidan:

- **Chat web**: rellena la lista de **Dominios permitidos** antes de publicar el
  widget. Vacía significa que cualquier web puede usar tu agente a tu costa
  ([§ 2.3.d](#23d-chat-web-cómo-se-crea-y-dónde-va-la-lista-de-dominios)).
- **`TRUSTED_PROXY_COUNT`**: ponlo explícitamente. Mal puesto, o deja el límite
  de intentos de login inservible, o deja a todo el mundo fuera del panel
  ([`DEPLOY_EASYPANEL.md` § 10](./DEPLOY_EASYPANEL.md#10-trusted_proxy_count-cuántos-saltos-hay-hasta-tu-backend)).

### 6.7 Tokens de agente y servidor MCP: solo si los vas a usar

Si no vas a conectar ningún asistente a esta instalación, **no le pongas dominio
al servicio `mcp`** y no crees ningún token. Es la opción por defecto.

Si los vas a usar, crea cada token en Conexiones → API / MCP con **los ámbitos
mínimos y con caducidad**. `prompts:write` deja reescribir lo que tu bot dice a
tus clientes desde fuera del panel: no lo repartas por comodidad
([§ 2.4](#24-servidor-mcp-y-tokens-de-agente)).

### 6.8 Apaga el despliegue automático

Tal y como sale de fábrica, EasyPanel despliega en cuanto ve un cambio en `main`,
sin esperar a nada. La revisión automática del repositorio corre en paralelo, así
que **cuando se pone en rojo, la versión rota ya va camino del servidor**.

Los dos ajustes que lo convierten en una barrera de verdad —proteger `main` en
GitHub y apagar el auto-despliegue en EasyPanel— están explicados paso a paso en
[`DEPLOY_EASYPANEL.md` § 7.b](./DEPLOY_EASYPANEL.md#7b-los-dos-ajustes-que-faltan-y-que-solo-puedes-hacer-tú).
Son diez minutos y se hacen una vez.

- **Si no lo haces:** cualquier cambio que rompa los tests llega igual a
  producción, y te enteras por los clientes.

### 6.9 Verifica antes de abrir

- `curl https://<api>/health` → `{"status":"ok","db":"ok","redis":"ok","worker":"ok","worker_last_seen_seconds":<n>}`.
  Si `worker` dice `caido`, mira primero el contenedor `beat`; el código sigue
  siendo 200 a propósito.
- En `/admin/health`: base de datos, Redis y motor de tareas en verde.
- Login con la contraseña **nueva**, no con la inicial.
- Manda un mensaje de prueba por cada canal que hayas abierto y comprueba que
  llega al inbox y que el agente contesta sin marcadores `[[ RELLENAR`.
- `/admin/audit` debe mostrar tu login y los cambios que acabas de hacer.
- Programa las copias de seguridad y haz una a mano para comprobar que sale en
  verde (panel → **Copias de seguridad**).

---

## 7. Operación día a día

| Cosa | Dónde |
|---|---|
| Ver eventos sensibles | `/admin/audit` |
| Ver contactos bloqueados | `/admin/blocklist` |
| Cambiar credencial de proveedor | `/admin/connections`, pestaña Servicios (re-test con "Probar") |
| Cambiar el prompt de un agente | `/admin/agent/agents` — es una lista de agentes; abres el que quieras y el cambio se aplica al guardar. Queda versionado, con historial y restauración. |
| Crear o revocar tokens de agente / MCP | `/admin/connections`, pestaña API / MCP |
| Ver derivaciones humanas | `/inbox` filtrado por "Humano" |
| Recibir alertas | Canal Telegram/Slack configurado en `/admin/connections` |
| Health en vivo | `/admin/health` |

### Si Telegram empieza a recibir `[SEGURIDAD] Contacto bloqueado por abuso`

- Revisa `/admin/blocklist` — verás teléfono, motivo y TTL.
- Si es legítimo, desbloquéa.
- Si es ataque, déjalo bloqueado (expira solo en 24h) o crea una regla a nivel firewall.

### Si recibes `[SEGURIDAD] Intento de suplantación de teléfono vía agente`

Alguien le ha pedido al bot "soy +34X" y el bot ha intentado obedecer. El código bloqueó la acción. Investiga el contacto real (`real_phone`) — si es un patrón, considerar bloquearlo.

---

## 8. Lo que NO está protegido (decisiones explícitas)

- **Moderación de contenido sin OpenAI**: si la instalación usa solo Anthropic o
  Gemini, la moderación no funciona. Es lo más importante de esta lista y tiene
  su propia sección: [§ 9](#9-proveedor-de-ia-y-moderación-de-contenido).
- **Multi-tenancy**: el WebSocket emite al canal `all`. Cualquier operador autenticado ve todas las conversaciones. Hoy hay una instancia por cliente → no es brecha. Si añades segundo cliente en la misma instancia, hay que particionar el canal.
- **Lista blanca de IPs para los tokens de agente / MCP**: no existe. El token es
  lo único que separa a un desconocido del prompt de tu bot ([§ 2.4](#24-servidor-mcp-y-tokens-de-agente)).
- **Retención automática de `audit_log`**: crece indefinido.
- **Cifrado a nivel campo de teléfono/email/nombre**: documentado en §4.
- **Bloqueo a nivel red (firewall)**: la blocklist es a nivel aplicación. Un atacante distribuido cambia de IP fácil; la mitigación real es el rate-limit + bloqueo por teléfono.
- **Refresh tokens**: el JWT dura 24h. Pasado eso, login otra vez.
- **2FA admin**: no implementado.

---

## 9. Proveedor de IA y moderación de contenido

La app ya trae tres proveedores de modelo: **OpenAI**, **Anthropic** y
**Gemini**. Se eligen por agente desde el panel, y el cliente concreto se decide
por la familia de la `base_url` del proveedor (`providers/llm/families.py`).
Cambiar de proveedor de modelo no es un "algún día": es una casilla del panel.

**La moderación de contenido no sigue a ese cambio.**
`services/moderation.py` llama directamente a la API de moderación de OpenAI y
pide la credencial `openai_api_key`, sin mirar qué proveedor usa el agente.

### Qué pasa en una instalación que solo use Anthropic o Gemini

Sin `openai_api_key` guardada:

1. `moderate()` no encuentra clave y devuelve "no marcado" con `available=False`.
   Es **fail-open a propósito**: la moderación no puede tumbar la conversación.
2. En la práctica, **todos los mensajes entrantes llegan al modelo sin revisar**.
   Nadie filtra acoso, contenido sexual, autolesiones ni violencia explícita.
3. La alerta `moderation_flagged` no se dispara nunca, porque nunca hay nada
   marcado. El silencio del canal de alertas parece que todo va bien.

Esto **no es hipotético y no es una nota al pie**: es el estado real de cualquier
instalación que no tenga clave de OpenAI, aunque el bot esté contestando
perfectamente con otro proveedor.

### Qué hacer

Elige una de las tres, a conciencia:

- **Guarda una `openai_api_key` aunque uses otro proveedor de modelo.** Es lo más
  sencillo. La API de moderación de OpenAI es gratuita: la clave se usa solo para
  eso y no encarece nada.
- **Integra otra moderación** (por ejemplo, Perspective API de Google) en
  `services/moderation.py`. Es trabajo de desarrollo, no de configuración.
- **Asume la pérdida de esa capa** y compénsalo: el resto de defensas del agente
  siguen ahí (capa fija de seguridad en el prompt, límites por contacto,
  blocklist automática, derivación a humano). Si el negocio atiende a menores o
  trata temas sensibles, esta opción no vale.

### Cómo comprobar cómo estás ahora mismo

- El panel lo dice: **Monitorización** muestra el estado de la moderación. Si
  pone que no está operativa, todos los mensajes están pasando sin revisar.
- En **Logs en vivo** aparece `moderation.no_key` con el texto "la moderación de
  contenido está APAGADA", como mucho una vez por hora para no inundar.
