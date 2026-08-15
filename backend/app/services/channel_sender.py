"""Dispatcher de envío de respuestas según canal (F4 / F5b).

El runtime (o la operadora desde el panel) decide a qué canal corresponde una
conversación y cómo enviar la respuesta:
  - whatsapp → YCloud (HTTP API)
  - web      → EventBus pub a canal específico de la conversación (lo recoge
               el WebSocket abierto del visitor)
  - instagram_dm → Graph API (Instagram Messaging)
  - email    → Gmail users.messages.send (F5b — respuesta libre en el hilo)

Devuelve un id externo si lo hay (para deduplicación + audit). Para web no
hay id externo, devuelve un uuid local. Para email devuelve el message_id de
Gmail (lo guarda el llamante en Message.extra para cerrar la idempotencia con
el poller de "Enviados").
"""
from __future__ import annotations

import uuid

from app.core.events import event_bus
from app.core.logging import get_logger
from app.models.conversation import Conversation, ConversationCanal
from app.providers.instagram import get_instagram_provider
from app.providers.whatsapp import get_whatsapp_provider

logger = get_logger(__name__)

# Ventanas de mensajería de Instagram (política de Meta):
#   - ≤24h desde el último mensaje del cliente → respuesta estándar.
#   - 24h–7 días → SOLO con la etiqueta HUMAN_AGENT (agente humano).
#   - >7 días → Meta NO permite responder (arriesga baneo si se fuerza).
IG_STANDARD_WINDOW_HOURS = 24
IG_HUMAN_AGENT_WINDOW_HOURS = 24 * 7

# WhatsApp: fuera de las 24 h desde el último mensaje del cliente solo se puede
# responder con plantilla aprobada. El panel ya lo comprobaba antes de enviar a
# mano; el agente no, así que su envío se iba al proveedor y volvía como error.
WA_SESSION_WINDOW_HOURS = 24

# Lo que devuelven los providers de WhatsApp/Instagram cuando NO han llegado a
# hacer la llamada por falta de credenciales. No es un identificador: es un
# "no hice nada". Si se persiste como si fuera un envío bueno, la bandeja
# enseña la conversación perfectamente contestada y el cliente no ha recibido
# nada. Lo traducimos a excepción (ver `SendNotConfirmed`).
PROVIDER_NOOP_ID = "noop"


class MessagingWindowClosed(RuntimeError):
    """El canal no admite el envío por estar fuera de su ventana de mensajería.

    La lanza `send_text_to_conversation` (p.ej. Instagram pasados 7 días) para
    que el llamante la traduzca a un error claro en vez de enviar y arriesgar
    una restricción de la cuenta.
    """


class SendNotConfirmed(RuntimeError):
    """El proveedor NO ha confirmado la entrega del mensaje.

    Hoy el caso real es la falta de credenciales (el provider devuelve el
    centinela "noop" sin llamar a la API). Un envío no confirmado no se puede
    dar por bueno: ni se persiste como enviado, ni se devuelve 200 al panel.
    """


def webchat_channel(conversation_id: str | uuid.UUID) -> str:
    """Nombre del canal Redis pub/sub para una conversación webchat."""
    return f"webchat:{conversation_id}"


async def last_inbound_age_hours(conv: Conversation) -> float | None:
    """Horas desde el ÚLTIMO mensaje del cliente (rol=user), o None si no hay.

    Es el reloj del que dependen las ventanas de mensajería de WhatsApp (24 h)
    y de Instagram (24 h / 7 días).
    """
    from datetime import datetime, timezone

    from sqlalchemy import desc, select

    from app.db.session import db_session as _db
    from app.models.message import Message, MessageRole

    async with _db() as db:
        last_in = (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv.id,
                    Message.rol == MessageRole.user,
                )
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

    if not last_in or not last_in.created_at:
        return None
    return (datetime.now(timezone.utc) - last_in.created_at).total_seconds() / 3600


async def instagram_window_state(conv: Conversation) -> str:
    """Estado de la ventana de mensajería de Instagram para esta conversación.

    Devuelve "standard" (≤24h), "human_agent" (24h–7 días) o "closed" (>7 días
    o sin ningún mensaje entrante). Se basa en el ÚLTIMO mensaje del cliente
    (rol=user): Meta cuenta la ventana desde ahí.
    """
    age_hours = await last_inbound_age_hours(conv)

    if age_hours is None:
        # Sin mensaje entrante no hay ventana abierta: no iniciamos conversación
        # (Meta exige que el cliente escriba primero).
        return "closed"
    if age_hours <= IG_STANDARD_WINDOW_HOURS:
        return "standard"
    if age_hours <= IG_HUMAN_AGENT_WINDOW_HOURS:
        return "human_agent"
    return "closed"


