import asyncio
import contextlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import case, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.events import event_bus, inbox_channel
from app.core.logging import get_logger
from app.core.ratelimit import consume_user_daily_quota, limit_spec, make_limiter
from app.db.session import SessionLocal, get_db
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
from app.models.message import Message, MessageRole
from app.models.user import User
from app.providers.whatsapp import get_whatsapp_provider
from app.services.channel_sender import (
    PROVIDER_NOOP_ID,
    MessagingWindowClosed,
    SendNotConfirmed,
    send_text_to_conversation,
    webchat_channel,
)
from app.services.agent_pause import (
    add_demo_conversation,
    list_demo_conversations,
    remove_demo_conversation,
)
from app.services.media import (
    MediaValidationError,
    categorize_mime,
    max_bytes_for,
    save_local,
    validate_size,
)
from app.schemas.common import OkResponse, Page
from app.services.audit import record_audit
from app.services.delivery_status import marcar_enviado

router = APIRouter(prefix="/conversations", tags=["conversations"])
ws_router = APIRouter(tags=["ws"])
logger = get_logger(__name__)
limiter = make_limiter()

# Cupo diario por USUARIO para "Corrige y re-redacta": cada llamada es una
# llamada COMPLETA al modelo con todo el historial de la conversación. Sin esto
# el único freno era el tope mensual global, que al saltar pausa el bot para
# todos los clientes. Configurable por entorno; 0 lo desactiva.
REFINE_DAILY_LIMIT_PER_USER = int(os.getenv("REFINE_DAILY_LIMIT_PER_USER", "150"))

# WebSocket de la bandeja: mismos valores que el del widget (api/webchat.py).
# El latido detecta el enlace muerto y la vida máxima garantiza que ninguna
# conexión se quede colgada indefinidamente aunque falle todo lo demás.
WS_HEARTBEAT_SECONDS = 25
WS_MAX_LIFETIME_SECONDS = 3600


class ContactMini(BaseModel):
    id: uuid.UUID
    telefono: str
    nombre: str | None
    # Instagram: el @usuario, para mostrarlo bajo el nombre real en la bandeja.
    social_handle: str | None = None
    email: str | None
    estado: str

    class Config:
        from_attributes = True


class ConversationOut(BaseModel):
    id: uuid.UUID
    contact: ContactMini
    canal: str
    status: str
    started_at: str
    ended_at: str | None
    last_message_at: str | None
    resumen: str | None
    # Asunto del correo (canal Email, F5a). NULL en el resto de canales.
    subject: str | None = None
    # Canal Voz (Retell) — metadatos de la llamada para la sección "Llamadas".
    # NULL en el resto de canales (y en llamadas previas al webhook). El fin de
    # la llamada se expone vía `ended_at` y el resumen vía `resumen`.
    call_duration_seconds: int | None = None
    call_recording_url: str | None = None
    call_ended_reason: str | None = None
    last_message_preview: str | None = None
    demo_active: bool = False
    archived: bool = False
    quarantined: bool = False
    quarantine_reason: str | None = None
    # 5d — True si la conversación tiene un borrador del agente sin enviar
    # (algún Message con extra.is_draft == True y extra.draft_sent != True).
    # El frontend pinta una etiqueta "Borrador" en la lista. Email-centric, pero
    # genérico (cualquier canal con borradores lo usaría).
    has_pending_draft: bool = False
    # True si la conversación tiene una SUGERENCIA del modo Entrenamiento sin
    # resolver (borrador con extra.training=True). Va a la pestaña "Sugerencias",
    # NO a "Pendientes" (es revisión, no trabajo urgente). Subconjunto de
    # has_pending_draft.
    has_training_suggestion: bool = False
    # Mensajes del cliente sin leer (rol user, leido_at IS NULL). Se pone a 0 al
    # abrir la conversación (endpoint mark-read). "Leído" es compartido entre
    # operadores (no es por-usuario).
    unread_count: int = 0
    # "Necesita a una persona" = la señal de "pendiente" del inbox: hay un
    # borrador de EMAIL sin enviar, O el cliente escribió lo último sin que nadie
    # contestara, O está derivada a humano y aún no ha respondido un operador (el
    # mensaje puente del bot no cuenta). Las sugerencias de Entrenamiento NO
    # cuentan aquí (van a su pestaña). En cuanto el operador responde, deja de
    # ser True. El inbox lo usa para la negrita, el contador y la pestaña
    # "Pendientes" (mismo criterio que el filtro pending_only).
    needs_action: bool = False


# Etiqueta del último mensaje en la bandeja cuando no hay texto que mostrar.
MEDIA_PREVIEW_LABEL: dict[str, str] = {
    "image": "[imagen]",
    "sticker": "[sticker]",
    "video": "[vídeo]",
    "document": "[documento]",
    "audio": "[audio]",
}


class MessageAttachmentOut(BaseModel):
    """Ficha de un adjunto REAL de un correo (canal Email).

    Los logos incrustados de una firma NO salen aquí: el proveedor los aparta en
    `extra["inline_attachments"]`. El binario no viaja en esta ficha; se pide
    bajo demanda al endpoint de descarga."""

    filename: str
    mime_type: str | None = None
    size: int | None = None
    attachment_id: str | None = None
    # False en los correos ingeridos ANTES de que el proveedor guardara la ficha
    # completa: de aquellos solo quedó el nombre del fichero, sin el
    # `attachment_id` que pide la API de Gmail, así que no se pueden descargar.
    # El panel usa esta bandera para no pintar un enlace que va a fallar.
    downloadable: bool = False


def _attachments_out(extra: dict) -> list[MessageAttachmentOut]:
    """Normaliza `extra["attachments"]` a fichas para el panel.

    Convive con DOS formatos históricos: los correos nuevos traen una lista de
    diccionarios (filename, mime_type, size, attachment_id) y los ingeridos
    antes de ese cambio, una lista de CADENAS con solo el nombre. Los viejos se
    exponen igual (para que la operadora vea qué venía adjunto) pero marcados
    como no descargables."""
    out: list[MessageAttachmentOut] = []
    for att in extra.get("attachments") or []:
        if isinstance(att, str):
            out.append(MessageAttachmentOut(filename=att, downloadable=False))
            continue
        if not isinstance(att, dict):
            continue
        attachment_id = att.get("attachment_id")
        out.append(
            MessageAttachmentOut(
                filename=str(att.get("filename") or "adjunto"),
                mime_type=att.get("mime_type"),
                size=att.get("size"),
                attachment_id=attachment_id,
                downloadable=bool(attachment_id),
            )
        )
    return out


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    rol: str
    contenido: str | None
    audio_url: str | None
    audio_transcript: str | None
    created_at: str
    metadata: dict | None = None
    media_type: str | None = None
    media_url: str | None = None
    media_mime: str | None = None
    media_size: int | None = None
    media_filename: str | None = None
    media_duration_seconds: int | None = None
    # F5c — Borrador del agente para email. Se derivan de extra para que el
    # frontend distinga un borrador pendiente sin tener que hurgar en metadata.
    is_draft: bool = False
    draft_sent: bool = False
    gmail_draft_id: str | None = None
    # Modo Entrenamiento — True si esta sugerencia la generó el agente en modo
    # Entrenamiento (sombra) en un canal NO email. Usa la MISMA UI de borrador,
    # pero el frontend ajusta la copy ("sugerencia" en vez de "correo") y al
    # enviar se enruta por el canal (no por Gmail). Derivado de extra.training.
    training: bool = False
    # 5d — HTML del correo (canal Email) para renderizado seguro en el frontend
    # (DOMPurify). NULL en mensajes sin HTML; el frontend cae a `contenido`.
    html_body: str | None = None
    # Retención email — True si el contenido se purgó de la BD (a los N meses).
    # El frontend muestra un stub "Contenido archivado" con botón para recuperar
    # el cuerpo desde Gmail bajo demanda. Se deriva de extra.purged.
    purged: bool = False
    # Adjuntos REALES del correo (canal Email). El panel pinta un enlace de
    # descarga por cada uno con `downloadable=True`; el binario se pide bajo
    # demanda a Gmail (aquí no se guarda copia).
    attachments: list[MessageAttachmentOut] = []
    # Entrega en WhatsApp: enviado | entregado | leido | fallido. NULL en los
    # canales que no dan acuse (web, correo, Instagram) y en los mensajes
    # anteriores a que esto existiera. `delivery_detail` solo viene con el
    # fallo, y dice por qué. Ver services/delivery_status.py.
    delivery_status: str | None = None
    delivery_detail: str | None = None


