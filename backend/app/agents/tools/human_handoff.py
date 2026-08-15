"""Tool: derivar la conversación a un operador humano.

Aviso a la operadora SOLO por canales internos: web push a la PWA + la
conversación pasa a estado `humano` y aparece en la bandeja. No se envía nada
a plataformas de terceros (RGPD: no exfiltrar datos del cliente).

Protecciones contra abuso:
- Cooldown por conversación: una sola derivación cada DERIVACION_COOLDOWN_SECS.
- Si la conversación ya está en estado `humano` o `cerrada`, no notificamos
  de nuevo (idempotente).
- Truncamos motivo antes de usarlo.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.agents.tools.registry import Tool, register_tool
from app.core.events import event_bus, inbox_channel
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.agent_config import AgentConfig
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
from app.models.message import Message, MessageRole
from app.providers.llm.base import LLMToolSchema
from app.services.flow_events import publish_agent_step
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)

DERIVACION_COOLDOWN_SECS = 600  # 10 min entre derivaciones de la misma conv
MAX_MOTIVO_LEN = 200
_COOLDOWN_KEY = "handoff:cooldown:{conv_id}"

# Mensaje que el bot envia al cliente justo antes de pasar la conversacion
# al equipo humano. Garantiza que el cliente sabe que ha sido transferido y
# no piensa que el bot lo ha ignorado. Hardcoded aqui (no en el prompt) para
# no depender de que el LLM lo recuerde en cada turno.
HANDOFF_BRIDGE_MESSAGE = "Te paso con el equipo. Te contestaran por aqui en cuanto puedan."


async def _cooldown_active(conv_id: str) -> bool:
    r = get_redis()
    return bool(await r.exists(_COOLDOWN_KEY.format(conv_id=conv_id)))


async def _set_cooldown(conv_id: str) -> None:
    r = get_redis()
    await r.set(_COOLDOWN_KEY.format(conv_id=conv_id), str(int(time.time())), ex=DERIVACION_COOLDOWN_SECS)


# Nombres propios que el mensaje puente no puede mencionar. La tupla en codigo
# estaba VACIA, asi que el saneado no filtraba absolutamente nada. Ahora sale de
# `app_settings` (clave `handoff.forbidden_names`, nombres separados por comas),
# que es donde el operador puede ponerlos de verdad. Vacio = no se filtra nada,
# igual que antes, pero al menos ya hay una forma de configurarlo.
FORBIDDEN_NAMES_SETTING = "handoff.forbidden_names"


async def _forbidden_names() -> tuple[str, ...]:
    try:
        from app.services.app_settings import get_app_setting

        raw = await get_app_setting(FORBIDDEN_NAMES_SETTING, "")
    except Exception as e:  # noqa: BLE001 — el filtro nunca bloquea la derivacion
        logger.warning("handoff.bridge.forbidden_read_error", error=str(e))
        return ()
    return tuple(n.strip().lower() for n in (raw or "").split(",") if n.strip())


async def _get_default_bridge_message() -> str:
    """Mensaje puente por defecto.

    Prioridad:
      1. El del AGENTE que esta atendiendo (`agents.handoff_bridge_message`).
         Hasta ahora ese campo se editaba en el panel y no lo leia nadie: solo
         se miraba el singleton legacy, asi que con varios agentes todos decian
         lo mismo.
      2. El del AgentConfig legacy (/admin/agent/config).
      3. El hardcoded HANDOFF_BRIDGE_MESSAGE.
    """
    from app.core.trace_context import get_agent_id

    agent_id = get_agent_id()
    if agent_id:
        try:
            from app.models.agent import Agent

            async with db_session() as db:
                agent = (
                    await db.execute(select(Agent).where(Agent.id == agent_id))
                ).scalar_one_or_none()
            if agent and agent.handoff_bridge_message and agent.handoff_bridge_message.strip():
                return agent.handoff_bridge_message.strip()[:280]
        except Exception as e:
            logger.warning("handoff.bridge.agent_read_error", error=str(e))

    try:
        async with db_session() as db:
            cfg = (
                await db.execute(select(AgentConfig).where(AgentConfig.is_active.is_(True)))
            ).scalar_one_or_none()
            if cfg and cfg.handoff_bridge_message and cfg.handoff_bridge_message.strip():
                return cfg.handoff_bridge_message.strip()[:280]
    except Exception as e:
        logger.warning("handoff.bridge.config_read_error", error=str(e))
    return HANDOFF_BRIDGE_MESSAGE


async def _sanitize_bridge_message(text: str | None) -> str:
    """Si el agente intenta meter nombres propios concretos en el mensaje
    puente, caemos al default configurable. Mantiene consistencia con el tono
    del negocio (el equipo es generico, no se menciona a personas).
    """
    default = await _get_default_bridge_message()
    if not text:
        return default
    candidate = text.strip()[:280]
    lower = candidate.lower()
    if any(n in lower for n in await _forbidden_names()):
        return default
    return candidate or default


# Canales donde el mensaje puente SE ENVÍA en caliente. Email queda fuera a
# propósito: en email el agente NUNCA envía, deja un borrador en Gmail para
# revisión (F5c), y voz (Retell) es de solo lectura — no hay forma de meter
# texto en una llamada en curso. En esos dos casos dejamos traza visible en
# lugar de un envío que no puede existir.
_BRIDGE_CHANNELS = (
    ConversationCanal.whatsapp,
    ConversationCanal.web,
    ConversationCanal.instagram_dm,
)


async def _send_handoff_bridge_message(
    conv: Conversation | None,
    contact: Contact | None,
    mensaje_custom: str | None = None,
) -> None:
    """Envia el mensaje puente al cliente POR SU CANAL y lo persiste como Message.

    Antes esto llamaba siempre al provider de WhatsApp con `contact.telefono`.
    En web el identificador es `web:<uuid>` y en Instagram `ig:<psid>`: el
    provider los rechazaba, la excepcion se tragaba y se volvia ANTES de
    persistir nada. El visitante pedia hablar con una persona y no veia
    absolutamente nada, y la operadora tampoco sabia que se le habia dicho.
    Ahora va por el dispatcher de canal (`services/channel_sender`).

    Si el agente genero un `mensaje_al_cliente` adaptado al contexto y no
    menciona nombres propios prohibidos, se usa ese. Si no, el default.

    Best-effort en cuanto a NO bloquear la derivacion: si el envio falla, la
    conversacion pasa a humano igual. Pero el fallo YA NO es mudo — queda en
    los logs en vivo para que la operadora sepa que el cliente no recibio el
    aviso.
    """
    from app.services.channel_sender import (
        MessagingWindowClosed,
        SendNotConfirmed,
        send_text_to_conversation,
    )

    if conv is None:
        logger.warning("handoff.bridge.no_conversation")
        return
    conversation_id = conv.id
    if not contact or not contact.telefono:
        logger.warning("handoff.bridge.no_contact")
        await push_runtime_log(
            level="warn",
            event="handoff.bridge.no_contact",
            message="Derivación sin contacto: el cliente no recibió el aviso de traspaso",
            conversation_id=str(conversation_id),
        )
        return
    # Modo Entrenamiento: el contrato del canal es "el bot NO le habla al
    # cliente, deja sugerencias para que las revise una persona". El mensaje
    # puente es un mensaje del bot como cualquier otro, así que aquí tampoco
    # sale. Antes salía, y era el único texto que se le colaba al cliente en un
    # canal en Entrenamiento.
    from app.services.agent_pause import is_channel_training

    if await is_channel_training(conv.canal.value):
        logger.info("handoff.bridge.training_skipped", canal=conv.canal.value)
        await push_runtime_log(
            level="info",
            event="handoff.bridge.skipped",
            message=(
                "Derivación a humano en un canal en Entrenamiento: NO se le ha "
                "escrito nada al cliente (en Entrenamiento el bot no envía)."
            ),
            conversation_id=str(conversation_id),
            channel=conv.canal.value,
        )
        return
    if conv.canal not in _BRIDGE_CHANNELS:
        logger.info("handoff.bridge.channel_skipped", canal=conv.canal.value)
        await push_runtime_log(
            level="info",
            event="handoff.bridge.skipped",
            message=(
                "Derivación a humano: en este canal no se envía el mensaje "
                "puente (el correo se responde desde el borrador y la voz no "
                "admite texto)."
            ),
            conversation_id=str(conversation_id),
            channel=conv.canal.value,
        )
        return

    text = await _sanitize_bridge_message(mensaje_custom)
    try:
        ext_id = await send_text_to_conversation(conv, text)
    except (MessagingWindowClosed, SendNotConfirmed) as e:
        # Motivo conocido y explicable (ventana cerrada / sin credenciales).
        logger.warning("handoff.bridge.not_sent", canal=conv.canal.value, error=str(e))
        await push_runtime_log(
            level="warn",
            event="handoff.bridge.not_sent",
            message=f"No se pudo avisar al cliente de la derivación: {e}",
            conversation_id=str(conversation_id),
            channel=conv.canal.value,
        )
        return
    except Exception as e:  # noqa: BLE001 — nunca bloquea la derivación
        logger.error("handoff.bridge.send_error", canal=conv.canal.value, error=str(e))
        await push_runtime_log(
            level="error",
            event="handoff.bridge.send_error",
            message=(
                "Falló el envío del mensaje puente al cliente; la conversación "
                "se deriva igualmente al equipo."
            ),
            conversation_id=str(conversation_id),
            channel=conv.canal.value,
        )
        return
    # Persistir el mensaje en BD para que aparezca en el panel del operador.
    try:
        from app.services.delivery_status import marcar_enviado

        async with db_session() as db:
            msg = Message(
                conversation_id=conversation_id,
                rol=MessageRole.assistant,
                contenido=text,
                extra=marcar_enviado({"kind": "handoff_bridge"}, ext_id, conv.canal),
            )
            db.add(msg)
            await db.commit()
    except Exception as e:
        logger.warning("handoff.bridge.persist_error", error=str(e))


async def avisar_derivacion_al_cliente(conversation_id: uuid.UUID) -> None:
    """Mensaje puente para las derivaciones que NO pasan por la herramienta.

    `derivar_humano` avisa al cliente, pero no es la única puerta a `humano`:
    la moderación y los fallos del runtime (modelo caído, respuesta vacía)
    dejan la conversación en manos de una persona igual, y hasta ahora lo
    hacían en SILENCIO. El cliente escribía, no recibía absolutamente nada, y
    no tenía forma de saber si alguien iba a contestarle: se iba.

    Best-effort de punta a punta. Si no hay conversación o contacto, si el
    canal no admite envío en caliente (email, voz) o si el envío falla, queda
    traza en los logs en vivo y la derivación sigue su curso: notificar al
    equipo es lo crítico, avisar al cliente es lo deseable.
    """
    try:
        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.id == conversation_id)
                )
            ).scalar_one_or_none()
            if conv is None:
                return
            contact = (
                await db.execute(select(Contact).where(Contact.id == conv.contact_id))
            ).scalar_one_or_none()
        await _send_handoff_bridge_message(conv, contact, None)
    except Exception as e:  # noqa: BLE001 — nunca bloquea la derivación
        logger.warning("handoff.bridge.aviso_error", error=str(e))


def extract_handoff_question(
    contenido: str | None,
    es_email: bool = False,
    subject: str | None = None,
) -> str:
    """Extrae la pregunta relevante del último mensaje del cliente para el
    aprendizaje ("hueco de conocimiento").

    En email, `Message.contenido` guarda el correo ÍNTEGRO (decisión de
    producto: la operadora ve el hilo completo en el inbox). Pero para el
    APRENDIZAJE eso mete el hilo entero (citas "El ... escribió:", firmas,
    ">"), y lo que interesa es solo el último intercambio → limpiamos con
    clean_email_body y anteponemos el asunto como contexto.

    Función PURA: sin DB, sin red — testeable en aislamiento.
    """
    question = (contenido or "").strip()
    if not question:
        return ""
    if es_email:
        from app.providers.gmail import clean_email_body

        question = clean_email_body(question).strip()
        subj = (subject or "").strip()
        if subj:
            question = f"Asunto: {subj}\n\n{question}"
    return question


async def _record_knowledge_gap_handoff(conversation_id: uuid.UUID) -> None:
    """Autoaprendizaje Fase 2: deja un hueco de conocimiento por derivación.

    El agente derivó porque no supo resolver → señal fuerte de que falta algo en
    la base de conocimiento. Guardamos la ÚLTIMA pregunta del cliente para que la
    operadora la revise en "Aprendizajes". Para email, solo el último mensaje
    relevante (sin hilo citado ni firmas), con el asunto como contexto.

    Best-effort: si algo falla, lo logueamos pero NUNCA rompemos la derivación
    (tener al equipo notificado es lo crítico).
    """
    try:
        from app.models.conversation import ConversationCanal
        from app.models.knowledge_gap import KnowledgeGap

        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.id == conversation_id)
                )
            ).scalar_one_or_none()
            last_user_msg = (
                await db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.rol == MessageRole.user,
                    )
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not last_user_msg:
                return
            es_email = bool(conv and conv.canal == ConversationCanal.email)
            subject = (last_user_msg.extra or {}).get("subject") if es_email else None
            question = extract_handoff_question(
                last_user_msg.contenido, es_email=es_email, subject=subject
            )
            if not question:
                return
            db.add(
                KnowledgeGap(
                    trigger="handoff",
                    conversation_id=conversation_id,
                    question=question[:2000],
                )
            )
            await db.commit()
    except Exception as e:
        logger.warning("handoff.knowledge_gap.error", error=str(e))


async def derivar_humano(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    conversation_id_str = ctx.get("conversation_id")
    if not conversation_id_str:
        return json.dumps({"error": "Falta conversation_id en contexto"})
    conversation_id = uuid.UUID(conversation_id_str)

    motivo_raw = (args.get("motivo") or "").strip() or "Solicitud del usuario"
    motivo_raw = motivo_raw[:MAX_MOTIVO_LEN]

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conversation_id))
        ).scalar_one_or_none()
        if not conv:
            return json.dumps({"error": "Conversación no encontrada"})
        # Si ya está derivada o cerrada, no volvemos a notificar.
        if conv.status in (ConversationStatus.humano, ConversationStatus.cerrada):
            logger.info("handoff.already_handed_off", status=conv.status.value)
            return json.dumps({"derivado": True, "already": True})
        # Cooldown: evita spam al canal del equipo.
        if await _cooldown_active(conversation_id_str):
            contact_cooldown = (
                await db.execute(select(Contact).where(Contact.id == conv.contact_id))
            ).scalar_one_or_none()
            conv.status = ConversationStatus.humano
            conv.derivada_a_humano_at = datetime.now(timezone.utc)
            await db.commit()
            # Mensaje puente tambien en cooldown (todavia es una derivacion real).
            await _send_handoff_bridge_message(conv, contact_cooldown, args.get("mensaje_al_cliente"))
            await publish_agent_step("derivar", conversation_id=conversation_id)
            logger.info("handoff.cooldown_active")
            return json.dumps({"derivado": True, "throttled": True})

        # Cargamos contacto ANTES de enviar el mensaje puente.
        contact = (
            await db.execute(select(Contact).where(Contact.id == conv.contact_id))
        ).scalar_one_or_none()

    # Envia el mensaje puente al cliente ANTES de cambiar el status, para que
    # el ultimo mensaje del bot sea claro y no se quede el cliente sin respuesta.
    await _send_handoff_bridge_message(conv, contact, args.get("mensaje_al_cliente"))

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conversation_id))
        ).scalar_one()
        conv.status = ConversationStatus.humano
        conv.derivada_a_humano_at = datetime.now(timezone.utc)
        await db.commit()

    await _set_cooldown(conversation_id_str)

    # Flujo en vivo: ilumina el nodo "Derivar a humano" del diagrama.
    await publish_agent_step("derivar", conversation_id=conversation_id)

    # Autoaprendizaje Fase 2: registra el hueco de conocimiento (best-effort).
    await _record_knowledge_gap_handoff(conversation_id)

    nombre = (contact.nombre or contact.telefono) if contact else "Usuario"

    # Aviso a la operadora SOLO por canales internos: web push (abajo) + la
    # conversación aparece en la bandeja como "necesita acción". Ya NO se manda
    # a Telegram/Slack (RGPD: no exfiltrar nombre/teléfono/resumen del cliente
    # a plataformas de terceros; el panel es suficiente).
    #
    # Web Push a la PWA del operador (texto plano, no HTML). Ruta relativa para
    # que el service worker abra la conversación en el propio panel. Usamos
    # notify_pending para COMPARTIR el cooldown por conversación con el chequeo
    # diferido (notify_pending_check) y no notificar dos veces lo mismo.
    try:
        from app.services.web_push import notify_pending as _push_notify_pending
        _push_nombre = (contact.nombre or contact.telefono) if contact else "Usuario"
        await _push_notify_pending(
            str(conversation_id),
            f"Derivación a humano — {_push_nombre}",
            motivo_raw or "Una conversación necesita tu atención.",
            f"/inbox?conversation={conversation_id}",
        )
    except Exception as e:
        logger.warning("handoff.webpush.error", error=str(e))
    # Log interno SIN el nombre del cliente (RGPD: los runtime logs no se
    # redactan; el operador identifica la conversación por su id/enlace).
    await push_runtime_log(
        level="info",
        event="handoff.human",
        message="Derivación a humano — una conversación necesita atención",
        conversation_id=str(conversation_id),
        motivo=motivo_raw[:80],
    )

    try:
        await event_bus.publish(
            inbox_channel(),
            "conversation.updated",
            {"conversation_id": str(conversation_id), "status": "humano"},
        )
    except Exception as e:
        logger.warning("handoff.publish.error", error=str(e))

    return json.dumps({"derivado": True})


register_tool(
    Tool(
        schema=LLMToolSchema(
            name="derivar_humano",
            description=(
                "Deriva la conversación a un operador humano cuando: el usuario lo "
                "pida explícitamente, la consulta requiera criterio profesional, el "
                "RAG no tenga respuesta clara o el usuario esté frustrado. Tras "
                "esta llamada NO sigas conversando, el equipo humano tomará el control."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "motivo": {
                        "type": "string",
                        "description": "Por qué se deriva (ej: 'usuario pide hablar con persona'). Máx 200 caracteres.",
                    },
                    "mensaje_al_cliente": {
                        "type": "string",
                        "description": (
                            "Mensaje corto y neutro que el bot enviara al cliente por WhatsApp "
                            "ANTES de que el equipo tome el relevo. Adaptalo al contexto. "
                            "NUNCA menciones nombres propios; di 'el equipo' (NO menciones nombres propios). "
                            "Si no lo incluyes, se envia un default generico. Maximo 280 caracteres."
                        ),
                    },
                },
                "required": ["motivo"],
            },
        ),
        handler=derivar_humano,
    )
)
