"""Definición canónica de la bandeja "Para hacer" (needs-action) del inbox.

Centraliza `pending_expr()` / `suggestion_expr()` (antes inline en
`app/api/conversations.py`) para que el endpoint del inbox Y las notificaciones
Web Push usen la MISMA regla y no se desincronicen nunca.

"Para hacer" (lo que el operador ve como accionable) = `pending | suggestion`:
  - pending: borrador de email sin enviar, o el último mensaje es del cliente
    (nadie contestó), o está derivada a humano y aún no respondió un operador,
    o lo último que se dijo NO le llegó al cliente.
  - suggestion: sugerencia del modo Entrenamiento sin resolver.
"""
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.conversation import Conversation, ConversationStatus
from app.models.message import Message, MessageRole


def pending_expr():
    """Conversación que NECESITA a una persona. Reutilizada por el filtro del
    inbox, /counts y el chequeo de notificaciones."""
    last_role_sq = (
        select(Message.rol)
        .where(Message.conversation_id == Conversation.id)
        .order_by(desc(Message.created_at))
        .limit(1)
        .scalar_subquery()
    )
    has_email_draft_sq = (
        select(Message.id)
        .where(
            Message.conversation_id == Conversation.id,
            Message.extra["is_draft"].astext == "true",
            func.coalesce(Message.extra["draft_sent"].astext, "false") != "true",
            func.coalesce(Message.extra["training"].astext, "false") != "true",
        )
        .exists()
    )
    # Lo último que se dijo en el hilo NO llegó: WhatsApp devolvió `failed`
    # (número que no existe, cliente que bloqueó a la empresa, ventana de 24 h
    # cerrada, plantilla pausada). Sin esto el fallo solo se veía entrando al
    # hilo y mirando el globo: para la bandeja la conversación estaba
    # contestada, y el cliente esperando algo que no recibió nunca. Ver
    # `services/delivery_status.py`.
    #
    # Las DIFUSIONES quedan fuera: una campaña a 500 números tiene siempre unos
    # cuantos muertos, y meter sesenta conversaciones en «Para hacer» de golpe
    # —ninguna con nada que hacer— es enterrar lo que sí importa. Los fallos de
    # una difusión se ven en su propia pantalla, con su recuento.
    #
    # Y una conversación CERRADA tampoco: cerrar es la salida de la operadora
    # cuando el cliente ha bloqueado a la empresa y no hay forma de entregarle
    # nada. Sin eso el hilo se quedaba en «Para hacer» para siempre, porque un
    # fallo de entrega no se puede deshacer.
    # Alias explícito: la subconsulta de "el último es un fallo de entrega"
    # mira la MISMA tabla que el `ORDER BY ... LIMIT 1` que la envuelve, y sin
    # separarlas SQLAlchemy correlaciona las dos contra la misma fila y la
    # condición no se cumple nunca.
    ultimo = aliased(Message)
    ultimo_no_entregado_sq = (
        select(ultimo.id)
        .where(
            ultimo.conversation_id == Conversation.id,
            ultimo.extra["delivery_status"].astext == "fallido",
            func.coalesce(ultimo.extra["source"].astext, "") != "outbound_campaign",
            # Y que sea el último del hilo: si el cliente ha vuelto a escribir
            # después, o se le ha contestado y esa sí llegó, ya no hay nada
            # colgando por aquí (lo cubren las otras reglas).
            ultimo.created_at
            == select(func.max(Message.created_at))
            .where(Message.conversation_id == Conversation.id)
            .scalar_subquery(),
        )
        .exists()
    )
    return (
        has_email_draft_sq
        | (last_role_sq == MessageRole.user)
        | (
            ultimo_no_entregado_sq
            & (Conversation.status != ConversationStatus.cerrada)
        )
        | (
            (Conversation.status == ConversationStatus.humano)
            & (last_role_sq == MessageRole.assistant)
        )
    )


def suggestion_expr():
    """Conversación con una sugerencia del modo Entrenamiento SIN resolver
    (borrador con extra.training=True que no se ha enviado)."""
    return (
        select(Message.id)
        .where(
            Message.conversation_id == Conversation.id,
            Message.extra["is_draft"].astext == "true",
            func.coalesce(Message.extra["draft_sent"].astext, "false") != "true",
            Message.extra["training"].astext == "true",
        )
        .exists()
    )


async def conversation_needs_action(db: AsyncSession, conversation_id) -> bool:
    """True si la conversación está en "Para hacer" tal y como la muestra el
    inbox: no archivada, no en cuarentena, y `pending | suggestion`."""
    stmt = (
        select(Conversation.id)
        .where(
            Conversation.id == conversation_id,
            Conversation.archived_at.is_(None),
            Conversation.quarantined_at.is_(None),
            pending_expr() | suggestion_expr(),
        )
    )
    return bool((await db.execute(select(stmt.exists()))).scalar())