def _message_out(m: Message) -> "MessageOut":
    """Serializa un Message a MessageOut, exponiendo los flags de borrador
    (F5c) desde extra. Centralizado para no repetir el mapeo en cada endpoint."""
    extra = m.extra or {}
    # `metadata` expone una COPIA de extra sin html_body (hasta 200KB por
    # mensaje, ya viaja en el campo html_body de abajo) ni email_headers
    # (cabeceras crudas internas): duplicarlos engordaba cada respuesta sin
    # aportar nada al frontend.
    meta = {k: v for k, v in extra.items() if k not in ("html_body", "email_headers")}
    return MessageOut(
        id=m.id,
        conversation_id=m.conversation_id,
        rol=m.rol.value,
        contenido=m.contenido,
        audio_url=m.audio_url,
        audio_transcript=m.audio_transcript,
        created_at=m.created_at.isoformat(),
        metadata=meta,
        media_type=m.media_type,
        media_url=m.media_url,
        media_mime=m.media_mime,
        media_size=m.media_size,
        media_filename=m.media_filename,
        media_duration_seconds=m.media_duration_seconds,
        is_draft=bool(extra.get("is_draft")),
        draft_sent=bool(extra.get("draft_sent")),
        gmail_draft_id=extra.get("gmail_draft_id"),
        training=bool(extra.get("training")),
        html_body=extra.get("html_body"),
        purged=bool(extra.get("purged")),
        attachments=_attachments_out(extra),
        delivery_status=extra.get("delivery_status"),
        delivery_detail=extra.get("delivery_detail"),
    )


class SendMessageBody(BaseModel):
    contenido: str


# Definición de "Para hacer" (pending | suggestion) centralizada en
# app/services/inbox_pending.py para que el inbox y las notificaciones Web Push
# usen la MISMA regla. Se importan con los nombres locales de siempre.
from app.services.inbox_pending import pending_expr as _pending_expr  # noqa: E402
from app.services.inbox_pending import suggestion_expr as _suggestion_expr  # noqa: E402


# ── Orden de la bandeja ────────────────────────────────────────────────────
#
# UN SOLO criterio, y lo decide el BACKEND. Antes se ordenaba dos veces con
# reglas distintas: aquí (humano primero + fecha) y otra vez en el frontend
# (pendientes primero), sobre las 50 filas que le habían llegado. Como el
# backend paginaba con SU criterio, una conversación pendiente que cayera en el
# puesto 51 no llegaba nunca al frontend y por tanto no podía subir: era
# invisible. Ahora el orden se pide con `sort`, se aplica al paginar, y el
# frontend pinta exactamente lo que recibe.
INBOX_SORTS = ("recent", "pending", "waiting")


def _inbox_order_by(sort: str) -> list:
    """Cláusula ORDER BY del inbox para el criterio pedido.

    - `recent`  (por defecto): lo último que se movió, arriba.
    - `pending`: lo que necesita a una persona primero y, dentro, lo más
      reciente. Usa la MISMA regla que el filtro "Para hacer" (_pending_expr /
      _suggestion_expr), así la pestaña y el orden nunca se contradicen.
    - `waiting`: lo que lleva más tiempo esperando (más antiguo primero); es la
      cola de "no dejar a nadie tirado".
    """
    if sort == "pending":
        return [
            case((_pending_expr() | _suggestion_expr(), 0), else_=1),
            desc(Conversation.last_message_at).nullslast(),
            desc(Conversation.started_at),
        ]
    if sort == "waiting":
        return [
            Conversation.last_message_at.asc().nullslast(),
            Conversation.started_at.asc(),
        ]
    return [
        desc(Conversation.last_message_at).nullslast(),
        desc(Conversation.started_at),
    ]