async def _gather_email_reply_headers(conv: Conversation) -> dict:
    """Recupera, del ÚLTIMO mensaje entrante (rol=user) del hilo, las cabeceras
    RFC necesarias para enhebrar una respuesta y el destinatario.

    Compartido por `create_email_draft_for_conversation` (F5c) y la rama email de
    `send_text_to_conversation` (F5b) para no duplicar el gathering. Devuelve:

      {"to_addr", "subject", "in_reply_to", "references"}

    `to_addr` es "" si no hay a quién responder (el llamante decide qué hacer).

    De dónde sale el destinatario (E11), por orden:
      1. Reply-To o From del ÚLTIMO correo entrante del hilo. Es a quien hay que
         contestar de verdad: el remitente del correo que estamos respondiendo.
      2. `conv.session_id` ("email:<dirección>"), que fija la dirección con la
         que se abrió el hilo. Cubre los mensajes antiguos guardados antes de
         registrar el remitente.
      3. `contact.email` del CRM, como último recurso.
    Antes se usaba SIEMPRE (3), así que en cuanto alguien editaba la ficha del
    contacto todos los borradores de ese hilo se iban a la dirección nueva
    aunque el cliente siguiera escribiendo desde la vieja.
    """
    from sqlalchemy import desc, select

    from app.db.session import db_session as _db
    from app.models.contact import Contact
    from app.models.message import Message, MessageRole

    async with _db() as db:
        contact = (
            await db.execute(select(Contact).where(Contact.id == conv.contact_id))
        ).scalar_one()
        # Último mensaje entrante (del cliente) del hilo: de él sacamos las
        # cabeceras para responder en el sitio correcto.
        last_in = (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv.id,
                    Message.rol == MessageRole.user,
                )
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

    extra = (last_in.extra if last_in else {}) or {}
    in_reply_to = extra.get("rfc822_message_id")
    # References: encadenamos las referencias previas + el id del último mensaje
    # para mantener el hilo bien formado.
    prev_refs = extra.get("references")
    references = (
        f"{prev_refs} {in_reply_to}".strip()
        if prev_refs and in_reply_to
        else (in_reply_to or prev_refs)
    )
    subject = extra.get("subject") or conv.subject or ""

    session_addr = ""
    conv_session = getattr(conv, "session_id", "") or ""
    if conv_session.startswith("email:"):
        session_addr = conv_session.split("email:", 1)[1]
    to_addr = (
        extra.get("reply_to_addr")
        or extra.get("from_addr")
        or session_addr
        or contact.email
        or ""
    )
    return {
        "to_addr": to_addr,
        "subject": subject,
        "in_reply_to": in_reply_to,
        "references": references,
    }


async def get_email_signature() -> str:
    """Firma que se añade a todo correo saliente (E8).

    La API de Gmail NO añade la firma de la cuenta: eso solo lo hace la interfaz
    web al redactar. Sin esto, todo lo que salía del sistema (borradores del
    agente y respuestas manuales del panel) llegaba al cliente sin nombre ni
    empresa.

    Orden: `Channel.config["email_signature"]` del canal de email (editable por
    despliegue) → `settings.EMAIL_SIGNATURE` → sin firma. Best-effort: un fallo
    leyendo el canal NUNCA puede impedir que salga el correo.
    """
    from app.core.config import settings

    try:
        from sqlalchemy import select

        from app.db.session import db_session as _db
        from app.models.channel import Channel, ChannelType

        async with _db() as db:
            rows = (
                await db.execute(
                    select(Channel)
                    .where(Channel.type == ChannelType.email, Channel.enabled.is_(True))
                    .order_by(Channel.created_at.asc())
                )
            ).scalars().all()
        for ch in rows:
            sig = ((ch.config or {}).get("email_signature") or "").strip()
            if sig:
                return sig
    except Exception as e:  # noqa: BLE001 — la firma nunca bloquea un envío
        logger.warning("email.signature.load_failed", error=str(e))
    return (settings.EMAIL_SIGNATURE or "").strip()


