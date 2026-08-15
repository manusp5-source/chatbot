"""Ajustes globales del panel (clave-valor).

Tabla KV pequeña para settings de despliegue que no encajan en una tabla
propia ni son por-agente (agent_config es versionado/inmutable). Hoy solo
guarda la zona horaria del dashboard (`dashboard_timezone`), pero está pensada
para crecer con otros ajustes globales sin migración por cada uno.

Lectura por `services/app_settings.get_app_setting(key, default)`; escritura
por `set_app_setting(key, value)`. Si la clave no existe, el lector cae al
default que le pase el llamante (para timezone, `settings.DASHBOARD_TIMEZONE`).
"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

# Clave de la zona horaria del dashboard/Home.
SETTING_DASHBOARD_TIMEZONE = "dashboard_timezone"


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
