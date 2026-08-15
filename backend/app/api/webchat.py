"""Canal Web (widget embebible) — F4.

Flujo:
  1. La web del cliente carga `widget.js`. El snippet llama a
     `POST /webchat/sessions` con la api_key del canal Web (la genera el
     administrador desde el panel) → recibe {visitor_id, session_token}.
     La sesión NO crea contacto ni conversación: solo abrir la burbuja no
     puede ensuciar la bandeja ni el CRM (ver "Identidad perezosa").
  2. `GET /webchat/history` devuelve el hilo persistido del visitante. El
     widget lo pinta al abrir, al reconectar el WebSocket y al volver a la
     pestaña. Sin esto el chat web NO tiene memoria visible: la entrega es
     Redis pub/sub puro y lo que se publica sin nadie escuchando se pierde,
     así que el visitante veía una pantalla en blanco mientras el agente
     conservaba todo el contexto ("como te comentaba antes…" sobre nada).
  3. Cada mensaje del visitante entra por `POST /webchat/messages` con el
     token. El backend lo persiste y lo encola en el mismo pipeline async
     que WhatsApp (buffer + drain_buffer). Ahí se crean, si hacen falta,
     el contacto y la conversación.
  4. El widget abre una WebSocket a `/webchat/ws/{conversation_id}?token=...`
     para recibir en vivo las respuestas del agente.

Identidad perezosa (el chat web no puede inflar el CRM):
  - Abrir la burbuja no crea nada. El contacto y la conversación se crean con
    el PRIMER mensaje de verdad. Antes, `openPanel()` creaba contacto +
    conversación vacía por cada visitante anónimo que hacía clic: la bandeja y
    Contactos se llenaban de filas vacías en días.
  - El contacto anónimo se crea con `in_crm=False`, igual que Instagram y voz.
    Solo entra al CRM si deja nombre o email (el propio agente lo promueve con
    la tool `crear_actualizar_contacto`).
  - telefono = `web:{uuid}` (mantiene el unique constraint).

Seguridad:
  - api_key del canal valida la petición inicial (no API key → 401).
  - session_token = `v1.{huella_api_key}.{caducidad}.{HMAC}`. Va ligado al
    visitor_id, a la api_key VIGENTE del canal y tiene caducidad. Así:
      · desactivar el canal desde el panel corta las sesiones abiertas;
      · regenerar la api_key invalida todos los tokens ya emitidos (que es lo
        que promete el panel al regenerarla);
      · un token filtrado caduca solo.
    Antes era un HMAC de conv_id+visitor_id sin versión ni caducidad, y
    `POST /messages` no miraba el canal: la única forma de cortar era borrar
    el canal o cambiar JWT_SECRET (que tira la sesión del panel de todos).
  - `allowed_domains` del canal: la api_key viaja en el HTML público de la web
    del cliente, o sea que es pública por diseño. La lista de dominios es lo
    único que impide copiar el snippet a otro sitio y gastarle el presupuesto
    de modelo. Si la lista está vacía, se acepta cualquier origen (compatible
    con las instalaciones que ya existen).
  - No exponemos endpoints admin desde aquí.

IMPORTANTE: este módulo NO debe usar `from __future__ import annotations`. Con
las anotaciones aplazadas (en string), FastAPI 0.115 no resuelve el tipo del
body en los endpoints envueltos por el decorador de slowapi (`@limiter.limit`)
y falla al CONSTRUIR las rutas al arrancar ("name 'CreateSessionBody' is not
defined"). webhooks.py/auth.py tampoco lo usan, por el mismo motivo.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    status,
)
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import settings
from app.core.events import event_bus, inbox_channel
from app.core.logging import get_logger
from app.core.ratelimit import make_limiter
from app.core.redis import get_redis
from app.db.session import SessionLocal, db_session
from app.models.channel import Channel, ChannelType
from app.models.contact import Contact, ContactOrigen
from app.models.conversation import (
    Conversation,
    ConversationCanal,
    ConversationStatus,
)
from app.models.message import Message, MessageRole
from app.services.channel_sender import webchat_channel
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)

# Rate limiter por IP del cliente real (X-Forwarded-For), igual que webhooks.py.
# La api_key del canal Web es PÚBLICA (viaja en el HTML del cliente vía
# widget.js), así que estos endpoints son anónimos y abiertos a internet:
# sin rate-limit, cualquiera puede martillearlos para crear sesiones/mensajes a
# mansalva e hinchar la factura del LLM (DoS económico — C1).
limiter = make_limiter()

# Tope diario GLOBAL del canal webchat (mensajes entrantes aceptados al día).
# Es la última red de seguridad contra un abuso distribuido (muchas IPs) que
# burle el rate-limit por IP: aunque cada IP respete su cubo, el agregado no
# puede disparar el coste de tokens sin control. Si se supera, devolvemos 429
# sin crear el mensaje ni llamar al LLM. Ajustable si el tráfico legítimo crece.
WEBCHAT_DAILY_CAP = 2000

# Vida del session_token. Corta lo justo para que una sesión filtrada no valga
# indefinidamente, y larga para que un visitante no pierda el hilo en la misma
# visita. Al caducar, el widget pide sesión nueva CONSERVANDO su visitor_id, o
# sea que sigue siendo el mismo contacto y ve su historial.
WEBCHAT_SESSION_TTL_SECONDS = 12 * 3600

# Nº máximo de mensajes que devuelve el historial de una tacada.
WEBCHAT_HISTORY_MAX = 200

# WebSocket del visitante: latido y vida máxima.
#  - El latido detecta el enlace muerto en sentido servidor→visitante.
#  - La vida máxima es el cinturón de seguridad: pase lo que pase, ninguna
#    conexión vive para siempre. El widget reconecta solo (con backoff) y
#    recupera lo perdido por el historial incremental.
WS_HEARTBEAT_SECONDS = 25
WS_MAX_LIFETIME_SECONDS = 3600

router = APIRouter(prefix="/webchat", tags=["webchat"])


def _is_valid_visitor_id(visitor_id: str) -> bool:
    """visitor_id se usa para construir Contact.telefono = "web:{visitor_id}"
    (columna String(40)). Si llega algo no-UUID o demasiado largo, el INSERT
    revienta con 500 (C1). Exigimos UUID válido y len <= 36 (tamaño de un UUID
    canónico) para que "web:" + visitor_id quepa de sobra en 40 chars.
    """
    if not visitor_id or len(visitor_id) > 36:
        return False
    try:
        uuid.UUID(visitor_id)
    except (TypeError, ValueError):
        return False
    return True


async def _webchat_daily_guard() -> bool:
    """Incrementa el contador diario del canal webchat. Devuelve True si AÚN se
    permite procesar (dentro del tope), False si se ha superado WEBCHAT_DAILY_CAP.

    Contador Redis `guard:webchat:count:{YYYYMMDD}` con INCR + EXPIRE 24h
    (best-effort: si Redis no responde, no bloqueamos tráfico legítimo).
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"guard:webchat:count:{day}"
    try:
        r = get_redis()
        async with r.pipeline(transaction=True) as p:
            p.incr(key)
            p.expire(key, 24 * 3600)
            results = await p.execute()
        count = int(results[0])
    except Exception:
        # Si Redis falla, no convertimos un problema de infraestructura en una
        # caída del widget para todos. El rate-limit por IP sigue activo.
        return True
    return count <= WEBCHAT_DAILY_CAP