def _esc_like(s: str) -> str:
    """Escapa los comodines de LIKE (% _ \\) para buscar el término LITERAL.

    Sin esto, teclear `%` en el buscador devolvía TODA la bandeja (y `_`
    convertía cada letra en un comodín). El buscador de Contactos ya lo hacía;
    esto pone el mismo criterio en la bandeja.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("")
async def list_conversations(
    status_filter: ConversationStatus | None = Query(None, alias="status"),
    canal_filter: str | None = Query(None, alias="canal"),
    archived: str = Query("hide", regex="^(hide|only|all)$"),
    quarantine: str = Query("hide", regex="^(hide|only|all)$"),
    # 'Activas': bandeja por defecto, oculta las cerradas (Bot + Humano).
    active_only: bool = Query(False),
    # Solo conversaciones con mensajes del cliente sin leer (para el filtro
    # "No leídas" del inbox).
    unread_only: bool = Query(False),
    # Vista "Pendientes": solo lo accionable por una persona — derivadas a
    # humano, borrador de email sin enviar, o último mensaje del cliente sin
    # contestar. (Las sugerencias de Entrenamiento NO entran aquí.)
    pending_only: bool = Query(False),
    # Vista "Sugerencias": solo conversaciones con una sugerencia del modo
    # Entrenamiento sin resolver (borrador con extra.training=True).
    suggestions_only: bool = Query(False),
    # Vista "Para hacer": une Pendientes + Sugerencias en una sola cola (cada
    # fila se etiqueta en el front con needs_action / has_training_suggestion).
    action_only: bool = Query(False),
    search: str | None = None,
    # Orden de la lista. ÚNICO criterio y decidido aquí (ver _inbox_order_by):
    # recent = lo último que se movió | pending = lo que necesita persona
    # primero | waiting = lo que lleva más esperando.
    # `Annotated[...] = "recent"` (y no `= Query("recent", ...)`) para que el
    # valor por defecto sea la CADENA: varios tests llaman a este endpoint como
    # una función normal, sin pasar por FastAPI.
    sort: Annotated[str, Query(regex="^(recent|pending|waiting)$")] = "recent",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[ConversationOut]:
    q = select(Conversation, Contact).join(Contact, Conversation.contact_id == Contact.id)
    if status_filter:
        q = q.where(Conversation.status == status_filter)
    elif active_only:
        # Bandeja 'Activas': todo menos cerradas (las archivadas ya se ocultan).
        q = q.where(Conversation.status != ConversationStatus.cerrada)
    if canal_filter:
        # Acepta el valor crudo del enum ConversationCanal (whatsapp | web |
        # instagram_dm). Si llega algo desconocido, simplemente devuelve vacío.
        q = q.where(Conversation.canal == canal_filter)
    # Filtro de archivadas: por defecto ocultamos. 'only' = solo archivadas.
    # 'all' = sin filtro.
    if archived == "hide":
        q = q.where(Conversation.archived_at.is_(None))
    elif archived == "only":
        q = q.where(Conversation.archived_at.is_not(None))
    # Cuarentena (spam en revisión): por defecto oculta de la bandeja; la cola
    # de revisión usa quarantine=only.
    if quarantine == "hide":
        q = q.where(Conversation.quarantined_at.is_(None))
    elif quarantine == "only":
        q = q.where(Conversation.quarantined_at.is_not(None))
    # Filtro "No leídas": conversaciones con algún mensaje del cliente sin leer.
    if unread_only:
        q = q.where(
            select(Message.id)
            .where(
                Message.conversation_id == Conversation.id,
                Message.rol == MessageRole.user,
                Message.leido_at.is_(None),
            )
            .exists()
        )
    # Pestaña "Pendientes" (vista por defecto del inbox): algo necesita a una
    # PERSONA. Tres casos: (a) hay sugerencia/borrador del agente sin resolver;
    # (b) el último mensaje es del cliente y nadie (bot ni humano) ha contestado;
    # (c) está derivada a humano y aún NO ha contestado una persona — el mensaje
    # puente del handoff es del bot (assistant), así que humano+último≠operator
    # sigue pendiente. En cuanto el operador responde (rol operator), DEJA de ser
    # pendiente (hasta que el cliente vuelva a escribir). El subquery del último
    # rol usa LIMIT 1 por conversación (tabla pequeña).
    # "Lo accionable" (reglas en _pending_expr / _suggestion_expr):
    if pending_only:
        q = q.where(_pending_expr())
    if suggestions_only:
        q = q.where(_suggestion_expr())
    # "Para hacer": Pendientes + Sugerencias en una sola lista.
    if action_only:
        q = q.where(_pending_expr() | _suggestion_expr())
    if search:
        # El buscador dice "Buscar mensajes, contactos…" — así que busca de
        # verdad: ficha del contacto (nombre, teléfono, email, @usuario de
        # Instagram), asunto del correo y TEXTO de los mensajes.
        #
        # Lo que NO se puede buscar aquí y conviene saber: las transcripciones
        # de audio (`Message.audio_transcript`) están CIFRADAS en la base de
        # datos, así que no hay LIKE que valga. El texto de los mensajes
        # (`Message.contenido`) sí está en claro y sí se busca.
        term = search.strip()
        like = f"%{_esc_like(term.lower())}%"
        like_raw = f"%{_esc_like(term)}%"
        msg_hit = (
            select(Message.id)
            .where(
                Message.conversation_id == Conversation.id,
                func.lower(Message.contenido).like(like, escape="\\"),
            )
            .exists()
        )
        q = q.where(
            func.lower(Contact.nombre).like(like, escape="\\")
            | Contact.telefono.like(like_raw, escape="\\")
            | func.lower(Contact.email).like(like, escape="\\")
            | func.lower(Contact.social_handle).like(like, escape="\\")
            | func.lower(Conversation.subject).like(like, escape="\\")
            | msg_hit
        )

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    rows = (
        await db.execute(
            # Un único criterio de orden, el que pidió el cliente. Se aplica
            # ANTES de paginar, así la primera página es de verdad la primera.
            q.order_by(*_inbox_order_by(sort))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    demo_set = await list_demo_conversations()

    # has_pending_draft (5d): en UNA sola query para toda la página (evita N+1),
    # calculamos el set de conversaciones que tienen algún Message borrador sin
    # enviar (extra.is_draft == true AND extra.draft_sent != true). En JSONB un
    # booleano true se compara por su texto "true".
    page_conv_ids = [conv.id for conv, _ in rows]
    pending_draft_ids: set[uuid.UUID] = set()
    # Separamos borradores de EMAIL (pendiente real) de sugerencias de
    # Entrenamiento (revisión, pestaña aparte).
    email_draft_ids: set[uuid.UUID] = set()
    training_suggestion_ids: set[uuid.UUID] = set()
    if page_conv_ids:
        draft_rows = (
            await db.execute(
                select(Message.conversation_id, Message.extra)
                .where(
                    Message.conversation_id.in_(page_conv_ids),
                    Message.extra["is_draft"].astext == "true",
                    func.coalesce(Message.extra["draft_sent"].astext, "false") != "true",
                )
            )
        ).all()
        for _cid, _extra in draft_rows:
            if (_extra or {}).get("training"):
                training_suggestion_ids.add(_cid)
            else:
                email_draft_ids.add(_cid)
        # Unión = "tiene algún borrador sin enviar" (para el badge/DraftCard).
        pending_draft_ids = email_draft_ids | training_suggestion_ids

    # Nº de mensajes del cliente sin leer por conversación, en UNA query agrupada
    # (evita N+1). El inbox lo pinta en negrita + badge cuando es > 0.
    unread_counts: dict[uuid.UUID, int] = {}
    if page_conv_ids:
        unread_rows = (
            await db.execute(
                select(Message.conversation_id, func.count())
                .where(
                    Message.conversation_id.in_(page_conv_ids),
                    Message.rol == MessageRole.user,
                    Message.leido_at.is_(None),
                )
                .group_by(Message.conversation_id)
            )
        ).all()
        unread_counts = {cid: int(n) for cid, n in unread_rows}

    # Preview del último mensaje, en UNA sola query para toda la página (antes
    # era un SELECT por fila → 51+ queries con page_size=50). DISTINCT ON
    # (conversation_id) + ORDER BY conversation_id, created_at DESC se queda con
    # el mensaje más reciente de cada conversación (PostgreSQL). Mismo patrón
    # agrupado que pending_draft_ids / unread_counts de arriba.
    previews: dict[uuid.UUID, str] = {}
    # Rol del último mensaje de cada conversación: se deriva de la MISMA query
    # del preview (solo añadimos el rol), gratis. Sirve para `needs_action`.
    last_role_by_conv: dict[uuid.UUID, MessageRole] = {}
    if page_conv_ids:
        last_rows = (
            await db.execute(
                select(
                    Message.conversation_id,
                    Message.rol,
                    Message.contenido,
                    Message.audio_transcript,
                    Message.extra,
                    Message.media_type,
                )
                .where(Message.conversation_id.in_(page_conv_ids))
                .distinct(Message.conversation_id)
                .order_by(Message.conversation_id, desc(Message.created_at))
            )
        ).all()
        for last_conv_id, last_rol, contenido, audio_transcript, extra, media_type in last_rows:
            last_role_by_conv[last_conv_id] = last_rol
            # Si el último mensaje es un correo purgado (retención email), su
            # contenido está vacío en BD: mostramos una etiqueta de archivo en
            # vez del fallback "[audio]" (que sería engañoso para un email).
            if (extra or {}).get("purged"):
                preview = "[correo archivado]"
            else:
                # Si no hay texto, decimos QUÉ llegó. Antes todo lo que no
                # fuese texto se etiquetaba "[audio]", así que una foto salía
                # en la bandeja como si fuera una nota de voz.
                preview = (
                    contenido
                    or audio_transcript
                    or MEDIA_PREVIEW_LABEL.get(media_type or "", "[audio]")
                )
            if preview and len(preview) > 80:
                preview = preview[:77] + "…"
            previews[last_conv_id] = preview

    items: list[ConversationOut] = []
    for conv, contact in rows:
        last_role = last_role_by_conv.get(conv.id)
        has_pending_draft = conv.id in pending_draft_ids
        has_training = conv.id in training_suggestion_ids
        has_email_draft = conv.id in email_draft_ids
        # "Necesita a una persona" (misma regla que el filtro pending_only):
        # borrador de EMAIL sin enviar, o el cliente escribió lo último sin
        # contestar, o derivada a humano y aún no ha respondido un operador. Las
        # sugerencias de Entrenamiento NO cuentan (van a la pestaña Sugerencias).
        needs_action = (
            has_email_draft
            or last_role == MessageRole.user
            or (conv.status == ConversationStatus.humano and last_role == MessageRole.assistant)
        )
        items.append(
            ConversationOut(
                id=conv.id,
                contact=ContactMini.model_validate(contact),
                canal=conv.canal.value,
                status=conv.status.value,
                started_at=conv.started_at.isoformat(),
                ended_at=conv.ended_at.isoformat() if conv.ended_at else None,
                last_message_at=conv.last_message_at.isoformat() if conv.last_message_at else None,
                resumen=conv.resumen,
                subject=conv.subject,
                call_duration_seconds=conv.call_duration_seconds,
                call_recording_url=conv.call_recording_url,
                call_ended_reason=conv.call_ended_reason,
                last_message_preview=previews.get(conv.id),
                demo_active=str(conv.id) in demo_set,
                archived=conv.archived_at is not None,
                quarantined=conv.quarantined_at is not None,
                quarantine_reason=conv.quarantine_reason,
                has_pending_draft=has_pending_draft,
                has_training_suggestion=has_training,
                unread_count=unread_counts.get(conv.id, 0),
                needs_action=needs_action,
            )
        )
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/counts")
async def conversation_counts(
    canal_filter: str | None = Query(None, alias="canal"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Contadores para los badges de las pestañas del inbox.

    Población = bandeja viva (no archivadas, no en cuarentena, no cerradas),
    salvo `quarantine` (la cola de Descartados). `action` es el total de "Para
    hacer" (Pendientes ∪ Sugerencias, sin doble conteo); `pending` y
    `suggestions` son el desglose para mostrar "X esperando · Y sugerencias".

    Si se pasa `canal`, los contadores se limitan a ese canal (así el badge
    cuadra con el filtro de canal seleccionado en el inbox).
    """
    live = (
        Conversation.archived_at.is_(None)
        & Conversation.quarantined_at.is_(None)
        & (Conversation.status != ConversationStatus.cerrada)
    )
    quarantined = (
        Conversation.archived_at.is_(None) & Conversation.quarantined_at.is_not(None)
    )
    if canal_filter:
        live = live & (Conversation.canal == canal_filter)
        quarantined = quarantined & (Conversation.canal == canal_filter)
    pending_e = _pending_expr()
    suggestion_e = _suggestion_expr()

    async def _count(condition) -> int:
        return (
            await db.execute(
                select(func.count()).select_from(Conversation).where(condition)
            )
        ).scalar_one()

    return {
        "action": await _count(live & (pending_e | suggestion_e)),
        "pending": await _count(live & pending_e),
        "suggestions": await _count(live & suggestion_e),
        # Conversaciones derivadas a humano (badge de la pestaña Humano).
        "humano": await _count(live & (Conversation.status == ConversationStatus.humano)),
        "quarantine": await _count(quarantined),
    }


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ConversationOut:
    row = (
        await db.execute(
            select(Conversation, Contact)
            .join(Contact, Conversation.contact_id == Contact.id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv, contact = row
    # Borradores sin enviar, separados por tipo (email pendiente vs sugerencia
    # de Entrenamiento). Una query trae el flag training de cada uno.
    draft_extras = (
        await db.execute(
            select(Message.extra).where(
                Message.conversation_id == conv.id,
                Message.extra["is_draft"].astext == "true",
                func.coalesce(Message.extra["draft_sent"].astext, "false") != "true",
            )
        )
    ).scalars().all()
    has_email_draft = any(not (e or {}).get("training") for e in draft_extras)
    has_training = any((e or {}).get("training") for e in draft_extras)
    has_pending_draft = has_email_draft or has_training
    unread_count = (
        await db.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == conv.id,
                Message.rol == MessageRole.user,
                Message.leido_at.is_(None),
            )
        )
    ).scalar_one()
    # Señal "pendiente" (misma regla que la lista): sugerencia sin resolver, o
    # cliente escribió lo último, o humano sin respuesta de un operador todavía.
    # De paso sacamos el último mensaje ENTERO para el preview: la lista ya lo
    # devuelve y esta ficha no, así que una conversación abierta desde el
    # contacto (deep-link) se pintaba sin vista previa.
    last_msg = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(desc(Message.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    last_role = last_msg.rol if last_msg else None
    last_preview: str | None = None
    if last_msg:
        if (last_msg.extra or {}).get("purged"):
            last_preview = "[correo archivado]"
        else:
            last_preview = (
                last_msg.contenido
                or last_msg.audio_transcript
                or MEDIA_PREVIEW_LABEL.get(last_msg.media_type or "", "[audio]")
            )
        if last_preview and len(last_preview) > 80:
            last_preview = last_preview[:77] + "…"
    needs_action = (
        has_email_draft
        or last_role == MessageRole.user
        or (conv.status == ConversationStatus.humano and last_role == MessageRole.assistant)
    )
    return ConversationOut(
        id=conv.id,
        contact=ContactMini.model_validate(contact),
        canal=conv.canal.value,
        status=conv.status.value,
        started_at=conv.started_at.isoformat(),
        ended_at=conv.ended_at.isoformat() if conv.ended_at else None,
        last_message_at=conv.last_message_at.isoformat() if conv.last_message_at else None,
        resumen=conv.resumen,
        subject=conv.subject,
        call_duration_seconds=conv.call_duration_seconds,
        call_recording_url=conv.call_recording_url,
        call_ended_reason=conv.call_ended_reason,
        demo_active=str(conv.id) in (await list_demo_conversations()),
        archived=conv.archived_at is not None,
        # Cuarentena: sin estos dos campos, una conversación en cuarentena
        # abierta desde la ficha del contacto se veía como una conversación
        # normal en la que el bot, misteriosamente, no contestaba. No salía ni
        # el aviso ni el botón de sacarla de la cuarentena.
        quarantined=conv.quarantined_at is not None,
        quarantine_reason=conv.quarantine_reason,
        last_message_preview=last_preview,
        has_pending_draft=has_pending_draft,
        has_training_suggestion=has_training,
        unread_count=unread_count,
        needs_action=needs_action,
    )


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    before: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[MessageOut]:
    q = select(Message).where(Message.conversation_id == conversation_id)
    if before:
        # Input externo crudo: un `before` malformado lanzaba ValueError → 500.
        # Validamos y respondemos 422 con un mensaje claro.
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Parámetro 'before' inválido: se espera una fecha ISO 8601",
            )
        q = q.where(Message.created_at < before_dt)
    rows = (await db.execute(q.order_by(desc(Message.created_at)).limit(limit))).scalars().all()
    rows = list(reversed(rows))  # cronológico ascendente
    return [_message_out(m) for m in rows]


@router.post("/{conversation_id}/messages")
async def send_operator_message(
    conversation_id: uuid.UUID,
    body: SendMessageBody,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MessageOut:
    row = (
        await db.execute(
            select(Conversation, Contact)
            .join(Contact, Conversation.contact_id == Contact.id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv, contact = row
    if conv.status == ConversationStatus.cerrada:
        raise HTTPException(status.HTTP_409_CONFLICT, "La conversación está cerrada")

    # Voz (Retell): el canal es de solo lectura desde el panel — no hay forma de
    # inyectar texto en una llamada. Antes channel_sender devolvía "" y se
    # persistía un mensaje que nunca llegaba al cliente (I7).
    if conv.canal == ConversationCanal.retell_voice:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "El canal de voz no admite respuestas desde el panel",
        )

    is_email = conv.canal == ConversationCanal.email

    # Ventana 24h: es una restricción EXCLUSIVA de WhatsApp (política de su API).
    # Instagram (Meta) y web NO deben heredarla — antes se comprobaba para todo
    # canal != email, bloqueando Instagram tras 24h con un aviso de "plantillas
    # WhatsApp" que no aplica. Solo la evaluamos para WhatsApp.
    if conv.canal == ConversationCanal.whatsapp:
        last_user_msg = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id, Message.rol == MessageRole.user)
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_user_msg and last_user_msg.created_at:
            age = datetime.now(timezone.utc) - last_user_msg.created_at
            if age > timedelta(hours=24):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Han pasado más de 24h desde el último mensaje del usuario. Las plantillas WhatsApp estarán disponibles en Fase 2.",
                )

    # Si estaba en bot, al responder el operador la conversación pasa a humano.
    # Excepción (5d/5b): para EMAIL NO cambiamos el status — el agente sigue
    # generando borradores; la operadora puede responder libremente sin "robarle"
    # el control al bot (coherente con la decisión de 5d sobre borradores).
    if conv.status == ConversationStatus.bot and not is_email:
        conv.status = ConversationStatus.humano

    # Instagram fuera de la ventana de Meta (>7 días): el envío se corta con un
    # aviso claro en vez de un 500 (aún no hemos hecho commit, así que el cambio
    # de status no se persiste).
    try:
        ext_id = await send_text_to_conversation(conv, body.contenido)
    except MessagingWindowClosed as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except SendNotConfirmed as e:
        # El proveedor no llegó a enviar (faltan credenciales o no se pudieron
        # descifrar). Ni se persiste el mensaje ni se devuelve 200: si esto
        # pasara callando, la operadora vería su respuesta en el hilo y el
        # cliente no habría recibido nada.
        logger.error("operator.send.not_confirmed", conversation_id=str(conv.id))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    # Marcamos sent_by="operator" para distinguir la respuesta manual de la
    # operadora (panel) de un saliente detectado por el poller (sent_by="gmail")
    # o un borrador del agente. provider_message_id (el id de Gmail para email)
    # cierra la idempotencia con el poller de "Enviados".
    msg_extra: dict = marcar_enviado({"sent_by": "operator"}, ext_id, conv.canal)

    msg = Message(
        conversation_id=conv.id,
        rol=MessageRole.operator,
        contenido=body.contenido,
        extra=msg_extra,
    )
    db.add(msg)
    conv.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(msg)

    try:
        await event_bus.publish(
            inbox_channel(),
            "message.new",
            {"conversation_id": str(conv.id), "message_id": str(msg.id), "rol": "operator"},
        )
    except Exception:
        pass

    return _message_out(msg)


@router.post("/{conversation_id}/attachment")
async def send_operator_attachment(
    conversation_id: uuid.UUID,
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MessageOut:
    """Envía un adjunto (audio/imagen/vídeo/doc/sticker) desde el panel.

    Flujo:
      1. Comprueba que el canal admite adjuntos (hoy, solo WhatsApp).
      2. Valida MIME y tamaño contra límites de WhatsApp.
      3. Sube a YCloud → obtiene media_id.
      4. Envía por WhatsApp con sendDirectly type=image|audio|video|document|sticker.
      5. Guarda el archivo en `/data/audios/uploads/{uuid}.ext` (para que
         el panel lo muestre en el histórico aunque YCloud purgue su copia).
      6. Inserta Message con rol=operator y media_*.

    El orden importa: el fichero se escribe en disco DESPUÉS de que el envío haya
    salido bien. Antes se guardaba primero y cualquier fallo posterior dejaba el
    fichero huérfano en el volumen.
    """
    row = (
        await db.execute(
            select(Conversation, Contact)
            .join(Contact, Conversation.contact_id == Contact.id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv, contact = row
    if conv.status == ConversationStatus.cerrada:
        raise HTTPException(status.HTTP_409_CONFLICT, "La conversación está cerrada")

    # Canal: este endpoint sube y envía SIEMPRE por WhatsApp. En Instagram o web
    # el envío se iba igualmente a YCloud con un identificador que no es un
    # teléfono (`ig:<psid>`, `web:<uuid>`) y el visitante veía un 502 que
    # hablaba del "proveedor de WhatsApp" en un chat de Instagram. Se corta aquí,
    # ANTES de leer el fichero y de escribir nada en disco (mismo patrón que el
    # canal de voz en send_operator_message).
    if conv.canal != ConversationCanal.whatsapp:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"El canal {conv.canal.value} no admite adjuntos desde el panel; "
            "de momento solo WhatsApp.",
        )

    # Ventana 24h (igual que send_text): restricción EXCLUSIVA de WhatsApp. El
    # envío de adjuntos va por YCloud (WhatsApp), así que solo aplica ahí.
    if conv.canal == ConversationCanal.whatsapp:
        last_user_msg = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id, Message.rol == MessageRole.user)
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_user_msg and last_user_msg.created_at:
            age = datetime.now(timezone.utc) - last_user_msg.created_at
            if age > timedelta(hours=24):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Han pasado más de 24h desde el último mensaje del cliente. Solo plantillas WhatsApp permitidas.",
                )

    # El tipo se decide ANTES de leer nada, y la lectura va acotada al tope de
    # ese tipo. Antes se hacía al revés: `await file.read()` metía el cuerpo
    # ENTERO en memoria y solo después se comprobaba el tamaño. Con el tope de
    # documento en 100 MB y sin nada delante que limite el cuerpo (el nginx del
    # panel no hace de proxy de la API: el navegador llama directo), unas pocas
    # peticiones en paralelo con un cuerpo de varios GB tumbaban el contenedor.
    mime = file.content_type or "application/octet-stream"
    try:
        media_kind = categorize_mime(mime)
        tope = max_bytes_for(media_kind)
    except MediaValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    # Se lee un byte de más: si aparece, es que se ha pasado del tope.
    file_bytes = await file.read(tope + 1)
    if not file_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Archivo vacío")
    try:
        validate_size(media_kind, len(file_bytes))
    except MediaValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    # Los errores de httpx/YCloud pueden incluir URLs internas y fragmentos de
    # la respuesta del proveedor: al cliente va un mensaje genérico; el detalle
    # completo queda en los logs.
    wa = get_whatsapp_provider()
    try:
        media_id = await wa.upload_media(file_bytes, mime, file.filename or "file")
    except Exception as e:
        logger.error("attachment.upload_failed", error=str(e))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "El proveedor de WhatsApp rechazó la subida del adjunto. Inténtalo de nuevo.",
        )
    try:
        ext_id = await wa.send_media(
            contact.telefono,
            media_id,
            media_kind,  # type: ignore[arg-type]
            caption=caption,
            filename=file.filename if media_kind == "document" else None,
        )
    except Exception as e:
        logger.error("attachment.send_failed", error=str(e), media_kind=media_kind)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "El proveedor de WhatsApp rechazó el envío del adjunto. Inténtalo de nuevo.",
        )
    if ext_id == PROVIDER_NOOP_ID:
        # Sin credenciales el provider no llega a llamar a la API y devuelve
        # "noop". No es un envío: ni 200 al panel ni mensaje en el hilo.
        logger.error("attachment.not_confirmed", conversation_id=str(conv.id))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "El adjunto no se envió: faltan credenciales de WhatsApp o no se "
            "pudieron descifrar (revisa /admin/credentials).",
        )

    # Envío confirmado: ya podemos dejar la copia local para el histórico.
    _safe_name, public_path = save_local(file_bytes, file.filename or "file")

    # Si la conv estaba en bot, al enviar el operador pasa a humano.
    if conv.status == ConversationStatus.bot:
        conv.status = ConversationStatus.humano

    msg = Message(
        conversation_id=conv.id,
        rol=MessageRole.operator,
        contenido=caption,  # caption visible junto al adjunto (image/video/doc)
        media_type=media_kind,
        media_url=public_path,
        media_mime=mime,
        media_size=len(file_bytes),
        media_filename=file.filename,
        extra=marcar_enviado({"media_provider_id": media_id}, ext_id, conv.canal),
    )
    db.add(msg)
    conv.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(msg)

    try:
        await event_bus.publish(
            inbox_channel(),
            "message.new",
            {"conversation_id": str(conv.id), "message_id": str(msg.id), "rol": "operator"},
        )
    except Exception:
        pass

    return _message_out(msg)


@router.post("/{conversation_id}/take-over", response_model=OkResponse)
async def take_over(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkResponse:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv.status = ConversationStatus.humano
    conv.asignada_a = user.id
    await db.commit()
    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "status": "humano"},
        )
    except Exception:
        pass
    return OkResponse()


