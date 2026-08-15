"""Implementación OpenAI del LLMProvider con tool use.

La api_key vive cifrada en la BD (tabla `credentials`, key `openai_api_key`)
y se lee vía `app.services.credentials.get_credential` en cada llamada.
Así un cambio desde `/admin/credentials` aplica sin reiniciar.

Soporta modelos GPT-5.x mini / nano. El SDK oficial maneja el formato de
mensajes y tool_calls.
"""
import json
import time
import uuid
from typing import Any

from openai import AsyncOpenAI

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
from app.providers.llm.families import detect_family
from app.providers.llm.telemetry import build_prompt_preview, record_llm_call
from app.providers.openai_factory import CHAT_TIMEOUT_SECONDS, get_async_openai
from app.services.credentials import get_credential

logger = get_logger(__name__)

# Base URL por defecto del secundario: OpenRouter (compatible con la API de
# OpenAI). Así basta con poner la clave en el panel; URL y modelo solo se
# rellenan si se quiere otro gateway u otro modelo de respaldo.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Modelo de respaldo por defecto (id de OpenRouter). Overridable en el panel.
DEFAULT_FALLBACK_MODEL = "deepseek/deepseek-v4-flash"

# Prefijos de los "reasoning models" de OpenAI: no admiten `temperature` (va
# fija a 1) y el tope de salida se llama `max_completion_tokens`.
REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def is_reasoning_model(model_name: str) -> bool:
    """True si el modelo ignora `temperature` (heurística por nombre)."""
    return any((model_name or "").startswith(p) for p in REASONING_MODEL_PREFIXES)