def append_signature(body_text: str, signature: str) -> str:
    """Pega la firma al final del cuerpo con el separador estándar "-- ".

    El separador (RFC 3676) es lo que hace que los clientes de correo la
    plieguen como firma y que nuestro propio `clean_email_body` la reconozca al
    leer la respuesta citada. Si el cuerpo ya trae esa firma (p. ej. un borrador
    editado a mano y reenviado), no se duplica. Función PURA.
    """
    sig = (signature or "").strip()
    if not sig:
        return body_text
    body = body_text or ""
    if sig in body:
        return body
    return f"{body.rstrip()}\n\n-- \n{sig}"


def _require_confirmed(ext_id: str, canal: str) -> str:
    """Traduce el centinela "noop" del provider en una excepción.

    Sin credenciales el provider no llega a llamar a la API y devuelve "noop".
    Nadie miraba ese valor: el mensaje se guardaba con
    extra.provider_message_id="noop" y la conversación aparecía contestada en
    la bandeja mientras el cliente no recibía nada. Un envío no confirmado es
    un fallo de envío, y como tal se propaga.
    """
    if ext_id == PROVIDER_NOOP_ID:
        logger.error("send.not_confirmed", canal=canal)
        raise SendNotConfirmed(
            f"El proveedor de {canal} no envió el mensaje: faltan credenciales "
            "o no se pudieron descifrar (revisa /admin/credentials)."
        )
    return ext_id


async def send_text_to_conversation(conv: Conversation, text: str) -> str:
    """Envía texto al cliente según el canal de la conversación.

    Lanza `MessagingWindowClosed` si el canal no admite el envío por su ventana
    de mensajería, y `SendNotConfirmed` si el proveedor no confirmó la entrega.
    """
    if conv.canal == ConversationCanal.whatsapp:
        from app.models.contact import Contact
        from app.db.session import db_session
        from sqlalchemy import select

        # Ventana de sesión de WhatsApp: pasadas 24 h desde el último mensaje
        # del cliente solo valen plantillas aprobadas. El panel ya lo miraba
        # antes de enviar a mano, pero el agente no: al liberar una cuarentena
        # vieja el envío salía igual, el proveedor lo rechazaba y la respuesta
        # se perdía en silencio. Si no hay ningún mensaje entrante NO cortamos
        # aquí (mismo criterio que el panel): ese caso no lo abre el agente.
        age_hours = await last_inbound_age_hours(conv)
        if age_hours is not None and age_hours > WA_SESSION_WINDOW_HOURS:
            raise MessagingWindowClosed(
                "Han pasado más de 24 h desde el último mensaje del cliente. "
                "WhatsApp solo permite responder con una plantilla aprobada "
                "fuera de esa ventana."
            )

        async with db_session() as db:
            contact = (
                await db.execute(select(Contact).where(Contact.id == conv.contact_id))
            ).scalar_one()
        wa = get_whatsapp_provider()
        return _require_confirmed(await wa.send_text(contact.telefono, text), "whatsapp")

    if conv.canal == ConversationCanal.web:
        # Publica al canal pub/sub que el WebSocket está escuchando.
        ext_id = str(uuid.uuid4())
        try:
            await event_bus.publish(
                webchat_channel(conv.id),
                "message.out",
                {
                    "conversation_id": str(conv.id),
                    "message_id": ext_id,
                    "text": text,
                },
            )
        except Exception as e:
            logger.warning("webchat.publish_failed", error=str(e))
        return ext_id

    if conv.canal == ConversationCanal.instagram_dm:
        from app.models.contact import Contact
        from app.db.session import db_session as _db
        from sqlalchemy import select

        # Ventana de Meta: fuera de los 7 días NO se envía (evita baneo). Entre
        # 24h y 7 días se envía con la etiqueta HUMAN_AGENT. El bot siempre
        # responde en caliente (≤24h), así que en la práctica solo la operadora
        # llega a "human_agent"; "closed" lo corta el llamante con un aviso.
        state = await instagram_window_state(conv)
        if state == "closed":
            raise MessagingWindowClosed(
                "Han pasado más de 7 días desde el último mensaje del cliente. "
                "Instagram no permite responder fuera de esa ventana (política "
                "de Meta)."
            )
        async with _db() as db:
            contact = (
                await db.execute(select(Contact).where(Contact.id == conv.contact_id))
            ).scalar_one()
        ig = get_instagram_provider()
        return _require_confirmed(
            await ig.send_text(
                contact.telefono, text, human_agent=(state == "human_agent")
            ),
            "Instagram",
        )

    if conv.canal == ConversationCanal.email:
        # F5b — Email: respuesta libre de la operadora desde el panel. Envía DE
        # VERDAD por Gmail, en el hilo correcto. (Distinto del flujo de borrador
        # del agente, que va por create_email_draft_for_conversation.)
        from app.providers.gmail import get_gmail_provider

        headers = await _gather_email_reply_headers(conv)
        to_addr = headers["to_addr"]
        if not to_addr:
            # Sin destinatario no hay envío posible. Antes se devolvía "" y el
            # llamante persistía el mensaje como enviado: la operadora veía su
            # respuesta en el hilo y el cliente no había recibido nada.
            logger.error("email.send.no_recipient", conversation_id=str(conv.id))
            raise SendNotConfirmed(
                "Este hilo de correo no tiene una dirección de respuesta válida, "
                "así que no se puede contestar desde aquí."
            )

        gmail = get_gmail_provider()
        body_with_signature = append_signature(text, await get_email_signature())
        try:
            result = await gmail.send_message(
                thread_id=conv.gmail_thread_id or "",
                to_addr=to_addr,
                subject=headers["subject"],
                body_text=body_with_signature,
                in_reply_to=headers["in_reply_to"],
                references=headers["references"],
            )
        except Exception as e:
            # Token de Google caído, 5xx de Gmail, hilo borrado… Se traduce a
            # excepción en vez de devolver "": con el "" el mensaje se guardaba
            # como enviado y la conversación se daba por contestada.
            logger.error("email.send.failed", conversation_id=str(conv.id), error=str(e))
            raise SendNotConfirmed(f"Gmail no ha aceptado el envío: {e}") from e
        # Devolvemos el message_id de Gmail: el llamante lo guarda en
        # Message.extra["provider_message_id"] para cerrar la idempotencia con el
        # poller de "Enviados" (store_outgoing_email lo detecta y no duplica).
        message_id = result.get("message_id") or ""
        if not message_id:
            # Gmail contestó pero sin id: no hay forma de saber si salió, y
            # además el sondeo de "Enviados" no podrá reconocerlo como nuestro.
            raise SendNotConfirmed(
                "Gmail no ha confirmado el envío (respuesta sin identificador)."
            )
        return message_id

    # Canal sin forma de enviar (hoy, la voz de Retell: una llamada en curso no
    # admite que le metas texto). Devolver "" hacía que el panel guardara el
    # mensaje como enviado y se quedara tan ancho.
    logger.error("send.unknown_channel", canal=conv.canal.value)
    raise SendNotConfirmed(
        f"Por el canal «{conv.canal.value}» no se puede enviar un mensaje escrito."
    )