@router.post("/{conversation_id}/return-to-bot", response_model=OkResponse)
async def return_to_bot(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OkResponse:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv.status = ConversationStatus.bot
    conv.asignada_a = None
    await db.commit()
    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "status": "bot"},
        )
    except Exception:
        pass
    return OkResponse()


@router.post("/{conversation_id}/demo-enable", response_model=OkResponse)
async def demo_enable(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OkResponse:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    await add_demo_conversation(conv.id)
    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "demo_active": True},
        )
    except Exception:
        pass
    return OkResponse()


@router.delete("/{conversation_id}/demo-enable", response_model=OkResponse)
async def demo_disable(
    conversation_id: uuid.UUID,
    _: User = Depends(get_current_user),
) -> OkResponse:
    await remove_demo_conversation(conversation_id)
    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conversation_id), "demo_active": False},
        )
    except Exception:
        pass
    return OkResponse()


@router.post("/{conversation_id}/archive", response_model=OkResponse)
async def archive_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OkResponse:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv.archived_at = datetime.now(timezone.utc)
    await db.commit()
    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "archived": True},
        )
    except Exception:
        pass
    return OkResponse()


@router.delete("/{conversation_id}/archive", response_model=OkResponse)
async def unarchive_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OkResponse:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv.archived_at = None
    await db.commit()
    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "archived": False},
        )
    except Exception:
        pass
    return OkResponse()


