"""Endpoints de voz para Retell AI.

Retell Custom LLM se conecta por **WebSocket**: en cada turno del usuario nos
envía `response_required` con la transcripción y esperamos un `response`. Este
módulo expone ese WebSocket (`/retell/llm-ws/{call_id}`) y reutiliza el agente
normal (KB, captación de leads, agendado de citas).

Persiste la transcripción turno a turno en `messages` con
`conv.canal=retell_voice`, así las llamadas se revisan en el inbox como
cualquier otra conversación.

Dos URLs hay que pegar en el panel de Retell y son distintas:
  - **Custom LLM** (WebSocket): `wss://.../api/v1/voice/retell/llm-ws` — es
    quien contesta al que llama.
  - **Webhook del agente** (HTTP): `https://.../api/v1/voice/retell/webhook` —
    es quien trae duración, grabación, motivo de fin y resumen al terminar.
Sin la segunda, las llamadas funcionan pero llegan sin metadatos.

Los frenos del panel (Pausado / Entrenamiento / conversación tomada por un
operador / cuarentena / moderación) se aplican aquí igual que en los canales de
texto: la voz es el canal MÁS CARO (tokens + minutos de Retell) y era el único
que los ignoraba, así que el tope de presupuesto —que pausa globalmente— no lo
apagaba.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel
from sqlalchemy import func, select

from app.agents.orchestrator import run_agent
from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.core.ratelimit import client_ip_key
from app.db.session import db_session
from app.models.contact import Contact, ContactOrigen
from app.models.conversation import (
    Conversation,
    ConversationCanal,
    ConversationStatus,
)
from app.models.user import User
from app.schemas.common import OkResponse
from app.models.message import Message, MessageRole
from app.providers.llm.base import LLMMessage
from app.providers.voice import get_retell_provider, is_call_authentic, register_ws_attempt
from app.services.agent_pause import (
    is_channel_paused,
    is_channel_training,
    is_in_demo_whitelist,
)
from app.services.budget import is_budget_pause_active
from app.services.moderation import moderate
from app.services.runtime_config import get_channel_config, get_runtime_for_channel
from app.services.runtime_logs import push_runtime_log
from app.services.security_alerts import notify_security

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

VOICE_CANAL = "retell_voice"

# Marca que el agente añade al final de su respuesta cuando la llamada debe
# terminar (I8). Se le enseña en el prompt del canal, se le quita del texto
# antes de mandarlo a Retell y se traduce a `end_call: true`.
END_CALL_MARKER = "[FIN_LLAMADA]"

# Todos los textos fijos del canal, configurables desde `Channel.config` sin
# tocar código (antes solo el saludo lo era; "Sigo aquí, dime." estaba clavado).
VOICE_TEXTS: dict[str, str] = {
    "greeting": "Hola, ¿en qué te puedo ayudar?",
    # Silencio del que llama: NO es un turno nuevo, no se vuelve a pasar por el
    # agente (I1). Solo se le pregunta si sigue ahí.
    "reminder_message": "¿Sigues ahí?",
    # Canal pausado / sin agente / en cuarentena: no gastamos ni un token.
    "unavailable_message": (
        "Ahora mismo no puedo atenderte. Perdona las molestias, "
        "vuelve a llamar en un rato."
    ),
    # La conversación la lleva una persona (o la moderación la ha derivado).
    "handoff_message": (
        "Te va a atender una persona del equipo. Te devolvemos la llamada "
        "enseguida. Gracias por llamar."
    ),
    # Modo Entrenamiento: el agente responde en la sombra (queda como
    # sugerencia en el panel) pero no se le dice al que llama.
    "training_message": (
        "Gracias por llamar. Tomo nota y una persona del equipo te devuelve "
        "la llamada enseguida."
    ),
    "error_message": "Disculpa, he tenido un problema. ¿Puedes repetir?",
}

# Instrucción que se añade al prompt del agente SOLO en voz: es lo que le da una
# forma de colgar (I8). `derivar_humano` está deliberadamente fuera de las tools
# de voz y el panel bloquea responder a una llamada, así que sin esto el canal
# se quedaba sin salida cuando alguien pedía hablar con una persona.
VOICE_END_CALL_INSTRUCTIONS = f"""

======================================
CANAL: LLAMADA DE TELÉFONO
======================================

Estás hablando por teléfono. Puedes terminar la llamada tú.

Cuando la conversación haya acabado —te despides, ya has resuelto lo que
pedían, o la persona no quiere nada más— di tu frase de despedida y añade al
final, en una línea aparte, exactamente esto: {END_CALL_MARKER}

Esa marca es una señal para el sistema: NO la digas, NO la leas en voz alta y
NO la menciones nunca. Si la conversación sigue, no la pongas.

