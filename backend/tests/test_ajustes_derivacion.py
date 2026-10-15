"""Ajustes de derivación desde el panel: SLA de respuesta humana y aviso de fuera de horario.

Hallazgo de la review del 14 oct: `handoff.sla_minutes` y `handoff.out_of_hours_notice` no
tenían ningún punto de escritura; eran «opt-in» solo por SQL directo. Aquí se prueban de punta a
punta contra Postgres real, con autenticación de verdad (admin sí, cliente no, sin token no).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


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

URL = "/api/v1/admin/settings/handoff"


def _client():
    import httpx

    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _usuario(role: str):
    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.models.user import User

    suf = uuid.uuid4().hex[:8]
    async with db_session() as db:
        u = User(
            email=f"ajustes-{suf}@test.local",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Ajustes",
            role=role,
        )
        db.add(u)
        await db.commit()
        uid = u.id
    return uid, create_access_token(subject=str(uid), extra={"role": role})


async def _limpiar(*uids) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    async with db_session() as db:
        for uid in uids:
            await db.execute(text("DELETE FROM audit_log WHERE user_id = :i"), {"i": str(uid)})
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(uid)})
        await db.execute(
            text("DELETE FROM app_settings WHERE key IN ('handoff.sla_minutes', 'handoff.out_of_hours_notice')")
        )
        await db.commit()


@pytestmark_db
def test_sin_token_y_con_rol_cliente_no_se_pueden_ver_ni_cambiar() -> None:
    async def _run():
        uid, token = await _usuario("cliente")
        try:
            async with _client() as c:
                sin = await c.get(URL)
                cli_get = await c.get(URL, headers={"Authorization": f"Bearer {token}"})
                cli_put = await c.put(
                    URL,
                    json={"sla_minutes": 15, "out_of_hours_notice": True},
                    headers={"Authorization": f"Bearer {token}"},
                )
            return sin.status_code, cli_get.status_code, cli_put.status_code
        finally:
            await _limpiar(uid)

    assert asyncio.run(_run()) == (401, 403, 403)


@pytestmark_db
def test_el_admin_activa_los_ajustes_y_la_logica_los_lee() -> None:
    from app.db.session import db_session
    from app.services.app_settings import get_app_setting
    from app.services.handoff_sla import (
        SETTING_AVISO_FUERA_DE_HORARIO,
        SETTING_SLA_MINUTES,
        aviso_fuera_de_horario_activo,
        sla_minutos,
    )

    async def _run():
        uid, token = await _usuario("admin")
        h = {"Authorization": f"Bearer {token}"}
        try:
            async with _client() as c:
                por_defecto = (await c.get(URL, headers=h)).json()
                put = await c.put(URL, json={"sla_minutes": 20, "out_of_hours_notice": True}, headers=h)
                despues = (await c.get(URL, headers=h)).json()
            async with db_session() as db:
                sla = sla_minutos(await get_app_setting(SETTING_SLA_MINUTES, "0", db))
                aviso = aviso_fuera_de_horario_activo(
                    await get_app_setting(SETTING_AVISO_FUERA_DE_HORARIO, "", db)
                )
            return por_defecto, put.status_code, despues, sla, aviso
        finally:
            await _limpiar(uid)

    por_defecto, put, despues, sla, aviso = asyncio.run(_run())

    assert por_defecto == {"sla_minutes": 0, "out_of_hours_notice": False}  # apagado por defecto
    assert put == 200
    assert despues == {"sla_minutes": 20, "out_of_hours_notice": True}
    assert sla == 20 and aviso is True  # la tarea y el puente leen lo que escribió el panel


@pytestmark_db
@pytest.mark.parametrize("sla", [-1, 1441, "abc"])
def test_un_sla_fuera_de_rango_se_rechaza_en_vez_de_corregirse_en_silencio(sla) -> None:
    async def _run():
        uid, token = await _usuario("admin")
        try:
            async with _client() as c:
                r = await c.put(
                    URL,
                    json={"sla_minutes": sla, "out_of_hours_notice": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
            return r.status_code
        finally:
            await _limpiar(uid)

    assert asyncio.run(_run()) == 422


@pytestmark_db
def test_dashboard_quality_exige_admin_y_devuelve_la_forma_esperada() -> None:
    """Autorización de `GET /dashboard/quality`: sin token 401, cliente 403, admin 200."""
    url = "/api/v1/admin/dashboard/quality"

    async def _run():
        uid_c, tok_c = await _usuario("cliente")
        uid_a, tok_a = await _usuario("admin")
        try:
            async with _client() as c:
                sin = await c.get(url)
                cli = await c.get(url, headers={"Authorization": f"Bearer {tok_c}"})
                adm = await c.get(url, headers={"Authorization": f"Bearer {tok_a}"})
            return sin.status_code, cli.status_code, adm.status_code, adm.json()
        finally:
            await _limpiar(uid_c, uid_a)

    sin, cli, adm, cuerpo = asyncio.run(_run())

    assert (sin, cli, adm) == (401, 403, 200)
    assert set(cuerpo) >= {"conversaciones", "huecos_kb", "huecos_kb_por_100", "primera_respuesta_humana"}
    assert set(cuerpo["primera_respuesta_humana"]) == {
        "derivaciones", "respondidas", "sin_respuesta", "mediana_min", "p90_min",
    }
