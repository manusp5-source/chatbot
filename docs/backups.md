# Backups de la base de datos

> ## ⚠️ Lo primero: guarda `ENCRYPTION_KEY` fuera del servidor
>
> Las copias se cifran con `ENCRYPTION_KEY`, que es una variable de entorno
> del contenedor, configurada en el panel de EasyPanel **del mismo servidor**.
> Si pierdes el servidor —el desastre exacto contra el que protege la copia
> remota— pierdes la clave con él, y el fichero cifrado del bucket es ruido
> irrecuperable. **Cópiala a un gestor de contraseñas el día del despliegue.**
> Si la cambias, las copias anteriores dejan de poder restaurarse.

Backups automáticos de Postgres mediante la tarea Celery
`app.tasks.backup_db.backup_db`. La frecuencia es
**configurable desde el panel** (Admin → Copias de seguridad): cada hora / 6 h
/ 12 h / diaria / semanal / mensual (defecto diaria a las 04:00 Europe/Madrid;
las de "días" corren a la hora que elijas). El beat dispara `backup_tick` cada
10 min y este decide contra la config de BD si toca copia.

Si en el panel hay un bucket S3-compatible configurado (Cloudflare R2 / Amazon
S3 / MinIO, claves `backup_s3_*` en credenciales), la copia se **cifra con
AES-256-GCM** (clave derivada de `ENCRYPTION_KEY`) y se sube a
`<bucket>/chatbot/<fecha>.dump.enc` con retención automática (7 diarias + 4
semanales; con más frecuencia, además las últimas 24-48). Cada intento queda
registrado en `backup_runs` y se ve en el panel, que también ofrece "Hacer
copia ahora", "Descargar última copia" (descifrada) y "Restaurar…" con doble
confirmación (`pg_restore --clean --single-transaction`).

### Dónde acaba cada copia (selector "Dónde se guarda cada copia")

| Destino | Con espacio en disco | Sin espacio en disco |
|---|---|---|
| **Servidor** | Dump a fichero en `BACKUP_DIR`. No se sube nada. | No hay copia: se avisa y se registra el fallo (sin bucket no hay dónde salvarla). |
| **Bucket** (recomendado) | Copia **directa al bucket**: pg_dump escribe por su salida estándar, se cifra por bloques y se sube en varias partes. No deja fichero en el servidor. | Igual. Además se avisa del disco. |
| **Ambos** | Dump a fichero + subida desde el fichero. | Copia directa al bucket, marcada como **degradada** (sin fichero local por falta de espacio). |

El guardia de disco (`BACKUP_MIN_FREE_GB`) solo bloquea la **escritura local**.
Nunca bloquea la subida: si hay bucket, la copia se hace igual. Es un fallo
fácil de cometer y caro: con destino bucket y el disco lleno, un guardia que
bloquease todo dejaría al sistema **sin ninguna copia**.

### Formatos del fichero cifrado

- `EKB1` — histórico, un solo bloque AES-GCM. Se sigue descifrando siempre.
- `EKB2` — por bloques (8 MB, `BACKUP_STREAM_CHUNK_MB`). Es el que permite
  cifrar y subir sin fichero intermedio, con un pico de memoria de un bloque
  en vez del doble del tamaño del dump. Cada bloque va atado a su posición y
  el último va marcado: un fichero cortado **no** descifra.

La descarga y la restauración distinguen los dos formatos solos.

> ⚠️ pg_dump/pg_restore de la imagen: `postgresql-client-17` (la BD de
> producción es pgvector **pg17**; pg_dump aborta si su versión es menor que
> la del server).

## Qué se copia y qué no

**SÍ se copia** (dump completo de la BD con `pg_dump --format=custom`):

- Conversaciones y mensajes (todos los canales).
- Contactos y notas/actividad del CRM.
- Credenciales cifradas del panel de administración (cifradas con
  `ENCRYPTION_KEY`: para restaurarlas en otro despliegue hace falta la MISMA
  clave en el `.env`/config del deployment).
- Configuración del agente, usuarios, knowledge base (incluidos los embeddings
  pgvector), logs de auditoría, etc. — toda tabla de la BD.

**NO se copia:**

- Los **audios** y uploads (`/data/audios`, `/data/audios/uploads`): viven en el
  volumen `audios_data`, fuera de la BD.
- **Redis**: solo contiene estado efímero (colas Celery, buffers, throttles);
  se reconstruye solo.
- El `.env` / variables de deployment (en particular `ENCRYPTION_KEY` y
  `JWT_SECRET`): guárdalos aparte en un gestor de secretos.

## Dónde viven los backups

```
/data/audios/backups/<prefijo>_YYYYMMDD_HHMMSS.dump   (timestamp en UTC)
```

dentro de los contenedores **app** y **worker** (volumen persistente
`audios_data`, el único montado — por eso comparten ruta con los audios, igual
que `UPLOADS_PATH`).

(solo con destino "Servidor" o "Ambos": con destino "Bucket" no queda fichero).

- **Retención local**: por antigüedad, `BACKUP_RETENTION_DAYS` (7 días), **y
  por número**, `BACKUP_RETENTION_MAX_FILES` (14 ficheros). El tope por número
  es imprescindible con frecuencias altas: cada hora son 168 dumps antes de
  que el primero cumpla 7 días, y la retención por edad no libera un byte.
  La limpieza corre **antes** del guardia de disco (si lo que llenó el disco
  son dumps viejos, se limpian antes de decidir que no cabe la copia de hoy)
  y otra vez después del volcado. El dump más reciente nunca se borra.
