"""Helpers para rate-limit que entienden proxies (X-Forwarded-For).

`slowapi.util.get_remote_address` solo mira `request.client.host`, que detrás de
un proxy (EasyPanel / nginx / Cloudflare) es siempre la IP del proxy → todo el
tráfico compartiría cubo.

Pero confiar en el PRIMER valor de `X-Forwarded-For` es inseguro: el cliente
puede falsificarlo (envía `X-Forwarded-For: <ip-aleatoria>` y nuestro proxy
añade la suya detrás). Por eso tomamos la IP que añadió NUESTRO proxy de
confianza: la posición `-TRUSTED_PROXY_COUNT` desde la derecha. Con
`TRUSTED_PROXY_COUNT=0` ignoramos XFF y usamos la IP de socket.

Además, el almacenamiento del rate-limit debe ser COMPARTIDO (Redis), no en
memoria de proceso, o el límite se multiplica por nº de workers/réplicas y se
reinicia en cada redeploy. `make_limiter()` crea limiters con storage Redis.

Dos cosas que se arreglaron aquí y conviene no volver a romper:

1. `X-Real-IP` NO se acepta a ciegas. Antes, una petición SIN `X-Forwarded-For`
   pero CON `X-Real-IP: 9.9.9.9` se metía en el cubo "9.9.9.9" tal cual: esa
   cabecera no tiene lógica de posición (es un valor suelto), así que si el
   proxy no la reescribe la pone el cliente y rotarla salta el límite de login.
   Ahora solo se mira si se declara explícitamente `TRUST_X_REAL_IP=true` Y hay
   un único proxy delante (`TRUSTED_PROXY_COUNT == 1`).

2. Si `TRUSTED_PROXY_COUNT` se queda CORTO (2 proxies delante y el valor a 1),
   la posición elegida es la IP interna del proxy de dentro: privada, igual
   para todo el mundo → un solo cubo para todo el tráfico y 5 peticiones tumban
   el login de todos. Ahora, si la posición elegida cae en una IP privada/
   reservada, se sigue hacia la izquierda hasta la primera IP pública (que es
   la que añadió el proxy más externo, no falsificable por el cliente) y se
   grita en los logs para que alguien corrija la variable.
"""
from __future__ import annotations

import ipaddress
import os
import time

from fastapi import Request
from slowapi import Limiter
from starlette.websockets import WebSocket

from app.core.config import settings

# Techo por defecto para TODA la API (por IP). No sustituye a los límites
# específicos de cada endpoint: es la red de seguridad para las rutas que no
# tienen ninguno. Se suma (AND) a los del decorador.
#
# En la suite de tests se desactiva de hecho: todas las peticiones salen de la
# misma "IP" (127.0.0.1) y comparten cubo en Redis, así que un techo real haría
# fallar tests por orden de ejecución en vez de por lo que comprueban. El límite
# como tal se prueba aparte, montando un limiter con su propio valor.
_IS_TEST_ENV = settings.APP_ENV.strip().lower() == "test"
_SIN_LIMITE_EN_TESTS = "1000000/minute"


def limit_spec(spec: str) -> str:
    """El límite tal cual, salvo en la suite de tests.

    En los tests todas las peticiones salen de 127.0.0.1 y el contador vive en
    Redis, así que un "10/hora" real se agota entre ejecuciones y hace fallar
    tests por el orden en que se corren, no por lo que comprueban. El valor
    declarado se sigue leyendo en el código, que es lo que importa al revisarlo.
    """
    return _SIN_LIMITE_EN_TESTS if _IS_TEST_ENV else spec


DEFAULT_API_RATE_LIMIT = os.getenv("API_DEFAULT_RATE_LIMIT") or limit_spec("600/minute")

# Solo se hace caso a X-Real-IP si se pide explícitamente (ver docstring).
TRUST_X_REAL_IP = os.getenv("TRUST_X_REAL_IP", "").strip().lower() in {"1", "true", "yes", "si", "sí"}

# Aviso de "TRUSTED_PROXY_COUNT mal puesto": como máximo uno cada 10 min para
# no inundar los logs con una línea por petición.
_MISCONFIG_WARN_EVERY = 600
_last_misconfig_warn = 0.0


def _peer(request: Request | WebSocket) -> str:
    client = getattr(request, "client", None)
    if client and client.host:
        return client.host
    return "unknown"


