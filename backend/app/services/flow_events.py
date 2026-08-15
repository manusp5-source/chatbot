"""Eventos de actividad del agente para el diagrama "Flujo en vivo".

`publish_agent_step` publica en el canal del inbox (el WS que ya escucha el
panel) un evento ligero `agent.step` SIN PII: solo el paso del pipeline
(clasificador / responder / borrador / derivar / tool), los ids implicados y
el nombre de la tool. El FlowDashboard lo usa para iluminar en tiempo real el
nodo que está actuando en cada momento.

Best-effort SIEMPRE: si Redis no está o el publish falla, se loguea en debug y
el pipeline real sigue como si nada (el diagrama es observabilidad, no lógica).
"""
from __future__ import annotations

import uuid

from app.core.events import event_bus, inbox_channel
from app.core.logging import get_logger

logger = get_logger(__name__)

# Pasos conocidos del pipeline (el frontend mapea cada uno a un nodo).
STEP_CLASIFICADOR = "clasificador"
STEP_RESPONDER = "responder"
STEP_BORRADOR = "borrador"
STEP_DERIVAR = "derivar"
STEP_TOOL = "tool"


async def publish_agent_step(
    step: str,
    *,
    conversation_id: uuid.UUID | str | None = None,
    agent_id: uuid.UUID | str | None = None,
    tool: str | None = None,
) -> None:
    """Publica un paso del pipeline del agente al WS del panel (best-effort)."""
    try:
        payload: dict[str, str] = {"step": step}
        if conversation_id:
            payload["conversation_id"] = str(conversation_id)
        if agent_id:
            payload["agent_id"] = str(agent_id)
        if tool:
            payload["tool"] = tool
        await event_bus.publish(inbox_channel(), "agent.step", payload)
    except Exception as e:  # noqa: BLE001 — observabilidad, nunca rompe el pipeline
        logger.debug("flow_events.publish_error", step=step, error=str(e))
