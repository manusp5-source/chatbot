"""Implementación Anthropic (Claude) del LLMProvider, con tool use.

Anthropic NO es compatible con la API de OpenAI. Las cuatro diferencias que
obligan a un cliente propio en vez de cambiarle la `base_url` al de OpenAI:

  1. Autenticación con cabecera `x-api-key`, no `Authorization: Bearer`, y con
     `anthropic-version` obligatoria.
  2. El endpoint es `POST /v1/messages`, no `/chat/completions`.
  3. El prompt de sistema va en un parámetro `system` APARTE. En `messages` solo
     caben `user` y `assistant`; un mensaje con `role="system"` es un error.
  4. Las herramientas usan `input_schema` (no `function.parameters`), la llamada
     llega como un BLOQUE `tool_use` dentro del contenido del assistant (no en
     un campo `tool_calls` hermano) y el resultado se devuelve como bloque
     `tool_result` dentro de un turno de USUARIO, no en un rol `tool` propio.

La traducción va en los dos sentidos: `_to_anthropic_messages` convierte los
`LLMMessage` del orquestador al formato de Anthropic, y `complete()` convierte
la respuesta de vuelta a `LLMCompletion` con `LLMToolCall` — el orquestador
(`agents/orchestrator.py`) no se entera de con qué proveedor está hablando.

El SDK oficial se importa PEREZOSAMENTE (dentro de la función). Si el paquete
`anthropic` no está instalado —despliegue viejo que no ha reconstruido la
imagen— solo falla este proveedor, con un mensaje que dice qué hacer; el
proveedor de OpenAI y las pasarelas compatibles siguen funcionando igual.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.llm.base import (
    LLMCompletion,
    LLMMessage,
    LLMNotConfiguredError,
    LLMProvider,
    LLMToolCall,
    LLMToolSchema,
)
from app.providers.llm.families import ANTHROPIC_BASE_URL
from app.providers.llm.telemetry import build_prompt_preview, record_llm_call
from app.services.credentials import get_credential

logger = get_logger(__name__)

# Mismo criterio que el resto del chat: más de un minuto esperando al modelo no
# le sirve a nadie — vale más rendirse y que salte el failover.
ANTHROPIC_TIMEOUT_SECONDS = 60.0
ANTHROPIC_CONNECT_TIMEOUT_SECONDS = 10.0
ANTHROPIC_MAX_RETRIES = 1

# `max_tokens` es OBLIGATORIO en la Messages API (en OpenAI es opcional). Si el
# agente no lo fija, hay que poner algo: 4096 da de sobra para una respuesta de
# atención al cliente sin arriesgar una factura por una respuesta desbocada.
DEFAULT_MAX_TOKENS = 4096

# Modelos que RECHAZAN `temperature` con un 400 (Anthropic quitó los parámetros
# de sampling a partir de Opus 4.7). No basta con la casilla
# `accepts_temperature` de la ficha del proveedor: si el admin la deja marcada
# por descuido, TODAS las respuestas fallarían con un 400 y el agente derivaría
# a humano sin que se entienda por qué. Este guardarraíl lo hace imposible.
NO_TEMPERATURE_PREFIXES = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-",
    "claude-mythos-",
)

# Traducción de `stop_reason` de Anthropic al vocabulario de OpenAI que ya
# aparece en el panel de trazas. Nadie ramifica por este valor (solo se
# registra), pero mezclar dos vocabularios en la misma columna haría ilegible el
# histórico. `refusal` no tiene equivalente en OpenAI y se deja tal cual.
_FINISH_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "tool_calls",
    "refusal": "refusal",
}


def rejects_temperature(model_name: str) -> bool:
    """True si el modelo devuelve 400 al recibir `temperature`."""
    return any((model_name or "").startswith(p) for p in NO_TEMPERATURE_PREFIXES)


class AnthropicProvider(LLMProvider):
    """Cliente para la Messages API de Anthropic (Claude).

    Misma forma de construcción que `OpenAIProvider` para que
    `resolve_llm_provider()` pueda tratarlos igual: o bien claves por nombre de
    credencial, o bien valores explícitos ya descifrados de una fila de
    `llm_providers`.
    """

    def __init__(
        self,
        *,
        api_key_credential: str = "anthropic_api_key",
        default_model: str | None = None,
        label: str = "anthropic",
        explicit_api_key: str | None = None,
        explicit_base_url: str | None = None,
        accepts_temperature: bool | None = None,
    ) -> None:
        self.api_key_credential = api_key_credential
        self.default_model = default_model
        self.label = label
        self.explicit_api_key = explicit_api_key or None
        self.explicit_base_url = (explicit_base_url or "").strip() or None
        self.accepts_temperature = accepts_temperature

    async def _resolve_config(self) -> tuple[str | None, str, str | None]:
        """(api_key, base_url, model). La clave explícita de la fila manda."""
        if self.explicit_api_key:
            api_key: str | None = self.explicit_api_key
        else:
            api_key = await get_credential(self.api_key_credential)
        base_url = self.explicit_base_url or ANTHROPIC_BASE_URL
        return (api_key or None), base_url, (self.default_model or None)

    async def is_configured(self) -> bool:
        """True si tiene clave. Anthropic no necesita más: la URL es fija y el
        modelo lo pone el agente (o el `fallback_model` si actúa de respaldo)."""
        api_key, _base_url, _model = await self._resolve_config()
        return bool(api_key)

    def _client(self, api_key: str, base_url: str) -> Any:
        return get_async_anthropic(api_key=api_key, base_url=base_url)

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        source: str = "agent",
    ) -> LLMCompletion:
        api_key, base_url, configured_model = await self._resolve_config()
        if not api_key:
            # Igual que en el cliente de OpenAI: esto es un FALLO, no una
            # respuesta. Devolverlo como texto acabaría mandándole al cliente
            # final del negocio un mensaje interno del panel por WhatsApp.
            logger.warning("llm.not_configured", provider=self.label)
            try:
                from app.services.runtime_logs import push_runtime_log

                await push_runtime_log(
                    level="error",
                    event="llm.not_configured",
                    message=(
                        f"El proveedor de modelo «{self.label}» no tiene credenciales: no se "
                        "puede responder. Configura la clave en Admin → Proveedores LLM."
                    ),
                    source=source,
                )
            except Exception:  # noqa: BLE001 — el aviso nunca tapa el error real
                pass
            raise LLMNotConfiguredError(
                f"El proveedor de modelo '{self.label}' no tiene credenciales configuradas"
            )

        client = self._client(api_key, base_url)

        # `configured_model` (rol de respaldo) manda sobre el modelo del agente:
        # el id del primario no existe en Anthropic.
        model_name = configured_model or model or settings.DEFAULT_LLM_MODEL
        system_prompt, anthropic_messages = _to_anthropic_messages(messages)

        params: dict[str, Any] = {
            "model": model_name,
            # Obligatorio en la Messages API.
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "messages": anthropic_messages,
        }
        if system_prompt:
            params["system"] = system_prompt
        # Doble condición: la casilla de la ficha Y el guardarraíl por modelo.
        allowed_by_row = self.accepts_temperature is not False
        if temperature is not None and allowed_by_row and not rejects_temperature(model_name):
            params["temperature"] = temperature
        if tools:
            params["tools"] = [_to_anthropic_tool(t) for t in tools]

        started = time.perf_counter()
        try:
            resp = await client.messages.create(**params)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            try:
                from app.services.trace_logger import log_error

                await log_error(
                    error_type="llm_call_failed",
                    message=str(e),
                    where="anthropic_client.complete",
                    details={"model": model_name, "provider": self.label},
                )
            except Exception:
                pass
            logger.error(
                "llm.call_failed", model=model_name, error=str(e), latency_ms=elapsed_ms
            )
            raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        content, tool_calls = _from_anthropic_content(resp.content)
        usage_obj = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage_obj, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)

        response_preview = (content or "").strip() or None
        if tool_calls and not response_preview:
            response_preview = "[tool_calls] " + ", ".join(tc.name for tc in tool_calls)
        raw_stop = getattr(resp, "stop_reason", None)
        finish_reason = _FINISH_REASON.get(raw_stop or "", raw_stop)

        await record_llm_call(
            model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            has_usage=usage_obj is not None,
            latency_ms=elapsed_ms,
            prompt_preview=build_prompt_preview(
                ([{"role": "system", "content": system_prompt}] if system_prompt else [])
                + anthropic_messages
            ),
            response_preview=response_preview,
            finish_reason=finish_reason,
            source=source,
        )

        usage: dict[str, int] = {}
        if usage_obj is not None:
            # Anthropic los llama input/output; el resto de la app espera los
            # nombres de OpenAI (usage_tracker, budget, panel de consumo).
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }

        return LLMCompletion(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )


# --------------------------- Traducción de ida ---------------------------


def _to_anthropic_tool(t: LLMToolSchema) -> dict[str, Any]:
    """LLMToolSchema → herramienta de Anthropic.

    Es un objeto PLANO con `input_schema`, no el envoltorio
    `{"type": "function", "function": {...}}` de OpenAI, y el JSON Schema de los
    parámetros se llama `input_schema` en vez de `parameters`.
    """
    return {"name": t.name, "description": t.description, "input_schema": t.parameters}


def _to_anthropic_messages(
    messages: list[LLMMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """`LLMMessage[]` → (system, messages) en formato Anthropic.

    Cuatro reglas que impone la Messages API y que hay que respetar aquí:

      - `system` sale de `messages` y va en su propio parámetro. Si hubiera
        varios (hoy no los hay, pero la interfaz lo permite) se concatenan.
      - El rol `tool` no existe: un resultado de herramienta es un bloque
        `tool_result` dentro de un turno de USUARIO. Los resultados
        CONSECUTIVOS se agrupan en un único turno, que es la forma que la API
        documenta cuando el modelo pide varias herramientas a la vez.
      - Un `assistant` con tool_calls lleva sus bloques `tool_use` DENTRO del
        contenido, junto al texto si lo hubiera.
      - El primer mensaje tiene que ser de `user`.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue

        if m.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                # La API rechaza un bloque de texto vacío; "" no es un
                # resultado válido, pero un guion sí y significa lo mismo.
                "content": m.content or "-",
            }
            # Agrupar con el turno anterior si ya era un user de tool_results.
            if out and out[-1]["role"] == "user" and _is_tool_result_turn(out[-1]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content and m.content.strip():
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments or {},
                    }
                )
            # Un assistant sin texto y sin tool_calls no tiene contenido válido
            # para Anthropic: se descarta en vez de mandar un turno vacío (400).
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        # role == "user"
        if m.content and m.content.strip():
            out.append({"role": "user", "content": m.content})

    # El primer turno DEBE ser de usuario. Puede no serlo cuando el negocio
    # abrió la conversación (el bot saludó primero) y ese saludo es el inicio
    # del histórico. Se antepone un turno mínimo y marcado como contexto, en vez
    # de tirar el saludo: perder el primer mensaje descuadra la conversación.
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(inicio de la conversación)"})

    return ("\n\n".join(system_parts) or None), out