class BulkArchiveBody(BaseModel):
    ids: list[uuid.UUID]


@router.post("/bulk-archive", response_model=OkResponse)
async def bulk_archive_conversations(
    body: BulkArchiveBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> OkResponse:
    """Archiva varias conversaciones a la vez (p.ej. vaciar la cola de
    'Revisión' de un tirón). Idempotente. Las conversaciones siguen siendo
    recuperables desde la pestaña 'Archivadas' (que ahora muestra también las
    que estaban en cuarentena).

    SOLO admin: es la acción MASIVA de la bandeja (hasta 500 de una). Archivar
    UNA sigue siendo del día a día del operador (`POST /{id}/archive`); vaciar
    la bandeja entera de golpe, no. Queda registrada en auditoría con el
    número de conversaciones afectadas.
    """
    ids = body.ids[:500]  # límite defensivo
    if not ids:
        return OkResponse()
    now = datetime.now(timezone.utc)
    convs = (
        await db.execute(select(Conversation).where(Conversation.id.in_(ids)))
    ).scalars().all()
    for conv in convs:
        conv.archived_at = now
    await record_audit(
        db,
        user_id=current_user.id,
        action="conversation.bulk_archived",
        entity="conversation",
        entity_id=None,
        after={
            "solicitadas": len(ids),
            "archivadas": len(convs),
            "ids": [str(c.id) for c in convs][:500],
        },
    )
    await db.commit()
    for conv in convs:
        try:
            await event_bus.publish(
                inbox_channel(),
                "conversation.updated",
                {"conversation_id": str(conv.id), "archived": True},
            )
        except Exception:
            pass
    return OkResponse()


async def _regla_que_retiene(conv: Conversation, db: AsyncSession):
    """La regla fija que volvería a descartar esta conversación, o None.

    Mira el último mensaje del cliente con los MISMOS datos que usa el runtime
    (dirección en email; nombre, teléfono y @usuario en el resto), para que lo
    que dice el panel al liberar y lo que hace el bot al llegar el siguiente
    mensaje no puedan contradecirse.
    """
    from app.services.classifier_rules import match_rules

    msg = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.rol == MessageRole.user)
            .order_by(desc(Message.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if msg is None:
        return None
    extra = msg.extra or {}
    if conv.canal == ConversationCanal.email:
        return await match_rules(
            [extra.get("from_addr")], extra.get("subject"), conv.canal.value
        )
    contact = (
        await db.execute(select(Contact).where(Contact.id == conv.contact_id))
    ).scalar_one_or_none()
    if contact is None:
        return None
    return await match_rules(
        [contact.nombre, contact.telefono, contact.social_handle], None, conv.canal.value
    )


@router.post("/{conversation_id}/release-quarantine", response_model=OkResponse)
async def release_quarantine(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> OkResponse:
    """Libera una conversación retenida por el clasificador ('no es spam'): la
    marca como revisada y re-dispara el bot sobre el último mensaje del cliente.

    SOLO admin: esto DESACTIVA un control de seguridad (el antispam) para esa
    conversación y además re-lanza al modelo, que cuesta dinero. Es una decisión
    de quien responde del sistema, no del turno de bandeja. Queda en auditoría
    con el motivo por el que estaba retenida.
    """
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    # Si una REGLA FIJA sigue cazando a este remitente, liberar no sirve de
    # nada: el siguiente mensaje la vuelve a retener (una regla es una orden
    # explícita, no una heurística) y el operador ve la conversación caer otra
    # vez en Descartados sin saber por qué. Mejor decirlo y no tocar nada: se
    # borra o se desactiva la regla, y entonces se libera.
    regla = await _regla_que_retiene(conv, db)
    if regla is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No se puede liberar: la regla fija «{regla.campo}: {regla.valor}» "
            "sigue activa y volvería a descartar el siguiente mensaje. "
            "Desactívala o bórrala en Agentes → Clasificador y vuelve a intentarlo.",
        )

    motivo = conv.quarantine_reason
    conv.quarantined_at = None
    conv.quarantine_reason = None
    conv.spam_reviewed = True
    await record_audit(
        db,
        user_id=current_user.id,
        action="conversation.quarantine_released",
        entity="conversation",
        entity_id=conv.id,
        before={"quarantine_reason": motivo},
        after={"re_dispara_bot": conv.status == ConversationStatus.bot},
    )
    await db.commit()
    # Si al descartarlo se archivó en Gmail, liberarlo lo devuelve a Recibidos y
    # le quita la etiqueta: si no, el correo se quedaría escondido en el buzón
    # aunque en el panel ya conste como bueno. Best-effort, no corta la respuesta.
    from app.services.gmail_quarantine import restore_in_gmail

    await restore_in_gmail(conv.id, conv.canal.value)
    if conv.status == ConversationStatus.bot:
        msg = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id, Message.rol == MessageRole.user)
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if msg:
            from app.tasks.process_message import process_message as task_process
            task_process.delay(str(msg.id))
    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "quarantined": False},
        )
    except Exception:
        pass
    return OkResponse()


