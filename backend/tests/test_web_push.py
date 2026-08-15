"""Tests de Web Push (notificaciones a la PWA del operador).

Cobertura:
  - generación de claves VAPID: longitudes correctas + la pública casa con la
    privada (round-trip por py_vapid). Puro, sin DB/Redis.
  - save_subscription hace upsert por endpoint (no duplica) y delete_subscription
    borra. (DB-gated)
  - send_to_all itera todas las suscripciones, y borra las expiradas (410) sin
    tocar las buenas. (DB-gated, con pywebpush y claves mockeadas → sin red ni Redis)
"""
from __future__ import annotations

import asyncio
import base64
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


def test_generate_vapid_keypair_roundtrip():
    """La clave pública generada corresponde a la privada (vía py_vapid)."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    from app.services.web_push import _generate_vapid_keypair

    pub, priv = _generate_vapid_keypair()
    # base64url sin padding: pública 65 bytes → 87 chars; privada 32 → 43.
    assert len(pub) == 87
    assert len(priv) == 43

    v = Vapid01.from_raw(priv.encode())
    derived = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert base64.urlsafe_b64encode(derived).rstrip(b"=").decode() == pub


async def _seed_user():
    from app.db.session import db_session
    from app.models.user import User, UserRole

    async with db_session() as db:
        u = User(
            email=f"op-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
            nombre="Operadora",
        )
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
        return uid


@pytestmark_db
@pytest.mark.asyncio
async def test_save_subscription_upserts_by_endpoint():
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.push_subscription import PushSubscription
    from app.services.web_push import delete_subscription, save_subscription

    uid = await _seed_user()
    endpoint = f"https://push.example/{uuid.uuid4().hex}"

    await save_subscription(uid, endpoint, "p256dh-A", "auth-A", "iPhone Safari")
    # Mismo endpoint → actualiza, no duplica.
    await save_subscription(uid, endpoint, "p256dh-B", "auth-B", "iPhone Safari")

    async with db_session() as db:
        rows = (
            await db.execute(
                select(PushSubscription).where(PushSubscription.endpoint == endpoint)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].p256dh == "p256dh-B"  # se quedó el último

    await delete_subscription(endpoint)
    async with db_session() as db:
        n = (
            await db.execute(
                select(func.count()).select_from(PushSubscription).where(
                    PushSubscription.endpoint == endpoint
                )
            )
        ).scalar_one()
    assert n == 0


@pytestmark_db
@pytest.mark.asyncio
async def test_send_to_all_cleans_expired(monkeypatch):
    """send_to_all envía a todas y borra solo las que devuelven 410."""
    from sqlalchemy import func, select

    import app.services.web_push as wp
    from app.db.session import db_session
    from app.models.push_subscription import PushSubscription

    uid = await _seed_user()
    good = f"https://push.example/good-{uuid.uuid4().hex}"
    dead = f"https://push.example/dead-{uuid.uuid4().hex}"
    await wp.save_subscription(uid, good, "k", "a", None)
    await wp.save_subscription(uid, dead, "k", "a", None)

    # Evita Redis (claves VAPID) y la red (pywebpush).
    async def _fake_keys():
        return "pub", "priv"

    def _fake_send_one(info, payload, private_key):
        return 410 if "dead-" in info["endpoint"] else None

    monkeypatch.setattr(wp, "ensure_vapid_keys", _fake_keys)
    monkeypatch.setattr(wp, "_send_one", _fake_send_one)

    await wp.send_to_all("Título", "Cuerpo", "/inbox")

    # La expirada se borró; la buena sigue.
    async with db_session() as db:
        n_dead = (
            await db.execute(
                select(func.count()).select_from(PushSubscription).where(
                    PushSubscription.endpoint == dead
                )
            )
        ).scalar_one()
        n_good = (
            await db.execute(
                select(func.count()).select_from(PushSubscription).where(
                    PushSubscription.endpoint == good
                )
            )
        ).scalar_one()
    assert n_dead == 0
    assert n_good == 1