def _is_tool_result_turn(turn: dict[str, Any]) -> bool:
    """True si el turno es una lista de bloques que arranca con tool_result."""
    content = turn.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and isinstance(content[0], dict)
        and content[0].get("type") == "tool_result"
    )


# -------------------------- Traducción de vuelta --------------------------


def _from_anthropic_content(blocks: Any) -> tuple[str | None, list[LLMToolCall]]:
    """Contenido de la respuesta de Anthropic → (texto, tool_calls).

    La respuesta es una LISTA de bloques heterogéneos, no un mensaje con un
    campo de texto. Los `text` se concatenan y cada `tool_use` se convierte en
    un `LLMToolCall` con `arguments` ya como dict — Anthropic manda el `input`
    parseado, así que aquí no hay `json.loads` que pueda romper (a diferencia de
    OpenAI, que lo manda como cadena JSON).

    Los bloques que no entendemos (`thinking`, etc.) se ignoran a propósito: no
    pedimos thinking, pero si un día se activa no debe romper la respuesta.
    """
    texts: list[str] = []
    tool_calls: list[LLMToolCall] = []
    for b in blocks or []:
        btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if btype == "text":
            text = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
            if text:
                texts.append(str(text))
        elif btype == "tool_use":
            if isinstance(b, dict):
                bid, name, args = b.get("id"), b.get("name"), b.get("input")
            else:
                bid, name, args = (
                    getattr(b, "id", None),
                    getattr(b, "name", None),
                    getattr(b, "input", None),
                )
            tool_calls.append(
                LLMToolCall(
                    id=str(bid or ""),
                    name=str(name or ""),
                    arguments=args if isinstance(args, dict) else {},
                )
            )
    return ("\n".join(texts) or None), tool_calls


