"""Helpers para escribir eventos de traza del agente.

Cada función es best-effort: nunca propaga excepciones para no romper el
flujo del agente (igual filosofía que `runtime_logs.push_runtime_log`).

Resuelve el `conversation_id` automáticamente desde `core.trace_context`
si no se pasa explícitamente. Así los módulos profundos (providers,
tools) no necesitan recibir el conv_id por parámetro.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.logging import _scrub_value, get_logger
from app.core.trace_context import get_conversation_id
from app.db.session import db_session
from app.models.agent_trace import AgentTraceEvent, TraceEventType, TraceLevel

logger = get_logger(__name__)


# Cap del payload para evitar JSONBs gigantes que saturen la BD.
_PAYLOAD_STR_MAX = 4000
_PROMPT_PREVIEW_MAX = 1200
_RESPONSE_PREVIEW_MAX = 1200


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if v is None:
            continue
        # Redacta PII (teléfono/email/claves/JWT) de previews, args de tools y
        # queries antes de persistir la traza en BD — mismo scrubber que los logs.
        out[k] = _scrub_value(_truncate(v, _PAYLOAD_STR_MAX))
    return out


async def _persist(
    event_type: TraceEventType,
    payload: dict[str, Any],
    *,
    conversation_id: str | uuid.UUID | None = None,
    message_id: str | uuid.UUID | None = None,
    level: TraceLevel = TraceLevel.info,
    latency_ms: int | None = None,
    summary: str | None = None,
) -> None:
    """Inserta el evento. Si no hay conversation_id (ni explícito ni en
    contexto), abandona silenciosamente — no tiene a quién atar el evento."""
    conv_id = conversation_id or get_conversation_id()
    if not conv_id:
        return
    try:
        conv_uuid = conv_id if isinstance(conv_id, uuid.UUID) else uuid.UUID(str(conv_id))
        msg_uuid: uuid.UUID | None = None
        if message_id is not None:
            msg_uuid = (
                message_id if isinstance(message_id, uuid.UUID) else uuid.UUID(str(message_id))
            )

        async with db_session() as db:
            stmt = pg_insert(AgentTraceEvent).values(
                conversation_id=conv_uuid,
                message_id=msg_uuid,
                event_type=event_type.value,
                level=level.value,
                payload=_normalize_payload(payload),
                latency_ms=latency_ms,
                summary=summary[:500] if summary else None,
            )
            await db.execute(stmt)
            await db.commit()
    except Exception as e:
        # No queremos derribar el flujo del agente por un fallo de telemetría.
        logger.warning("trace.persist_failed", error=str(e), event=event_type.value)


# ------------------------ helpers públicos por tipo ------------------------


async def log_llm_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    prompt_preview: str | None = None,
    response_preview: str | None = None,
    finish_reason: str | None = None,
    source: str = "agent",
    conversation_id: str | uuid.UUID | None = None,
    level: TraceLevel = TraceLevel.info,
) -> None:
    payload = {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
        "cost_usd": cost_usd,
        "finish_reason": finish_reason,
        "source": source,
        "prompt_preview": _truncate(prompt_preview, _PROMPT_PREVIEW_MAX),
        "response_preview": _truncate(response_preview, _RESPONSE_PREVIEW_MAX),
    }
    summary = f"LLM {model} · {prompt_tokens}+{completion_tokens} tok"
    if cost_usd is not None:
        summary += f" · ${cost_usd:.4f}"
    await _persist(
        TraceEventType.llm_call,
        payload,
        conversation_id=conversation_id,
        level=level,
        latency_ms=latency_ms,
        summary=summary,
    )


async def log_tool_invocation(
    *,
    tool_name: str,
    args: dict[str, Any] | None = None,
    result_preview: str | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
    conversation_id: str | uuid.UUID | None = None,
) -> None:
    payload = {
        "tool_name": tool_name,
        "args": args,
        "result_preview": _truncate(result_preview, _PAYLOAD_STR_MAX),
        "error": error,
    }
    level = TraceLevel.error if error else TraceLevel.info
    summary = f"Tool · {tool_name}" + (f" — error: {error[:80]}" if error else "")
    await _persist(
        TraceEventType.tool_invocation,
        payload,
        conversation_id=conversation_id,
        level=level,
        latency_ms=latency_ms,
        summary=summary,
    )


async def log_kb_lookup(
    *,
    query: str,
    hits: int,
    chunks: list[dict[str, Any]] | None = None,
    latency_ms: int | None = None,
    conversation_id: str | uuid.UUID | None = None,
) -> None:
    # Recorta cada chunk a un preview corto para no engordar el payload.
    chunks_preview = None
    if chunks:
        chunks_preview = [
            {
                "id": str(c.get("id", "")),
                "document_id": str(c.get("document_id", "")),
                "fuente": c.get("fuente"),
                "similarity": c.get("similarity"),
                "preview": _truncate(c.get("contenido", ""), 400),
            }
            for c in chunks[:8]
        ]
    payload = {
        "query": _truncate(query, 500),
        "hits": hits,
        "chunks": chunks_preview,
    }
    summary = f"KB · «{query[:60]}» → {hits} chunk(s)"
    await _persist(
        TraceEventType.kb_lookup,
        payload,
        conversation_id=conversation_id,
        latency_ms=latency_ms,
        summary=summary,
    )


async def log_router_decision(
    *,
    decision: str,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    conversation_id: str | uuid.UUID | None = None,
    level: TraceLevel = TraceLevel.info,
) -> None:
    payload = {
        "decision": decision,
        "reason": reason,
        "details": details,
    }
    summary = f"Router · {decision}" + (f" — {reason[:80]}" if reason else "")
    await _persist(
        TraceEventType.router_decision,
        payload,
        conversation_id=conversation_id,
        level=level,
        summary=summary,
    )


async def log_error(
    *,
    error_type: str,
    message: str,
    where: str | None = None,
    details: dict[str, Any] | None = None,
    conversation_id: str | uuid.UUID | None = None,
) -> None:
    payload = {
        "error_type": error_type,
        "message": _truncate(message, _PAYLOAD_STR_MAX),
        "where": where,
        "details": details,
    }
    summary = f"Error · {error_type}" + (f" @ {where}" if where else "")
    await _persist(
        TraceEventType.error,
        payload,
        conversation_id=conversation_id,
        level=TraceLevel.error,
        summary=summary,
    )