No puedes pasar la llamada a otra persona ni transferirla. Si te piden hablar
con alguien del equipo, no digas que se la pasas: tómale el recado (nombre,
motivo y un teléfono de contacto), confirma repitiéndoselo, di que alguien del
equipo le devolverá la llamada y termina con la marca de fin de llamada.
"""

# Números que Retell manda cuando no hay identificación de llamada.
_ANON_NUMBERS = {"anonymous", "unknown", "private", "restricted", "unavailable", "+00000000000"}


class VoiceStatusOut(BaseModel):
    """Estado del canal de voz para la página "Llamadas" (I7 / B3).

    Sirve para que la página pueda explicar POR QUÉ no ve datos en vez de
    enseñar huecos: sin canal conectado, o con el webhook de la llamada sin
    configurar en Retell (que es lo que trae duración, grabación y resumen).
    """

    connected: bool
    llm_webhook_url: str
    call_webhook_url: str
    # False cuando hay llamadas registradas pero NINGUNA tiene metadatos: señal
    # de que el webhook del agente no está puesto en Retell.
    call_webhook_configured: bool
    total_calls: int
    calls_with_metadata: int
    paused: bool
    training: bool
    # Tarifa de telefonía por minuto para estimar el coste de cada llamada.
    price_per_minute_usd: float
    # Gasto de TELEFONÍA del mes en curso (I10). El cliente no tenía ninguna
    # pantalla donde ver lo que le cuestan los minutos: la tabla de consumo del
    # panel solo sabe de tokens. Estos tres campos son esa pantalla.
    # `month_calls_without_cost` avisa de las llamadas cuyos minutos no se han
    # podido valorar (ni coste de Retell ni tarifa configurada), para que el
    # total no se lea como "esto es todo lo que se ha gastado".
    month_cost_usd: float
    month_minutes: float
    month_calls_without_cost: int


@dataclass
class TurnResult:
    """Lo que hay que contestarle a Retell en un turno."""

    text: str
    end_call: bool = False


def _retell_urls() -> tuple[str, str]:
    """(url del Custom LLM en wss://, url del webhook del agente en https://)."""
    base = settings.APP_BASE_URL.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return (
        f"{ws_base}/api/v1/voice/retell/llm-ws",
        f"{base}/api/v1/voice/retell/webhook",
    )


async def _voice_text(key: str) -> str:
    """Texto del canal de voz: el de `Channel.config` si lo hay, si no el default.

    Todos son configurables (V-03 lo era solo para el saludo). Así el único
    castellano fijo del canal deja de estar clavado en el código.
    """
    default = VOICE_TEXTS[key]
    channel_cfg = await get_channel_config(VOICE_CANAL)
    if channel_cfg and isinstance(channel_cfg.get(key), str):
        configured = channel_cfg[key].strip()
        if configured:
            return configured
    return default


async def _voice_greeting() -> str:
    """Saludo inicial del agente al descolgar (V-03)."""
    return await _voice_text("greeting")


def _latest_user_turn(transcript: list[dict]) -> tuple[str, int]:
    """(texto, índice) del último turno de usuario de la transcripción de Retell.

    Devolvemos también el índice para poder cortar el historial justo antes de
    ese turno. Antes se cortaba con `transcript[:-1]` mientras la búsqueda del
    texto iba hacia atrás: si el último elemento no era del usuario, el mismo
    turno entraba dos veces (una en el historial y otra como mensaje actual).
    """
    for idx in range(len(transcript or []) - 1, -1, -1):
        turn = transcript[idx]
        if turn.get("role") == "user" and turn.get("content"):
            return str(turn["content"]), idx
    return "", -1


def _normalize_from_number(raw: object) -> str | None:
    """Teléfono de quien llama, si Retell lo trae y es utilizable.

    Devuelve None para llamadas sin identificación (`anonymous`, oculto…), que
    es cuando toca caer a la clave sintética `voice:{call_id}`.
    """
    if not isinstance(raw, str):
        return None
    num = raw.strip()
    if not num or num.lower() in _ANON_NUMBERS:
        return None
    if not re.fullmatch(r"\+?\d{6,20}", num.replace(" ", "")):
        return None
    return num.replace(" ", "")


def _extract_from_number(payload: dict) -> str | None:
    """`from_number` de un mensaje `call_details` del WebSocket o del webhook.

    Es el dato que ya le pedimos expresamente a Retell con
    `config.call_details = true` y que se estaba tirando a la basura: sin él
    cada llamada del mismo cliente creaba una ficha nueva en Contactos con un
    "teléfono" tipo `voice:call_3a9f`.
    """
    call = payload.get("call")
    if isinstance(call, dict):
        found = _normalize_from_number(call.get("from_number"))
        if found:
            return found
    return _normalize_from_number(payload.get("from_number"))


def _strip_end_call_marker(text: str) -> TurnResult:
    """Quita la marca de fin de llamada del texto y la traduce a `end_call`."""
    if END_CALL_MARKER.lower() in (text or "").lower():
        cleaned = re.sub(re.escape(END_CALL_MARKER), "", text, flags=re.IGNORECASE)
        return TurnResult(text=cleaned.strip(), end_call=True)
    return TurnResult(text=text, end_call=False)


async def _resolve_contact_and_conversation(
    call_id: str, from_number: str | None, user_text: str, now: datetime
) -> tuple[uuid.UUID, uuid.UUID]:
    """Persiste el turno del usuario y devuelve (conversation_id, contact_id).

    - El contacto se busca por el TELÉFONO real de quien llama cuando Retell lo
      manda (`from_number`), así las llamadas del mismo número se encadenan en
      la misma ficha (y se cruzan con WhatsApp). Solo si la llamada viene sin
      identificación se usa la clave sintética `voice:{call_id}`.
    - La conversación es por LLAMADA: se busca por `session_id == call_id` y
      canal de voz. Antes se cogía cualquier conversación abierta del contacto,
      lo que con teléfono real habría metido la llamada dentro del hilo de
      WhatsApp del mismo cliente.
    """
    telefono_key = from_number or f"voice:{call_id}"
    async with db_session() as db:
        contact = (
            await db.execute(select(Contact).where(Contact.telefono == telefono_key))
        ).scalar_one_or_none()
        if not contact:
            # La voz tampoco contamina el CRM por defecto (igual que Instagram).
            contact = Contact(telefono=telefono_key, origen=ContactOrigen.manual, in_crm=False)
            db.add(contact)
            await db.flush()
        contact.ultimo_mensaje_at = now

        conv = (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.session_id == call_id,
                    Conversation.canal == ConversationCanal.retell_voice,
                )
                .order_by(Conversation.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not conv:
            conv = Conversation(
                contact_id=contact.id,
                canal=ConversationCanal.retell_voice,
                session_id=call_id,
                status=ConversationStatus.bot,
            )
            db.add(conv)
            await db.flush()
        elif conv.archived_at is not None:
            # Reconexión: Retell puede reabrir el WebSocket a mitad de llamada
            # (`auto_reconnect`), y al cerrarse el anterior archivamos. Si entra
            # otro turno es que la llamada sigue viva: vuelve a "Activas".
            conv.archived_at = None
            conv.ended_at = None
        conv.last_message_at = now

        db.add(
            Message(
                conversation_id=conv.id,
                rol=MessageRole.user,
                # La voz del cliente va al campo CIFRADO, no a `contenido`.
                # Es literalmente lo mismo que una nota de voz de WhatsApp —que
                # ya se guardaba cifrada— solo que una llamada entera. Dejar la
                # transcripción de una llamada en claro mientras un audio de
                # cinco segundos iba cifrado no tenía defensa.
                # `contenido` queda a NULL: la bandeja, el resumen rodante, la
                # ficha del contacto y los avisos ya leen
                # `contenido or audio_transcript`.
                contenido=None,
                audio_transcript=user_text,
                extra={"channel": VOICE_CANAL, "call_id": call_id},
            )
        )
        await db.commit()
        return conv.id, contact.id


async def _gate_voice_turn(conv_id, agent_cfg) -> TurnResult | None:
    """Aplica los MISMOS frenos que los canales de texto. None = seguir.

    Orden (el de `services/conversation.py`):
      1. sin agente configurado para el canal
      2. conversación en cuarentena
      3. conversación que NO está en manos del bot (un operador la ha tomado
         desde la bandeja mientras la llamada sigue viva)
      4. canal pausado — por su botón o por la PAUSA GLOBAL, que es la que
         acciona el tope mensual de gasto y el corte preventivo cuando no se
         puede medir el consumo. La whitelist demo gana sobre la pausa.
    La moderación va después, en el flujo, para no gastar su llamada cuando el
    canal ya está frenado.
    """
    if not agent_cfg:
        await push_runtime_log(
            level="error",
            event="voice.no_agent",
            message=(
                "Llamada entrante sin agente configurado para el canal de voz: se ha "
                "dicho al que llama que ahora no se le puede atender. Asigna un agente "
                "al canal Retell."
            ),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        return TurnResult(await _voice_text("unavailable_message"), end_call=True)

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        quarantined = conv.quarantined_at is not None
        conv_status = conv.status

    if quarantined:
        await push_runtime_log(
            level="warn",
            event="voice.quarantined",
            message="Llamada de una conversación en cuarentena: el agente no responde.",
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        return TurnResult(await _voice_text("unavailable_message"), end_call=True)

    if conv_status != ConversationStatus.bot:
        # Un operador ha tomado la conversación desde la bandeja. El bot deja de
        # hablar por teléfono AHORA (antes seguía contestando en paralelo).
        await push_runtime_log(
            level="info",
            event="voice.not_bot",
            message=(
                f"Llamada en estado {conv_status.value}: la lleva una persona, "
                "el agente deja de hablar."
            ),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        return TurnResult(await _voice_text("handoff_message"), end_call=True)

    # El corte por PRESUPUESTO no se lo salta ni la lista de demo. En los
    # canales de texto esto ya estaba así (`_pause_block_reason` en
    # services/conversation.py); en voz no, y voz es el canal caro: con el tope
    # ya superado, una llamada de demo seguía gastando minutos de Retell y
    # tokens en cada turno. Justo las conversaciones que más se usan.
    corte_por_gasto = await is_budget_pause_active()
    if corte_por_gasto or (
        await is_channel_paused(VOICE_CANAL) and not await is_in_demo_whitelist(conv_id)
    ):
        await push_runtime_log(
            level="warn",
            event="voice.paused",
            message=(
                "Se ha superado el tope de gasto del mes: la llamada se cierra sin "
                "gastar minutos ni tokens (ni las de demo se lo saltan)."
                if corte_por_gasto
                else "Canal de voz pausado: la llamada se cierra sin gastar tokens "
                "ni minutos."
            ),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        return TurnResult(await _voice_text("unavailable_message"), end_call=True)

    return None


async def _build_history(transcript: list[dict], upto: int, context_window: int) -> list[LLMMessage]:
    """Historial de la llamada RECORTADO a la ventana del agente (I2).

    Retell manda la transcripción entera en cada turno. Reenviarla completa hace
    que el coste de una llamada crezca al cuadrado con su duración: una llamada
    de diez minutos reenvía toda la conversación en cada intervención. Los
    canales de texto ya recortan a `agent_cfg.context_window`; ahora la voz
    también.
    """
    turns = (transcript or [])[:upto] if upto >= 0 else list(transcript or [])
    window = context_window if context_window and context_window > 0 else 20
    turns = turns[-window:]
    history: list[LLMMessage] = []
    for turn in turns:
        role = "assistant" if turn.get("role") == "agent" else "user"
        content = turn.get("content")
        if content:
            history.append(LLMMessage(role=role, content=str(content)))
    return history


async def _run_retell_turn(
    call_id: str,
    user_text: str,
    transcript: list[dict],
    *,
    from_number: str | None = None,
    upto: int = -1,
) -> TurnResult:
    """Procesa un turno de voz: persiste el mensaje, aplica los frenos del panel,
    llama al agente (KB/leads/citas), persiste la respuesta y devuelve qué decir
    y si hay que colgar.
    """
    started = time.perf_counter()
    now = datetime.now(timezone.utc)

    conv_id, _contact_id = await _resolve_contact_and_conversation(
        call_id, from_number, user_text, now
    )

    agent_cfg = await get_runtime_for_channel(VOICE_CANAL)
    gated = await _gate_voice_turn(conv_id, agent_cfg)
    if gated is not None:
        return gated

    # Moderación (igual que en texto): contenido marcado → deriva a persona y
    # el bot deja de hablar. Va después de los frenos para no gastar la llamada
    # a la API cuando el canal ya está parado.
    mod = await moderate(user_text)
    if mod.flagged:
        await push_runtime_log(
            level="warn",
            event="voice.moderation.flagged",
            message="Moderación bloqueó el turno de la llamada: derivada a persona.",
            categories=",".join(mod.categories),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        async with db_session() as db:
            conv_obj = (
                await db.execute(select(Conversation).where(Conversation.id == conv_id))
            ).scalar_one()
            if conv_obj.status == ConversationStatus.bot:
                conv_obj.status = ConversationStatus.humano
                conv_obj.derivada_a_humano_at = datetime.now(timezone.utc)
                await db.commit()
        await notify_security(
            kind="moderation_flagged",
            title="Llamada bloqueada por moderación",
            details={"call_id": call_id, "categorias": ", ".join(mod.categories)},
            throttle_key=f"voice:{call_id}",
        )
        return TurnResult(await _voice_text("handoff_message"), end_call=True)

    # Modo Entrenamiento: el agente SÍ piensa la respuesta (es lo que se está
    # entrenando) pero no se le dice a quien llama; queda como sugerencia en el
    # panel, igual que el borrador de los canales de texto.
    training = await is_channel_training(VOICE_CANAL)

    history = await _build_history(transcript, upto, agent_cfg.context_window)

    # `telefono` = clave del Contact de esta llamada. Con identificación de
    # llamada es el teléfono real; sin ella, la clave sintética.
    telefono_key = from_number or f"voice:{call_id}"
    try:
        response_text = await asyncio.wait_for(
            run_agent(
                system_prompt=agent_cfg.prompt_system + VOICE_END_CALL_INSTRUCTIONS,
                history=history,
                user_message=user_text,
                model=agent_cfg.model_name,
                max_tokens=agent_cfg.max_tokens,
                # Las tools de contacto lo toman del contexto, no de los args.
                context={
                    "conversation_id": str(conv_id),
                    "channel": VOICE_CANAL,
                    "telefono": telefono_key,
                    "agent_id": str(agent_cfg.id) if agent_cfg.id else None,
                },
                # Restringe a las tools del agente de voz (KB, leads, citas).
                tools_enabled=agent_cfg.tools_enabled,
                llm_provider_id=agent_cfg.llm_provider_id,
                fallback_provider_id=agent_cfg.fallback_provider_id,
                fallback_model=agent_cfg.fallback_model,
            ),
            timeout=settings.RETELL_TURN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("retell.llm.timeout", call_id=call_id)
        await push_runtime_log(
            level="error",
            event="voice.turn.timeout",
            message=(
                f"El agente tardó más de {settings.RETELL_TURN_TIMEOUT_SECONDS}s en "
                "contestar un turno de la llamada."
            ),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        return TurnResult(await _voice_text("error_message"))
    except Exception as e:
        logger.error("retell.llm.error", error=str(e))
        await push_runtime_log(
            level="error",
            event="voice.turn.error",
            message=f"Error del agente en un turno de la llamada: {e}",
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        return TurnResult(await _voice_text("error_message"))

    result = _strip_end_call_marker(response_text)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    async with db_session() as db:
        extra = {
            "channel": VOICE_CANAL,
            "call_id": call_id,
            "latency_ms": elapsed_ms,
        }
        if training:
            # Sugerencia, NO enviada: mismo marcaje que los borradores de texto,
            # así el historial no cuenta como dicho algo que nadie oyó.
            extra["is_draft"] = True
            extra["draft_sent"] = False
        db.add(
            Message(
                conversation_id=conv_id,
                rol=MessageRole.assistant,
                contenido=result.text,
                extra=extra,
            )
        )
        await db.commit()

    if training:
        await push_runtime_log(
            level="info",
            event="voice.training",
            message=(
                "Canal de voz en Entrenamiento: la respuesta del agente queda como "
                "sugerencia y NO se le dice a quien llama."
            ),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
        return TurnResult(await _voice_text("training_message"), end_call=True)

    await push_runtime_log(
        level="info",
        event="voice.turn",
        message=f"Turno Retell respondido en {elapsed_ms}ms",
        conversation_id=str(conv_id),
        channel=VOICE_CANAL,
    )
    return result


async def _close_call_conversation(call_id: str) -> None:
    """Cierra y ARCHIVA la conversación al cerrarse el WebSocket de la llamada.

    Retell cierra este WebSocket cuando la llamada termina, así que el cierre es
    la señal más fiable que tenemos. Antes el archivado dependía EN EXCLUSIVA
    del webhook del agente: si esa URL no estaba puesta en Retell (que es lo
    normal, porque hasta ahora ni se enseñaba), cada llamada dejaba una
    conversación viva para siempre en la bandeja "Activas". Idempotente: no
    pisa un `archived_at` ni un `ended_at` ya escritos.

    LA LLAMADA DERIVADA NO SE ARCHIVA. Si durante la llamada la moderación cortó
    o el agente pidió pasar con una persona, la conversación queda en `humano`:
    alguien tiene que devolver esa llamada. Archivarla la sacaba de «Para hacer»
    y de todos los contadores —que exigen `archived_at IS NULL`—, así que al
    cliente se le decía que le atenderían y no le atendía nadie: el hilo solo
    aparecía si a alguien se le ocurría mirar en «Archivadas».
    """
    try:
        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.session_id == call_id,
                        Conversation.canal == ConversationCanal.retell_voice,
                    )
                )
            ).scalar_one_or_none()
            if not conv:
                return
            now = datetime.now(timezone.utc)
            conv.ended_at = conv.ended_at or now
            if conv.status == ConversationStatus.humano:
                logger.info("retell.ws.close_sin_archivar", call_id=call_id)
                await push_runtime_log(
                    level="warn",
                    event="voice.derivada_pendiente",
                    message=(
                        "Una llamada ha terminado con la conversación derivada a una "
                        "persona: queda en «Para hacer» para que alguien la devuelva."
                    ),
                    conversation_id=str(conv.id),
                )
            else:
                conv.archived_at = conv.archived_at or now
            await db.commit()
    except Exception as e:  # noqa: BLE001 - best-effort, nunca rompe el cierre
        logger.warning("retell.ws.close_conversation_error", call_id=call_id, error=str(e))


# Llamadas de voz vivas en ESTE proceso. Cada una mantiene un WebSocket abierto
# y dispara el agente en cada turno: sin tope, un pico (o un abuso) agota el
# event loop y el pool de conexiones a BD (I9).
_active_calls = 0


@router.websocket("/retell/llm-ws/{call_id}")
async def retell_llm_ws(websocket: WebSocket, call_id: str) -> None:
    """Custom LLM de Retell por WebSocket.

    Retell se conecta a la URL base (`wss://.../voice/retell/llm-ws`) y añade
    `/{call_id}`. En cada turno envía `response_required`/`reminder_required`
    con la transcripción y respondemos con `response`.

    Seguridad: el protocolo WS de Retell no firma los mensajes. Para que no
    pueda abrirlo cualquiera (y disparar el agente + coste de LLM + tools),
    validamos el `call_id` contra la API de Retell ANTES de aceptar: solo se
    atienden llamadas reales de nuestra cuenta. Antes de eso, un tope de
    intentos por IP evita que la validación misma sirva para quemar la cuota de
    API del cliente.

    El bucle NO se bloquea esperando al modelo (I9): los `ping_pong` de Retell
    se contestan al instante y el turno se procesa en una tarea aparte. Si el
    pong sale tarde, Retell da la llamada por perdida y cuelga con "conexión
    perdida".
    """
    global _active_calls

    ip = client_ip_key(websocket)
    if not await register_ws_attempt(ip):
        await websocket.close(code=1008)
        return

    if settings.RETELL_WS_VALIDATE and not await is_call_authentic(call_id):
        # Rechazo durante el handshake (sin accept): conexión no autenticada.
        await websocket.close(code=1008)
        logger.warning("retell.ws.rejected", call_id=call_id)
        await push_runtime_log(
            level="warn",
            event="voice.ws.rejected",
            message="Conexión al WebSocket de voz rechazada: no se validó como llamada real.",
            channel=VOICE_CANAL,
            call_id=call_id,
        )
        return

    if _active_calls >= settings.RETELL_MAX_CONCURRENT_CALLS:
        await websocket.close(code=1013)  # try again later
        await push_runtime_log(
            level="error",
            event="voice.ws.capacity",
            message=(
                f"Llamada rechazada: ya hay {_active_calls} llamadas de voz simultáneas "
                f"(tope {settings.RETELL_MAX_CONCURRENT_CALLS})."
            ),
            channel=VOICE_CANAL,
            call_id=call_id,
        )
        return

    await websocket.accept()
    _active_calls += 1
    logger.info("retell.ws.connected", call_id=call_id)

    send_lock = asyncio.Lock()
    from_number: str | None = None
    pending: asyncio.Task | None = None

    async def _send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def _handle_turn(response_id, transcript: list[dict], text: str, upto: int) -> None:
        """Procesa un turno FUERA del bucle de recepción, para que los pings de
        Retell se sigan contestando mientras el modelo piensa."""
        try:
            result = await _run_retell_turn(
                call_id, text, transcript, from_number=from_number, upto=upto
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("retell.ws.turn_error", error=str(e), call_id=call_id)
            await push_runtime_log(
                level="error",
                event="voice.turn.crash",
                message=f"Turno de llamada abortado por un error inesperado: {e}",
                channel=VOICE_CANAL,
                call_id=call_id,
            )
            result = TurnResult(await _voice_text("error_message"))
        try:
            await _send(
                {
                    "response_type": "response",
                    "response_id": response_id,
                    "content": result.text,
                    "content_complete": True,
                    "end_call": result.end_call,
                }
            )
        except Exception:
            pass  # el socket ya no está: el bucle principal se entera y cierra

    try:
        # 1) Config: reconexión automática + que Retell mande call_details (que
        #    es donde viene el teléfono de quien llama).
        await _send(
            {"response_type": "config", "config": {"auto_reconnect": True, "call_details": True}}
        )
        # 2) Saludo inicial (begin message), sin pasar por el LLM.
        await _send(
            {
                "response_type": "response",
                "response_id": 0,
                "content": await _voice_greeting(),
                "content_complete": True,
                "end_call": False,
            }
        )
    except Exception as e:
        logger.error("retell.ws.init_error", error=str(e), call_id=call_id)
        await push_runtime_log(
            level="error",
            event="voice.ws.init_error",
            message=f"No se pudo iniciar la llamada (saludo/config): {e}",
            channel=VOICE_CANAL,
            call_id=call_id,
        )
        _active_calls -= 1
        return

    try:
        while True:
            data = await websocket.receive_json()
            itype = str(data.get("interaction_type") or "")
            if itype == "ping_pong":
                # Se contesta SIEMPRE al instante, aunque haya un turno en curso.
                await _send(
                    {"response_type": "ping_pong", "timestamp": data.get("timestamp")}
                )
                continue
            if itype == "call_details":
                found = _extract_from_number(data)
                if found:
                    from_number = found
                continue
            if itype == "reminder_required":
                # I1: un silencio NO es un turno nuevo. Antes se re-procesaba el
                # último turno del usuario —que ya se había contestado—, con lo
                # que se repetía la respuesta palabra por palabra, se duplicaba
                # la transcripción y se pagaba un turno entero por cada silencio.
                await _send(
                    {
                        "response_type": "response",
                        "response_id": data.get("response_id"),
                        "content": await _voice_text("reminder_message"),
                        "content_complete": True,
                        "end_call": False,
                    }
                )
                continue
            if itype != "response_required":
                # update_only y demás: no requieren respuesta.
                continue

            transcript = data.get("transcript") or []
            user_text, upto = _latest_user_turn(transcript)
            if not user_text:
                await _send(
                    {
                        "response_type": "response",
                        "response_id": data.get("response_id"),
                        "content": await _voice_text("reminder_message"),
                        "content_complete": True,
                        "end_call": False,
                    }
                )
                continue

            # Un `response_required` nuevo deja obsoleto al anterior: Retell ya
            # no espera aquella respuesta.
            if pending is not None and not pending.done():
                pending.cancel()
            pending = asyncio.create_task(
                _handle_turn(data.get("response_id"), transcript, user_text, upto)
            )
    except WebSocketDisconnect:
        logger.info("retell.ws.disconnect", call_id=call_id)
    except Exception as e:
        logger.error("retell.ws.error", error=str(e), call_id=call_id)
        await push_runtime_log(
            level="error",
            event="voice.ws.error",
            message=f"La conexión de la llamada se cortó por un error: {e}",
            channel=VOICE_CANAL,
            call_id=call_id,
        )
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        _active_calls -= 1
        if pending is not None and not pending.done():
            # Margen corto para que el turno en vuelo termine de PERSISTIRSE (su
            # texto ya no se oirá, pero la transcripción de la llamada no debe
            # perder el último intercambio). Pasado el margen, se corta.
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=2)
            except Exception:
                pending.cancel()
        # Retell cierra este socket cuando la llamada acaba: aprovechamos para
        # cerrar la conversación aunque el webhook del agente no esté puesto.
        await _close_call_conversation(call_id)


@router.get("/status", response_model=VoiceStatusOut)
async def voice_status(user: User = Depends(get_current_user)) -> VoiceStatusOut:
    """Estado del canal de voz para la página "Llamadas".

    Permite distinguir "no hay llamadas todavía" de "el canal no está conectado"
    y de "falta el webhook del agente en Retell" (que es lo que deja duración,
    grabación, resumen y motivo de fin vacíos para siempre).
    """
    from app.providers.voice.retell import get_retell_credentials
    from app.services.budget import voice_month_telephony_usage

    creds = await get_retell_credentials()
    llm_url, webhook_url = _retell_urls()
    month = await voice_month_telephony_usage()

    async with db_session() as db:
        total = (
            await db.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.canal == ConversationCanal.retell_voice)
            )
        ).scalar_one()
        with_meta = (
            await db.execute(
                select(func.count())
                .select_from(Conversation)
                .where(
                    Conversation.canal == ConversationCanal.retell_voice,
                    (Conversation.call_duration_seconds.isnot(None))
                    | (Conversation.call_ended_reason.isnot(None))
                    | (Conversation.call_recording_url.isnot(None)),
                )
            )
        ).scalar_one()

    return VoiceStatusOut(
        connected=creds is not None,
        llm_webhook_url=llm_url,
        call_webhook_url=webhook_url,
        # Con 0 llamadas no podemos afirmar nada: no marcamos fallo.
        call_webhook_configured=(total == 0 or with_meta > 0),
        total_calls=int(total),
        calls_with_metadata=int(with_meta),
        paused=await is_channel_paused(VOICE_CANAL),
        training=await is_channel_training(VOICE_CANAL),
        price_per_minute_usd=settings.RETELL_PRICE_PER_MINUTE_USD,
        month_cost_usd=month["cost_usd"],
        month_minutes=month["minutes"],
        month_calls_without_cost=month["calls_without_cost"],
    )


def _call_duration_seconds(call: dict) -> int | None:
    """Duración de la llamada en segundos a partir del objeto `call` de Retell.

    Prioriza `duration_ms`; si no viene, la calcula con
    `end_timestamp - start_timestamp` (ambos en milisegundos). Devuelve None si
    no hay datos suficientes o son inconsistentes.
    """
    duration_ms = call.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        return int(duration_ms // 1000)
    start = call.get("start_timestamp")
    end = call.get("end_timestamp")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
        return int((end - start) // 1000)
    return None


def _call_cost_usd(call: dict) -> float | None:
    """Coste de telefonía REAL que Retell adjunta a la llamada, en dólares.

    Retell lo manda en `call_cost.combined_cost`, en CENTAVOS
    (https://docs.retellai.com/api-references/get-call). Es el dato facturado,
    así que tiene preferencia sobre cualquier estimación nuestra.
    """
    cost = call.get("call_cost")
    if isinstance(cost, dict):
        combined = cost.get("combined_cost")
        if isinstance(combined, (int, float)) and combined >= 0:
            return round(float(combined) / 100.0, 4)
    return None


def _telephony_cost(call: dict, duration_seconds: int | None) -> tuple[float | None, str | None]:
    """(coste en dólares, origen) de los MINUTOS de una llamada.

    Orden de preferencia:
      1. `call_cost.combined_cost` de Retell → origen "retell". Es lo que
         factura de verdad, con su tarifa y sus productos (telefonía + voz +
         lo que tenga contratado el cliente).
      2. duración × `RETELL_PRICE_PER_MINUTE_USD` → origen "estimado". Solo si
         Retell no lo mandó (webhook antiguo, plan sin desglose de coste) y hay
         tarifa configurada en Ajustes.
      3. Nada que registrar → (None, None). Un cero inventado no es mejor que un
         hueco: haría creer al tope de gasto que la voz sale gratis.

    Se guarda el ORIGEN porque un 0.0 real de Retell y un 0.0 "no tengo tarifa
    configurada" son la misma cifra con significados opuestos.
    """
    real = _call_cost_usd(call)
    if real is not None:
        return real, "retell"
    price = float(settings.RETELL_PRICE_PER_MINUTE_USD or 0)
    if price > 0 and duration_seconds is not None and duration_seconds >= 0:
        return round(price * duration_seconds / 60.0, 4), "estimado"
    return None, None


def _ts_to_datetime(value: object) -> datetime | None:
    """Convierte un timestamp de Retell (epoch en milisegundos) a datetime UTC."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


async def _relink_contact_by_phone(db, conv: Conversation, from_number: str) -> None:
    """Reasigna la conversación al contacto del teléfono REAL de la llamada.

    Solo actúa si la conversación cuelga todavía de una ficha sintética
    (`voice:call_...`), que es lo que pasa cuando el WebSocket no llegó a
    recibir el `call_details`. Si ya existe un contacto con ese teléfono, se
    reengancha ahí (y se borra la ficha sintética si se queda huérfana); si no,
    se le pone el teléfono bueno a la que hay.
    """
    contact = (
        await db.execute(select(Contact).where(Contact.id == conv.contact_id))
    ).scalar_one_or_none()
    if not contact or not (contact.telefono or "").startswith("voice:"):
        return
    existing = (
        await db.execute(select(Contact).where(Contact.telefono == from_number))
    ).scalar_one_or_none()
    if existing is None:
        contact.telefono = from_number
        return
    if existing.id == contact.id:
        return
    conv.contact_id = existing.id
    await db.flush()
    others = (
        await db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.contact_id == contact.id)
        )
    ).scalar_one()
    if int(others) == 0:
        await db.delete(contact)


@router.post("/retell/webhook", response_model=OkResponse)
async def retell_call_webhook(request: Request) -> OkResponse:
    """Webhook de **ciclo de vida de la llamada** de Retell (call_started /
    call_ended / call_analyzed).

    Distinto del Custom LLM (que va por WebSocket): aquí Retell nos avisa de los
    hitos de la llamada y, al terminar, nos manda duración, motivo de fin,
    grabación, transcripción y (en call_analyzed) un resumen. Lo guardamos en la
    Conversation de esa llamada para la sección "Llamadas" (solo lectura).

    Seguridad: verifica la firma de Retell sobre el body raw (esquema
    `v=..,d=..` con la API key del canal). Si no hay credenciales o la firma no
    valida → 401 y se registra en el runtime log (mismo patrón que el endpoint
    legacy). El relleno es best-effort e idempotente; si la conversación aún no
    existe (el webhook puede llegar antes del primer turno) devolvemos 200 sin
    hacer nada.
    """
    body_bytes = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    provider = get_retell_provider()
    if not await provider.verify_call_webhook_signature(headers, body_bytes):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma inválida")

    import json

    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no JSON")

    event = str(payload.get("event") or "")
    call = payload.get("call") or {}
    if not isinstance(call, dict):
        call = {}
    call_id = str(call.get("call_id") or "")

    # Solo nos interesan los eventos de cierre/análisis (los que traen métricas).
    # call_started no aporta metadatos finales; lo aceptamos con 200 sin tocar BD.
    if event not in ("call_ended", "call_analyzed"):
        return OkResponse()
    if not call_id:
        return OkResponse()

    async with db_session() as db:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.session_id == call_id,
                    Conversation.canal == ConversationCanal.retell_voice,
                )
            )
        ).scalar_one_or_none()
        if not conv:
            # El webhook puede llegar antes que el primer turno del LLM (que es
            # quien crea la Conversation). No es un error: 200 y a otra cosa.
            await push_runtime_log(
                level="info",
                event="voice.webhook.no_conversation",
                message=f"Webhook Retell {event} sin conversación todavía (call_id={call_id})",
                channel=VOICE_CANAL,
            )
            return OkResponse()

        # Relleno best-effort e idempotente: solo escribimos lo que venga y no
        # pisamos un resumen ya existente con uno vacío.
        duration = _call_duration_seconds(call)
        if duration is not None:
            conv.call_duration_seconds = duration

        # I10 — el coste de los MINUTOS se GUARDA, no solo se loguea. Sin esto
        # el tope mensual de gasto medía tokens del modelo y se dejaba fuera la
        # mitad cara del canal más caro. `services/budget.py` lo suma.
        # `call_analyzed` llega después de `call_ended` y puede traer el coste
        # ya cerrado: dejamos que el dato REAL pise a la estimación, pero no al
        # revés (una estimación no debe borrar lo que Retell facturó).
        telephony_cost, cost_source = _telephony_cost(
            call, duration if duration is not None else conv.call_duration_seconds
        )
        if telephony_cost is not None and not (
            cost_source == "estimado" and conv.call_cost_source == "retell"
        ):
            conv.call_cost_usd = telephony_cost
            conv.call_cost_source = cost_source

        recording_url = call.get("recording_url")
        if isinstance(recording_url, str) and recording_url:
            conv.call_recording_url = recording_url

        ended_reason = call.get("disconnection_reason")
        if isinstance(ended_reason, str) and ended_reason:
            conv.call_ended_reason = ended_reason[:80]

        ended_at = _ts_to_datetime(call.get("end_timestamp"))
        if ended_at is not None:
            conv.ended_at = ended_at

        analysis = call.get("call_analysis") or {}
        if isinstance(analysis, dict):
            summary = analysis.get("call_summary")
            if isinstance(summary, str) and summary.strip():
                conv.resumen = summary.strip()

        # I3: el webhook también trae el teléfono de quien llamó. Si la ficha
        # quedó sintética (el WS no llegó a ver el call_details), se reengancha
        # al contacto de verdad.
        from_number = _extract_from_number(call)
        if from_number:
            await _relink_contact_by_phone(db, conv, from_number)

        # I7 — una llamada terminada está terminada: la ARCHIVAMOS (sale de la
        # bandeja "Activas" pero sigue visible en "Archivadas" y en la sección
        # Llamadas, que lista con archived=all). NO usamos status 'cerrada'
        # (estado retirado en 0021: una cerrada sería invisible). Idempotente:
        # call_analyzed llega después de call_ended y no re-archiva.
        conv.archived_at = conv.archived_at or datetime.now(timezone.utc)

        await db.commit()
        conv_id = conv.id

    if telephony_cost is not None:
        await push_runtime_log(
            level="info",
            event="voice.cost",
            message=(
                f"Coste de telefonía de la llamada: {telephony_cost:.4f} $ "
                f"({duration or 0}s, "
                + ("facturado por Retell" if cost_source == "retell" else "estimado con la tarifa por minuto de Ajustes")
                + "). Cuenta para el tope mensual de gasto."
            ),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )
    elif duration:
        # Ni Retell manda el coste ni hay tarifa configurada: los minutos de esta
        # llamada NO cuentan para el tope. Que se sepa, en vez de que el gasto
        # del canal más caro se quede en cero sin que nadie se entere.
        await push_runtime_log(
            level="warn",
            event="voice.cost.unknown",
            message=(
                f"Llamada de {duration}s sin coste de telefonía: Retell no lo mandó y "
                "no hay tarifa por minuto configurada (RETELL_PRICE_PER_MINUTE_USD). "
                "Estos minutos NO cuentan para el tope mensual de gasto."
            ),
            conversation_id=str(conv_id),
            channel=VOICE_CANAL,
        )

    await push_runtime_log(
        level="info",
        event="voice.webhook",
        message=f"Webhook Retell {event} procesado (call_id={call_id})",
        conversation_id=str(conv_id),
        channel=VOICE_CANAL,
    )
    return OkResponse()