def _is_private(value: str) -> bool:
    """True si la IP es privada/loopback/link-local/reservada (o no es una IP).

    Una IP así NUNCA puede ser la de un cliente real que viene de internet: si
    la posición configurada cae ahí, es que hay más proxies de los declarados.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True  # no es una IP → no sirve como clave de cubo
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _warn_misconfigured_proxy_count(chain: list[str], picked: str) -> None:
    """Grita (con throttle) que TRUSTED_PROXY_COUNT no cuadra con la cadena."""
    global _last_misconfig_warn
    now = time.monotonic()
    if now - _last_misconfig_warn < _MISCONFIG_WARN_EVERY:
        return
    _last_misconfig_warn = now
    try:
        from app.core.logging import get_logger

        get_logger(__name__).error(
            "ratelimit.trusted_proxy_count.mismatch",
            trusted_proxy_count=settings.TRUSTED_PROXY_COUNT,
            xff_entries=len(chain),
            picked_is_private=True,
            picked=picked,
            msg=(
                "TRUSTED_PROXY_COUNT no cuadra con los proxies reales: la posición "
                "elegida es una IP privada. Se usa la IP pública más a la derecha "
                "como apaño. Ajusta TRUSTED_PROXY_COUNT al nº real de proxies o el "
                "rate-limit compartirá cubo para todo el tráfico."
            ),
        )
    except Exception:  # pragma: no cover - el logging nunca puede romper el límite
        pass


def client_ip_key(request: Request | WebSocket) -> str:
    n = settings.TRUSTED_PROXY_COUNT
    if n > 0 and hasattr(request, "headers"):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            parts = [p.strip() for p in fwd.split(",") if p.strip()]
            if parts:
                # La IP añadida por nuestro proxy de confianza (desde la derecha).
                # Si hay menos entradas que proxies, caemos al primero conocido.
                idx = -min(n, len(parts))
                picked = parts[idx]
                if not _is_private(picked):
                    return picked
                # La posición configurada cae en una IP privada → hay más
                # proxies de los declarados. Seguimos hacia la izquierda hasta
                # la primera pública: sigue siendo una IP que añadió un proxy
                # (el más externo), no un valor que controle el cliente.
                for candidate in reversed(parts[: len(parts) + idx]):
                    if not _is_private(candidate):
                        _warn_misconfigured_proxy_count(parts, picked)
                        return candidate
                return picked
        # X-Real-IP: valor suelto, sin posición. Solo si se ha declarado que
        # nuestro (único) proxy lo reescribe. Si no, lo pondría el cliente.
        if TRUST_X_REAL_IP and n == 1:
            real = request.headers.get("x-real-ip")
            if real:
                return real.strip()
    return _peer(request)


def check_proxy_config() -> list[str]:
    """Avisos de configuración del rate-limit para el arranque.

    No puede comprobar la cadena real de proxies (eso solo se ve con tráfico),
    pero sí que en producción alguien haya DECIDIDO el valor en vez de heredar
    el 1 por defecto — que es justo el error que convierte el límite de login
    en global. Devuelve la lista de avisos (vacía si todo cuadra).
    """
    warnings: list[str] = []
    declared = os.getenv("TRUSTED_PROXY_COUNT")
    if settings.is_production and declared is None:
        warnings.append(
            "TRUSTED_PROXY_COUNT no está definido: se usa el valor por defecto (1). "
            "Si delante hay más de un proxy (p.ej. Cloudflare + Traefik), el rate-limit "
            "por IP mete TODO el tráfico en el mismo cubo y 5 intentos fallidos bloquean "
            "el login de todos los usuarios. Fíjalo al nº real de proxies."
        )
    if settings.TRUSTED_PROXY_COUNT < 0:
        warnings.append("TRUSTED_PROXY_COUNT negativo: se trata como 0 (se ignora X-Forwarded-For).")
    if TRUST_X_REAL_IP and settings.TRUSTED_PROXY_COUNT != 1:
        warnings.append(
            "TRUST_X_REAL_IP está activo con TRUSTED_PROXY_COUNT != 1: la cabecera se "
            "ignora (sin un único proxy que la reescriba, la pondría el cliente)."
        )
    if not settings.REDIS_URL:
        warnings.append(
            "REDIS_URL vacío: el rate-limit cae a memoria de proceso, así que el límite "
            "real se multiplica por el nº de workers y se reinicia en cada despliegue."
        )
    return warnings


def make_limiter(default_limits: list[str] | None = None) -> Limiter:
    """Limiter con storage COMPARTIDO en Redis (mismo límite real con N workers/
    réplicas). Si REDIS_URL no está, slowapi cae a memoria automáticamente."""
    return Limiter(
        key_func=client_ip_key,
        storage_uri=settings.REDIS_URL,
        default_limits=default_limits or [],
    )


# ---------------------------------------------------------------------------
# Cupo DIARIO por USUARIO para lo que cuesta dinero
#
# El límite por IP no sirve contra una sesión válida: un operador (o un token
# robado) puede quemar el presupuesto de OpenAI en un bucle desde una sola IP y
# el único freno era el tope MENSUAL global, que al saltar corta el bot para
# todo el mundo. Mismo patrón que el agente interno (agents/internal/budget.py):
# contador en Redis por usuario y día, con TTL.
# ---------------------------------------------------------------------------

_USER_QUOTA_KEY = "userquota:{name}:{user_id}:{ymd}"
_USER_QUOTA_TTL = 26 * 3600


def _quota_key(name: str, user_id) -> str:
    from datetime import datetime, timezone

    ymd = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _USER_QUOTA_KEY.format(name=name, user_id=user_id, ymd=ymd)


async def get_user_daily_count(name: str, user_id) -> int:
    """Consumo de hoy. Si Redis no responde devuelve -1 ("no se sabe")."""
    from app.core.redis import get_redis

    try:
        raw = await get_redis().get(_quota_key(name, user_id))
    except Exception:
        return -1
    if raw is None:
        return 0
    try:
        return int(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError):
        return 0


async def consume_user_daily_quota(name: str, user_id, limit: int) -> tuple[bool, int]:
    """Consume una unidad del cupo diario. Devuelve (permitido, usado_después).

    Incrementa PRIMERO y compara después: así dos peticiones simultáneas no
    pueden colarse las dos por el mismo hueco. Si Redis no responde se deja
    pasar (el cupo es un guardarraíl de coste, no un control de acceso: sin
    Redis el panel entero ya está roto) pero se registra el fallo.
    """
    from app.core.redis import get_redis

    if limit <= 0:
        return True, 0
    try:
        r = get_redis()
        key = _quota_key(name, user_id)
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _USER_QUOTA_TTL)
        results = await pipe.execute()
        used = int(results[0])
    except Exception as e:
        try:
            from app.core.logging import get_logger

            get_logger(__name__).error("ratelimit.user_quota.unavailable", quota=name, error=str(e))
        except Exception:
            pass
        return True, -1
    return used <= limit, used
