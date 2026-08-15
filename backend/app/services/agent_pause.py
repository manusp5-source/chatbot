"""Pause del agente (global + por canal) + modo Entrenamiento + whitelist demo.

Estado en Redis:
- key `agent:paused` = "1" | "0"   → atajo "apaga todos los canales"
- key `agent:paused:{canal}` = "1" → pausa solo ese canal
- key `agent:training:{canal}` = "1" → modo Entrenamiento (sombra) solo ese canal
- set `agent:demo_whitelist` = { conversation_id, ... }

Modo de 3 vías por canal (Activo / Entrenamiento / Pausado):
- **Pausado**: la pausa por canal (o el global) está activa. El agente no hace
  nada (early-return antes del clasificador).
- **Entrenamiento**: el canal NO está pausado pero su key `agent:training:{canal}`
  está a "1". El procesamiento SÍ continúa (clasificador + moderación + agente),
  pero el paso de envío genera un BORRADOR/sugerencia en vez de enviar.
- **Activo**: ni pausado ni en entrenamiento. Comportamiento legacy (auto-envía).

Reglas:
- Un canal se considera pausado si la pausa global esta activa O si su key
  por canal esta a "1". El global es un atajo: poner global=1 deja todos
  los canales como pausados sin tocar sus keys individuales. Poner global=0
  no quita las pausas individuales que pudiera haber.
- Pausado tiene PRECEDENCIA sobre Entrenamiento: un canal pausado no procesa,
  aunque tenga la marca de entrenamiento puesta. `is_channel_training` por eso
  devuelve False si el canal está pausado (estado efectivo, no la marca cruda).
- La whitelist demo SIEMPRE gana sobre la pausa: si una conversacion esta en
  ella, el agente la procesa aunque el canal o el global esten pausados. (La
  whitelist no interactúa con Entrenamiento: una conv demo en un canal en
  Entrenamiento sigue generando borrador, no envía.)
"""
from __future__ import annotations

from uuid import UUID

from app.core.redis import get_redis

PAUSED_KEY = "agent:paused"
PAUSED_KEY_PREFIX = "agent:paused:"
TRAINING_KEY_PREFIX = "agent:training:"
TRANSCRIBE_KEY_PREFIX = "agent:transcribe_audio:"
WHITELIST_KEY = "agent:demo_whitelist"

# Canales que tienen card propio en el dashboard. El valor coincide con
# ConversationCanal (whatsapp, web, instagram_dm, email, retell_voice) para que
# la key Redis `agent:paused:web` matchee `conv.canal == "web"` sin mapeos.
KNOWN_CHANNELS: tuple[str, ...] = (
    "whatsapp",
    "web",
    "instagram_dm",
    "email",
    "retell_voice",
)


def _decode(v) -> str | None:
    if v is None:
        return None
    return v.decode() if isinstance(v, bytes) else v


# ---------------------- Pausa global ----------------------

async def is_agent_paused() -> bool:
    """Pausa global (atajo)."""
    v = _decode(await get_redis().get(PAUSED_KEY))
    return v == "1"


async def set_agent_paused(paused: bool) -> None:
    await get_redis().set(PAUSED_KEY, "1" if paused else "0")


# ---------------------- Pausa por canal ----------------------

def _channel_key(canal: str) -> str:
    return PAUSED_KEY_PREFIX + canal


async def is_channel_paused(canal: str) -> bool:
    """True si el canal esta pausado (por global o por su propia key)."""
    if await is_agent_paused():
        return True
    v = _decode(await get_redis().get(_channel_key(canal)))
    return v == "1"


async def set_channel_paused(canal: str, paused: bool) -> None:
    await get_redis().set(_channel_key(canal), "1" if paused else "0")


async def get_channels_pause_state() -> dict[str, bool]:
    """Devuelve {canal: paused_bool} para todos los KNOWN_CHANNELS."""
    global_paused = await is_agent_paused()
    if global_paused:
        return {c: True for c in KNOWN_CHANNELS}
    r = get_redis()
    state: dict[str, bool] = {}
    for c in KNOWN_CHANNELS:
        v = _decode(await r.get(_channel_key(c)))
        state[c] = v == "1"
    return state


