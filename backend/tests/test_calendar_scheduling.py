"""Agenda: nada en el pasado, horario configurable y días cerrados.

Tres fallos que cubrían estos tests:
  - `agendar_cita` solo validaba que el fin fuera posterior al inicio, así que
    "apúntame el 3 de enero" creaba el evento en el AÑO PASADO.
  - `_slots_for_day` generaba el día entero sin descartar lo ya pasado: a las
    17:00 seguía ofreciendo las 9:30 de esa misma mañana.
  - el horario estaba fijo en código (Europe/Madrid, 9-18) sin comprobar fin de
    semana ni festivos: ofrecía sábado y domingo igual que un martes.
"""
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agents.tools.calendar import _slots_for_day, agendar_cita
from app.agents.tools.schedule_config import build_config

MADRID = ZoneInfo("Europe/Madrid")


def _cfg(**kw):
    base = dict(
        tz_name="Europe/Madrid",
        work_days="1,2,3,4,5",
        work_hours="09:00-18:00",
        holidays="",
        slot_step="30",
        min_notice="0",
    )
    base.update(kw)
    return build_config(**base)


# ── Configuración de horario ────────────────────────────────────────────────


def test_defaults_son_los_de_siempre():
    """Al desplegar, ninguna instalación cambia de horario."""
    cfg = _cfg()
    assert cfg.tz_name == "Europe/Madrid"
    assert cfg.work_hours == [(time(9, 0), time(18, 0))]
    assert cfg.work_days == {1, 2, 3, 4, 5}
    assert cfg.slot_step_min == 30


def test_horario_partido_y_otra_zona():
    """Un comercio con jornada partida que además abre sábados."""
    cfg = _cfg(
        tz_name="America/Mexico_City",
        work_hours="09:00-14:00,16:00-20:00",
        work_days="1,2,3,4,5,6",
    )
    assert cfg.tz_name == "America/Mexico_City"
    assert cfg.work_hours == [(time(9, 0), time(14, 0)), (time(16, 0), time(20, 0))]
    assert 6 in cfg.work_days


def test_zona_horaria_invalida_cae_al_default_sin_romper():
    cfg = _cfg(tz_name="Marte/Olympus")
    assert cfg.tz_name == "Europe/Madrid"


def test_fin_de_semana_y_festivos_estan_cerrados():
    cfg = _cfg(holidays="2026-12-25")
    sabado = datetime(2026, 8, 15, tzinfo=MADRID).date()   # sábado
    domingo = datetime(2026, 8, 16, tzinfo=MADRID).date()  # domingo
    martes = datetime(2026, 8, 11, tzinfo=MADRID).date()
    navidad = datetime(2026, 12, 25, tzinfo=MADRID).date()

    assert cfg.is_working_day(martes)
    assert not cfg.is_working_day(sabado)
    assert not cfg.is_working_day(domingo)
    assert not cfg.is_working_day(navidad)
    assert "sábado" in cfg.closed_reason(sabado)
    assert "festivo" in cfg.closed_reason(navidad)
    assert cfg.closed_reason(martes) is None


def test_quien_abre_sabados_puede_configurarlo():
    cfg = _cfg(work_days="1,2,3,4,5,6")
    assert cfg.is_working_day(datetime(2026, 8, 15, tzinfo=MADRID).date())


# ── Huecos del día ──────────────────────────────────────────────────────────


def test_no_se_ofrecen_horas_que_ya_han_pasado():
    """A las 17:00 no se puede ofrecer las 9:30 de esa misma mañana."""
    cfg = _cfg()
    dia = datetime(2026, 8, 11, tzinfo=MADRID)  # martes
    ahora = dia.replace(hour=17, minute=0)
    slots = _slots_for_day(dia, 30, [], cfg, now=ahora)

    assert slots, "debería quedar algún hueco de 17:00 a 18:00"
    for s in slots:
        assert datetime.fromisoformat(s["start"]) >= ahora
    assert all(s["label"] >= "17:00" for s in slots)


def test_dia_futuro_ofrece_la_jornada_entera():
    cfg = _cfg()
    dia = datetime(2026, 9, 15, tzinfo=MADRID)
    ahora = datetime(2026, 8, 11, 17, 0, tzinfo=MADRID)
    slots = _slots_for_day(dia, 30, [], cfg, now=ahora)
    assert slots[0]["label"] == "09:00"
    assert slots[-1]["label"] == "17:30"


