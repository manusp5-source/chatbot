# Deploy en EasyPanel desde GitHub

Repo: el tuyo, en GitHub.
Pre-requisitos: tener EasyPanel ya instalado y un dominio apuntando al VPS.


---

## Guía rápida (1 · 2 · 3)

Si solo quieres dejarlo funcionando, esto es lo mínimo:

1. **Despliega.** En EasyPanel crea el proyecto e importa `docker-compose.easypanel.yml`. EasyPanel te trae el `.env.example` a la pestaña Environment: son 12 variables (dominios + secretos) y no hace falta ninguna más. Genera los secretos con los comandos de más abajo. Pon dominios con SSL en `app` (puerto 8000) y `frontend` (puerto 80). Arranca.
   > En el campo **Host** del dominio va SOLO el nombre: `api.chat.tucliente.com`. Sin `https://` y sin barra final. Si pegas ahí la URL entera, EasyPanel guarda el dominio pero el enrutador no lo reconoce: te contesta su propio 404 con un certificado autofirmado, el panel se queda sin backend y **el login te dirá que las credenciales son incorrectas** aunque sean correctas.
2. **Entra y pon tus claves.** Login en tu dominio con `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD`. El Inicio te muestra un checklist: **cambia la contraseña**, **pega tu clave de IA** y **conecta un canal** (WhatsApp/Instagram/email). Las claves van en el panel → **Conexiones**, no en variables de entorno. La tarjeta desaparece sola al completar los 3 pasos.
3. **Copias de seguridad.** En EasyPanel → Project → Backups, programa un backup diario del volumen `db_data` (retención 30 días). Es lo único que necesitas respaldar.

El chatbot **arranca aunque no tengas todavía ninguna clave de IA ni canal** (modo degradado): lo configuras con calma desde el panel. Detalle completo en las secciones siguientes.

---

## 0. Genera los secretos

Abre una terminal (en tu ordenador vale) y ejecuta estos comandos. Guarda cada
resultado: los pegarás en las variables de entorno de EasyPanel.

```bash
# Contraseña de la base de datos (ojo: -hex, no -base64)
openssl rand -hex 24

# Firma de las sesiones (JWT)
openssl rand -base64 48

# Cifrado de las credenciales que guardes luego en el panel (formato Fernet)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **Por qué la de la base de datos se genera con `-hex`:** esa contraseña viaja
> dentro de la URL de conexión a Postgres (`DATABASE_URL`), y `-base64` mete
> símbolos como `+` o `/` que la rompen. El síntoma es `P1000 Authentication
> failed` y los servicios reiniciándose en bucle. Con `-hex` solo salen números
> y letras. El `JWT_SECRET` y la `ENCRYPTION_KEY` van sueltos, no dentro de
> ninguna URL, así que ahí el base64 no da problemas.

> **Ponla antes del primer despliegue.** Postgres fija su contraseña la primera
> vez que arranca y después ignora la variable. Si la cambias más tarde, el
> servicio `db` vuelve a sincronizarla en el siguiente despliegue (está
> preparado para eso), pero es una vuelta que te ahorras teniéndola desde el
> principio.

Y decide el **correo y la contraseña del administrador inicial** (mínimo 10
caracteres, con letras y números). La cambiarás desde el panel tras el primer
login.

---

## 1. Conectar tu repositorio

El código lo lee EasyPanel de **tu** repositorio de GitHub, así que primero
súbelo ahí. Si el repositorio es **privado**, cómo lo conectas depende del tipo
de servicio:

- **Servicios de tipo *App*** (los que se crean uno a uno): sí tienen pestaña
  GitHub. Conecta la cuenta una vez en EasyPanel → **Settings → Sources →
  GitHub** → *Connect*, autoriza el repositorio, y ya puedes elegirlo en cada
  servicio.
- **Servicios de tipo *Compose*** (un solo servicio con el fichero
  `docker-compose.easypanel.yml`): **NO tienen esa pestaña.** Solo hay
  `docker-compose.yml` inline y una casilla `Git` pelada, que se descarga el
  repositorio "a pelo", sin usar el token de GitHub que hayas guardado en los
  ajustes. Con un repositorio privado verás **"Cannot access repository"**, y no
  es culpa del token.

Para un servicio Compose con repositorio privado, la vía que funciona es **SSH +
deploy key de solo lectura**:

1. En el servicio, pon la dirección del repositorio en formato SSH:
   `git@github.com:tu-usuario/tu-repo.git`.
2. Guarda y pulsa **Generate SSH Key**. EasyPanel te muestra una clave pública.
3. Cópiala y pégala en GitHub, en tu repositorio → **Settings → Deploy keys →
   Add deploy key**. Deja el permiso en **solo lectura** (no marques "Allow
   write access").
4. Vuelve a EasyPanel y despliega.

> Si el código no es sensible, la otra salida es dejar el repositorio **público**
> y usar la dirección `https://github.com/tu-usuario/tu-repo.git`. Más simple,
> pero cualquiera puede leerlo.

## 2. Crear proyecto y servicios

**Project** → **Create Project** → nombre: `chatbot`.

> **Dos caminos posibles.** (A) Un único servicio de tipo **Compose** apuntando
> al fichero `docker-compose.easypanel.yml` de la raíz: crea los 7 servicios de
> golpe y ya trae los healthchecks, los márgenes de arranque y la
> sincronización de contraseña de la base de datos. (B) Los **7 servicios a
> mano**, que es lo que detalla el resto de esta sección. El camino A es el
> recomendado; el B te deja configurarlo todo a mano si prefieres verlo pieza a
> pieza.

Dentro del proyecto, crea **7 servicios** (uno por uno):

### 2.1 `db` — Postgres con pgvector

- **Type**: App
- **Source**: Docker Image
- **Image**: `pgvector/pgvector:pg16`
- **Environment** (Settings → Env):
  ```
  POSTGRES_USER=chatbot
  POSTGRES_PASSWORD=<EL_openssl_rand_-hex_24_DEL_PASO_0>
  POSTGRES_DB=chatbot
  ```
  > Genera con: `openssl rand -hex 24` (hex, no base64: ver paso 0).
- **Storage / Volumes**: Mount `/var/lib/postgresql/data` → volumen nuevo `db_data`
- **No publicar puertos** (solo interno)

### 2.2 `redis`

- **Source**: Docker Image
- **Image**: `redis:7-alpine`
- **Command**: `redis-server --appendonly yes`
- **Volume**: `/data` → `redis_data`
- **No publicar puertos**

### 2.3 `app` — backend FastAPI

