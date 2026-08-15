"""ContextVar para propagar conversation_id al trace sin contaminar firmas.

El orchestrator setea el conversation_id antes de invocar LLM/tools y los
módulos profundos (openai_client, tools, kb_search) lo leen para asociar
los eventos del trace con la conversación correcta.

Usar ContextVar (no thread-local) porque la app es async y queremos que el
valor se propague a tareas asyncio descendientes (`asyncio.create_task`
copia los ContextVars del padre).
"""
from __future__ import annotations

import contextvars

_conversation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_conversation_id", default=None
)

# Id del Agente (tabla `agents`) que atiende la conversación en curso. El
# orchestrator lo setea junto al conversation_id; el openai_client lo lee para
# atribuir el consumo de tokens a ese agente concreto (desglose "por agente").
# None cuando el LLM lo invoca algo que no es un Agente del modelo multi-agente
# (clasificador, agente interno, embeddings…), que se atribuyen por su `source`.
_agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_agent_id", default=None
)


def set_conversation_id(conversation_id: str | None) -> contextvars.Token:
    """Setea el conv_id para el trace. Devuelve el Token para resetearlo después."""
    return _conversation_id_var.set(conversation_id)


def reset_conversation_id(token: contextvars.Token) -> None:
    _conversation_id_var.reset(token)


def get_conversation_id() -> str | None:
    """Devuelve el conv_id activo en este contexto async, o None si no hay."""
    return _conversation_id_var.get()


def set_agent_id(agent_id: str | None) -> contextvars.Token:
    """Setea el agent_id activo. Devuelve el Token para resetearlo después."""
    return _agent_id_var.set(agent_id)


def reset_agent_id(token: contextvars.Token) -> None:
    _agent_id_var.reset(token)


def get_agent_id() -> str | None:
    """Devuelve el agent_id activo en este contexto async, o None si no hay."""
    return _agent_id_var.get()