def test_la_antelacion_minima_se_respeta():
    cfg = _cfg(min_notice="120")  # 2 horas
    dia = datetime(2026, 8, 11, tzinfo=MADRID)
    ahora = dia.replace(hour=10, minute=0)
    slots = _slots_for_day(dia, 30, [], cfg, now=ahora)
    assert slots[0]["label"] == "12:00"


def test_los_ocupados_se_descuentan():
    cfg = _cfg()
    dia = datetime(2026, 9, 15, tzinfo=MADRID)
    ahora = datetime(2026, 8, 11, tzinfo=MADRID)
    busy = [
        {
            "start": dia.replace(hour=10, minute=0).isoformat(),
            "end": dia.replace(hour=11, minute=0).isoformat(),
        }
    ]
    labels = [s["label"] for s in _slots_for_day(dia, 30, busy, cfg, now=ahora)]
    assert "09:00" in labels
    assert "10:00" not in labels
    assert "10:30" not in labels
    assert "11:00" in labels


def test_horario_partido_no_ofrece_la_hora_de_comer():
    cfg = _cfg(work_hours="09:00-14:00,16:00-20:00")
    dia = datetime(2026, 9, 15, tzinfo=MADRID)
    ahora = datetime(2026, 8, 11, tzinfo=MADRID)
    labels = [s["label"] for s in _slots_for_day(dia, 30, [], cfg, now=ahora)]
    assert "13:30" in labels
    assert "14:30" not in labels
    assert "15:30" not in labels
    assert "16:00" in labels


# ── agendar_cita ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_se_puede_agendar_en_el_pasado():
    """El caso literal de la auditoría: "apúntame el 3 de enero"."""
    ayer = datetime.now(MADRID) - timedelta(days=1)
    out = json.loads(
        await agendar_cita(
            {
                "start": ayer.isoformat(),
                "end": (ayer + timedelta(minutes=30)).isoformat(),
                "motivo": "Corte",
            },
            {},
        )
    )
    assert "error" in out
    assert "pasado" in out["error"]


@pytest.mark.asyncio
async def test_fin_anterior_al_inicio_sigue_rechazandose():
    manana = datetime.now(MADRID) + timedelta(days=3)
    out = json.loads(
        await agendar_cita(
            {
                "start": manana.isoformat(),
                "end": (manana - timedelta(minutes=30)).isoformat(),
                "motivo": "Corte",
            },
            {},
        )
    )
    assert "error" in out
    assert "posterior" in out["error"]


@pytest.mark.asyncio
async def test_no_se_agenda_un_dia_cerrado(monkeypatch):
    """Con el horario por defecto (L-V), un sábado se rechaza con motivo."""
    from app.agents.tools import calendar as cal

    async def fake_cfg():
        return _cfg()

    monkeypatch.setattr(cal, "get_schedule_config", fake_cfg)

    # Próximo sábado a las 11:00.
    ahora = datetime.now(MADRID)
    dias = (5 - ahora.weekday()) % 7 or 7
    sabado = (ahora + timedelta(days=dias)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    out = json.loads(
        await cal.agendar_cita(
            {
                "start": sabado.isoformat(),
                "end": (sabado + timedelta(minutes=30)).isoformat(),
                "motivo": "Corte",
            },
            {},
        )
    )
    assert "error" in out
    assert "sábado" in out["error"]


@pytest.mark.asyncio
async def test_sin_google_conectado_no_se_ofrecen_huecos(monkeypatch):
    """`list_busy_slots` devuelve [] tanto si está libre como si NO HAY
    calendario. Sin distinguirlo, el agente ofrecía el día entero libre y el
    fallo solo salía al final, con el cliente ya con hora elegida."""
    from app.agents.tools import calendar as cal

    async def sin_calendario():
        return False

    monkeypatch.setattr(cal, "_calendar_connected", sin_calendario)

    manana = (datetime.now(MADRID) + timedelta(days=30)).strftime("%Y-%m-%d")
    out = json.loads(await cal.consultar_disponibilidad({"fecha": manana}, {}))
    assert "error" in out
    assert "no está conectado" in out["error"]
    assert "slots_libres" not in out


@pytest.mark.asyncio
async def test_consultar_disponibilidad_de_una_fecha_pasada(monkeypatch):
    from app.agents.tools import calendar as cal

    async def fake_cfg():
        return _cfg()

    monkeypatch.setattr(cal, "get_schedule_config", fake_cfg)
    ayer = (datetime.now(MADRID) - timedelta(days=1)).strftime("%Y-%m-%d")
    out = json.loads(await cal.consultar_disponibilidad({"fecha": ayer}, {}))
    assert "error" in out
    assert "pasado" in out["error"]