- **Source**: GitHub → `tu-usuario/tu-repo` → branch `main`
- **Build**: Dockerfile en `./backend/`
- **Environment** (pega todo este bloque):
  ```
  APP_ENV=production
  APP_PORT=8000
  APP_BASE_URL=https://api.chat.tucliente.com
  FRONTEND_BASE_URL=https://chat.tucliente.com
  CORS_ALLOWED_ORIGINS=https://chat.tucliente.com

  DATABASE_URL=postgresql+asyncpg://chatbot:<POSTGRES_PASSWORD>@chatbot_db:5432/chatbot

  REDIS_URL=redis://chatbot_redis:6379/0
  CELERY_BROKER_URL=redis://chatbot_redis:6379/1
  CELERY_RESULT_BACKEND=redis://chatbot_redis:6379/2

  JWT_SECRET=<GENERA: openssl rand -base64 48>
  JWT_EXPIRATION_HOURS=24
  ENCRYPTION_KEY=<GENERA: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">

  INITIAL_ADMIN_EMAIL=<TU_EMAIL_ADMIN>
  INITIAL_ADMIN_PASSWORD=<GENERA_PASSWORD_FUERTE>

  AUDIO_STORAGE_PATH=/data/audios

  DEFAULT_LLM_MODEL=gpt-5.4-mini
  MESSAGE_BUFFER_SECONDS=8
  RESPONSE_SPLIT_MAX_PARTS=3

  RUN_MIGRATIONS=true
  ```
  > **Esto es el mínimo.** Hay otras ~30 variables opcionales (zona horaria de
  > las métricas, cuánto se guarda cada cosa, carpetas de copias y adjuntos,
  > comportamiento del agente…). Todas tienen un valor por defecto que
  > funciona, así que no hace falta ninguna para arrancar. Están explicadas una
  > a una, con su valor por defecto, en el **anexo** del final de esta guía.
  >
  > Una que sí conviene mirar desde el principio: **`TRUSTED_PROXY_COUNT`**
  > (§ 10). Mal puesta deja a todo el mundo sin poder entrar al panel.

  > Las claves de proveedores (YCloud, OpenAI, Resend, Telegram, Slack) **NO se ponen aquí**. Se configuran desde el panel admin de la app una vez desplegada (cifradas con Fernet en la DB).
  >
  > **Excepción — OAuth**: `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` (login con Google, Gmail, Calendar) e `INSTAGRAM_OAUTH_CLIENT_ID`/`INSTAGRAM_OAUTH_CLIENT_SECRET` (Instagram Business Login) SÍ van como variables de entorno si usas esas integraciones (también pueden gestionarse desde Admin → Credenciales).
  >
  > **IMPORTANTE — rotación**: si alguno de estos secretos llegó a quedar en el historial git, considéralo comprometido y genera valores nuevos. Cambiar `ENCRYPTION_KEY` obliga a re-introducir desde el panel admin **todas** las credenciales de proveedores (Conexiones → Servicios) **y las de los canales de Instagram y Retell** (Conexiones → Canales), porque las cifradas con la clave anterior dejan de ser legibles. El sistema no las borra: aparta el dato viejo por si recuperas la clave buena, pero mientras tanto esos canales no funcionan.

