"""Ingesta de correo entrante de Gmail (F5a — solo lectura).

`sync_incoming_emails()` es el corazón del polling: lo llama la task periódica
`poll_gmail` cada 2 minutos. Trae los correos nuevos del INBOX de la cuenta
Gmail conectada y los persiste como conversaciones (agrupadas por hilo) vía
`store_incoming`.

F5c (importante): tras `store_incoming`, este servicio encola el runtime del
agente (`task_process.delay`) igual que los webhooks de WhatsApp/Instagram. Para
el canal Email el runtime NUNCA envía: genera un BORRADOR REAL en Gmail para
revisión humana (ver services/conversation.py rama email). El gating (canal
pausado / status != bot / cuarentena) lo aplica el propio runtime; aquí no se
duplica.

F5b (sincronizar "Enviados"): un correo cuyo From es la propia cuenta no se
ignora sin más. Si es un envío real (etiqueta SENT y NO DRAFT), lo ingerimos
como mensaje SALIENTE en el hilo ya seguido (`store_outgoing_email`) para que el
hilo quede completo respondamos donde respondamos. Los BORRADORES (DRAFT) — p.
ej. los del agente de F5c — se SALTAN: un borrador no es un envío.

Estrategia de sincronización:
  - Primera vez (no hay last_history_id en Channel.config) → BOOTSTRAP:
    list_messages(q="in:inbox newer_than:1d -in:spam -in:promotions") y se ancla
    el historyId actual (de get_profile).
  - Siguientes → list_history(last_history_id) para traer solo lo nuevo.
  - Si history.list devuelve 404 (historyId caducado) → re-bootstrap re-anclando
    al historyId actual SIN reprocesar histórico (evita duplicar).

Idempotencia: confiamos en el check de `Message.extra["provider_message_id"]`
que ya hace `store_incoming` (el message_id de Gmail es estable).

Concurrencia: lock Redis (SET NX + TTL ~110s) para que dos beats no se solapen.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.channel import Channel, ChannelType
from app.providers.gmail import get_gmail_provider, parse_message_to_incoming
from app.services.conversation import store_incoming, store_outgoing_email
from app.services.runtime_logs import push_runtime_log
from app.tasks.process_message import process_message as task_process

logger = get_logger(__name__)

# Query de bootstrap: solo INBOX reciente, excluye spam y la pestaña de
# promociones (decisión del dueño: una sola cuenta, solo INBOX).
BOOTSTRAP_QUERY = "in:inbox newer_than:1d -in:spam -in:promotions"

# Etiquetas que descartan un correo ENTRANTE (E4). `history.list` devuelve todo
# mensaje añadido a CUALQUIER etiqueta, así que por ahí entraban SPAM, la
# papelera y las pestañas de Gmail — cosas que Google YA había apartado y que
# aquí se convertían en contacto + conversación. El bootstrap sí filtraba
# (`in:inbox -in:spam -in:promotions`); esto pone el camino incremental a la par.
INBOUND_EXCLUDED_LABELS = {"SPAM", "TRASH", "DRAFT", "CATEGORY_PROMOTIONS"}
# Y además exigimos INBOX, igual que el `in:inbox` del bootstrap.
INBOUND_REQUIRED_LABEL = "INBOX"

# Cola de reintentos (E1). Vive en Channel.config para que sobreviva a
# reinicios: si se perdiera, el correo se perdería con ella. Gmail NO vuelve a
# ofrecer un mensaje ya cubierto por el historyId, así que un fallo al
# procesarlo era un correo perdido para siempre.
RETRY_QUEUE_KEY = "ingest_retry_queue"
RETRY_QUEUE_MAX = 200

_LOCK_KEY = "gmail:ingest:lock"
_LOCK_TTL = 110  # seg — menor que la frecuencia de polling (120s)

# Compare-and-delete atómico: solo borra el lock si el valor sigue siendo el
# nuestro. Sin esto, una corrida que se pasa del TTL soltaba el lock que ya
# había cogido el beat siguiente y acababa habiendo DOS ingestas a la vez
# pisándose el last_history_id.
_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _new_lock_token() -> str:
    """Valor único por corrida: es lo que identifica al dueño del lock."""
    return uuid.uuid4().hex


async def _release_lock(redis, token: str | None) -> None:
    """Suelta el lock SOLO si sigue siendo nuestro (best-effort).

    `token=None` significa que nunca llegamos a tener el lock (Redis falló al
    adquirirlo y seguimos igualmente): entonces no hay nada que soltar y desde
    luego no el de otro.
    """
    if not token:
        return
    try:
        await redis.eval(_RELEASE_LOCK_LUA, 1, _LOCK_KEY, token)
    except Exception as e:
        # Si no se puede soltar, el TTL lo hace por nosotros.
        logger.warning("gmail.sync.unlock_failed", error=str(e))


async def _load_email_channel() -> Channel | None:
    """Canal de email activo. Si hay MÁS DE UNO, coge el más antiguo y avisa.

    Antes esto era `scalar_one_or_none()`, que LANZA con dos filas. Y con dos
    filas se acaba: basta borrar el canal y volver a provisionarlo dejando el
    viejo activo. A partir de ese momento el sondeo reventaba en cada ciclo y no
    entraba ni un correo, en silencio. Un duplicado de configuración no puede
    tumbar la ingesta: elegimos uno de forma determinista (el más antiguo, que
    es el que tiene el last_history_id bueno) y dejamos el aviso.
    """
    async with db_session() as db:
        rows = (
            await db.execute(
                select(Channel)
                .where(
                    Channel.type == ChannelType.email,
                    Channel.enabled.is_(True),
                )
                .order_by(Channel.created_at.asc(), Channel.id.asc())
            )
        ).scalars().all()
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning("gmail.sync.multiple_channels", count=len(rows))
        await push_runtime_log(
            level="warn",
            event="gmail.sync.multiple_channels",
            message=(
                f"Hay {len(rows)} canales de Email activos. Se usa el más "
                "antiguo; borra los duplicados en Conexiones."
            ),
            channel="email",
        )
    return rows[0]


async def _save_history_id(
    channel_id,
    history_id: str,
    account_email: str | None = None,
    retry_queue: list[dict] | None = None,
) -> None:
    """Persiste el last_history_id (y opcionalmente el email y la cola de
    reintentos) en Channel.config."""
    async with db_session() as db:
        ch = (
            await db.execute(select(Channel).where(Channel.id == channel_id))
        ).scalar_one_or_none()
        if not ch:
            return
        cfg = dict(ch.config or {})
        if history_id:
            cfg["last_history_id"] = str(history_id)
        if account_email:
            cfg["gmail_email_address"] = account_email
        if retry_queue is not None:
            # Tope de tamaño: una cola desbocada no puede engordar el JSONB del
            # canal sin freno. Nos quedamos con los más recientes.
            cfg[RETRY_QUEUE_KEY] = retry_queue[-RETRY_QUEUE_MAX:]
        ch.config = cfg
        await db.commit()


def _is_relevant_inbound(labels: set[str]) -> bool:
    """¿Este correo entrante debe entrar al panel? (E4)

    Mismo criterio que la query de bootstrap: tiene que estar en INBOX y no
    estar en SPAM, papelera, borradores ni en la pestaña Promociones. Todo lo
    que Google ya apartó se queda fuera.
    """
    if labels & INBOUND_EXCLUDED_LABELS:
        return False
    # Correos muy antiguos ingeridos por el bootstrap podrían no traer etiquetas
    # (respuestas parciales de la API): si no sabemos nada, no descartamos.
    if labels and INBOUND_REQUIRED_LABEL not in labels:
        return False
    return True


async def _process_message_ids(
    message_ids: list[str], account_email: str
) -> tuple[int, dict[str, str]]:
    """Trae cada mensaje, lo parsea y lo almacena. Devuelve
    `(almacenados, fallidos)`, donde `fallidos` es {message_id: motivo}.

    Los fallidos NO se dan por perdidos: el llamante los mete en la cola de
    reintentos persistente. Antes se logueaba el error y se seguía, y como el
    ancla de historial avanzaba igual, Gmail no volvía a ofrecer ese correo
    nunca más.

    Ruteo (F5b):
      - Saliente (From == cuenta conectada): miramos las etiquetas Gmail.
        - DRAFT (o sin SENT) → SE SALTA: un borrador NO es un envío. Así NO
          ingerimos los borradores que el agente crea en Gmail (F5c).
        - SENT y NO DRAFT → es una respuesta enviada (desde Gmail o desde el
          panel). La ingerimos como mensaje SALIENTE en el hilo ya seguido vía
          store_outgoing_email (idempotente; NO encola el agente).
      - Entrante (From != cuenta) → flujo de siempre: store_incoming + encolar
        el runtime del agente.
    """
    if not message_ids:
        return 0, {}
    gmail = get_gmail_provider()
    stored = 0
    failed: dict[str, str] = {}
    account = (account_email or "").lower()
    for mid in message_ids:
        # Cada correo se procesa de forma AISLADA: si uno falla (dirección
        # larguísima, payload raro, etc.) lo saltamos y seguimos. Antes una
        # excepción aquí abortaba TODO el sondeo y dejaba de entrar cualquier
        # correo (p.ej. un rebote de mailer-daemon con dirección >40 car.).
        try:
            raw = await gmail.get_message(mid)
            if not raw:
                # Casi siempre es que el token de Google se ha caído a mitad de
                # la pasada. Con un `continue` a secas el correo NO entraba en
                # la cola de reintentos y el ancla avanzaba igual: Gmail no
                # vuelve a ofrecer ese id, así que ese correo se perdía. Ahora
                # cuenta como fallo y se reintenta en la pasada siguiente.
                failed[mid] = "Gmail no devolvió el mensaje (¿credenciales caídas?)"
                logger.warning("gmail.get_message.vacio", message_id=mid)
                continue
            sender = (raw.get("from_addr") or "").lower()
            labels = set(raw.get("label_ids") or [])

            # ---------- SALIENTE: el From es la propia cuenta conectada ----------
            if account and sender == account:
                # Un borrador NO es un envío: lo saltamos (cubre los borradores del
                # agente de F5c, etiquetados DRAFT). Solo ingerimos lo que tiene
                # SENT y NO DRAFT (una respuesta realmente enviada).
                if "DRAFT" in labels or "SENT" not in labels:
                    logger.info("gmail.skip_draft_or_unsent", message_id=mid)
                    continue
                incoming = parse_message_to_incoming(raw)
                msg_id = await store_outgoing_email(incoming, list(labels))
                if msg_id is not None:
                    stored += 1
                    # NUNCA encolamos el agente para un saliente nuestro.
                continue

            # ---------- ENTRANTE: correo de un cliente ----------
            # E4: el camino incremental (history.list) devuelve TODO mensaje
            # añadido a cualquier etiqueta. Filtramos aquí con el mismo criterio
            # que la query de bootstrap para no meter spam ni promociones.
            if not _is_relevant_inbound(labels):
                logger.info("gmail.skip_filtered_label", message_id=mid)
                continue
            incoming = parse_message_to_incoming(raw)
            msg_id = await store_incoming(incoming)
            if msg_id is not None:
                stored += 1
                # F5c: encolamos el runtime del agente para este correo, igual que
                # los webhooks de WhatsApp/Instagram (mismo task `process_message`).
                # El gating (canal pausado / status != bot / cuarentena) lo aplica el
                # propio runtime en conversation.py; aquí no lo duplicamos. Para
                # email, el runtime NUNCA envía: genera un BORRADOR para revisión.
                task_process.delay(str(msg_id))
        except Exception as e:
            logger.error("gmail.process_message.failed", message_id=mid, error=str(e))
            failed[mid] = str(e)[:300]
            continue
    return stored, failed


async def _merge_retry_queue(
    previous: list[dict], attempted_ids: list[str], failed: dict[str, str]
) -> list[dict]:
    """Actualiza la cola de reintentos tras una pasada (E1).

    - Lo que estaba en la cola y ESTA VEZ salió bien, sale de la cola.
    - Lo que vuelve a fallar suma un intento.
    - Lo que agota `GMAIL_INGEST_MAX_ATTEMPTS` sale de la cola con un AVISO
      VISIBLE en Monitorización: es un correo que se pierde de verdad y alguien
      tiene que ir a buscarlo a Gmail a mano.
    - Lo nuevo que falla entra en la cola con un intento.
    """
    max_attempts = max(1, int(settings.GMAIL_INGEST_MAX_ATTEMPTS))
    now_iso = datetime.now(timezone.utc).isoformat()
    by_id = {str(e.get("id")): dict(e) for e in previous if e.get("id")}
    attempted = set(attempted_ids)

    out: list[dict] = []
    for mid, entry in by_id.items():
        if mid not in attempted:
            # No se intentó en esta pasada (p.ej. la cola venía recortada):
            # se mantiene tal cual.
            out.append(entry)
            continue
        if mid not in failed:
            # Se recuperó: fuera de la cola.
            logger.info("gmail.retry.recovered", message_id=mid)
            continue
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_error"] = failed[mid]
        entry["last_attempt_at"] = now_iso
        if entry["attempts"] >= max_attempts:
            logger.error(
                "gmail.retry.exhausted", message_id=mid, error=entry["last_error"]
            )
            await push_runtime_log(
                level="error",
                event="gmail.retry.exhausted",
                message=(
                    "Un correo no ha podido procesarse tras "
                    f"{entry['attempts']} intentos y se descarta. Gmail no lo "
                    "vuelve a ofrecer: búscalo a mano en la bandeja."
                ),
                channel="email",
                gmail_message_id=mid,
                error=entry["last_error"],
            )
            continue
        out.append(entry)

    # Fallos NUEVOS (no estaban ya en la cola).
    for mid, err in failed.items():
        if mid in by_id:
            continue
        logger.warning("gmail.retry.enqueued", message_id=mid, error=err)
        out.append(
            {
                "id": mid,
                "attempts": 1,
                "last_error": err,
                "first_failed_at": now_iso,
                "last_attempt_at": now_iso,
            }
        )
    return out


async def sync_incoming_emails() -> dict:
    """Sincroniza los correos entrantes. Devuelve un pequeño resumen (para logs
    y tests). No lanza si no hay canal/credenciales: hace return temprano."""
    channel = await _load_email_channel()
    if not channel:
        logger.info("gmail.sync.no_channel")
        return {"status": "no_channel", "stored": 0}

    # Lock Redis para evitar solape de beats. El valor es un token único de esta
    # corrida: al soltarlo solo se borra si sigue siendo el nuestro (ver
    # _release_lock). `lock_token=None` = corremos SIN lock y no soltamos nada.
    redis = get_redis()
    lock_token: str | None = _new_lock_token()
    got_lock = False
    try:
        got_lock = bool(await redis.set(_LOCK_KEY, lock_token, nx=True, ex=_LOCK_TTL))
    except Exception as e:
        # Si Redis falla, seguimos sin lock (mejor procesar que quedarnos
        # parados); el check de idempotencia evita duplicados igualmente. Pero
        # NO somos dueños de nada: al acabar no se borra el lock de nadie.
        logger.warning("gmail.sync.lock_failed", error=str(e))
        got_lock = True
        lock_token = None
    if not got_lock:
        logger.info("gmail.sync.locked")
        return {"status": "locked", "stored": 0}

    try:
        gmail = get_gmail_provider()
        profile = await gmail.get_profile()
        if not profile:
            # E3: sin perfil = sin credenciales válidas. `google_oauth` ya ha
            # dejado el motivo concreto en la integración y en Monitorización;
            # aquí añadimos la consecuencia práctica, que es la que le importa
            # a quien mira el panel: han dejado de entrar correos.
            logger.warning("gmail.sync.no_profile")
            await push_runtime_log(
                level="error",
                event="gmail.sync.no_credentials",
                message=(
                    "La cuenta de Gmail no responde a la autenticación: han "
                    "DEJADO DE ENTRAR correos. Revisa la conexión de Google en "
                    "Conexiones."
                ),
                channel="email",
            )
            return {"status": "no_credentials", "stored": 0}
        account_email = profile.get("email_address") or ""
        current_history_id = profile.get("history_id") or ""

        cfg = dict(channel.config or {})
        last_history_id = cfg.get("last_history_id")
        retry_queue = list(cfg.get(RETRY_QUEUE_KEY) or [])

        # ---------- REINTENTOS (E1) ----------
        # Antes que nada, los correos que fallaron en pasadas anteriores. Van
        # primero porque son los más antiguos y porque su ventana en Gmail no se
        # reabre: si no los recuperamos aquí, no los recupera nadie.
        retry_ids = [str(e.get("id")) for e in retry_queue if e.get("id")]
        retried_stored = 0
        retry_failed: dict[str, str] = {}
        if retry_ids:
            logger.info("gmail.retry.pass", count=len(retry_ids))
            retried_stored, retry_failed = await _process_message_ids(
                retry_ids, account_email
            )

        # ---------- BOOTSTRAP (primera vez) ----------
        if not last_history_id:
            message_ids = await gmail.list_messages(BOOTSTRAP_QUERY)
            stored, failed = await _process_message_ids(message_ids, account_email)
            new_queue = await _merge_retry_queue(
                retry_queue, retry_ids + message_ids, {**retry_failed, **failed}
            )
            # Ancla el historyId actual: a partir de aquí solo lo nuevo.
            await _save_history_id(
                channel.id, current_history_id, account_email, retry_queue=new_queue
            )
            logger.info("gmail.sync.bootstrap", found=len(message_ids), stored=stored)
            return {
                "status": "bootstrap",
                "stored": stored + retried_stored,
                "found": len(message_ids),
                "retry_pending": len(new_queue),
            }

        # ---------- INCREMENTAL (history.list) ----------
        result = await gmail.list_history(last_history_id)
        if result.get("expired"):
            # historyId caducado: re-anclamos al actual SIN reprocesar histórico.
            new_queue = await _merge_retry_queue(retry_queue, retry_ids, retry_failed)
            await _save_history_id(
                channel.id, current_history_id, account_email, retry_queue=new_queue
            )
            logger.warning("gmail.sync.history_expired.rebootstrap", anchored=current_history_id)
            # Aviso VISIBLE, no solo en el log del contenedor. Gmail caduca el
            # ancla en torno a la semana, así que con el worker parado unos días
            # (o las credenciales revocadas y repuestas) todo lo que entró en ese
            # hueco NO se ingiere nunca. Nadie iba a enterarse: los correos
            # simplemente no aparecían.
            await push_runtime_log(
                level="error",
                event="gmail.sync.history_expired",
                message=(
                    "Gmail ha caducado el punto de sincronización del correo (pasa "
                    "cuando el sondeo lleva varios días parado). Los correos que "
                    "llegaran mientras tanto NO han entrado en el panel: revisa la "
                    "bandeja de Gmail de esos días a mano."
                ),
            )
            return {
                "status": "history_expired",
                "stored": retried_stored,
                "retry_pending": len(new_queue),
            }

        message_ids = result.get("message_ids") or []
        stored, failed = await _process_message_ids(message_ids, account_email)
        # El ancla avanza SIEMPRE (Gmail no permite otra cosa), pero ya no se
        # pierde nada: lo que falló va a la cola de reintentos persistente.
        new_queue = await _merge_retry_queue(
            retry_queue, retry_ids + message_ids, {**retry_failed, **failed}
        )
        new_history_id = result.get("history_id") or current_history_id
        await _save_history_id(
            channel.id, new_history_id, account_email, retry_queue=new_queue
        )
        logger.info("gmail.sync.incremental", found=len(message_ids), stored=stored)
        return {
            "status": "incremental",
            "stored": stored + retried_stored,
            "found": len(message_ids),
            "retry_pending": len(new_queue),
        }
    finally:
        # Soltamos el lock SOLO si sigue siendo el nuestro (best-effort).
        await _release_lock(redis, lock_token)