class OpenAIProvider(LLMProvider):
    """Cliente para un endpoint compatible con la API de OpenAI.

    Sirve tanto para el proveedor PRIMARIO (OpenAI directo) como para un
    SECUNDARIO por gateway compatible (OpenRouter / Azure / Together / etc.):
    basta con apuntar a otra `base_url`, otra clave y otro `model`. Todo se lee
    de credenciales en cada llamada, así un cambio en el panel aplica sin
    reiniciar.
    """

    def __init__(
        self,
        *,
        api_key_credential: str = "openai_api_key",
        base_url_credential: str | None = None,
        model_credential: str | None = None,
        default_base_url: str | None = None,
        default_model: str | None = None,
        label: str = "openai",
        # --- Modo "fila de proveedor" (proveedores configurables por agente):
        # valores literales ya resueltos (clave descifrada) que tienen prioridad
        # sobre las credenciales por nombre. Se usa desde resolve_llm_provider().
        explicit_api_key: str | None = None,
        explicit_base_url: str | None = None,
        accepts_temperature: bool | None = None,
    ) -> None:
        self.api_key_credential = api_key_credential
        self.base_url_credential = base_url_credential
        self.model_credential = model_credential
        # base_url / model usados si la credencial está vacía (p.ej. OpenRouter +
        # deepseek por defecto en el secundario): así basta con poner la clave.
        self.default_base_url = default_base_url
        self.default_model = default_model
        self.label = label
        self.explicit_api_key = explicit_api_key or None
        self.explicit_base_url = explicit_base_url or None
        # None = decidir por heurística de nombre (compat); True/False = lo fija
        # el proveedor (un modelo no-OpenAI de nombre "gpt-…" NO debe caer en la
        # rama reasoning que le quita temperature).
        self.accepts_temperature = accepts_temperature

    async def _resolve_config(self) -> tuple[str | None, str | None, str | None]:
        """(api_key, base_url, model) leídos de credenciales. '' se trata como None.

        Si hay clave explícita (fila de proveedor), tiene prioridad; para el
        proveedor "OpenAI" sembrado sin clave propia, cae a la credencial
        openai_api_key (retrocompatibilidad)."""
        if self.explicit_api_key:
            return self.explicit_api_key, self.explicit_base_url, self.default_model
        api_key = await get_credential(self.api_key_credential)
        base_url = (
            await get_credential(self.base_url_credential)
            if self.base_url_credential
            else None
        )
        base_url = base_url or self.explicit_base_url or self.default_base_url
        model = (
            await get_credential(self.model_credential)
            if self.model_credential
            else None
        )
        model = model or self.default_model
        return (api_key or None), (base_url or None), (model or None)

    async def is_configured(self) -> bool:
        """True si tiene lo necesario para llamar.

        El primario solo necesita la clave. El secundario (gateway) necesita
        además `base_url` y `model`: su host y su id de modelo son distintos.
        """
        api_key, base_url, model = await self._resolve_config()
        if not api_key:
            return False
        if self.base_url_credential and not base_url:
            return False
        if self.model_credential and not model:
            return False
        return True

    def _client(self, api_key: str, base_url: str | None) -> AsyncOpenAI:
        """Cliente HTTP, con timeout y reutilizado entre llamadas.

        Cacheado en la fábrica: crear uno por llamada abría un pool de
        conexiones nuevo cada vez y ninguno se cerraba.
        """
        return get_async_openai(
            api_key=api_key, base_url=base_url, timeout=CHAT_TIMEOUT_SECONDS
        )

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
        if not api_key or (self.base_url_credential and not base_url) or (
            self.model_credential and not configured_model
        ):
            # NO es una respuesta: es un fallo. Devolverlo como texto acababa
            # mandándole al cliente final del negocio un mensaje interno del
            # panel ("Configura las claves en el panel admin") por WhatsApp.
            logger.warning("llm.not_configured", provider=self.label)
            try:
                from app.services.runtime_logs import push_runtime_log

                await push_runtime_log(
                    level="error",
                    event="llm.not_configured",
                    message=(
                        f"El proveedor de modelo «{self.label}» no tiene credenciales: no se "
                        "puede responder. Configura la clave en Admin → Credenciales."
                    ),
                    source=source,
                )
            except Exception:  # noqa: BLE001 — el aviso nunca tapa el error real
                pass
            raise LLMNotConfiguredError(
                f"El proveedor de modelo '{self.label}' no tiene credenciales configuradas"
            )
        client = self._client(api_key, base_url or None)

        oai_messages = [_to_openai_message(m) for m in messages]
        oai_tools = [_to_openai_tool(t) for t in tools] if tools else None

        # El secundario fuerza SU modelo (configured_model): el id del primario
        # (p.ej. "gpt-5.4-mini") no existe en el gateway. El primario no tiene
        # model_credential → usa el modelo del agente que viene por parámetro.
        model_name = configured_model or model or settings.DEFAULT_LLM_MODEL
        # Los modelos GPT-5.x / o1 / o3 / o4 son "reasoning models": OpenAI no
        # permite ajustar temperature (debe ser 1, fijo) y `max_tokens` se llama
        # allí `max_completion_tokens`. El proveedor puede fijar si acepta
        # temperature (flag de la fila); si no se especifica (None), caemos a la
        # heurística por nombre (compat con el proveedor por defecto OpenAI).
        if self.accepts_temperature is not None:
            is_reasoning = not self.accepts_temperature
        else:
            is_reasoning = is_reasoning_model(model_name)
        params: dict[str, Any] = {
            "model": model_name,
            "messages": oai_messages,
        }
        if temperature is not None and not is_reasoning:
            params["temperature"] = temperature
        if max_tokens:
            # El tope de tokens SÍ se puede aplicar a un reasoning model: solo
            # cambia el nombre del parámetro. Antes se descartaba junto con la
            # temperature, así que con el modelo por defecto (gpt-5.4-mini) el
            # campo "Max tokens" del formulario de Agentes no hacía NADA.
            params["max_completion_tokens" if is_reasoning else "max_tokens"] = max_tokens
        if oai_tools:
            params["tools"] = oai_tools

        started = time.perf_counter()
        try:
            resp = await client.chat.completions.create(**params)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            # Trace del error para poder debuggear desde el panel.
            try:
                from app.services.trace_logger import log_error
                await log_error(
                    error_type="llm_call_failed",
                    message=str(e),
                    where="openai_client.complete",
                    details={"model": model_name, "is_reasoning": is_reasoning},
                )
            except Exception:
                pass
            logger.error("llm.call_failed", model=model_name, error=str(e), latency_ms=elapsed_ms)
            raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        choice = resp.choices[0]
        msg = choice.message

        # Telemetría best-effort (tokens, presupuesto y traza). Vive en
        # providers/llm/telemetry.py para que el cliente de Anthropic use
        # EXACTAMENTE la misma contabilidad: un consumo que no se apunta es un
        # tope de gasto que no salta.
        prompt_tokens = (resp.usage.prompt_tokens or 0) if resp.usage else 0
        completion_tokens = (resp.usage.completion_tokens or 0) if resp.usage else 0
        response_preview = (msg.content or "").strip() or None
        if msg.tool_calls and not response_preview:
            response_preview = "[tool_calls] " + ", ".join(
                tc.function.name for tc in msg.tool_calls
            )
        await record_llm_call(
            model=params["model"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=(resp.usage.total_tokens if resp.usage else None),
            has_usage=bool(resp.usage),
            latency_ms=elapsed_ms,
            # Previews cortos para el trace (sistema + último user; suficiente
            # para entender qué pidió el agente sin volcar todo el historial).
            prompt_preview=build_prompt_preview(oai_messages),
            response_preview=response_preview,
            finish_reason=choice.finish_reason,
            source=source,
        )

        tool_calls: list[LLMToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(LLMToolCall(id=tc.id, name=tc.function.name, arguments=args))

        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }

        return LLMCompletion(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
        )


def _to_openai_message(m: LLMMessage) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""}
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in m.tool_calls
        ]
        # OpenAI requiere content=null en assistant cuando hay tool_calls
        if not out.get("content"):
            out["content"] = None
    return out


