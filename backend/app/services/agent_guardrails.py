"""Guardrails contra abuso del agente desde WhatsApp.

Cualquier número puede mandarle mensajes al bot — es la mayor superficie de
ataque. Aquí centralizamos los topes que evitan:

- DoS económico: alguien manda 10.000 mensajes para hinchar la factura OpenAI.
- Context bombing: un mensaje gigantesco que pasa entero al LLM y agota tokens.
- Audio bombing: audio de horas que Whisper procesa y cobra.

Los topes son por número de teléfono. Si se excede:
- `messages_per_minute` → el mensaje SE GUARDA igual (queda en la bandeja) pero
  el agente no lo contesta. Antes se descartaba antes de tocar la BD y el
  mensaje no existía en ninguna parte.
- `llm_calls_per_hour` → no se llama al modelo.
- Solo el flood de verdad (`ABUSE_MESSAGES_PER_MINUTE`, muy por encima del tope
  normal) cuenta como infracción; si se repite, el contacto entra en
  `blocklist` durante `AUTO_BLOCK_TTL_SECS` (hard-block).

Los topes salen de variables de entorno para poder subirlos sin tocar código:
15 fotos seguidas de un producto es un caso NORMAL, no un abuso.
"""
import os
import time

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services.security_alerts import notify_security

logger = get_logger(__name__)


def _int_env(name: str, default: int) -> int:
    """Entero de entorno con respaldo. Un valor basura no puede tumbar la
    ingesta: si no se puede leer, se usa el valor por defecto."""
    try:
        raw = (os.getenv(name) or "").strip()
        return int(raw) if raw else default
    except (TypeError, ValueError):
        logger.warning("guard.bad_env_value", name=name)
        return default


# Límites por contacto (ajustables por entorno).
# MAX_MESSAGES_PER_MINUTE estaba en 10 fijo en el código: un cliente mandando
# fotos de un producto lo cruzaba sin ser un abuso. Subido a 30 y configurable.
MAX_MESSAGES_PER_MINUTE = _int_env("GUARDRAIL_MAX_MESSAGES_PER_MINUTE", 30)
# A partir de aquí ya no es una ráfaga humana: son mensajes automatizados. Solo
# esto cuenta como infracción de cara al bloqueo.
ABUSE_MESSAGES_PER_MINUTE = _int_env(
    "GUARDRAIL_ABUSE_MESSAGES_PER_MINUTE", MAX_MESSAGES_PER_MINUTE * 3
)
MAX_LLM_CALLS_PER_HOUR = _int_env("GUARDRAIL_MAX_LLM_CALLS_PER_HOUR", 60)
MAX_USER_MESSAGE_CHARS = 4000          # trunca antes de mandar al LLM
MAX_AUDIO_BYTES = 8 * 1024 * 1024      # ~8 MB ≈ varios minutos de OGG/Opus

# Si un contacto rompe el rate-limit BLOCK_TRIGGER_HITS veces en
# BLOCK_TRIGGER_WINDOW_SECS, se le bloquea durante AUTO_BLOCK_TTL_SECS.
BLOCK_TRIGGER_HITS = _int_env("GUARDRAIL_BLOCK_TRIGGER_HITS", 5)
BLOCK_TRIGGER_WINDOW_SECS = 600
# Bloqueo MANUAL desde el panel: sigue siendo de 24 h (lo decide una persona).
BLOCKLIST_TTL_SECS = 24 * 3600
# Bloqueo AUTOMÁTICO por flood: 1 hora. Antes eran 24 h y un falso positivo
# dejaba a un cliente real invisible un día entero, con desbloqueo a mano.
AUTO_BLOCK_TTL_SECS = _int_env("GUARDRAIL_AUTO_BLOCK_TTL_SECS", 3600)

# Veredictos de `evaluate_incoming`.
VERDICT_ACCEPT = "accept"      # procesar con normalidad
VERDICT_THROTTLE = "throttle"  # guardar el mensaje, pero no contestar
VERDICT_BLOCKED = "blocked"    # contacto en blocklist: no se procesa nada

_MSG_KEY = "guard:msg:{phone}"
_LLM_KEY = "guard:llm:{phone}"
_HITS_KEY = "guard:hits:{phone}"
_BLOCK_KEY = "guard:blocked:{phone}"


async def _incr_with_ttl(key: str, ttl: int) -> int:
    r = get_redis()
    async with r.pipeline(transaction=True) as p:
        p.incr(key)
        p.expire(key, ttl)
        results = await p.execute()
    return int(results[0])


async def is_phone_blocked(phone: str) -> bool:
    r = get_redis()
    return bool(await r.exists(_BLOCK_KEY.format(phone=phone)))