# ---------------------- Modo Entrenamiento por canal ----------------------

def _training_key(canal: str) -> str:
    return TRAINING_KEY_PREFIX + canal


async def is_channel_training_flag(canal: str) -> bool:
    """Marca CRUDA de entrenamiento del canal (ignora la pausa).

    Úsalo solo para construir el estado de la UI (self_training). Para decidir
    el flujo del runtime usa `is_channel_training`, que respeta la precedencia
    de Pausado sobre Entrenamiento.
    """
    v = _decode(await get_redis().get(_training_key(canal)))
    return v == "1"


async def is_channel_training(canal: str) -> bool:
    """Estado EFECTIVO de entrenamiento: True si el canal tiene la marca de
    entrenamiento Y no está pausado (Pausado tiene precedencia).

    Si devuelve True, el runtime debe correr clasificador + moderación + agente
    y, en el paso de envío, generar un borrador/sugerencia en vez de enviar.
    """
    if await is_channel_paused(canal):
        return False
    return await is_channel_training_flag(canal)


async def set_channel_training(canal: str, training: bool) -> None:
    """Activa/desactiva el modo Entrenamiento del canal.

    Activar Entrenamiento NO toca la pausa, pero un canal en Entrenamiento no
    debe estar pausado a la vez (estados mutuamente excluyentes en la UI). El
    llamante (endpoint) es quien garantiza la exclusión: al poner Entrenamiento
    quita la pausa por canal, y al pausar quita la marca de entrenamiento.
    """
    await get_redis().set(_training_key(canal), "1" if training else "0")


async def get_channels_training_state() -> dict[str, bool]:
    """Devuelve {canal: training_efectivo_bool} para todos los KNOWN_CHANNELS.

    Efectivo = marca puesta y canal no pausado (mismo criterio que
    is_channel_training). Para la UI de 3 estados.
    """
    r = get_redis()
    paused_state = await get_channels_pause_state()
    state: dict[str, bool] = {}
    for c in KNOWN_CHANNELS:
        if paused_state.get(c):
            state[c] = False
            continue
        v = _decode(await r.get(_training_key(c)))
        state[c] = v == "1"
    return state


# ---------------------- Transcripción de notas de voz por canal ----------------------

def _transcribe_key(canal: str) -> str:
    return TRANSCRIBE_KEY_PREFIX + canal


async def is_channel_transcription_enabled(canal: str) -> bool:
    """True si hay que transcribir las notas de voz que entran por ese canal.

    ENCENDIDO por defecto: solo se apaga si la key está explícitamente a "0".
    Así un canal que nunca se ha tocado transcribe, que es lo esperable.

    Es INDEPENDIENTE de la pausa y del estado de la conversación: un canal
    pausado sigue transcribiendo, para poder leer la nota de voz en el inbox
    aunque el bot no conteste. Este flag es el único freno de la transcripción.
    """
    v = _decode(await get_redis().get(_transcribe_key(canal)))
    return v != "0"


async def set_channel_transcription(canal: str, enabled: bool) -> None:
    await get_redis().set(_transcribe_key(canal), "1" if enabled else "0")


async def get_channels_transcription_state() -> dict[str, bool]:
    """Devuelve {canal: transcribe_bool} para todos los KNOWN_CHANNELS."""
    r = get_redis()
    state: dict[str, bool] = {}
    for c in KNOWN_CHANNELS:
        v = _decode(await r.get(_transcribe_key(c)))
        state[c] = v != "0"
    return state


# ---------------------- Whitelist demo (por conversacion) ----------------------

async def is_in_demo_whitelist(conversation_id: UUID | str) -> bool:
    return bool(await get_redis().sismember(WHITELIST_KEY, str(conversation_id)))


async def add_demo_conversation(conversation_id: UUID | str) -> None:
    await get_redis().sadd(WHITELIST_KEY, str(conversation_id))


async def remove_demo_conversation(conversation_id: UUID | str) -> None:
    await get_redis().srem(WHITELIST_KEY, str(conversation_id))


async def list_demo_conversations() -> set[str]:
    members = await get_redis().smembers(WHITELIST_KEY)
    return {_decode(m) for m in members if m is not None}