- **Guardia de disco**: si quedan menos de `BACKUP_MIN_FREE_GB` (por defecto
  2 GB) libres, NO se escribe el dump a fichero y se alerta
  (`backup_skipped_low_disk`). Con bucket configurado la copia se hace igual,
  directa al bucket (ver tabla de destinos). Un fallo del dump alerta como
  `backup_failed`, intenta el rescate directo al bucket (destino "Ambos") y se
  reintenta una vez a los 5 minutos.

## Que la copia valga algo (verificación)

- **Tras cada subida** se comprueba el tamaño real del objeto en el bucket. Una
  subida cortada ya no se registra en verde.
- **Una vez al día** (lo dispara `backup_tick`, con candado en Redis):
  1. prueba de conexión al bucket — si el token de Cloudflare caduca se sabe
     ese día, no la noche que falle la copia;
  2. verificación real de la última copia: se descarga, se descifra y se
     comprueba que empieza por la cabecera de un dump de PostgreSQL. Queda en
     el histórico como tipo *Verificación*.
- También a mano: `POST /admin/backups/verify`.

## Avisos

Las alertas de copias van al panel (**Monitorización → Logs en vivo**) y,
además, como **notificación push a la PWA** del operador en los casos que
importan: copia fallida, copia saltada por disco, subida fallida, copia
degradada, bucket inalcanzable y última copia no restaurable. No hay
integración con Telegram ni con Slack (se eliminó del producto: los avisos no
salen de la aplicación).

## Runbook de restauración

> ⚠️ La restauración **PISA los datos actuales** de la BD (`--clean`). Hazla
> con el agente parado para que no entren escrituras a mitad.

Todos los comandos se ejecutan por SSH en el servidor (host Docker/EasyPanel).

**1. Localiza los contenedores** (los nombres reales varían según el deploy):

```bash
docker ps --format '{{.Names}}' | grep -Ei 'db|backend|worker'
# ejemplo de salida:
#   <proyecto>_db.1.xxxx        ← Postgres (pgvector/pg16)
#   <proyecto>_backend.1.xxxx   ← API
#   <proyecto>_worker.1.xxxx    ← Celery worker (tiene los backups montados)
```

**2. Para el worker y la API** (evita escrituras durante la restauración). En
EasyPanel: botón *Stop* de los servicios app y worker. A mano:

```bash
docker stop <contenedor_backend> <contenedor_worker>
# (si paras el worker, copia ANTES el dump fuera, ver paso 3 con docker cp
#  directo al volumen, o usa la API en su lugar — ambos montan el volumen)
```

**3. Saca el dump del volumen al host y mételo en el contenedor de la BD:**

```bash
# listar los backups disponibles (vale cualquier contenedor que monte el volumen)
docker exec <contenedor_worker> ls -lh /data/audios/backups/

# volumen → host
docker cp <contenedor_worker>:/data/audios/backups/<dump>.dump /tmp/

# host → contenedor de la BD
docker cp /tmp/<dump>.dump <contenedor_db>:/tmp/
```

**4. Restaura** (el usuario y la base de datos los tienes en la
`DATABASE_URL` del deployment):

```bash
docker exec -it <contenedor_db> pg_restore \
  -U <usuario> -d <base_de_datos> \
  --clean --if-exists --no-owner \
  /tmp/<dump>.dump
```

- `--clean --if-exists`: borra cada objeto antes de recrearlo (sin fallar si
  no existe) — restauración idempotente sobre una BD viva.
- `--no-owner`: no intenta recrear los owners originales (todo queda del
  usuario con el que conectas).
- Avisos tipo `errors ignored on restore` sobre `DROP EXTENSION vector` o
  esquemas de sistema son normales con `--clean`; lo importante es que las
  tablas queden pobladas.

**5. Verifica y rearranca:**

```bash
docker exec -it <contenedor_db> psql -U <usuario> -d <base_de_datos> \
  -c "select count(*) from message;"
docker start <contenedor_backend> <contenedor_worker>
```

Entra al panel y comprueba que conversaciones/contactos están. Las
credenciales cifradas solo descifran si `ENCRYPTION_KEY` es la misma que
cuando se hizo el backup.

## Descargar una copia fuera del servidor

Lo normal es hacerlo desde el panel ("Descargar última copia": la baja del
bucket y la descifra). A mano, contra el fichero local del servidor:

```bash
# Opción A — desde tu máquina, en un solo paso (ssh + docker cp en el server):
ssh root@SERVIDOR "docker cp \$(docker ps -qf name=worker | head -1):/data/audios/backups/<dump>.dump /tmp/" \
  && scp root@SERVIDOR:/tmp/<dump>.dump ./backups/

# Opción B — directamente del volumen en el host (sin pasar por el contenedor):
ssh root@SERVIDOR
docker volume inspect audios_data --format '{{.Mountpoint}}'
# → /var/lib/docker/volumes/audios_data/_data
exit
scp root@SERVIDOR:/var/lib/docker/volumes/audios_data/_data/backups/*.dump ./backups/
```

## Limitaciones que quedan (importante)

Una copia que vive en **el mismo disco del mismo servidor** que la BD protege
contra borrados accidentales, migraciones que salen mal y corrupción lógica,
pero **NO contra la pérdida total del servidor** (disco roto, borrado del VPS,
ransomware del host). Por eso el destino recomendado es el bucket: conecta
Cloudflare R2 en Admin → Copias de seguridad → *Proveedor de almacenamiento* y
elige "Bucket" o "Ambos". Sin bucket, descarga copias a mano periódicamente.

Y con bucket queda un cabo suelto que solo puedes atar tú: **la clave de
cifrado vive en el mismo servidor** (aviso del principio de este documento).
Copia `ENCRYPTION_KEY` a un gestor de contraseñas o la copia remota no te
servirá el día que pierdas la máquina.
