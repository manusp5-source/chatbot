"""Contabilidad de una llamada al modelo: tokens, coste, presupuesto y traza.

Estaba escrito a mano dentro de `openai_client.complete()`. Al entrar Anthropic
como segundo cliente nativo había que repetirlo entero, y un consumo que no se
apunta es un tope de gasto que no salta: si el proveedor nuevo se olvidaba de
llamar a `check_budget_and_maybe_pause`, el agente podía seguir gastando por
encima del tope sin que nada lo parase. Vive aquí para que sea imposible
implementar un proveedor y dejarse la contabilidad.

Se conserva EXACTAMENTE la estructura de antes, incluidos los dos `try/except`
por separado: son best-effort a propósito — que falle Redis, la BD de trazas o
la tabla de precios no puede tumbar una respuesta al cliente final. Son dos
bloques y no uno para que un fallo apuntando el gasto no impida además dejar
traza del error, y al revés.
"""
from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.core.trace_context import get_agent_id, get_conversation_id

logger = get_logger(__name__)

# Modelos por los que YA hemos avisado de que no tienen tarifa, para no repetir
# el aviso en cada llamada (esto corre en la ruta caliente de cada mensaje).
_warned_unpriced: set[str] = set()


async def record_llm_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int | None,
    has_usage: bool,
    latency_ms: int,
    prompt_preview: str | None,
    response_preview: str | None,
    finish_reason: str | None,
    source: str,
) -> None:
    """Apunta tokens, comprueba presupuesto y deja traza. Nunca lanza."""
    conv_id = get_conversation_id()
    agent_id = get_agent_id()

    try:
        from app.services.usage_tracker import track_usage

        if has_usage:
            await track_usage(
                source=source,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                conversation_id=conv_id,
                agent_id=agent_id,
            )
            # Check de presupuesto del bot publico: si superado, pausa el
            # agente. NO aplica al agente interno (tiene su propio budget
            # check en app.agents.internal.budget).
            if source == "agent":
                from app.services.budget import check_budget_and_maybe_pause

                # `agent_id` va explícito: además del tope global, aplica el
                # tope propio del agente que ha gastado.
                await check_budget_and_maybe_pause(agent_id)
    except Exception:
        pass

    try:
        from app.services.llm_pricing import estimate_cost_usd
        from app.services.trace_logger import log_llm_call

        cost_usd = round(estimate_cost_usd(model, prompt_tokens, completion_tokens), 6)
        await _warn_if_unpriced(
            model=model,
            tokens=prompt_tokens + completion_tokens,
            source=source,
        )
        # AgentTraceEvent requiere conversation_id NOT NULL → solo
        # registramos el llm_call detallado para el agente publico (que
        # corre en una conversation). El agente interno tiene auditoria
        # propia en audit_log.
        if source == "agent" and conv_id:
            await log_llm_call(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                prompt_preview=prompt_preview,
                response_preview=response_preview,
                finish_reason=finish_reason,
                source=source,
            )
    except Exception:
        pass


async def _warn_if_unpriced(*, model: str, tokens: int, source: str, cost_usd: float | None = None) -> None:
    """Avisa UNA vez por modelo si se está consumiendo sin tarifa conocida.

    De un modelo que no está en la tabla no sabemos el precio, así que su coste
    se estima con una tarifa de respaldo (ver `llm_pricing.PRECIO_RESPALDO`).
    Eso mantiene vivo el tope de presupuesto, pero el número que se enseña es
    aproximado y hay que decirlo: con Anthropic y Gemini el riesgo sube, porque
    sus identificadores no siempre casan con los de OpenRouter a la primera.

    Se mira el MODELO, no el coste: mientras se miraba el coste ("si sale 0 es
    que no tiene tarifa"), poner una tarifa de respaldo dejaba este aviso mudo
    justo cuando más falta hace.

    `cost_usd` se acepta por compatibilidad y no se usa.
    """
    from app.services.llm_pricing import modelo_sin_tarifa

    if tokens <= 0 or not model or not modelo_sin_tarifa(model):
        return
    if model in _warned_unpriced:
        return
    _warned_unpriced.add(model)
    logger.warning("llm.model_unpriced", model=model, source=source)
    try:
        from app.services.runtime_logs import push_runtime_log

        await push_runtime_log(
            level="warn",
            event="llm.model_unpriced",
            message=(
                f"El modelo «{model}» está consumiendo tokens y no tiene precio en la "
                "tabla de tarifas: su gasto se está estimando por lo alto para que el "
                "tope de presupuesto siga funcionando, pero el importe que ves NO es el "
                "real. Añade su tarifa en Admin → Precios de modelos o pulsa «Actualizar "
                "precios»."
            ),
            source=source,
        )
    except Exception:  # noqa: BLE001 — el aviso nunca tapa la respuesta
        pass


def build_prompt_preview(messages: list[dict[str, Any]]) -> str | None:
    """Preview corto para el panel: resumen del 'system' y último 'user'.

    El histórico completo puede ser de cientos de tokens — para el panel nos
    basta con el mensaje al que el agente está respondiendo.
    """
    last_user = next(
        (
            m.get("content")
            for m in reversed(messages)
            if m.get("role") == "user" and m.get("content")
        ),
        None,
    )
    sys_msg = next(
        (m.get("content") for m in messages if m.get("role") == "system" and m.get("content")),
        None,
    )
    parts: list[str] = []
    if sys_msg:
        sys_text = _as_text(sys_msg)
        parts.append("[system] " + (sys_text[:240] + "…" if len(sys_text) > 240 else sys_text))
    if last_user:
        user_text = _as_text(last_user)
        parts.append("[user] " + (user_text[:600] + "…" if len(user_text) > 600 else user_text))
    return "\n".join(parts) if parts else None


def _as_text(value: Any) -> str:
    """Texto plano de un `content` que puede venir en bloques.

    En Anthropic el contenido de un turno es una LISTA de bloques
    (`{"type": "text", ...}`, `{"type": "tool_result", ...}`), no una cadena.
    Sin esto el preview del panel enseñaría el `repr` de una lista de dicts.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            str(b.get("text", ""))
            for b in value
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return str(value)