- **Volume**: `/data/audios` → `audios_data`
- **Domain**: añade `api.chat.tucliente.com` apuntando al puerto `8000` con SSL (Let's Encrypt)
- **Healthcheck**: GET `/health` — configúralo manualmente en EasyPanel (el Dockerfile no declara HEALTHCHECK)

### 2.4 `worker` — Celery worker

- **Source**: mismo repo / branch
- **Build**: Dockerfile en `./backend/`
- **Command override**: `celery -A app.tasks worker --loglevel=info --concurrency=4`
- **Environment**: copia LAS MISMAS variables que `app`, pero cambia:
  ```
  RUN_MIGRATIONS=false
  ```
- **Volume**: `/data/audios` → mismo `audios_data` (compartido con `app`)
- **No publicar puertos**

### 2.5 `beat` — Celery beat (el reloj)

- **Source**: mismo repo / branch
- **Build**: Dockerfile en `./backend/`
- **Command override**: `celery -A app.tasks beat --loglevel=info --schedule=/tmp/celerybeat-schedule`
- **Environment**: igual que `worker` (`RUN_MIGRATIONS=false`)
- **No publicar puertos**
- **Healthcheck** (si creas los servicios a mano, configúralo tú; el compose ya
  lo trae):
  ```
  python -c "import os,sys,time; p='/tmp/celerybeat-schedule'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 1200 else 1)"
  ```

> **Por qué este servicio necesita vigilancia.** `beat` es el reloj: es quien
> dice "toca hacer la copia", "toca borrar lo que ya ha caducado", "toca
> comprobar que el motor de tareas sigue vivo". Él no hace ese trabajo, solo lo
> manda. Si se muere, no falla nada de forma visible: simplemente dejan de
> pasar cosas. Y despista, porque lo que se ve luego en el panel es que el
> *worker* no da señales — cuando el worker está perfectamente y lo que falta
> es quien le manda el trabajo.
>
> El `--schedule` fija en qué fichero apunta beat lo que ya ha disparado, para
> que la comprobación pueda mirar si sigue funcionando: beat lo reescribe cada
> pocos minutos, así que un fichero parado más de 20 minutos significa que beat
> no está trabajando. Ese fichero es desechable; si se borra, se rehace solo.

### 2.6 `frontend` — Panel React

- **Source**: mismo repo / branch
- **Build**: Dockerfile en `./frontend/`
- **Build args**:
  ```
  VITE_API_BASE_URL=https://api.chat.tucliente.com
  ```
- **Environment**:
  ```
  MCP_BASE_URL=https://mcp.chat.tucliente.com
  ```
  (runtime, no build: es la URL que el panel enseña en Conexiones → API / MCP.
  Si la dejas vacía, el panel enseña la del backend y el comando que copie el
  cliente no conectará con nada.)
- **Port**: `80`
- **Domain**: `chat.tucliente.com` con SSL

---

### 2.7 `mcp` — Servidor MCP

Lo que permite conectar un asistente (Claude, n8n) a esta instalación
para monitorizarla, revisar la base de conocimiento y afinar los prompts sin
entrar al panel. Expone 13 herramientas: `monitor_overview`, `get_llm_costs`,
`get_channels_status`, `get_health`, `list_interactions`,
`get_interaction_metadata`, `list_kb_documents`, `get_kb_document`, `search_kb`,
`create_kb_document`, `update_kb_document`, `list_agents` y
`update_agent_prompt`. Es un cliente HTTP fino delante de `/api/v1/agent-api`:
**sin base de datos, sin Redis, sin estado y sin volúmenes.**

**Va dentro de `docker-compose.easypanel.yml` como una pieza más.** Por el
camino A (servicio Compose) se levanta con el despliegue normal: no hay que
crearlo aparte ni darle ninguna clave. Lo de abajo es para el camino B, el de
montar los servicios a mano.

- **Source**: mismo repo / branch
- **Build**: Dockerfile en `./mcp-server/`
- **Environment**:
  ```
  AGENT_API_BASE_URL=http://app:8000
  ```
  (el nombre del servicio dentro de la red del proyecto: así el tráfico no sale
  a internet para dar la vuelta por el dominio público)
- **Port**: `8080`
- **Domain**: `mcp.chat.tucliente.com` con SSL — es lo único que hay que decidir
  a mano, y solo si los agentes se van a conectar desde fuera
- **Healthcheck**: viene en la imagen

**No le pongas ninguna clave.** No necesita las variables del backend y no debe
tenerlas: quién puede hacer qué lo decide el backend a partir del token de
agente que manda cada cliente MCP. Los tokens se crean en el panel, en
**Conexiones → API / MCP**, con sus ámbitos, y se enseñan una sola vez.

Si no vas a dar acceso de agentes a esta instalación, déjalo sin dominio: el
servicio sigue levantado igual, simplemente no se le llega desde fuera. Lo único
que quedará sin efecto es esa pestaña del panel.

---

## 3. Orden de arranque

1. `db` y `redis` primero (deben estar healthy)
2. `app` (corre migraciones + seed en su entrypoint)
3. `worker` y `beat` (esperan a que app esté healthy)
4. `frontend` y `mcp`

EasyPanel respeta `depends_on` si lo configuras, o simplemente arrancas en este orden.

---

## 4. Verificación post-deploy

```bash
curl https://api.chat.tucliente.com/health
# → {"status":"ok","db":"ok","redis":"ok","worker":"ok","worker_last_seen_seconds":42}
```

**Cómo se lee esa respuesta:**

| Campo | Qué significa |
|---|---|
| `status` | `ok` = la API puede hablar con la base de datos y con Redis. |
| `db` / `redis` | Las dos piezas de las que depende la propia API. Si alguna falla, la respuesta pasa a `503` y EasyPanel reinicia el contenedor. |
| `worker` | Si el motor de tareas sigue vivo: `ok`, `caido` (más de 15 minutos sin dar señales), `sin_latido` (todavía no ha dado ninguna — normal los primeros minutos tras desplegar) o `desconocido` (Redis caído, no hay forma de saberlo). |
| `worker_last_seen_seconds` | Segundos desde la última señal del worker. |

> **El estado del worker NO cambia el código de respuesta: sigue siendo 200
> aunque ponga `caido`.** Es deliberado. Este endpoint es el latido del
> contenedor de la API: si devolviera error por un problema del worker,
> EasyPanel reiniciaría la API una y otra vez, y reiniciar la API no resucita
> al worker — lo único que consigue es tirar el panel, perder los mensajes que
> entran por los webhooks y dejarte sin la pantalla desde la que diagnosticar
> la avería. El worker tiene su propia comprobación de salud y su propio
> reinicio automático; eso es lo que debe encargarse de él.
>
> Si ves `"worker":"caido"`, mira los contenedores **worker** y **beat** en
> EasyPanel — y empieza por **beat**: si el que ha muerto es beat, nadie manda
> trabajo al worker y el síntoma es exactamente el mismo.

Login en `https://chat.tucliente.com`:
- Email: el valor que pusiste en `INITIAL_ADMIN_EMAIL`
- Password: el valor que pusiste en `INITIAL_ADMIN_PASSWORD`

Cambia el password admin inmediatamente desde `/admin/users`.

---

## 5. Configurar credenciales reales

Desde el panel admin (`https://chat.tucliente.com/admin/connections`). Cada
clave se pega, se guarda y se prueba dentro de la tarjeta del canal o de la API
que la usa, y en esa misma tarjeta tienes las URLs que hay que copiar al panel
del proveedor (webhooks y URIs de redirección). Las claves:

- `openai_api_key`: tu key de OpenAI → Probar conexión
- `ycloud_api_key`, `ycloud_phone_number`, `ycloud_webhook_secret`
- `resend_api_key`, `resend_from_email`
- `telegram_bot_token`, `telegram_chat_id`
- `slack_webhook_url`

Se cifran con Fernet antes de persistirse.

El horario de atención (zona horaria, días laborables, tramos, festivos y las
reglas de las citas) NO está aquí: se fija en **Agentes → pestaña Horarios**.
Es lo que el agente mira para saber si estáis abiertos.

---

## 5.b Personaliza el prompt de tus agentes (obligatorio antes de abrir)

La instalación siembra dos agentes: **Agente de Texto** (WhatsApp, widget web,
Instagram, email) y **Agente de Voz** (llamadas). Los dos vienen con un prompt
**plantilla**: las reglas de comportamiento ya están escritas y funcionan, pero
los datos del negocio están vacíos y marcados así:

```
[[ RELLENAR: nombre comercial tal y como lo conocen los clientes ]]
```

Ve a **Admin → Agentes**, abre cada uno y rellena sus secciones: quién es el
negocio, qué vende, horario, qué no puede hacer, cuándo pasar con una persona y
el tono. Si abres un canal sin hacerlo, el agente le contestará a tus clientes
con los marcadores dentro del mensaje.

El **checklist de Inicio** tiene un paso para esto y no se da por hecho hasta
que no queda ni un marcador `[[ RELLENAR` en ningún agente activo.

Los precios, condiciones y demás detalle fino **no van en el prompt**: van en
**Base de conocimiento**. Así se cambian sin tocar el prompt.

El seed es *create-only por nombre*: en una instalación que ya funcione no pisa
nada de lo que hayas escrito.

---

## Modo degradado (arranca sin claves de proveedor)

El backend **no necesita ninguna clave de IA, WhatsApp o Instagram para arrancar**. `validate_production()` (en `app/core/config.py`) solo exige los secretos de infraestructura: `JWT_SECRET`, `ENCRYPTION_KEY`, credenciales del admin inicial y URLs reales. Las claves de proveedor viven cifradas en la base de datos y se leen bajo demanda (`app/services/credentials.py`), así que su ausencia no rompe el arranque.

Qué pasa cuando falta una clave, en vez de un crash:

- **Sin clave de IA** → el agente responde `"(LLM no configurado. Configura las claves en el panel admin.)"` y lo registra como `llm.not_configured`. El resto del panel (inbox, contactos, métricas) funciona con normalidad.
- **Sin WhatsApp (YCloud)** → los envíos devuelven un aviso `"ycloud_api_key no configurada en /admin/credentials"`; el webhook entrante responde `401` si aún no hay `ycloud_webhook_secret`, sin tumbar el servicio.
- **Sin Instagram / email** → esos canales simplemente no operan hasta que pegues sus claves; nada más falla.

Por eso el flujo recomendado es: despliega con las 7 variables base → entra al panel → configura claves y canales cuando los tengas. El **checklist de Inicio** te guía en ese orden y se oculta al completarse.

---

## 6. Webhooks de los canales

Un *webhook* es la dirección a la que WhatsApp o Instagram avisan cada vez que
alguien te escribe. Sin esto, los mensajes no llegan: el chatbot no va a
buscarlos, se los tienen que traer.

### 6.a WhatsApp (YCloud)

En el panel de YCloud, configura:

- **URL**: `https://api.chat.tucliente.com/api/v1/webhooks/ycloud`
- **Eventos**: los DOS. `whatsapp.inbound_message.received` (texto y audio) y
  `whatsapp.message.updated`. Sin el segundo, el panel no se entera de si el
  mensaje llegó al móvil del cliente y la burbuja se queda sin estado de entrega.
- **Header / Auth**: HMAC SHA256 con el secret `ycloud_webhook_secret`

### 6.b Instagram (mensajes directos)

Instagram se conecta en dos partes: primero el canal desde el panel del
chatbot, y después el webhook en Meta. Las dos hacen falta.

**Parte 1 — en el panel del chatbot** (`Conexiones` → Instagram):

1. Conecta la cuenta con el botón de Instagram (usa el *Business Login* de
   Meta; hace falta una cuenta de Instagram **profesional**, no personal).
2. En la configuración del canal rellena estos dos campos, que son los que
   dejan pasar los avisos de Meta:
   - **`verify_token`**: te lo inventas tú. Cualquier texto largo sin espacios
     vale (por ejemplo, el resultado de `openssl rand -hex 16`). Es una
     contraseña compartida: la pones aquí y la volverás a pegar en Meta.
   - **`app_secret`**: el *App Secret* de tu app de Meta (Meta for Developers
     → tu app → Configuración → Básica). Con él se comprueba que cada aviso
     viene de Meta de verdad y no de un tercero.

**Parte 2 — en Meta for Developers** (tu app → Webhooks → Instagram):

3. **Callback URL**: `https://api.chat.tucliente.com/api/v1/webhooks/instagram`
4. **Verify Token**: exactamente el mismo texto que pusiste en el paso 2.
5. Pulsa *Verify and Save*. Meta llama a esa dirección en ese momento para
   comprobar que existe y que el token coincide.
6. Suscríbete al campo **`messages`** (y a `messaging_postbacks` si usas
   botones). Sin suscribirte a los campos, el webhook queda verificado pero no
   llega ni un mensaje.

**Si Meta dice que no puede verificar la URL:**

| Lo que ves | Qué pasa |
|---|---|
| `The URL couldn't be validated` / 403 | El *Verify Token* de Meta no es idéntico al del canal. Ojo con los espacios al copiar y pegar. |
| Error 403 y los tokens sí coinciden | El canal de Instagram no está activo en el panel, o le falta el `app_secret`. |
| El webhook se verifica pero no llega ningún mensaje | Falta suscribirse al campo `messages`, o la cuenta de Instagram no es profesional. |
| En los registros aparece `Firma inválida` (401) | El `app_secret` del panel no es el de la app de Meta que envía los avisos (típico al tener varias apps). |

Para ver qué está llegando de verdad, enciende un rato `IG_WEBHOOK_DEBUG=true`
(vuelca la *forma* de los mensajes en **Logs en vivo**, no su contenido) y
apágalo después.

---

## 7. Actualizaciones futuras (y cómo evitar desplegar algo roto)

> **Lee esta sección entera antes de subir el primer cambio.** El
> auto-despliegue de EasyPanel viene encendido y **hay que apagarlo** (§ 7.b).
> Mientras esté encendido, un cambio que rompa los tests llega a producción
> igual: la revisión automática corre en paralelo, no delante.

Así es como funciona **de fábrica**, antes de hacer los ajustes del § 7.b:

```bash
# en local
git pull
# editas código
git add -A && git commit -m "..."
git push origin main         # el auto-despliegue mira 'main'
```

EasyPanel detecta el push y reconstruye `app`, `worker`, `beat`, `frontend` y `mcp` con los cambios, sin comprobar nada. Las migraciones nuevas se aplican solas en el siguiente arranque del servicio `app` (porque `RUN_MIGRATIONS=true`).

Ese es justo el comportamiento que vamos a cambiar. Después del § 7.b, el flujo
pasa a ser: rama → Pull Request → revisión en verde → despliegue.

### 7.a Qué comprueba el repositorio en cada cambio

El repositorio tiene una revisión automática (GitHub Actions, fichero
`.github/workflows/ci.yml`) que en cada cambio:

- ejecuta los ~400 tests del backend contra una base de datos de verdad,
- aplica todas las migraciones, las deshace y las vuelve a aplicar (así se ve
  hoy si alguna no sabe dar marcha atrás, y no el día que haya que darla),
- compila el panel y comprueba sus tipos,
- construye las dos imágenes de Docker igual que las construye EasyPanel (un
  `Dockerfile` roto se veía antes solo al desplegar, es decir, en producción),
- pasa el linter en modo informativo: sale en el informe, pero no bloquea.

> **Del 1 de julio al 11 de agosto de 2026 esa revisión estuvo en rojo, las 55
> veces que se ejecutó.** Se caía antes de llegar a los tests por una variable
> de configuración que faltaba, así que **en todo ese tiempo nadie ejecutó los
> tests automáticamente, ni siquiera en lo que está desplegado ahora mismo**.
> Ya está arreglado. La moraleja: si la revisión aparece en rojo, no es ruido.

### 7.b Los dos ajustes que faltan (y que solo puedes hacer tú)

Tal y como está montado hoy, esa revisión **no impide nada**: se lanza con el
mismo `push` que dispara el despliegue de EasyPanel, así que corren en
paralelo. Cuando se pone en rojo, la versión rota ya va camino del servidor.

Para que sea una barrera de verdad hay que hacer dos cosas a mano, una en
GitHub y otra en EasyPanel. Son 10 minutos y se hacen una sola vez.

#### Ajuste 1 — GitHub: que no se pueda meter nada en `main` sin pasar la revisión

1. Entra en `https://github.com/tu-usuario/tu-repo`.
2. Arriba, pestaña **Settings** (Ajustes).
3. En la columna de la izquierda, **Branches** (Ramas).
4. Botón **Add branch protection rule** (o **Add rule**).
5. En *Branch name pattern* escribe exactamente: `main`
6. Marca **Require a pull request before merging**. Esto obliga a que los
   cambios entren por una propuesta y no directamente. Dentro de esa opción,
   si no hay nadie más que revise, pon *Required approvals* a **0**: no necesitas que otra
   persona apruebe, solo que la revisión automática esté en verde.
7. Marca **Require status checks to pass before merging**.
8. En el buscador que aparece debajo, escribe y selecciona estas tres
   comprobaciones (aparecen en la lista después de que la revisión se haya
   ejecutado al menos una vez):
   - `backend`
   - `frontend`
   - `Construir imágenes Docker`
9. Marca también **Require branches to be up to date before merging**.
10. Guarda con **Create** / **Save changes**.

**Qué cambia en tu día a día:** ya no haces `git push origin main`. Trabajas en
una rama, la subes, abres una *Pull Request* y GitHub no te deja mezclarla
hasta que las tres comprobaciones están en verde. Cuando lo estén, pulsas
*Merge* y ahí sí se despliega.

```bash
git checkout -b arreglo-de-lo-que-sea
# editas
git add -A && git commit -m "..."
git push -u origin arreglo-de-lo-que-sea
# GitHub te da el enlace para abrir la Pull Request; ábrela y espera al verde
```

> **Si algún día necesitas saltártelo** (una urgencia real), en esa misma
> pantalla existe la casilla *Do not allow bypassing the above settings*. Si la
> dejas **sin marcar**, con permisos de administración se puede forzar el merge. Déjala sin
> marcar: es tu salida de emergencia.

#### Ajuste 2 — EasyPanel: que despliegue solo cuando la revisión pase

Ahora mismo EasyPanel despliega en cuanto ve un cambio, sin mirar nada. Lo que
vamos a hacer es apagar eso y dejar que sea GitHub quien le avise, y solo
cuando todo esté en verde.

1. Entra en EasyPanel y abre el proyecto `chatbot`.
2. Abre el servicio y ve a la pestaña donde configuraste el repositorio
   (según la versión se llama **Source**, **Git** o **Deployments**).
3. Busca el interruptor **Auto Deploy** (auto-despliegue) y **apágalo**.
   Con esto, un cambio en GitHub ya no despliega solo.
4. En esa misma pantalla busca la **URL de despliegue por webhook** (aparece
   como *Deploy webhook*, *Webhook URL* o *Deployment URL*). Es una dirección
   larga con un código dentro: quien la conozca puede lanzar un despliegue.
   **Cópiala y trátala como una contraseña.**
5. Vuelve a GitHub → **Settings** → **Secrets and variables** → **Actions** →
   botón **New repository secret**.
   - *Name*: `EASYPANEL_DEPLOY_WEBHOOK`
   - *Secret*: la dirección que copiaste en el paso 4.
   - **Add secret**.

Ya está. A partir de ahí el flujo es: mezclas la Pull Request → se ejecuta la
revisión → si termina en verde, GitHub avisa a EasyPanel y EasyPanel despliega;
si termina en rojo, no avisa a nadie y el servidor se queda con la versión
anterior, funcionando.

De eso se encarga el fichero `.github/workflows/deploy.yml`, que ya está en el
repositorio. **Mientras no crees el secreto del paso 5 no hace nada** (avisa en
el informe y se queda quieto), así que puedes hacer el Ajuste 1 hoy y el 2
cuando quieras — pero mientras no hagas el 2, sigue desplegándose todo, en
verde y en rojo.

#### Cómo comprobar que ha quedado bien

1. Haz un cambio pequeño en una rama y abre una Pull Request.
2. En la propia Pull Request tienen que aparecer las tres comprobaciones, y el
   botón de mezclar tiene que estar **bloqueado** hasta que se pongan verdes.
3. Mézclala y ve a la pestaña **Actions** del repositorio: debe aparecer
   *Desplegar en EasyPanel* justo después de *CI*.
4. En EasyPanel, el servicio debe empezar a reconstruirse en ese momento y no
   antes.

### 7.c Lo que el despliegue automático NO hace por ti

Da igual que despliegue EasyPanel solo o que lo dispare la revisión: estas
cosas siguen siendo tuyas.

- **No rota secretos**. Si cambias `JWT_SECRET`/`ENCRYPTION_KEY`/`POSTGRES_PASSWORD`, hazlo en la pestaña Environment de cada servicio y propaga.
- **No reescribe el prompt de tus agentes**. El seed solo los crea si no existen, y lo hace con la plantilla (§ 5.b). Para cambiarlos, ve a `/admin/agent/agents`.
- **No introduce credenciales de proveedor** (OpenAI, YCloud, etc.). Configúralas en `/admin/connections` tras el primer login.
- **No purga el historial git** si tuvieras secretos comprometidos en commits viejos. Es decisión + acción manual.
- **No mergea ramas**. Si trabajas en una rama, hazle merge a `main` para que el despliegue dispare.

---

## 8. Copias de seguridad

Las copias de la base de datos las hace **la propia aplicación**, no EasyPanel: panel → **Copias de seguridad**. Ahí eliges cada cuánto (cada hora / 6 h / 12 h / diaria / semanal / mensual) y **dónde acaba cada copia**.

**Haz esto el día del despliegue:**

1. **Guarda `ENCRYPTION_KEY` en un gestor de contraseñas.** Las copias se cifran con ella. Vive como variable de entorno de este mismo servidor: si pierdes el servidor —el desastre del que te protege la copia remota— pierdes la clave con él y el fichero cifrado del bucket **no se puede recuperar de ninguna manera**. Si algún día la cambias, las copias anteriores dejan de restaurarse.

   Y aunque puedas descifrar el fichero: **dentro del volcado hay datos que van cifrados otra vez con esa misma clave** — las credenciales de proveedor, los datos personales de las fichas y, desde la migración 0054, las claves de los canales de Instagram y Retell. Restaurar ese volcado en una instalación con otra `ENCRYPTION_KEY` te devuelve la base de datos, pero esos canales se quedan mudos hasta que vuelvas a meter sus claves desde el panel. La clave va **junto** a la copia, no en otro sitio.
2. **Conecta un bucket** (Cloudflare R2, Amazon S3 o cualquier S3-compatible) en la tarjeta *Proveedor de almacenamiento*: endpoint, Access Key ID, Secret y bucket. Pulsa *Probar conexión*.
3. **Elige destino "Bucket"** (recomendado). Cada copia se cifra y se sube; no deja fichero en el disco del servidor. "Ambos" guarda además una copia local; "Servidor" no sube nada — y si el disco se llena, ese día no hay copia de ninguna clase.
4. Pulsa *Hacer copia ahora* y comprueba que sale en verde en el registro.

La app comprueba sola, a diario, que se puede conectar con el bucket y que la última copia se puede descifrar y es un dump de verdad. Si algo falla, te llega un aviso al panel y una notificación push.

Detalle completo, restauración y runbook: `docs/backups.md`.

**Además** (opcional, cinturón y tirantes): EasyPanel → Project → Backups → copia del volumen `db_data`, retención 30 días. Es una copia del volumen entero, sin cifrar y en el mismo proveedor; no sustituye a la del bucket.

---

## 9. Disco lleno en el VPS (runbook)

El panel avisa en **Salud → Almacenamiento** (y en "Logs en vivo") cuando el disco pasa del 85%. Antes de tocar nada, mira ahí los tamaños reales: si la BD y los audios ocupan MB (lo normal), **el culpable es el host, no la app** — casi siempre Docker: caché de builds (cada deploy reconstruye 4 imágenes), imágenes viejas y logs de contenedores.

En el VPS (SSH o terminal de EasyPanel):

```bash
docker system df            # diagnóstico: qué ocupa Docker
docker builder prune -af    # caché de builds (suele ser lo más grande)
docker image prune -af      # imágenes viejas sin contenedor en marcha
```

Es seguro: no toca contenedores en marcha ni volúmenes de datos (BD/audios). La siguiente build tardará más (sin caché).

Si sigue alto:

```bash
# Logs de contenedores sin límite (pueden crecer GBs)
du -sh /var/lib/docker/containers/*/*-json.log | sort -h | tail
# Journal del sistema
journalctl --disk-usage && journalctl --vacuum-size=200M
```

Prevención — limitar los logs de Docker en `/etc/docker/daemon.json` (+ `systemctl restart docker`, reinicia contenedores):

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "3" } }
```

### ¿Y la copia de seguridad de esa noche?

Depende del destino que tengas elegido en panel → Copias de seguridad:

- **Bucket** o **Ambos**: la copia **sí se hace**. Va directa al bucket, sin escribir nada en el disco lleno, y en el registro aparece como copia *degradada* (sin fichero local). No tienes que hacer nada, pero libera espacio igual: el disco lleno acaba tumbando la base de datos.
- **Servidor**: esa copia **no se hace**. Sale un aviso (`backup_skipped_low_disk`) y queda en rojo en el registro. Cuando hayas liberado espacio, entra en el panel y pulsa **Hacer copia ahora**: no esperes a la de mañana, te quedarías un día entero sin copia. Y aprovecha para conectar un bucket y cambiar el destino, que es lo que evita este escenario.

La app limpia sola los dumps locales viejos (por antigüedad y por número) antes de decidir si hay espacio, así que si lo que llenó el disco fueron las propias copias, se resuelve solo.

---

## 10. `TRUSTED_PROXY_COUNT`: cuántos "saltos" hay hasta tu backend

Esta variable parece un detalle y es de las pocas que, mal puesta, **puede
dejar a todo el mundo sin poder entrar al panel**. Merece cinco minutos.

### Qué hace y por qué importa tanto

El backend limita los intentos de login: cinco fallos seguidos **desde la misma
IP** y esa IP se queda fuera un rato. Es lo que impide que alguien pruebe
contraseñas a lo bruto.

El problema es que tu backend nunca ve la IP de quien le escribe: ve la del
proxy que tiene delante (Traefik, el que reparte el tráfico en EasyPanel). La
IP real viaja dentro de una cabecera, `X-Forwarded-For`, que es una lista: cada
proxy por el que pasa la petición añade una entrada.

`TRUSTED_PROXY_COUNT` le dice al backend **cuántas entradas de esa lista puso
tu propia infraestructura**, para saber cuál leer.

- **Si el número es correcto:** el backend lee la IP real. Cada persona tiene
  sus cinco intentos.
- **Si el número se queda corto** (pones 1 y en realidad hay 2, el caso típico
  al activar Cloudflare): la posición que lee es una IP interna, la misma para
  todo el mundo. Entonces los cinco intentos **dejan de ser por persona y pasan
  a ser cinco en total**: si alguien falla la contraseña cinco veces, nadie más
  puede entrar hasta que pase el rato. No hay ningún mensaje que lo explique;
  parece que el login "se ha roto".
- **Si el número se pasa** (pones 2 y solo hay 1): el backend lee una entrada
  que ha puesto quien llama, no tu proxy. Es decir, quien quiera saltarse el
  límite solo tiene que cambiarla en cada intento.

### Cómo saber cuántos saltos tienes de verdad

Cuéntalos hacia atrás desde tu backend. Cada pieza por la que pasa la petición
antes de llegar a él suma uno:

| Tu montaje | Valor |
|---|---|
| EasyPanel con su dominio y su SSL, y nada más delante (**lo normal**) | `1` |
| EasyPanel + Cloudflare con el **proxy activado** (la nube naranja) | `2` |
| EasyPanel + Cloudflare + un nginx tuyo por delante | `3` |
| Sin dominio, entrando por IP y puerto directo | `0` |

**Cloudflare cuenta solo si la nube está naranja.** Si en el panel de DNS de
Cloudflare el registro tiene la nube **gris** (*DNS only*), Cloudflare no toca
el tráfico y no suma: sigue siendo `1`. Naranja (*Proxied*) sí suma.

### Cómo comprobar que has acertado

El backend te lo dice solo. Ve al panel → **Logs en vivo** (o a los registros
del servicio `app` en EasyPanel) y busca:

1. **Al arrancar**, si en producción no has definido la variable, sale un aviso
   diciendo que se está usando el valor por defecto (1). Si tu montaje es de
   los de `1`, no pasa nada; pero es mejor **escribirla explícitamente**, así
   queda claro que es una decisión y no un descuido.
2. **Con tráfico real**, si el número se queda corto aparece un error con el
   texto `ratelimit.trusted_proxy_count.mismatch` y la explicación:
   *"la posición elegida es una IP privada"*. Ese mensaje significa
   **súbelo en uno y vuelve a desplegar**.

> El sistema tiene un apaño interno para que ese fallo no bloquee a todo el
> mundo mientras lo corriges (cuando detecta que la posición cae en una IP
> interna, busca la primera IP pública). Es una red de seguridad, no la
> solución: pon el número bien.

**Dónde se pone:** en el servicio `app`, como el resto de variables. Ejemplo:

```
TRUSTED_PROXY_COUNT=2
```

---

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| «Cannot access repository» al desplegar | El servicio *Compose* no usa el token de GitHub de los ajustes. Repositorio público, o dirección SSH + *deploy key* de solo lectura (paso 1). |
| «required variable POSTGRES_PASSWORD is missing a value» | Pegaste el `.env.example` sin rellenar. Los secretos vienen vacíos a propósito: genéralos (paso 0) y vuelve a pegar las variables. |
| «P1000 Authentication failed» en los logs de `app` | La contraseña del backend no coincide con la que tiene la base de datos. El servicio `db` la sincroniza al arrancar, así que **normalmente basta con volver a desplegar**. Si insiste, comprueba que la generaste con `-hex` y no con `-base64`. |
| `unhealthy, dependency failed to start` en el primer despliegue | El primer arranque migra y siembra datos y puede pasar del minuto. El compose ya da 180 s de margen a `app` y 30 s a `db` y `redis`; si tu servidor va justo, sube esos `start_period`. |
| El backend no pasa a *healthy* | Falta un secreto obligatorio o una URL. El log dice cuál por su nombre: es una comprobación a propósito. |
| `NO SE HAN PODIDO SEMBRAR LOS DATOS INICIALES` y el servicio `app` no arranca | El primer arranque no ha podido crear el usuario administrador, así que el panel se quedaría con un login que no acepta a nadie. Se para a propósito. El log lleva justo debajo las causas y cómo arreglarlo (casi siempre, `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD`). |
| El login no acepta a nadie, ni con la contraseña correcta | Dos sospechosos. (1) `TRUSTED_PROXY_COUNT` mal puesto: el límite de intentos se ha vuelto global (§ 10). (2) El seed nunca llegó a crear el admin: mira el log del primer arranque de `app`. |
| Todo parece bien pero no se contesta a nadie, no se hacen copias y no se transcriben audios | El motor de tareas está parado. Mira `/health`: si dice `"worker":"caido"`, revisa **primero el contenedor `beat`** y luego el `worker`. Si beat muere, el worker está vivo pero sin trabajo que hacer, y el síntoma es idéntico. Los dos tienen comprobación de salud y se reinician solos; si uno se reinicia en bucle, el problema está en sus variables de entorno. |
| Los cambios llegan a producción aunque la revisión automática esté en rojo | Faltan los dos ajustes del § 7.b: protección de la rama en GitHub y auto-despliegue apagado en EasyPanel. |
| El panel carga pero no llama a la API | El build arg `VITE_API_BASE_URL` apunta mal. Hay que **reconstruir** el panel; reiniciarlo no sirve, la URL se hornea al compilar. |
| Errores de CORS en el navegador | `CORS_ALLOWED_ORIGINS` tiene que ser exactamente el dominio del panel, con `https` y **sin barra final**. |
| El login dice **"Credenciales incorrectas"** con la contraseña buena | Casi siempre el panel no llega al backend. Comprueba, por este orden: que el **Host** del dominio de `app` sea solo el nombre (sin `https://` ni barra final), que `https://TU-API/health` conteste `status: ok` desde fuera, y que `CORS_ALLOWED_ORIGINS` sea el dominio del panel. |

