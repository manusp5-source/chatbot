"""Lectura de credenciales cifradas desde la BD con cache distribuido en Redis.

Antes el cache era por proceso (in-memory dict). Eso significaba que cuando
un admin guardaba una credencial nueva via `/admin/credentials`, el `app`
invalidaba su cache local pero `worker` y `beat` seguian con el valor viejo
hasta que expirara su propio TTL (>30s de desincronizacion entre procesos).

Ahora el cache vive en Redis con TTL 60s y la invalidacion borra la clave
para todos los procesos a la vez. Las lecturas siguen siendo ultra rapidas
(Redis local del mismo proyecto en EasyPanel).
"""
from sqlalchemy import select

from app.core.encryption import get_encryption_service
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.credential import Credential

logger = get_logger(__name__)

_CACHE_PREFIX = "credential:"
_CACHE_TTL_SECONDS = 60
# Marca para credenciales que existen pero estan vacias o que no existen,
# evita tener que ir a BD cada vez que un proveedor no esta configurado.
_NULL_SENTINEL = "\x00null\x00"


class CredentialUnavailableError(RuntimeError):
    """La credencial EXISTE en la BD pero no se ha podido leer/descifrar.

    Es un caso distinto de "no configurada" (ahi `get_credential` devuelve
    None). Confundir los dos es peligroso: un consumidor que lee None como
    "esta integracion no esta puesta" sigue adelante como si nada — asi los
    backups salian en verde sin subir NADA al bucket. Los llamantes que no
    pueden fallar en silencio piden `strict=True` y se enteran.
    """


async def invalidate_credential_cache(key: str | None = None) -> None:
    """Borra una clave del cache distribuido o todas si key=None."""
    r = get_redis()
    if key is None:
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=f"{_CACHE_PREFIX}*", count=100)
            if keys:
                await r.delete(*keys)
            if cursor == 0:
                break
    else:
        await r.delete(f"{_CACHE_PREFIX}{key}")


async def get_credential(key: str, *, strict: bool = False) -> str | None:
    """Valor en claro de una credencial, o None si NO esta configurada.

    `strict=True`: si la credencial esta guardada pero no se puede descifrar,
    lanza `CredentialUnavailableError` en vez de devolver None. Uselo desde
    cualquier sitio donde "no configurada" y "no se pudo leer" lleven a
    decisiones distintas (backups: no subir vs. dar la copia por buena).
    """
    r = get_redis()
    cache_key = f"{_CACHE_PREFIX}{key}"
    cached = await r.get(cache_key)
    if cached is not None:
        return None if cached == _NULL_SENTINEL else cached

    async with db_session() as db:
        row = (await db.execute(select(Credential).where(Credential.key == key))).scalar_one_or_none()
    if not row or not row.value_encrypted:
        await r.setex(cache_key, _CACHE_TTL_SECONDS, _NULL_SENTINEL)
        return None
    try:
        plain = get_encryption_service().decrypt(row.value_encrypted)
    except Exception as e:
        # OJO: aqui NO se cachea el sentinel. Si lo hicieramos, durante 60s
        # TODOS los procesos verian una credencial GUARDADA como si no
        # existiera — y quien no use strict ni siquiera podria distinguirlo.
        logger.error("credential.decrypt.error", key=key, error=str(e))
        if strict:
            raise CredentialUnavailableError(
                f"La credencial '{key}' esta guardada pero no se pudo descifrar "
                f"(¿ENCRYPTION_KEY cambiada?): {e}"
            ) from e
        return None
    await r.setex(cache_key, _CACHE_TTL_SECONDS, plain)
    return plain