def _to_openai_tool(t: LLMToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }


class FallbackLLMProvider(LLMProvider):
    """Encadena un proveedor primario y uno secundario.

    En CADA `complete()`: intenta el primario; si lanza una excepción (timeout,
    429, 5xx, red…) o no está configurado, registra el failover y reintenta con
    el secundario — MISMO prompt, MISMAS tools, MISMO agente; solo cambia el
    backend de LLM. Si el secundario no está configurado, propaga el error del
    primario (el flujo de conversación lo deriva a humano, como hasta ahora).

    El failover es POR LLAMADA y no por `run_agent` entero a propósito: si el
    fallo ocurre a mitad del bucle de tools, reintentar todo re-ejecutaría tools
    con efectos (guardar contacto, enviar mensajes) → doble efecto y doble coste.

    Primario y secundario pueden ser de FAMILIAS distintas (p. ej. Claude como
    principal y GPT de respaldo, o al revés). Funciona porque los dos lados
    hablan en `LLMMessage`/`LLMToolSchema` y cada cliente traduce a su protocolo:
    el mismo prompt y las mismas herramientas valen para los dos. Lo único que
    se exige del secundario, además de `complete()`, es `is_configured()`.
    """

    def __init__(self, primary: LLMProvider, secondary: LLMProvider) -> None:
        self.primary = primary
        self.secondary = secondary

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        source: str = "agent",
    ) -> LLMCompletion:
        primary_exc: Exception
        try:
            return await self.primary.complete(
                messages, tools, model, temperature, max_tokens, source
            )
        except Exception as e:  # noqa: BLE001 — cualquier fallo dispara el failover
            # Incluye LLMNotConfiguredError: "el primario no tiene clave" es un
            # motivo de failover como cualquier otro, no una respuesta con texto
            # de aviso que se le pueda entregar al cliente.
            primary_exc = e
            logger.warning("llm.primary_failed", error=str(e))

        # El primario no dio respuesta. Si tampoco hay secundario, propagamos:
        # el flujo de conversación lo traduce en derivación a una persona.
        if not await self.secondary.is_configured():
            raise primary_exc

        reason = str(primary_exc)
        try:
            from app.services.runtime_logs import push_runtime_log
            await push_runtime_log(
                level="warn",
                event="llm.failover",
                message="El LLM primario falló; usando el proveedor de fallback",
                error=reason[:500],
                source=source,
            )
        except Exception:
            pass
        try:
            from app.services.trace_logger import log_error
            await log_error(
                error_type="llm_failover",
                message=reason[:500],
                where="FallbackLLMProvider.complete",
            )
        except Exception:
            pass

        # El secundario usa su propio modelo (model=None → su model_credential).
        # Si TAMBIÉN falla, la excepción propaga → el flujo deriva a humano.
        return await self.secondary.complete(
            messages, tools, None, temperature, max_tokens, source
        )


_provider: LLMProvider | None = None
_secondary: "OpenAIProvider | None" = None


def _get_secondary() -> "OpenAIProvider":
    """Proveedor de FALLBACK (secundario). Sigue siendo el mismo mecanismo por
    credenciales (`llm_fallback_*`) para no romper la config existente."""
    global _secondary
    if _secondary is None:
        _secondary = OpenAIProvider(
            api_key_credential="llm_fallback_api_key",
            base_url_credential="llm_fallback_base_url",
            model_credential="llm_fallback_model",
            default_base_url=OPENROUTER_BASE_URL,
            default_model=DEFAULT_FALLBACK_MODEL,
            label="fallback",
        )
    return _secondary


def get_llm_provider() -> LLMProvider:
    """Proveedor LLM POR DEFECTO: primario OpenAI (credencial openai_api_key)
    con failover al secundario. Compatibilidad hacia atrás — lo siguen usando
    las tareas de sistema (aprendizaje) que no tienen agente asociado.
    """
    global _provider
    if _provider is None:
        primary = OpenAIProvider(api_key_credential="openai_api_key", label="openai")
        _provider = FallbackLLMProvider(primary, _get_secondary())
    return _provider