# ------------------------- Fábrica cacheada de clientes -------------------------
#
# Mismo problema y misma solución que `providers/openai_factory.py`: un cliente
# por llamada abre un pool de conexiones nuevo que nadie cierra, y el pool queda
# atado al event loop donde se creó (Celery abre uno por task). Se cachea por
# (loop, clave, base_url) para que cambiar la clave en el panel aplique sin
# reiniciar. Vive aquí y no en la fábrica de OpenAI porque el objeto y su
# constructor son de otro SDK.

_clients: dict[tuple, tuple[asyncio.AbstractEventLoop, Any]] = {}


def get_async_anthropic(*, api_key: str, base_url: str = ANTHROPIC_BASE_URL) -> Any:
    """Cliente AsyncAnthropic del event loop actual (lo crea si no existe)."""
    try:
        import httpx
        from anthropic import AsyncAnthropic
    except ImportError as e:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            "Falta el paquete «anthropic» para usar proveedores Claude. "
            "Añádelo a backend/requirements.txt y reconstruye la imagen."
        ) from e

    loop = asyncio.get_running_loop()
    # Poda best-effort: suelta los clientes de loops ya cerrados y evita que un
    # id() reciclado devuelva un cliente muerto.
    for key, (cached_loop, _client) in list(_clients.items()):
        if cached_loop.is_closed():
            _clients.pop(key, None)

    key = (id(loop), api_key, base_url)
    entry = _clients.get(key)
    if entry is not None:
        return entry[1]

    client = AsyncAnthropic(
        api_key=api_key,
        base_url=base_url,
        timeout=httpx.Timeout(
            ANTHROPIC_TIMEOUT_SECONDS, connect=ANTHROPIC_CONNECT_TIMEOUT_SECONDS
        ),
        max_retries=ANTHROPIC_MAX_RETRIES,
    )
    _clients[key] = (loop, client)
    return client


async def close_anthropic_clients() -> None:
    """Cierra los clientes del loop actual (apagado de la API)."""
    loop_id = id(asyncio.get_running_loop())
    for key in [k for k in _clients if k[0] == loop_id]:
        _loop, client = _clients.pop(key)
        try:
            await client.close()
        except Exception:
            pass


def reset_anthropic_clients() -> None:
    """Vacía la caché sin cerrar nada. Solo para los tests."""
    _clients.clear()