class CloseBody(BaseModel):
    resumen: str | None = None
    enviar_resumen_email: bool = False


@router.post("/{conversation_id}/close", response_model=OkResponse)
async def close_conversation(
    conversation_id: uuid.UUID,
    body: CloseBody,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OkResponse:
    row = (
        await db.execute(
            select(Conversation, Contact)
            .join(Contact, Conversation.contact_id == Contact.id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    conv, contact = row
    conv.status = ConversationStatus.cerrada
    conv.ended_at = datetime.now(timezone.utc)
    if body.resumen:
        conv.resumen = body.resumen
    await db.commit()

    if body.enviar_resumen_email and contact.email and (body.resumen or conv.resumen):
        import html as _html

        from app.providers.email import send_email
        subject = "Resumen de tu consulta"
        nombre_seguro = _html.escape(contact.nombre) if contact.nombre else ""
        resumen_seguro = _html.escape(body.resumen or conv.resumen or "").replace("\n", "<br>")
        body_html = (
            f"<p>Hola{(' ' + nombre_seguro) if nombre_seguro else ''},</p>"
            "<p>Aquí tienes el resumen de nuestra conversación:</p>"
            f"<blockquote>{resumen_seguro}</blockquote>"
            "<p>Si tienes cualquier duda, respóndenos.</p>"
        )
        await send_email(contact.email, subject, body_html)

    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "status": "cerrada"},
        )
    except Exception:
        pass

    # Canal web: avisamos AL VISITANTE de que la operadora ha cerrado el chat.
    # Sin este evento su burbuja se queda abierta como si nada: al escribir se
    # topaba con un conflicto y lo interpretaba como sesión caducada, abriendo
    # una conversación nueva. El widget escucha `conversation.closed` y pinta el
    # cierre. Un fallo publicando NO puede tumbar el cierre (que ya está en BD),
    # así que va en su propio try aparte.
    if conv.canal == ConversationCanal.web:
        try:
            await event_bus.publish(
                webchat_channel(conv.id),
                "conversation.closed",
                {"conversation_id": str(conv.id)},
            )
        except Exception as e:
            logger.warning("webchat.closed_publish_failed", error=str(e))
    return OkResponse()


@router.post("/{conversation_id}/mark-read", response_model=OkResponse)
async def mark_read(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OkResponse:
    # Que la conversación EXISTA. Sin esto, marcar leído un id inventado (o uno
    # ya borrado) respondía 200 "ok" sin haber tocado nada: el panel se quedaba
    # tan tranquilo creyendo que lo había hecho.
    exists = (
        await db.execute(select(Conversation.id).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversación no encontrada")
    # Un único UPDATE en vez de load+loop: marca leídos todos los mensajes del
    # cliente pendientes de esta conversación (apoyado por el índice parcial
    # ix_messages_unread de la migración 0023).
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.rol == MessageRole.user,
            Message.leido_at.is_(None),
        )
        .values(leido_at=now)
    )
    await db.commit()
    return OkResponse()


# ---------------------------------------------------------------------------
# F5c — Borradores del agente (canal Email). El agente NUNCA envía un correo:
# crea un borrador REAL en Gmail que la operadora revisa, edita y envía desde
# aquí. Estos endpoints requieren la misma auth que el resto del panel y
# validan ownership (que el Message pertenece a la conversation).
# ---------------------------------------------------------------------------


class SendDraftBody(BaseModel):
    text: str


class RefineDraftBody(BaseModel):
    instruction: str


async def _load_draft_message(
    db: AsyncSession, conversation_id: uuid.UUID, message_id: uuid.UUID
) -> tuple[Conversation, Contact, Message]:
    """Carga conv + contact + el Message borrador, validando ownership y estado.

    Lanza HTTPException si no existe, no pertenece a la conversación, no es un
    borrador o ya fue enviado."""
    row = (
        await db.execute(
            select(Conversation, Contact)
            .join(Contact, Conversation.contact_id == Contact.id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversación no encontrada")
    conv, contact = row
    msg = (
        await db.execute(select(Message).where(Message.id == message_id))
    ).scalar_one_or_none()
    # Ownership: el message debe pertenecer a esta conversación.
    if not msg or msg.conversation_id != conv.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Borrador no encontrado")
    extra = msg.extra or {}
    if not extra.get("is_draft"):
        raise HTTPException(status.HTTP_409_CONFLICT, "El mensaje no es un borrador")
    if extra.get("draft_sent"):
        raise HTTPException(status.HTTP_409_CONFLICT, "El borrador ya fue enviado")
    return conv, contact, msg


@router.post("/{conversation_id}/drafts/{message_id}/send")
async def send_draft(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    body: SendDraftBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageOut:
    """Envía un borrador/sugerencia del agente (posiblemente editado por la
    operadora).

    Cubre dos orígenes con la MISMA UI de borrador:
      - **Email (legacy):** el borrador tiene un `gmail_draft_id`. Si el texto
        cambió, actualiza el draft en Gmail (manteniendo el enhebrado) y lo
        envía por Gmail.
      - **Entrenamiento (cualquier otro canal):** la sugerencia NO tiene
        `gmail_draft_id`. Se envía por el canal correcto vía
        `send_text_to_conversation` (WhatsApp/IG/Web), igual que enviaría el
        agente en modo Activo.

    Al éxito, marca el Message como enviado y devuelve su versión actualizada."""
    conv, contact, msg = await _load_draft_message(db, conversation_id, message_id)

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El borrador no puede estar vacío")

    # Borrador ORIGINAL del agente (antes de tu edición a mano). Lo guardamos
    # ahora porque más abajo `msg.contenido` se sobrescribe con el texto enviado.
    original_draft = (msg.contenido or "").strip()

    extra = dict(msg.extra or {})
    draft_id = extra.get("gmail_draft_id")

    if draft_id:
        # --- Email (Gmail) — comportamiento legacy intacto ---
        from app.providers.gmail import get_gmail_provider

        gmail = get_gmail_provider()

        # Si la operadora editó el texto, re-escribimos el borrador en Gmail con
        # las mismas cabeceras de hilo del último mensaje entrante (para no
        # perder el enhebrado). Si no cambió, enviamos el draft tal cual.
        if text != (msg.contenido or ""):
            last_in = (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id, Message.rol == MessageRole.user)
                    .order_by(desc(Message.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            in_extra = (last_in.extra if last_in else {}) or {}
            in_reply_to = in_extra.get("rfc822_message_id")
            prev_refs = in_extra.get("references")
            references = (
                f"{prev_refs} {in_reply_to}".strip()
                if prev_refs and in_reply_to
                else (in_reply_to or prev_refs)
            )
            subject = in_extra.get("subject") or conv.subject or ""
            try:
                await gmail.update_draft(
                    draft_id=draft_id,
                    thread_id=conv.gmail_thread_id or "",
                    to_addr=contact.email or "",
                    subject=subject,
                    body_text=text,
                    in_reply_to=in_reply_to,
                    references=references,
                )
            except Exception as e:
                logger.error("draft.update_failed", conversation_id=str(conv.id), error=str(e))
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, f"Gmail rechazó la edición del borrador: {e}"
                )

        try:
            sent = await gmail.send_draft(draft_id)
        except Exception as e:
            logger.error("draft.send_failed", conversation_id=str(conv.id), error=str(e))
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Gmail rechazó el envío del borrador: {e}"
            )
        provider_message_id = sent.get("message_id")
    else:
        # --- Entrenamiento (WhatsApp / Instagram / Web) ---
        # Enviamos por el canal de la conversación, igual que el modo Activo. No
        # troceamos: la operadora envía exactamente el texto que dejó. Si el
        # canal no devuelve id (web), provider_message_id queda vacío.
        try:
            provider_message_id = await send_text_to_conversation(conv, text)
        except MessagingWindowClosed as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        except Exception as e:
            logger.error("suggestion.send_failed", conversation_id=str(conv.id), error=str(e))
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"No se pudo enviar la sugerencia: {e}"
            )

    # Actualiza el Message: ya no es borrador, queda como respuesta enviada.
    # Conservamos extra.training (si lo tenía) como traza del origen.
    extra["is_draft"] = False
    extra["draft_sent"] = True
    extra = marcar_enviado(extra, provider_message_id, conv.canal)

    # Aprendizaje: si editaste el borrador a mano antes de enviar, registramos la
    # corrección (igual que "Corregir", pero con marcador de edición manual). Así
    # tu feedback queda guardado y visible en "Aprendizajes". Si no cambiaste el
    # texto, o ya venía de "Corregir" (mismo texto), no se duplica.
    if original_draft and text != original_draft:
        from app.models.agent_correction import AgentCorrection, MANUAL_EDIT_INSTRUCTION
        db.add(
            AgentCorrection(
                conversation_id=conv.id,
                message_id=msg.id,
                canal=conv.canal.value,
                original_text=original_draft,
                instruction=MANUAL_EDIT_INSTRUCTION,
                resulting_text=text,
                created_by=current_user.id,
            )
        )

    msg.contenido = text
    msg.extra = extra
    conv.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(msg)

    try:
        await event_bus.publish(
            inbox_channel(),
            "message.updated",
            {"conversation_id": str(conv.id), "message_id": str(msg.id), "draft_sent": True},
        )
    except Exception:
        pass

    return _message_out(msg)


@router.post("/{conversation_id}/drafts/{message_id}/refine")
@limiter.limit(limit_spec("20/minute"))
async def refine_draft(
    request: Request,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    body: RefineDraftBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    """Autoaprendizaje Fase 1 — "Corrige y re-redacta".

    En vez de reescribir tú el texto a mano, le das al agente una instrucción en
    lenguaje natural ("sé más cálida, no ofrezcas descuentos") y RE-REDACTA la
    respuesta. Sigue siendo un borrador (no se envía). Cada corrección se
    registra (AgentCorrection) como materia prima de la Fase 2.

    Funciona igual para sugerencias de Entrenamiento (WhatsApp/IG/Web) y para
    borradores de email; en email reescribimos también el borrador en Gmail."""
    from app.agents.orchestrator import run_agent
    from app.models.agent_correction import AgentCorrection
    from app.providers.llm.base import LLMMessage
    from app.services.runtime_config import get_runtime_for_channel

    instruction = (body.instruction or "").strip()
    if not instruction:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Dime qué quieres cambiar")
    instruction = instruction[:1000]

    # Cupo diario por usuario ANTES de tocar el modelo (esto es lo que cuesta).
    allowed, used = await consume_user_daily_quota(
        "refine", user.id, REFINE_DAILY_LIMIT_PER_USER
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Has llegado a tu tope diario de re-redacciones ({REFINE_DAILY_LIMIT_PER_USER}). "
            "Se reinicia mañana; si necesitas más, pídeselo a un admin.",
        )

    conv, contact, msg = await _load_draft_message(db, conversation_id, message_id)
    previous_text = msg.contenido or ""

    agent_cfg = await get_runtime_for_channel(conv.canal.value)
    if not agent_cfg:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No hay un agente configurado para este canal"
        )

    # Historial igual que en la generación normal: últimos N mensajes en orden,
    # EXCLUYENDO borradores sin enviar (incluido este). El borrador a corregir lo
    # añadimos aparte como última respuesta del agente, para que el modelo sepa
    # qué tiene que reescribir.
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc())
            .limit(agent_cfg.context_window)
        )
    ).scalars().all()
    rows = list(reversed(rows))
    role_map = {
        MessageRole.user: "user",
        MessageRole.assistant: "assistant",
        MessageRole.operator: "assistant",
        MessageRole.system: "system",
    }
    history: list[LLMMessage] = []
    for m in rows:
        mextra = m.extra or {}
        if mextra.get("is_draft") and not mextra.get("draft_sent"):
            continue
        content = m.contenido or m.audio_transcript
        if not content:
            continue
        history.append(LLMMessage(role=role_map[m.rol], content=content))
    if previous_text:
        history.append(LLMMessage(role="assistant", content=previous_text))

    steer = (
        "[Instrucción interna del equipo — NO la menciones al cliente] "
        "La operadora ha revisado tu última respuesta y te indica cómo "
        f"mejorarla: {instruction}\n\n"
        "Reescribe esa respuesta para el cliente aplicando la indicación. "
        "Devuelve SOLO la respuesta final, sin comentarios ni explicaciones."
    )

    try:
        new_text = await run_agent(
            system_prompt=agent_cfg.prompt_system,
            history=history,
            user_message=steer,
            model=agent_cfg.model_name,
            temperature=float(agent_cfg.temperature),
            max_tokens=agent_cfg.max_tokens,
            context={"conversation_id": str(conv.id), "telefono": contact.telefono},
            tools_enabled=agent_cfg.tools_enabled,
            llm_provider_id=agent_cfg.llm_provider_id,
            fallback_provider_id=agent_cfg.fallback_provider_id,
            fallback_model=agent_cfg.fallback_model,
        )
    except Exception as e:
        logger.error("draft.refine_failed", conversation_id=str(conv.id), error=str(e))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "El agente no pudo re-redactar la respuesta. Inténtalo de nuevo.",
        )

    new_text = (new_text or "").strip()
    if not new_text:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "El agente devolvió una respuesta vacía. Prueba a reformular la corrección.",
        )

    extra = dict(msg.extra or {})
    draft_id = extra.get("gmail_draft_id")
    if draft_id:
        # Email: reescribimos también el borrador en Gmail (manteniendo el hilo)
        # para que al enviar salga el texto corregido.
        from app.providers.gmail import get_gmail_provider

        last_in = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id, Message.rol == MessageRole.user)
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        in_extra = (last_in.extra if last_in else {}) or {}
        in_reply_to = in_extra.get("rfc822_message_id")
        prev_refs = in_extra.get("references")
        references = (
            f"{prev_refs} {in_reply_to}".strip()
            if prev_refs and in_reply_to
            else (in_reply_to or prev_refs)
        )
        subject = in_extra.get("subject") or conv.subject or ""
        try:
            await get_gmail_provider().update_draft(
                draft_id=draft_id,
                thread_id=conv.gmail_thread_id or "",
                to_addr=contact.email or "",
                subject=subject,
                body_text=new_text,
                in_reply_to=in_reply_to,
                references=references,
            )
        except Exception as e:
            logger.error(
                "draft.refine.gmail_update_failed",
                conversation_id=str(conv.id),
                error=str(e),
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Gmail rechazó la actualización del borrador: {e}",
            )

    # Persistimos el nuevo texto (sigue siendo borrador) y registramos la
    # corrección para aprender de ella (Fase 2).
    msg.contenido = new_text
    db.add(
        AgentCorrection(
            conversation_id=conv.id,
            message_id=msg.id,
            canal=conv.canal.value,
            original_text=previous_text or None,
            instruction=instruction,
            resulting_text=new_text,
            created_by=user.id,
        )
    )
    await db.commit()
    await db.refresh(msg)

    try:
        await event_bus.publish(
            inbox_channel(),
            "message.updated",
            {"conversation_id": str(conv.id), "message_id": str(msg.id), "refined": True},
        )
    except Exception:
        pass

    return _message_out(msg)