def _primary_from_row(row, *, forced_model: "str | None" = None) -> LLMProvider:
    """Cliente concreto a partir de una fila de llm_providers.

    Se elige la clase según la FAMILIA de API de su `base_url` (ver
    `providers/llm/families.py`): Anthropic habla su propio protocolo, Gemini va
    por su capa compatible con OpenAI, y todo lo demás —incluido el proveedor
    "OpenAI" sembrado con base_url vacía— usa el cliente genérico de siempre.

    El proveedor "OpenAI" sembrado (base_url y api_key vacíos) se comporta
    EXACTAMENTE como el primario por credencial de siempre. `forced_model`
    fija el modelo (para el rol de RESPALDO: los ids del primario no existen
    en otro gateway).
    """
    label_prefix = "provider" if not forced_model else "fallback"
    if not row.base_url and not row.api_key:
        return OpenAIProvider(
            api_key_credential="openai_api_key",
            default_model=forced_model,
            label="openai" if not forced_model else "fallback:openai",
        )

    family = detect_family(row.base_url)
    if family == "anthropic":
        # Import perezoso: anthropic_client vive en el mismo paquete y el SDK
        # solo se necesita si de verdad hay un proveedor Claude configurado.
        from app.providers.llm.anthropic_client import AnthropicProvider

        return AnthropicProvider(
            explicit_api_key=row.api_key or None,
            explicit_base_url=row.base_url or None,
            accepts_temperature=bool(row.accepts_temperature),
            default_model=forced_model,
            label=f"{label_prefix}:{row.name}",
        )
    if family == "gemini":
        # Import perezoso también: gemini_client importa ESTE módulo
        # (hereda de OpenAIProvider), así que a nivel de módulo sería circular.
        from app.providers.llm.gemini_client import GeminiProvider

        return GeminiProvider(
            explicit_api_key=row.api_key or None,
            explicit_base_url=row.base_url or None,
            accepts_temperature=bool(row.accepts_temperature),
            default_model=forced_model,
            label=f"{label_prefix}:{row.name}",
        )
    return OpenAIProvider(
        explicit_api_key=row.api_key or None,
        explicit_base_url=row.base_url or None,
        accepts_temperature=bool(row.accepts_temperature),
        default_model=forced_model,
        label=f"provider:{row.name}" if not forced_model else f"fallback:{row.name}",
    )


async def resolve_llm_provider(
    provider_id: "uuid.UUID | None",
    fallback_provider_id: "uuid.UUID | None" = None,
    fallback_model: "str | None" = None,
) -> LLMProvider:
    """Proveedor efectivo para un agente, según su `llm_provider_id`.

    - provider_id None o inexistente → proveedor por defecto (is_default) o, si
      no hay ninguno, OpenAI por credencial (idéntico a get_llm_provider()).
    - Respaldo (secundario del failover), por prioridad:
        1. `fallback_provider_id` (+ `fallback_model`) del AGENTE, si lo tiene.
        2. El proveedor GLOBAL marcado is_fallback (con su fallback_model).
        3. Compatibilidad: las credenciales legacy llm_fallback_* (OpenRouter
           + deepseek por defecto), como siempre.

    Lee las filas (y descifra claves) EN CADA llamada, a propósito: editar en
    el panel aplica sin reiniciar. Construir los objetos es barato.
    """
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.llm_provider import LLMProvider as _Row

    row = None
    fb_row = None
    global_fb = None
    try:
        async with db_session() as db:
            if provider_id is not None:
                row = (
                    await db.execute(select(_Row).where(_Row.id == provider_id))
                ).scalar_one_or_none()
            if row is None:
                row = (
                    await db.execute(select(_Row).where(_Row.is_default.is_(True)))
                ).scalar_one_or_none()
            if fallback_provider_id is not None:
                fb_row = (
                    await db.execute(select(_Row).where(_Row.id == fallback_provider_id))
                ).scalar_one_or_none()
            if fb_row is None:
                global_fb = (
                    await db.execute(select(_Row).where(_Row.is_fallback.is_(True)))
                ).scalar_one_or_none()
    except Exception as e:  # BD caída: no rompemos el flujo, usamos el default
        logger.warning("llm.provider.resolve_failed", error=str(e))
        return get_llm_provider()

    # Secundario del failover (por prioridad agente → global → legacy).
    if fb_row is not None:
        secondary = _primary_from_row(
            fb_row, forced_model=fallback_model or fb_row.fallback_model or None
        )
    elif global_fb is not None:
        secondary = _primary_from_row(global_fb, forced_model=global_fb.fallback_model or None)
    else:
        secondary = _get_secondary()

    if row is None:
        primary: LLMProvider = OpenAIProvider(
            api_key_credential="openai_api_key", label="openai"
        )
    else:
        primary = _primary_from_row(row)
    return FallbackLLMProvider(primary, secondary)
