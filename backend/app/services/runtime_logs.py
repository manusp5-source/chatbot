"""Buffer circular en Redis con los ultimos N eventos del runtime.

Pensado para que el operador vea actividad del bot en vivo desde el panel
sin tener que ir a EasyPanel a leer logs raw del contenedor.

Solo eventos importantes (no spam de DB queries):
- mensaje recibido / respondido
- agente pausado / reactivado
- derivacion a humano
- moderacion flagged
- rate limit por contacto
- error LLM
- budget excedido
- webhook signature invalida
- task celery falla

Almacena hasta 1000 lineas via LPUSH + LTRIM, con caducidad de RETENTION_DAYS.

DATOS PERSONALES: el filtro de PII se aplica AQUI, al ESCRIBIR. El log
estructurado (core/logging.py) ya pasaba por `_scrub_pii`, pero este buffer no
lo tocaba nadie: telefonos, correos y trozos crudos de la respuesta del
proveedor salian tal cual por la pantalla "Logs en vivo" del panel. Confiar en
que cada llamante enmascare lo suyo no funciona (hay decenas de llamadas y cada
una decide), asi que se enmascara en el unico sitio por el que pasan todas.
"""
from __future__ import annotations

import json
import time
from typing import Any

# Mismo filtro que el log estructurado: telefonos, emails, claves de API,
# Bearer y JWT. Se reutiliza a proposito para que no haya dos listas de
# patrones que se desincronicen.
from app.core.logging import _scrub_value as scrub_pii
from app.core.redis import get_redis

RUNTIME_LOGS_KEY = "runtime:logs"
MAX_LINES = 1000
# Recortar a 1000 lineas NO es retencion: una instalacion tranquila se guarda
# esas 1000 (con lo que lleven dentro) para siempre. Con TTL el buffer caduca.
RETENTION_SECONDS = 7 * 24 * 3600

# Claves que NO se enmascaran: son metadatos del propio buffer, nunca PII, y
# pasarlas por el filtro solo puede estropearlas.
_UNSCRUBBED_KEYS = {"ts", "level", "event"}


async def push_runtime_log(
    level: str,
    event: str,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Empuja una linea al buffer (best-effort, nunca rompe el flujo).

    El mensaje y TODOS los campos extra pasan por el filtro de PII antes de
    tocar Redis.
    """
    try:
        entry: dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "level": level,
            "event": event,
            "message": scrub_pii(message) if message else message,
        }
        for k, v in fields.items():
            if v is None:
                continue
            if k in _UNSCRUBBED_KEYS:
                entry[k] = v
                continue
            # Un objeto suelto (UUID, Enum, excepcion...) lo serializaria luego
            # `default=str`, DESPUES del filtro, y su texto se colaria sin
            # enmascarar. Se pasa a texto antes de filtrar.
            if not isinstance(v, (str, int, float, bool, dict, list, tuple)):
                v = str(v)
            entry[k] = scrub_pii(v)
        # OJO: el filtro se aplica campo a campo, NUNCA sobre el JSON entero
        # (`ts` son 13 digitos y el patron de telefono se lo comeria, dejando
        # la linea ilegible).
        payload = json.dumps(entry, default=str)
        r = get_redis()
        async with r.pipeline(transaction=True) as p:
            p.lpush(RUNTIME_LOGS_KEY, payload)
            p.ltrim(RUNTIME_LOGS_KEY, 0, MAX_LINES - 1)
            p.expire(RUNTIME_LOGS_KEY, RETENTION_SECONDS)
            await p.execute()
    except Exception:
        pass


async def list_runtime_logs(
    limit: int = 200,
    level: str | None = None,
    event_prefix: str | None = None,
) -> list[dict]:
    """Devuelve los ultimos N eventos (mas reciente primero), con filtros."""
    try:
        r = get_redis()
        raw = await r.lrange(RUNTIME_LOGS_KEY, 0, limit * 2)
        out: list[dict] = []
        for line in raw:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if level and entry.get("level") != level:
                continue
            if event_prefix and not str(entry.get("event", "")).startswith(event_prefix):
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []
