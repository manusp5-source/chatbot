"""Lectura/escritura de ajustes globales del panel (tabla KV `app_settings`).

Wrapper fino sobre el modelo AppSetting para que el resto del código no toque
la tabla directamente. Las funciones aceptan una sesión existente (la del
request) o abren la suya con `db_session()` si no se les pasa ninguna.

Uso típico (zona horaria del dashboard):

    tz = await get_app_setting(SETTING_DASHBOARD_TIMEZONE,
                               settings.DASHBOARD_TIMEZONE, db)
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import db_session
from app.models.app_setting import AppSetting

# Zonas horarias ofrecidas en el desplegable del panel. Lista corta de las más
# habituales para negocios de habla hispana; el backend valida cualquier zona
# IANA válida, así que se puede ampliar sin tocar la validación.
COMMON_TIMEZONES = [
    "Europe/Madrid",
    "Europe/London",
    "Atlantic/Canary",
    "Europe/Lisbon",
    "Europe/Paris",
    "Europe/Berlin",
    "America/Mexico_City",
    "America/Bogota",
    "America/Lima",
    "America/Santiago",
    "America/Argentina/Buenos_Aires",
    "America/Montevideo",
    "America/New_York",
    "America/Los_Angeles",
    "UTC",
]


async def get_app_setting(
    key: str, default: str, db: AsyncSession | None = None
) -> str:
    """Devuelve el valor de `key` o `default` si no existe la fila."""
    if db is not None:
        value = (
            await db.execute(select(AppSetting.value).where(AppSetting.key == key))
        ).scalar_one_or_none()
        return value if value is not None else default
    async with db_session() as own_db:
        value = (
            await own_db.execute(select(AppSetting.value).where(AppSetting.key == key))
        ).scalar_one_or_none()
        return value if value is not None else default


async def set_app_setting(key: str, value: str, db: AsyncSession) -> None:
    """Inserta o actualiza `key` (upsert). No hace commit: lo hace el llamante."""
    stmt = (
        pg_insert(AppSetting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
    )
    await db.execute(stmt)