async def block_phone(phone: str, reason: str, ttl_secs: int | None = None) -> None:
    """Mete un teléfono en blocklist y notifica al equipo.

    `ttl_secs` permite bloqueos manuales con duración distinta a la del
    auto-bloqueo por abuso (por defecto BLOCKLIST_TTL_SECS).
    """
    r = get_redis()
    ttl = ttl_secs or BLOCKLIST_TTL_SECS
    await r.set(_BLOCK_KEY.format(phone=phone), reason, ex=ttl)
    logger.warning("guard.phone_blocked", phone="<PHONE>", reason=reason)
    await notify_security(
        kind="phone_blocked",
        title="Contacto bloqueado por abuso",
        details={"phone": phone, "motivo": reason, "duracion_h": ttl // 3600},
        throttle_key=phone,
    )


async def unblock_phone(phone: str) -> None:
    r = get_redis()
    await r.delete(_BLOCK_KEY.format(phone=phone))


async def list_blocked_phones() -> list[dict]:
    """Devuelve los teléfonos actualmente bloqueados (clave → motivo + TTL)."""
    r = get_redis()
    out: list[dict] = []
    prefix = _BLOCK_KEY.format(phone="")
    async for key in r.scan_iter(match=f"{prefix}*"):
        phone = key[len(prefix):]
        reason = await r.get(key)
        ttl = await r.ttl(key)
        out.append({"phone": phone, "reason": reason or "", "ttl_seconds": int(ttl) if ttl else 0})
    return out


async def _register_hit(phone: str, reason: str) -> None:
    """Cuenta una infracción de rate-limit; si pasa el umbral, bloquea.

    El bloqueo automático dura AUTO_BLOCK_TTL_SECS (1 h), no las 24 h del
    bloqueo manual: aquí decide una heurística, no una persona.
    """
    key = _HITS_KEY.format(phone=phone)
    hits = await _incr_with_ttl(key, BLOCK_TRIGGER_WINDOW_SECS)
    if hits >= BLOCK_TRIGGER_HITS:
        await block_phone(
            phone,
            reason=f"{hits} rate-limit hits ({reason})",
            ttl_secs=AUTO_BLOCK_TTL_SECS,
        )


async def evaluate_incoming(phone: str) -> str:
    """Qué hacemos con un mensaje entrante de este número: VERDICT_*.

    Distingue la ráfaga legítima (throttle: se guarda, no se contesta) del
    flood automatizado (que sí cuenta como infracción de cara al bloqueo).
    Antes ambos casos eran lo mismo y un cliente mandando 15 fotos acababa
    bloqueado 24 h con sus mensajes sin guardar en ninguna parte.
    """
    if await is_phone_blocked(phone):
        return VERDICT_BLOCKED
    minute_bucket = int(time.time() // 60)
    key = _MSG_KEY.format(phone=phone) + f":{minute_bucket}"
    count = await _incr_with_ttl(key, 90)
    if count <= MAX_MESSAGES_PER_MINUTE:
        return VERDICT_ACCEPT
    logger.warning("guard.msg_rate_exceeded", phone="<PHONE>", count=count)
    if count > ABUSE_MESSAGES_PER_MINUTE:
        await _register_hit(phone, "msg_rate")
    return VERDICT_THROTTLE


async def can_accept_message(phone: str) -> bool:
    """¿Aceptamos un mensaje entrante más de este número en este minuto?

    Compatibilidad: True solo cuando el mensaje se puede procesar entero. Quien
    necesite distinguir "ráfaga" de "bloqueado" usa `evaluate_incoming`.
    """
    return await evaluate_incoming(phone) == VERDICT_ACCEPT


async def can_call_llm(phone: str) -> bool:
    """¿Aceptamos una llamada más al LLM para este contacto en esta hora?"""
    if await is_phone_blocked(phone):
        return False
    hour_bucket = int(time.time() // 3600)
    key = _LLM_KEY.format(phone=phone) + f":{hour_bucket}"
    count = await _incr_with_ttl(key, 4000)
    if count > MAX_LLM_CALLS_PER_HOUR:
        logger.warning("guard.llm_rate_exceeded", phone="<PHONE>", count=count)
        await _register_hit(phone, "llm_rate")
        return False
    return True


def truncate_user_text(text: str) -> str:
    if len(text) <= MAX_USER_MESSAGE_CHARS:
        return text
    logger.info("guard.user_text_truncated", original_len=len(text))
    return text[:MAX_USER_MESSAGE_CHARS] + "\n…[mensaje recortado por longitud]"