---

## Notas de seguridad

- Repo **privado** — no hay riesgo de exposición pública del código.
- `.env` **nunca en git** (verificado).
- Las claves de proveedores (OpenAI, etc.) viven cifradas en la DB, no en variables de entorno del contenedor.
- HTTPS automático con Let's Encrypt.
- Cambia el `INITIAL_ADMIN_PASSWORD` en el primer login.
- Si comprometes alguna clave, regenérala desde el panel admin (al actualizar, se re-cifra).

---

## Anexo — todas las variables, una a una

Para desplegar **no hace falta ninguna de estas**: el `docker-compose.easypanel.yml`
ya trae un valor por defecto que funciona para todas. Están aquí para cuando
quieras cambiar algo concreto (cuánto se guarda cada cosa, la zona horaria de
las métricas, la carpeta de las copias, el comportamiento del agente…): copia la
línea que te interese y añádela a las 12 de tu `.env`.

```bash
# ─────────────────────────────────────────────────────────────────────────
# Chatbot — variables para importar docker-compose.easypanel.yml
# ─────────────────────────────────────────────────────────────────────────
# Cópialo a `.env` junto al compose, o pega estos valores en la pestaña
# Environment del servicio. NUNCA subas a git el `.env` con valores reales:
# este `.example` va a git con los secretos vacíos a propósito.
#
# CÓMO LEER ESTE ARCHIVO
#   [OBLIGATORIA]  sin ella el despliegue PARA y te dice cuál falta.
#   [OPCIONAL]     si no la pones, se queda con el valor indicado. Todas las
#                  opcionales tienen un valor por defecto que funciona: no
#                  hace falta tocar ninguna para arrancar.
#
# Las claves de proveedores (OpenAI, YCloud/WhatsApp, Instagram, Resend,
# Telegram, Slack…) NO van aquí: el chatbot ARRANCA sin ellas (modo degradado)
# y se configuran luego desde el panel → Conexiones, cifradas en la base de
# datos. Las únicas excepciones son las de OAuth, más abajo.
# Guía completa: DEPLOY_EASYPANEL.md


# ═════════════════════════════════════════════════════════════════════════
# 1. LO MÍNIMO PARA ARRANCAR (7 variables obligatorias)
# ═════════════════════════════════════════════════════════════════════════

# --- Dominios (los tuyos, con https, SIN barra final) ---
# [OBLIGATORIA] API (servicio `app`, puerto 8000).
APP_BASE_URL=https://api.chat.tucliente.com
# [OBLIGATORIA] Panel (servicio `frontend`, puerto 80).
FRONTEND_BASE_URL=https://chat.tucliente.com
# [RECOMENDADA] Servidor MCP (servicio `mcp`, puerto 8080), SIN el /mcp final.
# Es lo que el panel enseña en Conexiones → API / MCP para conectar agentes
# (Claude, n8n). Si la dejas vacía, el panel enseña la URL del backend,
# que no es donde vive el MCP y no funcionará.
MCP_BASE_URL=https://mcp.chat.tucliente.com

# --- Base de datos (servicio `db`) ---
# [OBLIGATORIA] Genera con: openssl rand -hex 24
#
# ⚠️ Con -hex, NUNCA con -base64. Esta contraseña viaja dentro de la URL de
# conexión a Postgres, y el base64 mete símbolos como "+" o "/" que la rompen:
# el backend no puede entrar y verás "P1000 Authentication failed" con los
# servicios reiniciándose en bucle. Con -hex solo salen números y letras a-f.
POSTGRES_PASSWORD=

# --- Secretos de seguridad ---
# [OBLIGATORIA] Firma de sesiones (JWT). Mínimo 32 caracteres; el backend
# aborta el arranque si es más corto. Va suelto, no dentro de una URL, así que
# aquí el base64 sí vale. Genera con: openssl rand -base64 48
JWT_SECRET=

# [OBLIGATORIA] Clave con la que se cifran, en la base de datos, las claves de
# proveedores que guardes desde el panel — y también las copias de seguridad.
# Formato Fernet (32 bytes en base64). Genera con:
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#
# GUÁRDALA EN UN GESTOR DE CONTRASEÑAS el mismo día del despliegue. Si la
# pierdes, las copias cifradas del bucket NO se pueden recuperar de ninguna
# manera. Si la cambias, hay que volver a introducir TODAS las claves de
# proveedores desde el panel y las copias anteriores dejan de restaurarse.
ENCRYPTION_KEY=

# --- Admin inicial (se crea en el primer arranque si la BD está vacía) ---
# [OBLIGATORIA] No valen los de ejemplo: el backend rechaza `admin@local` y
# `ChangeMe123!` en producción. Cambia la contraseña en el primer login.
INITIAL_ADMIN_EMAIL=admin@tucliente.com
# [OBLIGATORIA] Mínimo 10 caracteres, con letras y números.
INITIAL_ADMIN_PASSWORD=


# ═════════════════════════════════════════════════════════════════════════
# 2. BASE DE DATOS — nombres (casi nunca hay que tocarlos)
# ═════════════════════════════════════════════════════════════════════════

# [OPCIONAL] Por defecto: chatbot (los dos).
# Solo defínelos si tu instalación ya existía con otros nombres: el volumen
# conserva los de la primera vez y cambiarlos aquí NO migra los datos.
# POSTGRES_USER=chatbot
# POSTGRES_DB=chatbot


# ═════════════════════════════════════════════════════════════════════════
# 3. RED Y SEGURIDAD
# ═════════════════════════════════════════════════════════════════════════

# [OPCIONAL] Por defecto: el valor de FRONTEND_BASE_URL, y solo ese.
# Dominios que pueden llamar a la API desde un navegador. Añade otro (separado
# por comas) solo si sirves el widget de chat desde la web del cliente.
# Con https y SIN barra final, o el navegador dará error de CORS.
# CORS_ALLOWED_ORIGINS=https://chat.tucliente.com,https://www.tucliente.com

# [OPCIONAL] Por defecto: 1
# Cuántos proxies hay por delante del backend. Sirve para saber cuál es la IP
# real de quien llama (la que se usa para limitar los intentos de login).
#
# ⚠️ SI ESTE NÚMERO NO CUADRA CON LA REALIDAD, EL LÍMITE DE INTENTOS DE LOGIN
# SE VUELVE GLOBAL: en vez de contar 5 intentos por persona, cuenta 5 en total
# para todo el mundo junto, y a partir del sexto NADIE puede entrar al panel.
# Cómo saber cuántos hay de verdad en tu instalación: DEPLOY_EASYPANEL.md § 10.
# TRUSTED_PROXY_COUNT=1


# ═════════════════════════════════════════════════════════════════════════
# 4. IDENTIDAD Y PRESENTACIÓN
# ═════════════════════════════════════════════════════════════════════════

# [OPCIONAL] Por defecto: Chatbot
# Nombre visible del producto: título de la API, correos que salen del sistema
# y páginas de OAuth. Cada instalación pone el suyo.
# APP_NAME=Chatbot

# [OPCIONAL] Por defecto: Europe/Madrid
# Zona horaria del negocio. Es la que se usa para agrupar las métricas del
# Inicio ("hoy", "ayer", por horas). Sin esto se leerían en UTC y no cuadraría
# con lo que ve el cliente. Formato: Continente/Ciudad.
# DASHBOARD_TIMEZONE=Europe/Madrid


# ═════════════════════════════════════════════════════════════════════════
# 5. ALMACENAMIENTO EN DISCO
# ═════════════════════════════════════════════════════════════════════════
# ⚠️ Las tres rutas cuelgan de /data/audios A PROPÓSITO: ese es el ÚNICO
# volumen persistente montado en `app` y `worker`. Cualquier ruta fuera de ahí
# se PIERDE al recrear el contenedor (y con ella, las copias).

# [OPCIONAL] Por defecto: /data/audios
# Notas de voz que llegan de los clientes.
# AUDIO_STORAGE_PATH=/data/audios

# [OPCIONAL] Por defecto: /data/audios/uploads
# Ficheros que envía el operador desde el panel (audios, imágenes, documentos).
# UPLOADS_PATH=/data/audios/uploads

# [OPCIONAL] Por defecto: /data/audios/backups
# Copias de seguridad de la base de datos guardadas en el propio servidor.
# BACKUP_DIR=/data/audios/backups


# ═════════════════════════════════════════════════════════════════════════
# 6. COPIAS DE SEGURIDAD
# ═════════════════════════════════════════════════════════════════════════
# El destino (bucket / servidor / ambos), la frecuencia y las credenciales del
# bucket se configuran DESDE EL PANEL → Copias de seguridad, no aquí. Estas
# variables solo controlan la limpieza de las copias que quedan en el servidor.

# [OPCIONAL] Por defecto: 7
# Días que se conserva cada copia local antes de borrarla.
# BACKUP_RETENTION_DAYS=7

# [OPCIONAL] Por defecto: 14
# Tope de copias locales, ADEMÁS de la retención por días. Sin tope, una
# frecuencia horaria acumula 168 ficheros antes de que el primero cumpla 7
# días y llena el disco. 0 = sin tope (no recomendado).
# BACKUP_RETENTION_MAX_FILES=14

# [OPCIONAL] Por defecto: 2.0
# Si quedan menos GB libres que esto, NO se escribe la copia en el servidor
# (llenar el disco tumbaría la base de datos) y salta un aviso. Con destino
# "bucket" la copia se hace igual, en streaming y sin fichero local.
# BACKUP_MIN_FREE_GB=2.0

# [OPCIONAL] Por defecto: se deriva del nombre de la base de datos.
# Prefijo de los ficheros de copia (<prefijo>_<fecha>.dump).
# BACKUP_FILE_PREFIX=


# ═════════════════════════════════════════════════════════════════════════
# 7. CUÁNTO SE GUARDA CADA COSA (retenciones — protección de datos)
# ═════════════════════════════════════════════════════════════════════════
# Todas se miden en días salvo donde se diga. Una tarea diaria hace la
# limpieza. 0 = desactivar esa limpieza (los datos se guardan para siempre).

# [OPCIONAL] Por defecto: 270 (9 meses)
# Días que se guarda el FICHERO de una nota de voz. La transcripción se
# conserva; lo que se borra es el audio, que es lo pesado y lo sensible.
# AUDIO_RETENTION_DAYS=270

# [OPCIONAL] Por defecto: 180
# Días que se guardan los adjuntos (imágenes, vídeos, documentos), tanto los
# que manda el cliente como los que envía el operador. La fila del mensaje se
# conserva: en el histórico sigue poniendo "imagen recibida" con su fecha.
# MEDIA_RETENTION_DAYS=180

# [OPCIONAL] Por defecto: 6 MESES (esta va en meses, no en días)
# Pasado ese tiempo se vacía el CONTENIDO de los correos guardados: Gmail es
# el archivo, la base de datos solo una copia de trabajo. Se conserva el
# asunto y la referencia para poder recuperarlo desde Gmail.
# EMAIL_RETENTION_MONTHS=6

# [OPCIONAL] Por defecto: 30
# Días que se guardan las trazas del agente. Son cortas a propósito: llevan
# dentro trozos de mensajes de clientes en claro. Sirven para depurar lo de
# esta semana, no como archivo.
# TRACE_RETENTION_DAYS=30

# [OPCIONAL] Por defecto: 90 · Consumo y coste de las llamadas al modelo.
# LLM_USAGE_RETENTION_DAYS=90

# [OPCIONAL] Por defecto: 365 · Registro de auditoría (quién hizo qué).
# AUDIT_RETENTION_DAYS=365

# [OPCIONAL] Por defecto: 180 · Envíos masivos ya terminados.
# OUTBOUND_RETENTION_DAYS=180

# [OPCIONAL] Por defecto: 180
# Huecos de conocimiento YA resueltos. Los pendientes no caducan nunca.
# RESOLVED_GAP_RETENTION_DAYS=180


# ═════════════════════════════════════════════════════════════════════════
# 8. COMPORTAMIENTO DEL AGENTE
# ═════════════════════════════════════════════════════════════════════════
# Estos son los valores POR DEFECTO de las configuraciones nuevas. Un agente
# ya creado se edita desde el panel, no desde aquí.

# [OPCIONAL] Por defecto: gpt-5.4-mini
# DEFAULT_LLM_MODEL=gpt-5.4-mini

# [OPCIONAL] Por defecto: 8
# Segundos que el agente espera tras el último mensaje antes de contestar. La
# gente escribe en ráfagas: sin esta espera, el agente responde a cada trozo
# por separado. Más segundos agrupan mejor; menos responden antes.
# MESSAGE_BUFFER_SECONDS=8

# [OPCIONAL] Por defecto: 6
# Suelo de esa espera para los canales de texto. Evita que un agente mal
# configurado conteste a trocitos. Las llamadas de voz no aplican este suelo.
# MIN_TEXT_BUFFER_SECONDS=6

# [OPCIONAL] Por defecto: 3
# En cuántos mensajes seguidos como máximo se parte una respuesta larga.
# RESPONSE_SPLIT_MAX_PARTS=3

# [OPCIONAL] Por defecto: true
# Si lo pones en false, las conversaciones nuevas nacen en "humano" y el
# agente NO contesta solo. Útil mientras entrenas al agente.
# AGENT_AUTORESPONSE_DEFAULT=true


# ═════════════════════════════════════════════════════════════════════════
# 9. OAUTH — las únicas claves de proveedor que van en variables
# ═════════════════════════════════════════════════════════════════════════
# El resto de claves van en el panel → Conexiones. Estas dos parejas pueden
# ir aquí porque hacen falta para montar el propio botón de "conectar".
# También se pueden gestionar desde Admin → Credenciales.

# [OPCIONAL] Google (login con Google, Gmail y Calendar). Una sola app de
# Google Cloud Console sirve para los tres. La URL de retorno que autorices
# allí tiene que coincidir EXACTAMENTE con:
#   ${APP_BASE_URL}/api/v1/oauth/google/callback     (servicios)
#   ${APP_BASE_URL}/api/v1/auth/google/callback      (login)
# GOOGLE_OAUTH_CLIENT_ID=
# GOOGLE_OAUTH_CLIENT_SECRET=

# [OPCIONAL] Instagram Business Login (app de Meta). Son el mismo par de
# credenciales que el App ID / App Secret de Facebook. Cuando los rotes,
# cámbialos en los dos sitios. Cómo se conecta el canal y cómo se configura
# su webhook: DEPLOY_EASYPANEL.md § 6.b.
# INSTAGRAM_OAUTH_CLIENT_ID=
# INSTAGRAM_OAUTH_CLIENT_SECRET=


# ═════════════════════════════════════════════════════════════════════════
# 10. AVISOS AL MÓVIL Y DIAGNÓSTICO
# ═════════════════════════════════════════════════════════════════════════

# [OPCIONAL] Por defecto: se deriva del correo del admin.
# Contacto del responsable que exigen los servicios de notificaciones push
# (es un requisito suyo, no se usa para nada más). Admite "mailto:algo@..."
# o un correo a secas.
# VAPID_SUBJECT=mailto:tu@correo.com

# [OPCIONAL] Por defecto: false
# Vuelca en "Logs en vivo" la estructura de los mensajes que llegan de
# Instagram. Es para depurar un problema concreto: enciéndelo, mira, apágalo.
# No vuelca el contenido de los mensajes, solo su forma.
# IG_WEBHOOK_DEBUG=false
```
