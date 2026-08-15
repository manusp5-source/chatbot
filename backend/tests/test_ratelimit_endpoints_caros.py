"""Lo que cuesta dinero tiene freno propio (B2).

Antes solo tenían límite ocho sitios: autenticación, widget web y webhooks.
Todo lo demás — incluida la re-redacción de un borrador, que es una llamada
COMPLETA al modelo con el historial de la conversación — se podía llamar en
bucle desde cualquier sesión válida. El único freno era el tope mensual global,
que al saltar pausa el bot para TODOS los clientes.

Aquí se comprueba por HTTP que el cupo diario por usuario corta ANTES de tocar
el modelo, y que la API tiene un techo por defecto.
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


def test_la_api_tiene_techo_por_defecto():
    """Red de seguridad para las rutas sin límite propio."""
    from app.core.ratelimit import DEFAULT_API_RATE_LIMIT, make_limiter

    assert DEFAULT_API_RATE_LIMIT, "no hay techo por defecto para la API"
    limiter = make_limiter(default_limits=[DEFAULT_API_RATE_LIMIT])
    assert limiter._default_limits, "el limiter se creó sin límite por defecto"

    from app.main import app

    assert app.state.limiter._default_limits, (
        "la app monta el limiter sin techo por defecto: una ruta sin decorador "
        "se puede llamar en bucle sin freno"
    )


@pytest.mark.skipif(not _db_available(), reason="DB no disponible en este entorno")
def test_el_cupo_diario_corta_la_re_redaccion_antes_de_llamar_al_modelo(monkeypatch):
    import httpx

    from app.api import conversations as conv_api
    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.main import app
    from app.models.user import User

    # Cupo de 1 al día para este test: la segunda llamada tiene que cortarse.
    monkeypatch.setattr(conv_api, "REFINE_DAILY_LIMIT_PER_USER", 1)

    async def _run():
        suf = uuid.uuid4().hex[:8]
        async with db_session() as db:
            u = User(
                email=f"refine-{suf}@test.local",
                password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
                nombre="Operador",
                role="cliente",
            )
            db.add(u)
            await db.commit()
            uid = u.id
        token = create_access_token(subject=str(uid), extra={"role": "cliente"})
        headers = {"Authorization": f"Bearer {token}"}
        url = f"/api/v1/conversations/{uuid.uuid4()}/drafts/{uuid.uuid4()}/refine"
        body = {"instruction": "más corto"}
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                # 1ª: consume el cupo y sigue (llega al 404 del borrador que no
                # existe, sin haber llamado al modelo).
                r1 = await c.post(url, headers=headers, json=body)
                assert r1.status_code == 404, r1.text
                # 2ª: cortada por el cupo, ANTES de mirar siquiera el borrador.
                r2 = await c.post(url, headers=headers, json=body)
                assert r2.status_code == 429, (
                    "el cupo diario por usuario no corta: una sesión válida puede "
                    f"quemar el presupuesto en bucle (→ {r2.status_code})"
                )
                assert "tope diario" in r2.text
        finally:
            from sqlalchemy import text

            from app.core.redis import get_redis

            async with db_session() as db:
                await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(uid)})
                await db.commit()
            try:
                from datetime import datetime, timezone

                ymd = datetime.now(timezone.utc).strftime("%Y%m%d")
                await get_redis().delete(f"userquota:refine:{uid}:{ymd}")
            except Exception:
                pass

    asyncio.run(_run())