async def create_email_draft_for_conversation(
    conv: Conversation, text: str
) -> dict | None:
    """F5c — Email: NO envía, crea un BORRADOR REAL en Gmail para revisión.

    Recupera del ÚLTIMO mensaje entrante del hilo las cabeceras RFC necesarias
    para enhebrar la respuesta (In-Reply-To/References = rfc822_message_id del
    cliente; subject; destinatario = email del contacto). Llama
    `gmail.create_draft(...)` y devuelve un dict con el resultado para que el
    runtime persista el Message borrador:

      {"draft_id", "message_id", "to_addr", "subject", "in_reply_to", "references"}

    Devuelve None si no se pudo crear (sin destinatario o error de Gmail): el
    runtime no persistirá un borrador fantasma.
    """
    from app.providers.gmail import get_gmail_provider

    # Mismo gathering de cabeceras que la respuesta libre (F5b): destinatario +
    # In-Reply-To/References/subject del último mensaje entrante.
    headers = await _gather_email_reply_headers(conv)
    to_addr = headers["to_addr"]
    if not to_addr:
        logger.error("email.draft.no_recipient", conversation_id=str(conv.id))
        return None

    in_reply_to = headers["in_reply_to"]
    references = headers["references"]
    subject = headers["subject"]

    gmail = get_gmail_provider()
    # El borrador se crea CON la firma ya puesta: es lo que la operadora va a
    # revisar y enviar tal cual desde Gmail, así que tiene que ver exactamente
    # lo que recibirá el cliente.
    body_with_signature = append_signature(text, await get_email_signature())
    try:
        result = await gmail.create_draft(
            thread_id=conv.gmail_thread_id or "",
            to_addr=to_addr,
            subject=subject,
            body_text=body_with_signature,
            in_reply_to=in_reply_to,
            references=references,
        )
    except Exception as e:
        logger.error("email.draft.create_failed", conversation_id=str(conv.id), error=str(e))
        return None

    return {
        "draft_id": result.get("draft_id"),
        "message_id": result.get("message_id"),
        "to_addr": to_addr,
        "subject": subject,
        "in_reply_to": in_reply_to,
        "references": references,
    }
