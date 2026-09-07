"""Orchestrator del agente con tool use.

Bucle: LLM → tool_calls → ejecutar tools → LLM (con results) → respuesta final.
Máximo N iteraciones para evitar loops infinitos.
"""
import json
import time
import uuid
from typing import Any

from app.agents.guardarrail_clinico import Veredicto
from app.agents.guardarrail_clinico import evaluar_conversacion as evaluar_guardarrail
from app.agents.guardarrail_clinico import mensaje_para
from app.agents.tools import ALL_TOOLS
from app.core.logging import get_logger
from app.core.trace_context import (
    reset_agent_id,
    reset_conversation_id,
    set_agent_id,
    set_conversation_id,
)
from app.providers.llm import get_llm_provider, resolve_llm_provider
from app.providers.llm.base import LLMMessage, LLMToolCall
from app.services.flow_events import publish_agent_step
from app.services.trace_logger import (
    TraceLevel,
    log_router_decision,
    log_tool_invocation,
)

logger = get_logger(__name__)

MAX_ITERATIONS = 5


async def _linea_de_hoy() -> str:
    """Una línea con la fecha y la hora del negocio, para anteponer al prompt.

    Por qué existe: el agente no tenía NINGUNA forma de saber qué día era. Con un
    horario en la base de conocimiento y sin fecha, a "¿estáis abiertos?" solo
    podía contestar adivinando — y adivinaba que sí, incluso con el documento de
    vacaciones delante. Un agente de recepción que no sabe qué día es no puede
    hacer su trabajo.

    La zona horaria es la del negocio (`calendar.timezone`), no la del servidor:
    un contenedor en UTC hacía que "esta tarde" significara otra cosa.

    Ante cualquier fallo devuelve cadena vacía y el prompt se queda como estaba.
    Saber la fecha es una mejora, no un requisito para contestar.
    """
    try:
        from app.agents.tools.schedule_config import get_schedule_config, now_in

        cfg = await get_schedule_config()
        ahora = now_in(cfg.tz)
        dias = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
        meses = (
            "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        )
        return (
            f"Hoy es {dias[ahora.weekday()]}, {ahora.day} de {meses[ahora.month - 1]} "
            f"de {ahora.year}, y son las {ahora:%H:%M} ({cfg.tz_name}). Usa esta fecha "
            "para saber qué día es hoy: no la adivines ni se la preguntes a nadie.\n\n"
        )
    except Exception as e:  # noqa: BLE001 — sin fecha se contesta igual
        logger.warning("agente.fecha_no_disponible", error=str(e))
        return ""


async def run_agent(
    system_prompt: str,
    history: list[LLMMessage],
    user_message: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    context: dict[str, Any] | None = None,
    tools_enabled: list[str] | None = None,
    llm_provider_id: "uuid.UUID | None" = None,
    fallback_provider_id: "uuid.UUID | None" = None,
    fallback_model: "str | None" = None,
) -> str:
    """Devuelve la respuesta final del agente (texto).

    `tools_enabled`: lista blanca de nombres de tool que el agente puede usar.
    None = SIN CONFIGURAR → todas las tools registradas (legacy). Lista vacía =
    NINGUNA (un agente que solo conversa). Son dos cosas distintas: ver
    `_resolve_allowed_tools`.

    Si se pasa una lista, SOLO se ofrecen al LLM esas tools y `_run_tool` rechaza
    cualquier llamada a una tool fuera de la lista (defensa en profundidad:
    aunque el modelo alucine un nombre, no se ejecuta). Esto es lo que hace que
    el agente de voz no pueda derivar a humano: `derivar_humano` no entra en su
    lista.
    """
    llm = await resolve_llm_provider(llm_provider_id, fallback_provider_id, fallback_model)
    allowed = _resolve_allowed_tools(tools_enabled)
    tool_schemas = [t.schema for name, t in ALL_TOOLS.items() if name in allowed]

    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=await _linea_de_hoy() + system_prompt)
    ]
    messages.extend(history)
    messages.append(LLMMessage(role="user", content=user_message))

    ctx = context or {}
    # Setea el conversation_id para que los módulos profundos (LLM client,
    # tools, KB) puedan asociar sus eventos de trace sin recibir el id por
    # parámetro. Se resetea al salir.
    conv_id = ctx.get("conversation_id")
    token = set_conversation_id(conv_id) if conv_id else None
    # Atribución del consumo: el id del Agente que atiende (si lo hay) para que
    # el openai_client lo registre en cada llamada. Se resetea al salir.
    agent_id = ctx.get("agent_id")
    agent_token = set_agent_id(agent_id) if agent_id else None

    try:
        # ── Guardarrail clinico ────────────────────────────────────────────
        # Antes del modelo, no despues. Si el mensaje trae contenido clinico,
        # el LLM no llega a verlo: no hay forma de que opine sobre el sintoma
        # de un paciente si nunca recibe el mensaje.
        #
        # Va aqui dentro del try y no antes porque el trace_context ya esta
        # puesto: la derivacion queda asociada a su conversacion en el
        # registro, que es lo que convierte esto en prueba de cumplimiento y
        # no en una buena intencion.
        # Se le pasa el historial, no solo el mensaje: el filtro corre sobre una
        # cadena pero el modelo recibe la conversacion entera, y un paciente que
        # continua el tema con un pronombre se saltaba el guardarrail sin
        # proponerselo. Ver `evaluar_conversacion` y los casos M* del arnes.
        veredicto = evaluar_guardarrail(history, user_message)
        if veredicto is not None:
            return await _derivar_por_guardarrail(veredicto, ctx, allowed)

        for iteration in range(MAX_ITERATIONS):
            completion = await llm.complete(
                messages=messages,
                tools=tool_schemas,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            if completion.tool_calls:
                messages.append(
                    LLMMessage(role="assistant", content=completion.content, tool_calls=completion.tool_calls)
                )
                for tc in completion.tool_calls:
                    tool_result = await _run_tool(tc, ctx, allowed)
                    messages.append(
                        LLMMessage(role="tool", tool_call_id=tc.id, content=tool_result, name=tc.name)
                    )
                continue

            return (completion.content or "").strip()

        logger.warning("agent.max_iterations_reached")
        await log_router_decision(
            decision="max_iterations_reached",
            reason=f"Agente excedió {MAX_ITERATIONS} iteraciones sin respuesta final",
            level=TraceLevel.warn,
        )
        return "Necesito un momento para procesar esto. Si es urgente, dímelo y aviso al equipo."
    finally:
        if token is not None:
            reset_conversation_id(token)
        if agent_token is not None:
            reset_agent_id(agent_token)


def _resolve_allowed_tools(tools_enabled: list[str] | None) -> set[str]:
    """Lista blanca efectiva de nombres de tool para esta ejecución.

    Distingue DOS casos que antes se confundían:

      - `None` = SIN CONFIGURAR (agente legacy, campo nunca tocado) → todas las
        tools registradas.
      - `[]` = NINGUNA, elegido a propósito → conjunto vacío.

    Antes el check era `if not tools_enabled`, y una lista vacía es falsy: quien
    desmarcaba todas las casillas en el panel para tener un agente que solo
    conversara acababa con uno que agendaba citas en Google y derivaba
    conversaciones. Exactamente lo contrario de lo que pidió.

    Una lista se intersecta con las tools que existen de verdad (un nombre que
    ya no exista simplemente se ignora, no rompe).
    """
    if tools_enabled is None:
        return set(ALL_TOOLS.keys())
    return {name for name in tools_enabled if name in ALL_TOOLS}


async def _derivar_por_guardarrail(
    veredicto: Veredicto, ctx: dict[str, Any], allowed: set[str]
) -> str:
    """Deriva a humano sin pasar por el modelo, y deja rastro.

    Devuelve "" cuando la derivacion se ha hecho de verdad: la propia tool
    manda el mensaje puente al paciente, y `process_message` ya trata el vacio
    como "ya esta contestado, no envies nada mas". Devolver texto aqui le
    llegaria al paciente por duplicado.

    Cuando el agente NO tiene `derivar_humano` (el de voz, por diseño) no hay
    nadie a quien pasar la conversacion, asi que se devuelve el mensaje para
    que el paciente al menos oiga que tiene que hablar con una persona. Callar
    ahi seria peor que cualquiera de las dos alternativas.
    """
    mensaje = mensaje_para(veredicto)

    logger.info(
        "guardarrail.clinico",
        nivel=veredicto.nivel.value,
        termino=veredicto.termino,
        puede_derivar="derivar_humano" in allowed,
    )
    await log_router_decision(
        # Una derivacion por lo que dice ESTE mensaje y otra por lo que venia
        # diciendose son dos decisiones distintas. Si comparten etiqueta, nadie
        # puede medir cuantas veces actua la cobertura multi-turno.
        decision=(
            "guardarrail_clinico_continuacion" if veredicto.continuacion else "guardarrail_clinico"
        ),
        reason=veredicto.motivo,
        level=TraceLevel.warn,
    )
    await publish_agent_step(
        "guardarrail",
        conversation_id=ctx.get("conversation_id"),
        agent_id=ctx.get("agent_id"),
    )

    if "derivar_humano" not in allowed:
        return mensaje

    tool = ALL_TOOLS.get("derivar_humano")
    if tool is None:  # pragma: no cover — la tool se registra al importar
        logger.error("guardarrail.sin_tool_derivar")
        return mensaje

    try:
        await tool.handler({"motivo": veredicto.motivo, "mensaje_al_cliente": mensaje}, ctx)
    except Exception as e:
        # Si la derivacion falla (Redis caido, base inaccesible), el paciente
        # NO se queda sin respuesta y el modelo sigue sin ver el mensaje. Es el
        # peor caso aceptable: contestamos nosotros con el texto fijo.
        logger.error("guardarrail.derivacion_fallida", error=str(e))
        return mensaje

    return ""


async def _run_tool(call: LLMToolCall, ctx: dict[str, Any], allowed: set[str]) -> str:
    # Defensa en profundidad: aunque el schema solo ofreció las tools
    # permitidas, si el modelo alucina/invoca una fuera de la lista blanca, no
    # la ejecutamos. Crítico para garantizar que el agente de voz no derive a
    # humano por mucho que el LLM intente llamar a `derivar_humano`.
    if call.name not in allowed:
        await log_tool_invocation(
            tool_name=call.name,
            args=call.arguments,
            error=f"Tool no permitida para este agente: {call.name}",
        )
        return json.dumps({"error": f"Tool no disponible: {call.name}"})

    tool = ALL_TOOLS.get(call.name)
    if not tool:
        await log_tool_invocation(
            tool_name=call.name,
            args=call.arguments,
            error=f"Tool desconocida: {call.name}",
        )
        return json.dumps({"error": f"Tool desconocida: {call.name}"})

    started = time.perf_counter()
    # Flujo en vivo: ilumina el nodo de la tool que se está ejecutando.
    await publish_agent_step(
        "tool",
        conversation_id=ctx.get("conversation_id"),
        agent_id=ctx.get("agent_id"),
        tool=call.name,
    )
    try:
        result = await tool.handler(call.arguments, ctx)
        result_str = result if isinstance(result, str) else json.dumps(result)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        await log_tool_invocation(
            tool_name=call.name,
            args=call.arguments,
            result_preview=result_str,
            latency_ms=elapsed_ms,
        )
        return result_str
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.error("tool.error", tool=call.name, error=str(e))
        await log_tool_invocation(
            tool_name=call.name,
            args=call.arguments,
            error=str(e),
            latency_ms=elapsed_ms,
        )
        return json.dumps({"error": str(e)})
