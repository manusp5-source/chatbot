"""Estado de entrega de los mensajes que salen por WhatsApp.

EL PROBLEMA QUE RESUELVE. Que el proveedor acepte un mensaje no significa que
el cliente lo reciba. Meta responde 200 con un `wamid` y minutos después manda
un webhook de estado diciendo que no ha podido entregarlo: número que no existe,
cliente que bloqueó a la empresa, ventana de 24 h cerrada, plantilla pausada,
límite de la cuenta. Ese webhook llegaba, se leía y se tiraba a los logs en
vivo —un buffer de 1000 líneas en Redis que se pierde al reiniciar y que hay
que ir a mirar a mano—. En la bandeja el mensaje seguía apareciendo como
enviado tan tranquilo. La operadora daba la conversación por contestada y el
cliente no había recibido nada.

CÓMO SE GUARDA. En `Message.extra` (JSONB), junto al resto de banderas del
mensaje (`is_draft`, `gmail_draft_id`…), no en columnas nuevas: la búsqueda por
`metadata->>'provider_message_id'` ya tiene índice desde la migración 0022, que
es justo por donde entra el webhook. Tres claves:

    delivery_status  enviado | entregado | leido | fallido
    delivery_detail  el motivo, solo cuando falla
    delivery_at      cuándo se supo, en ISO

EL ORDEN IMPORTA. Los estados de WhatsApp no llegan ordenados: un `sent` puede
entrar después de un `delivered` (reintentos, varios webhooks en paralelo). Por
eso solo se sube de escalón —enviado → entregado → leído— y nunca se baja. El
fallo es la excepción: manda siempre, porque es lo único que obliga a alguien a
hacer algo.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import db_session
from app.models.message import Message

logger = get_logger(__name__)

ENVIADO = "enviado"
ENTREGADO = "entregado"
LEIDO = "leido"
FALLIDO = "fallido"

# Escalón de cada estado. El fallo va fuera de la escala a propósito (ver
# arriba): no compite, gana.
_ESCALONES = {ENVIADO: 1, ENTREGADO: 2, LEIDO: 3}

# Cómo llama cada proveedor a cada estado. WhatsApp Cloud API (Meta) y YCloud
# usan las mismas palabras salvo `accepted`, que es de YCloud y significa "lo
# tengo en cola", o sea lo mismo que `sent` para lo que aquí importa.
_TRADUCCION = {
    "accepted": ENVIADO,
    "sent": ENVIADO,
    "delivered": ENTREGADO,
    "read": LEIDO,
    "failed": FALLIDO,
    "undeliverable": FALLIDO,
}

MAX_DETALLE = 300


def traducir_estado(bruto: str | None) -> str | None:
    """Estado del proveedor → estado nuestro. None si no lo conocemos."""
    return _TRADUCCION.get((bruto or "").strip().lower())


def _es_ascenso(actual: str | None, nuevo: str) -> bool:
    """¿El estado nuevo aporta algo sobre el que ya teníamos?"""
    if nuevo == FALLIDO:
        return actual != FALLIDO
    if actual == FALLIDO:
        # Un `delivered` tardío no borra un fallo: si Meta dijo que no llegó,
        # eso es lo que la operadora tiene que ver.
        return False
    return _ESCALONES.get(nuevo, 0) > _ESCALONES.get(actual or "", 0)


async def registrar_estado_entrega(
    provider_message_id: str,
    estado_bruto: str,
    *,
    detalle: str | None = None,
) -> bool:
    """Anota en el mensaje qué ha pasado con su entrega.

    Devuelve True si se ha escrito algo. False si el id no es de un mensaje
    nuestro (webhooks de otra instalación, mensajes anteriores a esto), si el
    estado no lo conocemos o si no aportaba nada sobre lo que ya había.

    Best-effort: nunca lanza. Un webhook de estado no puede tumbar la respuesta
    al proveedor, porque entonces Meta lo reintenta en bucle.
    """
    pmid = (provider_message_id or "").strip()
    if not pmid:
        return False
    estado = traducir_estado(estado_bruto)
    if estado is None:
        logger.info("delivery.status.desconocido", estado=str(estado_bruto)[:40])
        return False

    try:
        async with db_session() as db:
            # Cerrojo de fila: esto es leer-modificar-escribir sobre un JSONB.
            # Dos webhooks del mismo mensaje a la vez (Meta reintenta un lote
            # mientras llega el siguiente) leían los dos el mismo `extra` y el
            # segundo pisaba al primero. Si el que perdía era el `failed`, el
            # fallo desaparecía: justo lo que esto viene a evitar.
            #
            # `order_by` explícito porque sin él, con dos filas compartiendo
            # identificador (un borrador enviado y su copia recogida por el
            # sondeo de Enviados), Postgres puede devolver una u otra en cada
            # llamada y el estado saltaría de un mensaje a otro.
            msg = (
                await db.execute(
                    select(Message)
                    .where(Message.extra["provider_message_id"].astext == pmid)
                    .order_by(Message.created_at.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if msg is None:
                return False
            extra = dict(msg.extra or {})
            if not _es_ascenso(extra.get("delivery_status"), estado):
                return False
            extra["delivery_status"] = estado
            extra["delivery_at"] = datetime.now(timezone.utc).isoformat()
            if estado == FALLIDO:
                extra["delivery_detail"] = (detalle or "sin motivo")[:MAX_DETALLE]
            else:
                extra.pop("delivery_detail", None)
            # Reasignación completa: SQLAlchemy no detecta mutaciones dentro de
            # un JSONB, así que modificar `msg.extra` en sitio no se guardaría.
            msg.extra = extra
            await db.commit()
            conversation_id = msg.conversation_id
    except Exception as e:  # noqa: BLE001 — un estado nunca rompe el webhook
        logger.warning("delivery.status.error", error=str(e), provider_message_id=pmid[:40])
        return False

    await _avisar_al_panel(conversation_id, estado)
    return True


async def _avisar_al_panel(conversation_id, estado: str) -> None:
    """Refresca la bandeja abierta para que el globo cambie sin recargar."""
    from app.core.events import event_bus, inbox_channel

    try:
        await event_bus.publish(
            inbox_channel(),
            "message.updated",
            {"conversation_id": str(conversation_id), "delivery_status": estado},
        )
    except Exception:  # noqa: BLE001 — el aviso en vivo nunca corta el flujo
        pass


def canal_con_acuses(canal) -> bool:
    """¿Este canal nos dice después si el mensaje llegó?

    Solo WhatsApp. Instagram no manda acuses de lectura por webhook en esta
    integración, el chat web se entrega en el momento por el WebSocket (si no
    llega, no hay nada que anotar) y en correo el agente ni siquiera envía:
    deja un borrador. Marcar "enviado" en esos tres pintaría en la bandeja un
    estado que no va a avanzar nunca y que la operadora leería como "pendiente
    de entregar".
    """
    return getattr(canal, "value", canal) == "whatsapp"


def marcar_enviado(extra: dict | None, provider_message_id: str | None, canal=None) -> dict:
    """Extra de un mensaje SALIENTE recién aceptado por el proveedor.

    Se llama en el momento de persistir. Sin esto no habría de dónde subir: un
    mensaje sin estado inicial cuyo webhook de `delivered` se perdiera se
    quedaría igual de mudo que antes.
    """
    salida = dict(extra or {})
    if not provider_message_id:
        return salida
    salida["provider_message_id"] = provider_message_id
    if canal is None or canal_con_acuses(canal):
        salida["delivery_status"] = ENVIADO
    return salida