@router.post("/{conversation_id}/drafts/{message_id}/discard")
async def discard_draft(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OkResponse:
    """Descarta un borrador/sugerencia del agente y elimina su Message.

    Para borradores de email también borra el draft en Gmail (best-effort). Para
    sugerencias de Entrenamiento (sin gmail_draft_id) solo elimina el Message."""
    from app.providers.gmail import get_gmail_provider

    conv, _contact, msg = await _load_draft_message(db, conversation_id, message_id)
    extra = msg.extra or {}
    draft_id = extra.get("gmail_draft_id")
    if draft_id:
        # Best-effort: si Gmail falla, igual quitamos el Message del panel.
        await get_gmail_provider().delete_draft(draft_id)

    await db.delete(msg)
    await db.commit()

    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conv.id), "draft_discarded": True},
        )
    except Exception:
        pass

    return OkResponse()


# ---------------------------------------------------------------------------
# Retención email — Recuperar el contenido de un correo PURGADO bajo demanda.
# Gmail es el archivo; a los N meses el contenido del correo se vacía de la BD
# (ver services/gmail_retention.py) conservando solo el id de Gmail. Aquí lo
# volvemos a traer de Gmail por ese id y lo DEVOLVEMOS para mostrarlo, sin
# re-persistirlo (la minimización se mantiene).
# ---------------------------------------------------------------------------


