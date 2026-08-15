"""Moderación de contenido del usuario antes de pasarlo al LLM.

Usa la API de moderación de OpenAI (gratuita). Si el contenido se marca como
inseguro (acoso, contenido sexual, autolesiones, violencia explícita, etc.),
NO lo mandamos al LLM, derivamos la conversación a humano y notificamos al
canal de seguridad.

Fail-open: si la moderación API falla (red, no key), seguimos adelante. La
moderación es una capa extra, no la única defensa.

PERO NO MUDA. Antes, sin clave, devolvía "no marcado" sin ni un log: la
moderación llevaba apagada desde el minuto uno de la instalación y no había
absolutamente nada — ni en Monitorización ni en el panel — que lo dijera. Ahora:

  - la falta de clave y los errores se empujan a "Logs en vivo" (igual que hace
    el cliente de transcripción, que sí lo tenía bien),
  - `ModerationResult` lleva `available`, para distinguir "he mirado y está
    limpio" de "no he podido mirar",
  - `moderation_status()` expone el estado para pintarlo en el panel.
"""
from openai import AsyncOpenAI

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.providers.openai_factory import MODERATION_TIMEOUT_SECONDS, get_async_openai
from app.services.credentials import get_credential
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)

# El aviso de "la moderación no está operativa" va delante de CADA mensaje: sin
# freno llenaría los logs. Se emite una vez por ventana.
_ALERT_KEY = "moderation:alerted:{reason}"
_ALERT_TTL = 3600  # 1 hora


async def _alert_once(reason: str, message: str, **extra) -> None:
    """Empuja el aviso a los logs en vivo como mucho una vez por hora."""
    try:
        was_set = await get_redis().set(
            _ALERT_KEY.format(reason=reason), "1", ex=_ALERT_TTL, nx=True
        )
    except Exception:  # noqa: BLE001 — sin Redis avisamos igual (mejor ruido que silencio)
        was_set = True
    if not was_set:
        return
    try:
        await push_runtime_log(
            level="error",
            event=f"moderation.{reason}",
            message=message,
            **extra,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("moderation.alert_failed", error=str(e))


async def _get_client() -> AsyncOpenAI | None:
    api_key = await get_credential("openai_api_key")
    if not api_key:
        return None
    # Va delante de CADA mensaje del usuario: si tarda, frena la conversación
    # entera. Timeout corto y, al ser fail-open, un corte no bloquea nada.
    return get_async_openai(api_key=api_key, timeout=MODERATION_TIMEOUT_SECONDS)


async def moderation_status() -> dict:
    """Estado de la moderación para el panel / Monitorización.

    `operativa=False` significa que TODO mensaje entrante está pasando sin
    revisar. Es información que el administrador tiene que poder ver.
    """
    configured = bool(await get_credential("openai_api_key"))
    return {
        "operativa": configured,
        "modelo": "omni-moderation-latest",
        "detalle": (
            "Revisando los mensajes entrantes."
            if configured
            else "SIN clave de OpenAI: los mensajes entrantes NO se están moderando."
        ),
    }


class ModerationResult:
    def __init__(
        self,
        flagged: bool,
        categories: list[str] | None = None,
        available: bool = True,
    ) -> None:
        self.flagged = flagged
        self.categories = categories or []
        # False = no se ha podido comprobar (sin clave o error). No es lo mismo
        # que "comprobado y limpio", aunque en ambos casos se deje pasar.
        self.available = available


async def moderate(text: str) -> ModerationResult:
    if not text or not text.strip():
        return ModerationResult(flagged=False)
    client = await _get_client()
    if client is None:
        logger.warning("moderation.no_key")
        await _alert_once(
            "no_key",
            "No hay clave de OpenAI: la moderación de contenido está APAGADA y los "
            "mensajes entrantes pasan al agente sin revisar. Configura "
            "openai_api_key en Admin → Credenciales.",
        )
        return ModerationResult(flagged=False, available=False)  # fail-open
    try:
        resp = await client.moderations.create(
            model="omni-moderation-latest",
            input=text[:4000],
        )
        result = resp.results[0]
        flagged = bool(result.flagged)
        cats: list[str] = []
        if flagged and result.categories:
            cats = [k for k, v in result.categories.model_dump().items() if v]
        # Telemetry best-effort: la moderacion es gratuita pero contamos llamadas.
        try:
            from app.services.usage_tracker import track_usage
            await track_usage(source="moderation", model="omni-moderation-latest")
        except Exception:
            pass
        return ModerationResult(flagged=flagged, categories=cats)
    except Exception as e:
        logger.error("moderation.error", error=str(e))
        await _alert_once(
            "error",
            "La moderación de contenido está fallando: los mensajes entrantes "
            "pasan al agente sin revisar.",
            error=str(e)[:300],
        )
        return ModerationResult(flagged=False, available=False)  # fail-open
