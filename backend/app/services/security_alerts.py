"""Alertas de seguridad hacia el PANEL ("Logs en vivo").

Avisos internos de que algo raro está pasando (rate-limit roto repetidamente,
audio rechazado, intento de suplantación, presupuesto…). Antes iban a
Telegram/Slack; ahora se muestran en el panel (`push_runtime_log`) para no
exfiltrar datos a plataformas de terceros (RGPD) — el panel es suficiente.

Se aplica throttling para no inundar con avisos repetidos (Redis, TTL corto).
"""
import time

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)

_ALERT_THROTTLE_KEY = "sec_alert:{kind}:{key}"
_ALERT_THROTTLE_SECS = 600  # no más de 1 alerta del mismo (kind, key) cada 10 min


async def _should_alert(kind: str, key: str) -> bool:
    r = get_redis()
    cache_key = _ALERT_THROTTLE_KEY.format(kind=kind, key=key)
    # SET NX EX = atómico, falla si ya existe → ya hemos alertado recientemente.
    set_ok = await r.set(cache_key, str(int(time.time())), ex=_ALERT_THROTTLE_SECS, nx=True)
    return bool(set_ok)


async def notify_security(
    kind: str,
    title: str,
    details: dict | None = None,
    *,
    throttle_key: str | None = None,
) -> None:
    """Muestra una alerta de seguridad en el panel ("Logs en vivo").

    Args:
        kind: identificador corto del tipo de evento (e.g. "rate_limit",
            "phone_override", "moderation_flagged"). Se usa para throttling.
        title: titular humano breve.
        details: kv adicionales que se adjuntan al evento del log.
        throttle_key: clave secundaria para diferenciar throttle (ej: phone,
            user_id). Si no se pasa, el throttle es global por `kind`.
    """
    if not await _should_alert(kind, throttle_key or "_"):
        return
    try:
        await push_runtime_log(
            level="warn",
            event=f"security.{kind}",
            message=f"[Seguridad] {title}"[:300],
            **{str(k)[:60]: str(v)[:300] for k, v in (details or {}).items()},
        )
    except Exception as e:
        logger.error("security_alert.notify_failed", error=str(e))