class RecoveredEmail(BaseModel):
    """Contenido recuperado de Gmail para mostrar (NO se persiste en BD)."""

    contenido: str
    html_body: str | None = None


@router.post("/{conversation_id}/messages/{message_id}/recover")
async def recover_email(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> RecoveredEmail:
    """Recupera el cuerpo de un correo archivado (purgado) desde Gmail.

    El contenido NO se vuelve a guardar en la BD: solo se devuelve para mostrarlo
    en el panel (la minimización de datos se mantiene). Requiere auth y valida
    que el Message pertenece a la conversación (mismo patrón que los borradores).
    """
    from app.providers.gmail import get_gmail_provider
    from app.providers.gmail.client import sanitize_email_html_document

    # Ownership: la conversación debe existir y el message pertenecerle.
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversación no encontrada")
    msg = (
        await db.execute(select(Message).where(Message.id == message_id))
    ).scalar_one_or_none()
    if not msg or msg.conversation_id != conv.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mensaje no encontrado")

    extra = msg.extra or {}
    if not extra.get("purged"):
        raise HTTPException(status.HTTP_409_CONFLICT, "No es un mensaje archivado")

    provider_message_id = extra.get("provider_message_id")
    if not provider_message_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "El mensaje no tiene id de Gmail para recuperarlo",
        )

    try:
        fetched = await get_gmail_provider().get_message(provider_message_id)
    except Exception as e:
        logger.error("email.recover.failed", conversation_id=str(conv.id), error=str(e))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "No se pudo recuperar de Gmail (puede que ya no exista o falte la conexión)",
        )
    if not fetched:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "No se pudo recuperar de Gmail (puede que ya no exista o falte la conexión)",
        )

    # Re-saneo del HTML igual que en la ingesta (primera barrera a nivel de
    # documento). El saneado real (lista blanca) lo hace el frontend con
    # DOMPurify. NO persistimos nada: solo devolvemos para mostrar.
    html = sanitize_email_html_document(fetched.get("html") or "") or None
    return RecoveredEmail(contenido=fetched.get("body") or "", html_body=html)


# ---------------------------------------------------------------------------
# Descarga de adjuntos de correo. Gmail es el ARCHIVO: aquí no se guarda copia
# del fichero, se pide el binario bajo demanda y se pasa al panel. Sin esto la
# operadora tenía que salirse a Gmail para abrir lo que le mandaba un cliente.
# Misma comprobación de propiedad que `recover_email` (la conversación existe y
# el mensaje es suyo) y, además, el adjunto tiene que estar en la ficha DE ESE
# mensaje: si no, un id de adjunto suelto serviría para bajar ficheros de
# cualquier otro correo del buzón.
# ---------------------------------------------------------------------------


def _content_disposition(filename: str) -> str:
    """Cabecera de descarga con el nombre original.

    Doble forma (RFC 6266): un `filename` ASCII para los clientes antiguos y un
    `filename*` en UTF-8 para los nombres con tildes o eñes. La versión ASCII se
    limpia a conciencia porque va DENTRO de comillas en una cabecera HTTP: un
    nombre con comillas o saltos de línea permitiría inyectar cabeceras."""
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip() or "adjunto"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


@router.get(
    "/{conversation_id}/messages/{message_id}/attachments/{attachment_id}",
    response_class=Response,
)
@limiter.limit(limit_spec("60/minute"))
async def download_email_attachment(
    request: Request,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    """Descarga un adjunto de un correo desde Gmail y lo sirve al panel.

    Devuelve 404 si la conversación o el mensaje no existen (o el mensaje no es
    de esa conversación), 409 si el correo se ingirió antes de que se guardara
    la ficha del adjunto (no hay id con el que pedírselo a Gmail) y 502 si Gmail
    no responde o falta la conexión.
    """
    from app.providers.gmail import get_gmail_provider

    # Ownership: la conversación debe existir y el message pertenecerle.
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversación no encontrada")
    msg = (
        await db.execute(select(Message).where(Message.id == message_id))
    ).scalar_one_or_none()
    if not msg or msg.conversation_id != conv.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mensaje no encontrado")

    extra = msg.extra or {}
    adjuntos = extra.get("attachments") or []
    # Correos ingeridos ANTES de que el proveedor guardara la ficha completa:
    # `attachments` es una lista de nombres, sin `attachment_id`. No se pueden
    # bajar, pero eso NO es un error del servidor: se explica qué hacer.
    if adjuntos and all(isinstance(a, str) for a in adjuntos):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Este correo se guardó antes de que se registraran los datos del "
            "adjunto: solo quedó su nombre. Pulsa 'Recuperar de Gmail' para "
            "volver a leerlo y poder descargarlo.",
        )

    ficha = next(
        (
            a
            for a in adjuntos
            if isinstance(a, dict) and a.get("attachment_id") == attachment_id
        ),
        None,
    )
    if not ficha:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Adjunto no encontrado")

    provider_message_id = extra.get("provider_message_id")
    if not provider_message_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "El mensaje no tiene id de Gmail para descargar el adjunto",
        )

    try:
        data = await get_gmail_provider().get_attachment(
            provider_message_id, attachment_id
        )
    except Exception as e:
        logger.error(
            "email.attachment.failed", conversation_id=str(conv.id), error=str(e)
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "No se pudo descargar de Gmail (puede que ya no exista o falte la conexión)",
        )
    if data is None:
        # get_attachment devuelve None cuando no hay credenciales de Gmail.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "No se pudo descargar de Gmail (puede que ya no exista o falte la conexión)",
        )

    filename = str(ficha.get("filename") or "adjunto")
    return Response(
        content=data,
        media_type=str(ficha.get("mime_type") or "application/octet-stream"),
        headers={"Content-Disposition": _content_disposition(filename)},
    )


# WebSocket
@ws_router.websocket("/ws/inbox")
async def ws_inbox(websocket: WebSocket, ticket: str | None = None) -> None:
    """Empuja al panel los eventos de la bandeja.

    Autenticación por TICKET efímero de un solo uso (POST /auth/ws-ticket): el
    JWT completo en la query string acababa en logs de proxies. El fallback de
    transición `?token=` se retiró una vez desplegado el frontend con tickets
    (los frontends servidos siempre van a la par del backend).

    Cuatro tareas en paralelo; la primera que termine cierra la conexión (mismo
    patrón que el WebSocket del widget en api/webchat.py):

      · lector del socket — la ÚNICA forma de enterarse de que la operadora ha
        cerrado la pestaña. Starlette solo detecta la desconexión en `receive()`
        o cuando falla un `send`. Antes esto era un `async for` sobre el bus sin
        leer nunca del socket: si en el canal de la bandeja no se publicaba nada,
        la corrutina se quedaba colgada para siempre con su cola y su tarea. El
        bus ya comparte una sola conexión a Redis por proceso, así que no fugaba
        conexiones, pero sí una tarea y una cola por pestaña abierta.
      · bombeo del bus — termina también si el bus se cae, para que el panel
        reconecte en vez de quedarse "conectado" y mudo.
      · latido — detecta el enlace muerto en sentido servidor→panel.
      · vida máxima — ninguna conexión vive para siempre.
    """
    user = None
    if ticket:
        from app.api.auth import consume_ws_ticket
        from app.models.user import User as _User
        from sqlalchemy import select as _select

        uid = await consume_ws_ticket(ticket)
        if uid:
            async with SessionLocal() as db:
                user = (
                    await db.execute(_select(_User).where(_User.id == uuid.UUID(uid)))
                ).scalar_one_or_none()
            if user and not user.activo:
                user = None
    if not user:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    channel = inbox_channel()
    send_lock = asyncio.Lock()

    async def _send(payload: str) -> None:
        async with send_lock:
            await websocket.send_text(payload)

    async def _drain_socket() -> None:
        """No esperamos nada del operador; leemos solo para saber si sigue ahí."""
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

    async def _pump_bus() -> None:
        events = event_bus.subscribe(channel)
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
    except WebSocketDisconnect:
        logger.info("ws.disconnect")
    except Exception as e:
        logger.error("ws.error", error=str(e))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await websocket.close()
