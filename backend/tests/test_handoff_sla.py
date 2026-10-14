"""Escalado por tiempo de derivaciones sin respuesta y aviso de fuera de horario."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.tools.schedule_config import build_config
from app.services.handoff_sla import (
    TEXTO_FUERA_DE_HORARIO,
    aviso_fuera_de_horario_activo,
    sla_minutos,
)

MADRID = ZoneInfo("Europe/Madrid")


def _cfg():
    return build_config("Europe/Madrid", "1,2,3,4,5", "09:00-18:00", "", "30", "60")


# --- ajustes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valor, esperado",
    [(None, 0), ("", 0), ("abc", 0), ("-5", 0), ("0", 0), ("15", 15), (" 30 ", 30)],
)
def test_el_sla_mal_configurado_deja_el_escalado_apagado(valor, esperado) -> None:
    assert sla_minutos(valor) == esperado


@pytest.mark.parametrize(
    "valor, esperado",
    [(None, False), ("", False), ("0", False), ("1", True), ("true", True), ("Sí", True)],
)
def test_el_aviso_fuera_de_horario_es_opt_in(valor, esperado) -> None:
    assert aviso_fuera_de_horario_activo(valor) is esperado


# --- horario ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "momento, dentro",
    [
        (datetime(2026, 10, 14, 10, 0, tzinfo=MADRID), True),  # miércoles, mañana
        (datetime(2026, 10, 14, 9, 0, tzinfo=MADRID), True),  # justo al abrir
        (datetime(2026, 10, 14, 18, 0, tzinfo=MADRID), False),  # justo al cerrar
        (datetime(2026, 10, 14, 3, 0, tzinfo=MADRID), False),  # madrugada
        (datetime(2026, 10, 17, 11, 0, tzinfo=MADRID), False),  # sábado
        # Misma hora UTC que las 10:00 de Madrid en octubre: la zona importa.
        (datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 14, 17, 30, tzinfo=timezone.utc), False),  # 19:30 en Madrid
    ],
)
def test_dentro_de_horario_respeta_dias_tramos_y_zona(momento, dentro) -> None:
    assert _cfg().dentro_de_horario(momento) is dentro


def test_un_festivo_cuenta_como_fuera_de_horario() -> None:
    cfg = build_config("Europe/Madrid", "1,2,3,4,5", "09:00-18:00", "2026-10-14", "30", "60")

    assert cfg.dentro_de_horario(datetime(2026, 10, 14, 10, 0, tzinfo=MADRID)) is False


# --- aviso de fuera de horario en el mensaje puente ------------------------------------------


async def _sufijo(*, activo: bool, momento: datetime) -> str:
    from app.agents.tools import human_handoff as hh

    cfg = _cfg()
    with patch(
        "app.services.app_settings.get_app_setting",
        AsyncMock(return_value="1" if activo else ""),
    ), patch(
        "app.agents.tools.schedule_config.get_schedule_config", AsyncMock(return_value=cfg)
    ), patch("app.agents.tools.schedule_config.now_in", lambda _tz: momento):
        return await hh._aviso_fuera_de_horario()


@pytest.mark.asyncio
async def test_sin_activar_el_mensaje_puente_no_cambia_nunca() -> None:
    assert await _sufijo(activo=False, momento=datetime(2026, 10, 14, 3, 0, tzinfo=MADRID)) == ""


@pytest.mark.asyncio
async def test_activado_y_con_el_negocio_cerrado_se_avisa() -> None:
    sufijo = await _sufijo(activo=True, momento=datetime(2026, 10, 14, 3, 0, tzinfo=MADRID))

    assert sufijo == TEXTO_FUERA_DE_HORARIO


@pytest.mark.asyncio
async def test_activado_pero_en_horario_no_se_dice_nada() -> None:
    assert await _sufijo(activo=True, momento=datetime(2026, 10, 14, 10, 0, tzinfo=MADRID)) == ""


@pytest.mark.asyncio
async def test_si_falla_la_lectura_del_horario_la_derivacion_sigue_sin_frase_extra() -> None:
    from app.agents.tools import human_handoff as hh

    with patch(
        "app.services.app_settings.get_app_setting",
        AsyncMock(side_effect=RuntimeError("base caída")),
    ):
        assert await hh._aviso_fuera_de_horario() == ""


# --- la tarea: un solo aviso por derivación --------------------------------------------------


class _RedisFalso:
    def __init__(self) -> None:
        self.claves: set[str] = set()

    async def set(self, clave, _valor, nx=False, ex=None):
        if nx and clave in self.claves:
            return None
        self.claves.add(clave)
        return True


async def _ejecutar_tarea(*, sla: str, vencidas: list[dict], redis: _RedisFalso):
    from app.tasks import escalate_handoffs as t

    avisos = AsyncMock()
    contexto = MagicMock()
    contexto.__aenter__ = AsyncMock(return_value=MagicMock())
    contexto.__aexit__ = AsyncMock(return_value=False)
    with patch("app.services.app_settings.get_app_setting", AsyncMock(return_value=sla)), patch(
        "app.db.session.db_session", lambda: contexto
    ), patch(
        "app.services.handoff_sla.derivaciones_vencidas", AsyncMock(return_value=vencidas)
    ), patch("app.core.redis.get_redis", lambda: redis), patch(
        "app.services.web_push.notify", avisos
    ), patch("app.services.runtime_logs.push_runtime_log", AsyncMock()):
        avisadas = await t._escalar()
    return avisadas, avisos


def _vencida(minutos: int = 30) -> dict:
    derivada = datetime.now(timezone.utc) - timedelta(minutes=minutos)
    return {
        "conversation_id": str(uuid.uuid4()),
        "canal": "whatsapp",
        "derivada": derivada,
        "esperando_min": minutos,
    }


@pytest.mark.asyncio
async def test_con_el_sla_apagado_la_tarea_no_hace_nada() -> None:
    avisadas, avisos = await _ejecutar_tarea(sla="0", vencidas=[_vencida()], redis=_RedisFalso())

    assert avisadas == 0
    avisos.assert_not_awaited()


@pytest.mark.asyncio
async def test_avisa_una_sola_vez_por_derivacion_aunque_la_tarea_corra_otra_vez() -> None:
    redis = _RedisFalso()
    vencidas = [_vencida()]

    primera, avisos1 = await _ejecutar_tarea(sla="15", vencidas=vencidas, redis=redis)
    segunda, avisos2 = await _ejecutar_tarea(sla="15", vencidas=vencidas, redis=redis)

    assert primera == 1 and segunda == 0
    avisos1.assert_awaited_once()
    avisos2.assert_not_awaited()


# --- SQL contra Postgres real ------------------------------------------------------------------


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


@pytestmark_db
def test_derivaciones_vencidas_contra_la_base_de_datos_real() -> None:
    """Cuatro conversaciones: vencida sin respuesta, vencida con respuesta, reciente, y en bot."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.services.handoff_sla import derivaciones_vencidas

    marca = uuid.uuid4().hex[:8]
    ahora = datetime.now(timezone.utc)

    async def _run():
        async with db_session() as db:
            ids = {}
            for nombre, estado, hace_min in [
                ("vencida", ConversationStatus.humano, 60),
                ("respondida", ConversationStatus.humano, 60),
                ("reciente", ConversationStatus.humano, 3),
                ("en_bot", ConversationStatus.bot, 60),
            ]:
                c = Contact(telefono=f"web:sla-{marca}-{nombre}", origen=ContactOrigen.web, in_crm=False)
                db.add(c)
                await db.flush()
                conv = Conversation(
                    contact_id=c.id,
                    canal=ConversationCanal.web,
                    session_id=uuid.uuid4().hex,
                    status=estado,
                    derivada_a_humano_at=ahora - timedelta(minutes=hace_min),
                )
                db.add(conv)
                await db.flush()
                ids[nombre] = str(conv.id)
            m = Message(
                conversation_id=uuid.UUID(ids["respondida"]),
                rol=MessageRole.operator,
                contenido="Ya voy",
            )
            db.add(m)
            await db.flush()
            m.created_at = ahora - timedelta(minutes=40)
            await db.commit()

        try:
            async with db_session() as db:
                vencidas = await derivaciones_vencidas(db, 15, ahora, limite=500)
        finally:
            async with db_session() as db:
                for c in (
                    await db.execute(
                        select(Contact).where(Contact.telefono.like(f"web:sla-{marca}-%"))
                    )
                ).scalars():
                    await db.delete(c)
                await db.commit()
        return ids, vencidas

    ids, vencidas = asyncio.run(_run())
    encontradas = {v["conversation_id"] for v in vencidas}

    assert ids["vencida"] in encontradas
    assert ids["respondida"] not in encontradas  # un humano ya contestó
    assert ids["reciente"] not in encontradas  # aún dentro del plazo
    assert ids["en_bot"] not in encontradas  # no está esperando a una persona
    esperando = next(v for v in vencidas if v["conversation_id"] == ids["vencida"])["esperando_min"]
    assert 59 <= esperando <= 61