# ---------- canal ----------


def _api_key_fingerprint(api_key: str) -> str:
    """Huella pública y corta de la api_key. Va DENTRO del token para poder
    localizar el canal sin pedirle la clave al widget en cada petición, y para
    que el token muera solo en cuanto la clave se regenere."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


async def _enabled_webchat_channels() -> list:
    async with db_session() as db:
        return (
            (
                await db.execute(
                    select(Channel).where(
                        Channel.type == ChannelType.webchat,
                        Channel.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )


async def _resolve_channel_by_api_key(api_key: str) -> Channel | None:
    """Busca el canal Web ACTIVO con esa api_key.

    La api_key de webchat vive en `Channel.config['api_key']` en plano, y ahí
    se queda: es pública por diseño, viaja en el HTML de la web del cliente.
    Lo que la protege es `allowed_domains`, no el secreto. (Los canales que SÍ
    tienen secretos de servidor —Instagram, Retell— los guardan cifrados desde
    la migración 0054; ver `app/services/channel_secrets.py`.)

    Comparación en tiempo constante: comparar secretos con `==` filtra por
    tiempo cuántos caracteres coinciden. Es gratis hacerlo bien.
    """
    if not api_key:
        return None
    for ch in await _enabled_webchat_channels():
        stored = (ch.config or {}).get("api_key")
        if stored and hmac.compare_digest(str(stored), api_key):
            return ch
    return None


async def _resolve_channel_by_fingerprint(fingerprint: str) -> Channel | None:
    """Canal ACTIVO cuya api_key vigente tiene esa huella.

    Si el canal se desactiva o se regenera la clave, aquí ya no aparece → el
    token deja de validar sin tocar JWT_SECRET.
    """
    if not fingerprint:
        return None
    for ch in await _enabled_webchat_channels():
        stored = (ch.config or {}).get("api_key")
        if stored and hmac.compare_digest(_api_key_fingerprint(str(stored)), fingerprint):
            return ch
    return None


# ---------- dominios permitidos ----------


def _normalize_host(value: str | None) -> str | None:
    """Saca el host de lo que sea que haya escrito el administrador o mande el
    navegador: 'https://Ejemplo.com/algo', 'ejemplo.com:8443', ' ejemplo.com '.
    """
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if "//" in raw:
        raw = urlsplit(raw).netloc or raw.split("//", 1)[1]
    raw = raw.split("/", 1)[0]
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1]
    if raw.startswith("[") and "]" in raw:  # IPv6 literal
        raw = raw[1 : raw.index("]")]
    else:
        raw = raw.split(":", 1)[0]
    return raw.strip(".") or None


def channel_allowed_domains(channel: Channel) -> list[str]:
    """Lista de dominios configurada en el canal (vacía = sin restricción)."""
    raw = (channel.config or {}).get("allowed_domains")
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        pattern = item.strip().lower()
        if pattern.startswith("*."):
            base = _normalize_host(pattern[2:])
            if base:
                out.append("*." + base)
            continue
        host = _normalize_host(pattern)
        if host:
            out.append(host)
    return out


def host_matches_domain(host: str, pattern: str) -> bool:
    """`ejemplo.com` = exacto. `*.ejemplo.com` = el dominio y sus subdominios."""
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern


def _request_origin_host(request: Request) -> str | None:
    """Host desde el que se hace la petición. `Origin` es el fiable en CORS;
    `Referer` es el respaldo para navegadores/casos que no lo mandan."""
    origin = request.headers.get("origin")
    if origin and origin.lower() != "null":
        host = _normalize_host(origin)
        if host:
            return host
    return _normalize_host(request.headers.get("referer"))


def _enforce_allowed_domains(request: Request, channel: Channel) -> None:
    """403 si el canal tiene lista de dominios y el origen no está en ella.

    Sin lista configurada no se restringe nada (instalaciones existentes). Con
    lista, una petición SIN origen (curl, script) tampoco pasa: es justo el
    tráfico que la lista existe para frenar.
    """
    patterns = channel_allowed_domains(channel)
    if not patterns:
        return
    host = _request_origin_host(request)
    if host and any(host_matches_domain(host, p) for p in patterns):
        return
    logger.warning(
        "webchat.origin_rejected", host=host or "", channel_id=str(channel.id)
    )
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "Este widget no está autorizado en este dominio",
    )


# ---------- session token ----------

_TOKEN_VERSION = "v1"


def _sign_session(visitor_id: str, fingerprint: str, expires_at: int) -> str:
    msg = f"{_TOKEN_VERSION}.{fingerprint}.{expires_at}.{visitor_id}".encode()
    return hmac.new(settings.JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]


def _make_session_token(visitor_id: str, api_key: str, now: int | None = None) -> tuple[str, int]:
    """Devuelve (token, caducidad_epoch)."""
    fingerprint = _api_key_fingerprint(api_key)
    expires_at = int(now or time.time()) + WEBCHAT_SESSION_TTL_SECONDS
    sig = _sign_session(visitor_id, fingerprint, expires_at)
    return f"{_TOKEN_VERSION}.{fingerprint}.{expires_at}.{sig}", expires_at


async def _verify_session_token(visitor_id: str, token: str) -> Channel:
    """Valida el token y devuelve el canal VIGENTE. Levanta 401 si no cuela.

    Comprueba, en este orden: formato, caducidad, canal activo con esa api_key
    (o sea: canal encendido y clave sin regenerar) y firma.
    """
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "sesión no válida")
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_VERSION:
        raise unauthorized
    _version, fingerprint, raw_exp, sig = parts
    try:
        expires_at = int(raw_exp)
    except (TypeError, ValueError):
        raise unauthorized from None
    if expires_at <= int(time.time()):
        raise unauthorized
    channel = await _resolve_channel_by_fingerprint(fingerprint)
    if channel is None:
        # Canal desactivado o api_key regenerada: la sesión muere aquí.
        raise unauthorized
    if not hmac.compare_digest(_sign_session(visitor_id, fingerprint, expires_at), sig):
        raise unauthorized
    return channel


def _ws_path(conversation_id, token: str, visitor_id: str) -> str:
    return f"/api/v1/webchat/ws/{conversation_id}?token={token}&v={visitor_id}"


# ---------- lectura/creación de contacto y conversación ----------


async def _find_contact(db, visitor_id: str) -> Contact | None:
    return (
        await db.execute(
            select(Contact).where(Contact.telefono == f"web:{visitor_id}")
        )
    ).scalar_one_or_none()


async def _latest_conversation(db, contact_id, only_open: bool = False):
    stmt = select(Conversation).where(
        Conversation.contact_id == contact_id,
        Conversation.canal == ConversationCanal.web,
    )
    if only_open:
        stmt = stmt.where(Conversation.status != ConversationStatus.cerrada)
    stmt = stmt.order_by(Conversation.started_at.desc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


# ---------- POST /webchat/sessions ----------


class CreateSessionBody(BaseModel):
    api_key: str
    visitor_id: str | None = None  # si el cliente conserva uno en localStorage
    name: str | None = None
    email: str | None = None
    referrer: str | None = None


class SessionOut(BaseModel):
    conversation_id: uuid.UUID | None
    visitor_id: str
    session_token: str
    expires_at: int  # epoch segundos
    ws_path: str | None  # None mientras no haya conversación


@router.post("/sessions", response_model=SessionOut)
@limiter.limit("10/minute")
async def create_session(request: Request, body: CreateSessionBody) -> SessionOut:
    channel = await _resolve_channel_by_api_key(body.api_key)
    if not channel:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "api_key inválida o canal deshabilitado"
        )
    _enforce_allowed_domains(request, channel)

    # Si el cliente NO trae visitor_id, generamos uno (UUID válido). Si lo trae
    # (conservado en localStorage), lo validamos: un valor no-UUID o > 36 chars
    # rompería el INSERT de Contact.telefono (String(40)) con un 500.
    if body.visitor_id is None:
        visitor_id = str(uuid.uuid4())
    elif _is_valid_visitor_id(body.visitor_id):
        visitor_id = body.visitor_id
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "visitor_id inválido"
        )

    conv_id = None
    name = (body.name or "").strip() or None
    email = (body.email or "").strip() or None

    async with db_session() as db:
        contact = await _find_contact(db, visitor_id)
        if contact is None and (name or email):
            # Identidad de verdad (la web le pidió nombre/email): eso SÍ es un
            # lead y entra al CRM. Un visitante anónimo, no: abrir la burbuja
            # no crea nada (ver "Identidad perezosa" en el docstring).
            contact = Contact(
                telefono=f"web:{visitor_id}",
                nombre=name,
                email=email,
                origen=ContactOrigen.web,
                in_crm=True,
            )
            db.add(contact)
            await db.flush()
        elif contact is not None:
            if name and not contact.nombre:
                contact.nombre = name
            if email and not contact.email:
                contact.email = email
            if name or email:
                contact.in_crm = True
            conv = await _latest_conversation(db, contact.id, only_open=True)
            conv_id = conv.id if conv else None
        await db.commit()

    token, expires_at = _make_session_token(visitor_id, (channel.config or {})["api_key"])
    return SessionOut(
        conversation_id=conv_id,
        visitor_id=visitor_id,
        session_token=token,
        expires_at=expires_at,
        ws_path=_ws_path(conv_id, token, visitor_id) if conv_id else None,
    )


# ---------- GET /webchat/history ----------


class HistoryMessage(BaseModel):
    id: uuid.UUID
    role: str  # "user" (el visitante) | "bot" (agente u operadora)
    text: str
    created_at: str


class HistoryOut(BaseModel):
    conversation_id: uuid.UUID | None
    status: str | None
    ws_path: str | None
    messages: list[HistoryMessage]


# Roles que ve el visitante. `system` es fontanería interna: nunca sale.
_VISIBLE_ROLES = {
    MessageRole.user: "user",
    MessageRole.assistant: "bot",
    MessageRole.operator: "bot",
}


@router.get("/history", response_model=HistoryOut)
@limiter.limit("60/minute")
async def webchat_history(
    request: Request,
    visitor_id: str = Query(...),
    token: str = Query(...),
    after: uuid.UUID | None = Query(None),
    limit: int = Query(WEBCHAT_HISTORY_MAX, ge=1, le=WEBCHAT_HISTORY_MAX),
) -> HistoryOut:
    """Hilo del visitante, autenticado con su session_token.

    `after` = id del último mensaje que ya tiene el widget → solo devuelve lo
    posterior. Es lo que cierra la ventana entre la foto del historial y la
    suscripción al WebSocket, y lo que recupera lo publicado mientras el
    visitante tenía la pestaña en segundo plano o el socket caído.
    """
    if not _is_valid_visitor_id(visitor_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "visitor_id inválido")
    await _verify_session_token(visitor_id, token)

    async with db_session() as db:
        contact = await _find_contact(db, visitor_id)
        if contact is None:
            return HistoryOut(
                conversation_id=None, status=None, ws_path=None, messages=[]
            )
        # La última conversación aunque esté cerrada: si la operadora la cierra
        # desde el panel, el visitante debe seguir viendo lo que hablaron (y el
        # widget le avisa de que está cerrada), no una pantalla en blanco.
        conv = await _latest_conversation(db, contact.id)
        if conv is None:
            return HistoryOut(
                conversation_id=None, status=None, ws_path=None, messages=[]
            )
        stmt = select(Message).where(Message.conversation_id == conv.id)
        if after is not None:
            ref = (
                await db.execute(select(Message).where(Message.id == after))
            ).scalar_one_or_none()
            if ref is not None and ref.conversation_id == conv.id:
                stmt = stmt.where(Message.created_at > ref.created_at)
        rows = (
            (await db.execute(stmt.order_by(Message.created_at.asc()).limit(limit)))
            .scalars()
            .all()
        )
        conv_id, conv_status = conv.id, conv.status.value

    messages = []
    for m in rows:
        role = _VISIBLE_ROLES.get(m.rol)
        if role is None:
            continue
        text = (m.contenido or "").strip()
        if not text:
            # El widget no pinta adjuntos, pero callarse deja huecos raros en
            # el hilo. Se deja constancia en texto.
            text = "[archivo adjunto]" if m.media_type else ""
        if not text:
            continue
        messages.append(
            HistoryMessage(
                id=m.id,
                role=role,
                text=text,
                created_at=m.created_at.isoformat() if m.created_at else "",
            )
        )

    return HistoryOut(
        conversation_id=conv_id,
        status=conv_status,
        ws_path=_ws_path(conv_id, token, visitor_id),
        messages=messages,
    )


# ---------- POST /webchat/messages ----------


class IncomingWebMessage(BaseModel):
    visitor_id: str
    token: str
    text: str
    # Aceptado y descartado: la conversación se deriva del visitante, no de lo
    # que diga el cliente. Se mantiene el campo para no devolver 422 a widgets
    # viejos que sigan cacheados en algún navegador.
    conversation_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    ws_path: str


@router.post("/messages", response_model=MessageOut)
@limiter.limit("30/minute")
async def send_webchat_message(request: Request, body: IncomingWebMessage) -> MessageOut:
    # visitor_id participa en el HMAC del token, así que un valor inválido ya
    # fallaría el _verify_session_token; lo validamos explícitamente igualmente
    # para responder 422 (no 401) ante un visitor_id malformado.
    if not _is_valid_visitor_id(body.visitor_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "visitor_id inválido")
    channel = await _verify_session_token(body.visitor_id, body.token)
    _enforce_allowed_domains(request, channel)

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mensaje vacío")
    if len(text) > 4000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mensaje demasiado largo")

    # Tope diario global del canal: cubre la creación de mensajes entrantes
    # ANTES de persistir nada o tocar el LLM. Si se supera, 429 limpio.
    if not await _webchat_daily_guard():
        logger.warning("webchat.daily_cap_exceeded")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Límite diario del canal web alcanzado",
        )

    now = datetime.now(timezone.utc)
    async with db_session() as db:
        contact = await _find_contact(db, body.visitor_id)
        if contact is None:
            # Primer mensaje de verdad: aquí (y solo aquí) nace la identidad.
            # Anónimo → fuera del CRM, igual que Instagram y voz. El agente lo
            # promueve en cuanto capture nombre o email.
            contact = Contact(
                telefono=f"web:{body.visitor_id}",
                origen=ContactOrigen.web,
                in_crm=False,
            )
            db.add(contact)
            await db.flush()
        contact.ultimo_mensaje_at = now

        # Si la operadora cerró la conversación desde el panel, se abre una
        # nueva SOBRE EL MISMO CONTACTO. Antes el widget recibía un 409, lo
        # trataba como sesión caducada y borraba también el visitor_id: el
        # mismo visitante aparecía como dos personas y se perdían su nombre y
        # su email.
        conv = await _latest_conversation(db, contact.id, only_open=True)
        if conv is None:
            conv = Conversation(
                contact_id=contact.id,
                canal=ConversationCanal.web,
                session_id=body.visitor_id,
                status=(
                    ConversationStatus.bot
                    if settings.AGENT_AUTORESPONSE_DEFAULT
                    else ConversationStatus.humano
                ),
            )
            db.add(conv)
            await db.flush()
        if conv.archived_at is not None:
            conv.archived_at = None

        msg = Message(
            conversation_id=conv.id,
            rol=MessageRole.user,
            contenido=text,
            extra={"channel": "web", "visitor_id": body.visitor_id},
        )
        db.add(msg)
        conv.last_message_at = now
        await db.commit()
        await db.refresh(msg)
        message_id = msg.id
        conv_id = conv.id
        conv_is_bot = conv.status == ConversationStatus.bot

    # Eco al inbox del panel (operador ve el mensaje en tiempo real).
    with contextlib.suppress(Exception):
        await event_bus.publish(
            inbox_channel(),
            "message.new",
            {
                "conversation_id": str(conv_id),
                "message_id": str(message_id),
                "rol": "user",
            },
        )

    await push_runtime_log(
        level="info",
        event="message.in",
        message="Mensaje web recibido",
        conversation_id=str(conv_id),
        channel="web",
    )

    # Encolar para el agente (mismo pipeline que WhatsApp).
    if conv_is_bot:
        # "Escribiendo…" al canal del visitante: entre el buffer (8 s por
        # defecto) y la latencia del modelo pasan 15-20 segundos, y sin ninguna
        # señal el widget parece roto. El propio widget lo pinta también en
        # local; este evento cubre el resto de pestañas del mismo visitante.
        with contextlib.suppress(Exception):
            await event_bus.publish(
                webchat_channel(conv_id), "typing", {"conversation_id": str(conv_id)}
            )
        from app.services.conversation import handle_incoming_message
        await handle_incoming_message(message_id)

    return MessageOut(
        message_id=message_id,
        conversation_id=conv_id,
        ws_path=_ws_path(conv_id, body.token, body.visitor_id),
    )


# ---------- WebSocket /webchat/ws/{conv_id} ----------

ws_router = APIRouter(tags=["webchat-ws"])


@ws_router.websocket("/webchat/ws/{conversation_id}")
async def webchat_ws(
    websocket: WebSocket,
    conversation_id: str,
    token: str | None = None,
    v: str | None = None,
) -> None:
    """Empuja al visitante lo que publique el agente en su canal.

    Cuatro tareas en paralelo; la primera que termine cierra la conexión:

      · lector del socket — la ÚNICA forma de enterarse de que el visitante se
        ha ido. Starlette solo detecta la desconexión en `receive()` o cuando
        falla un `send`. Antes esto era un `async for` sobre el bus sin leer
        nunca del socket: si en ese canal no se publicaba nada (el caso normal
        en cuanto el visitante cierra la burbuja), la corrutina se quedaba
        bloqueada para siempre, con su suscripción a Redis colgando.
      · bombeo del bus — termina también si el bus se cae, para que el widget
        reconecte en vez de quedarse "conectado" y mudo.
      · latido — detecta el enlace muerto en sentido servidor→visitante.
      · vida máxima — ninguna conexión anónima vive para siempre.
    """
    try:
        conv_uuid = uuid.UUID(conversation_id)
    except (TypeError, ValueError):
        await websocket.close(code=4400)
        return
    if not v or not _is_valid_visitor_id(v) or not token:
        await websocket.close(code=4401)
        return
    try:
        await _verify_session_token(v, token)
    except HTTPException:
        await websocket.close(code=4401)
        return

    # La conversación tiene que existir, ser web y ser DE ESTE visitante: el
    # token va ligado al visitante, no a la conversación.
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(Conversation, Contact)
                .join(Contact, Conversation.contact_id == Contact.id)
                .where(Conversation.id == conv_uuid)
            )
        ).first()
    if not row:
        await websocket.close(code=4404)
        return
    conv, contact = row
    if conv.canal != ConversationCanal.web or contact.telefono != f"web:{v}":
        await websocket.close(code=4404)
        return

    await websocket.accept()
    chan = webchat_channel(conv_uuid)
    send_lock = asyncio.Lock()

    async def _send(payload: str) -> None:
        async with send_lock:
            await websocket.send_text(payload)

    async def _drain_socket() -> None:
        """No esperamos nada del visitante; leemos solo para saber si sigue."""
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

    async def _pump_bus() -> None:
        events = event_bus.subscribe(chan)
        try:
            async for ev in events:
                await _send(json.dumps(ev))
        finally:
            with contextlib.suppress(Exception):
                await events.aclose()

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(WS_HEARTBEAT_SECONDS)
            await _send('{"type":"ping","payload":{}}')

    async def _lifetime() -> None:
        await asyncio.sleep(WS_MAX_LIFETIME_SECONDS)

    tasks = [
        asyncio.create_task(coro)
        for coro in (_drain_socket(), _pump_bus(), _heartbeat(), _lifetime())
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await websocket.close()
        logger.info("webchat.ws.closed", conv_id=str(conv_uuid))
