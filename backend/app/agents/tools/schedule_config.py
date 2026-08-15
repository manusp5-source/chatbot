"""Horario de atención del negocio: zona horaria, días, tramos y festivos.

Todo esto estaba FIJO en el código: `Europe/Madrid` y de 9 a 18, sin comprobar
fin de semana ni festivos, con un comentario que decía "configurable en el
futuro". El agente ofrecía sábado y domingo igual que un martes, y un comercio
que abre sábados, o un negocio en otro huso horario, no tenía forma de
ajustarlo. Y cada instalación corre en su propio servidor.

Los valores viven en la tabla KV `app_settings` (claves `calendar.*`), así que
se editan sin tocar código ni reiniciar. Los DEFAULTS son exactamente los
valores que había antes, para que ninguna instalación existente cambie de
comportamiento al desplegar.

Claves y formato:

  calendar.timezone        "Europe/Madrid"       zona IANA
  calendar.work_days       "1,2,3,4,5"           1=lunes … 7=domingo
  calendar.work_hours      "09:00-18:00"         varios tramos: "09:00-14:00,16:00-20:00"
  calendar.holidays        ""                    "2026-12-25,2026-01-06" (festivos cerrados)
  calendar.slot_step_min   "30"                  cada cuánto se ofrece hueco
  calendar.min_notice_min  "0"                   antelación mínima para una cita
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.logging import get_logger

logger = get_logger(__name__)

SETTING_TZ = "calendar.timezone"
SETTING_WORK_DAYS = "calendar.work_days"
SETTING_WORK_HOURS = "calendar.work_hours"
SETTING_HOLIDAYS = "calendar.holidays"
SETTING_SLOT_STEP = "calendar.slot_step_min"
SETTING_MIN_NOTICE = "calendar.min_notice_min"

# Defaults = comportamiento anterior, para no cambiarle el horario a nadie al
# desplegar. La única diferencia es que ahora el fin de semana SÍ se descarta:
# ofrecer sábado y domingo a un negocio de lunes a viernes era un fallo, no una
# funcionalidad, y quien abra sábados lo pone en `calendar.work_days`.
DEFAULT_TZ_NAME = "Europe/Madrid"
DEFAULT_WORK_DAYS = "1,2,3,4,5"
DEFAULT_WORK_HOURS = "09:00-18:00"
DEFAULT_SLOT_STEP_MIN = 30
DEFAULT_MIN_NOTICE_MIN = 0


@dataclass
class ScheduleConfig:
    tz: ZoneInfo
    tz_name: str
    # isoweekday: 1=lunes … 7=domingo
    work_days: set[int]
    # Tramos [(inicio, fin)] en hora local, ordenados.
    work_hours: list[tuple[time, time]]
    holidays: set[date] = field(default_factory=set)
    slot_step_min: int = DEFAULT_SLOT_STEP_MIN
    min_notice_min: int = DEFAULT_MIN_NOTICE_MIN

    def is_working_day(self, d: date) -> bool:
        return d.isoweekday() in self.work_days and d not in self.holidays

    def closed_reason(self, d: date) -> str | None:
        """Por qué no se atiende ese día, en lenguaje llano. None = sí se abre."""
        if d in self.holidays:
            return "Ese día el negocio está cerrado (festivo)."
        if d.isoweekday() not in self.work_days:
            nombres = {1: "lunes", 2: "martes", 3: "miércoles", 4: "jueves",
                       5: "viernes", 6: "sábado", 7: "domingo"}
            return f"El negocio no atiende los {nombres[d.isoweekday()]}."
        return None


def _parse_work_days(raw: str) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if 1 <= n <= 7:
            out.add(n)
    return out or _parse_work_days(DEFAULT_WORK_DAYS)


def _parse_work_hours(raw: str) -> list[tuple[time, time]]:
    """"09:00-14:00,16:00-20:00" → [(9:00, 14:00), (16:00, 20:00)]."""
    ranges: list[tuple[time, time]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        start_s, _, end_s = part.partition("-")
        try:
            start = time.fromisoformat(start_s.strip())
            end = time.fromisoformat(end_s.strip())
        except ValueError:
            continue
        if end > start:
            ranges.append((start, end))
    if not ranges:
        return [(time(9, 0), time(18, 0))]
    return sorted(ranges)


def _parse_holidays(raw: str) -> set[date]:
    out: set[date] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(date.fromisoformat(part))
        except ValueError:
            continue
    return out


def _parse_int(raw: str, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(n, maximum))


def build_config(
    tz_name: str,
    work_days: str,
    work_hours: str,
    holidays: str,
    slot_step: str,
    min_notice: str,
) -> ScheduleConfig:
    """Construye la config desde strings crudos. PURA: testeable sin BD."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("schedule.bad_timezone", value=tz_name)
        tz_name = DEFAULT_TZ_NAME
        tz = ZoneInfo(DEFAULT_TZ_NAME)
    return ScheduleConfig(
        tz=tz,
        tz_name=tz_name,
        work_days=_parse_work_days(work_days),
        work_hours=_parse_work_hours(work_hours),
        holidays=_parse_holidays(holidays),
        slot_step_min=_parse_int(slot_step, DEFAULT_SLOT_STEP_MIN, 5, 240),
        min_notice_min=_parse_int(min_notice, DEFAULT_MIN_NOTICE_MIN, 0, 7 * 24 * 60),
    )


async def get_schedule_config() -> ScheduleConfig:
    """Lee la configuración de horario. Ante cualquier fallo, los defaults."""
    try:
        from app.services.app_settings import get_app_setting

        return build_config(
            tz_name=await get_app_setting(SETTING_TZ, DEFAULT_TZ_NAME),
            work_days=await get_app_setting(SETTING_WORK_DAYS, DEFAULT_WORK_DAYS),
            work_hours=await get_app_setting(SETTING_WORK_HOURS, DEFAULT_WORK_HOURS),
            holidays=await get_app_setting(SETTING_HOLIDAYS, ""),
            slot_step=await get_app_setting(SETTING_SLOT_STEP, str(DEFAULT_SLOT_STEP_MIN)),
            min_notice=await get_app_setting(SETTING_MIN_NOTICE, str(DEFAULT_MIN_NOTICE_MIN)),
        )
    except Exception as e:  # noqa: BLE001 — sin BD, horario por defecto
        logger.warning("schedule.config_read_error", error=str(e))
        return build_config(
            DEFAULT_TZ_NAME, DEFAULT_WORK_DAYS, DEFAULT_WORK_HOURS, "",
            str(DEFAULT_SLOT_STEP_MIN), str(DEFAULT_MIN_NOTICE_MIN),
        )


def now_in(tz: ZoneInfo) -> datetime:
    """Ahora, en la zona del negocio. Aislado para poder fijarlo en tests."""
    return datetime.now(tz)
