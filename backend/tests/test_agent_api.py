"""Tests de la API para agentes externos (M16 fase 3, /api/v1/agent-api).

Cobertura (vía ASGI real con httpx, DB-gated):
  - Auth: sin token / token falso → 401; token revocado → 401 inmediato.
  - Ámbitos: token sin el scope requerido → 403 (lectura no puede escribir).
  - KB: crear (indexa), leer, editar (guarda versión + reindexa).
  - Prompts: editar guarda el anterior en agent_prompt_history.
  - El token se guarda hasheado (el valor en claro no está en la BD).
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

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


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


async def _fake_embed(texts):
    # El indexador pide vectores + si son REALES: `ok=False` significa "esto son
    # ceros de relleno" y marca el documento como indexado sin semántica.
    from app.providers.embeddings import EmbeddingResult

    return EmbeddingResult(vectors=[[0.01] * 1536 for _ in texts], ok=True)


def test_agent_api_auth_scopes_and_flows():
    import httpx
    from sqlalchemy import select

    import app.api.admin as admin
    from app.db.session import db_session
    from app.main import app
    from app.models.agent import Agent
    from app.models.agent_prompt_history import AgentPromptHistory
    from app.models.agent_token import AgentToken
    from app.models.document import Document
    from app.models.user import User

    async def _run():
        async with db_session() as db:
            user = User(
                email=f"apitest-{uuid.uuid4().hex[:8]}@test.local",
                password_hash="x",
                nombre="API Test",
                role="admin",
            )
            db.add(user)
            await db.flush()
            agent = Agent(name="Agente API", prompt_system="Eres amable.", created_by=user.id)
            db.add(agent)
            await db.commit()
            await db.refresh(user)
            await db.refresh(agent)
            user_id, agent_id = user.id, agent.id

        async with db_session() as db:
            u = await db.get(User, user_id)
            full = await admin.create_agent_token(
                admin.AgentTokenCreateIn(
                    name="Asistente", scopes=["kb:read", "kb:write", "prompts:write"]
                ),
                db=db,
                me=u,
            )
            ro = await admin.create_agent_token(
                admin.AgentTokenCreateIn(name="RO", scopes=["kb:read"]), db=db, me=u
            )

        # El token en claro NUNCA está en la BD (solo su hash).
        async with db_session() as db:
            t = await db.get(AgentToken, full.id)
            assert t.token_hash != full.token and full.token not in (t.token_hash or "")
            assert len(t.token_hash) == 64  # sha256 hex

        doc_id = None
        try:
            with patch(
                "app.services.kb_indexer.embed_texts_with_status", new=AsyncMock(side_effect=_fake_embed)
            ):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                    base = "/api/v1/agent-api"
                    H = {"Authorization": f"Bearer {full.token}"}

                    # 401 sin token / token falso
                    assert (await client.get(f"{base}/kb/documents")).status_code == 401
                    assert (
                        await client.get(
                            f"{base}/kb/documents",
                            headers={"Authorization": "Bearer agt_falso"},
                        )
                    ).status_code == 401

                    # 403 sin ámbito (kb:read no puede escribir; full no tiene monitor)
                    assert (
                        await client.get(
                            f"{base}/monitor/overview", headers=H
                        )
                    ).status_code == 403

                    # KB: crear → leer → editar
                    r = await client.post(
                        f"{base}/kb/documents",
                        headers=H,
                        json={"titulo": "Prueba", "contenido": "Envíos en 48 horas."},
                    )
                    assert r.status_code == 200, r.text
                    doc_id = r.json()["document_id"]
                    assert r.json()["status"] == "indexado"

                    r = await client.get(f"{base}/kb/documents/{doc_id}", headers=H)
                    assert "48 horas" in r.json()["contenido"]

                    r = await client.put(
                        f"{base}/kb/documents/{doc_id}",
                        headers=H,
                        json={"contenido": "Envíos en 24 horas."},
                    )
                    assert r.status_code == 200, r.text

                    # RO no puede escribir
                    r = await client.put(
                        f"{base}/kb/documents/{doc_id}",
                        headers={"Authorization": f"Bearer {ro.token}"},
                        json={"contenido": "hack"},
                    )
                    assert r.status_code == 403

                    # Prompts: editar guarda historial
                    r = await client.put(
                        f"{base}/agents/{agent_id}/prompt",
                        headers=H,
                        json={"prompt_system": "Eres amable y breve."},
                    )
                    assert r.status_code == 200, r.text
                    async with db_session() as db:
                        hist = (
                            await db.execute(
                                select(AgentPromptHistory).where(
                                    AgentPromptHistory.agent_id == agent_id
                                )
                            )
                        ).scalars().all()
                        assert len(hist) == 1 and hist[0].prompt_system == "Eres amable."

                    # Revocar → 401 inmediato
                    async with db_session() as db:
                        u = await db.get(User, user_id)
                        await admin.revoke_agent_token(full.id, db=db, me=u)
                    assert (
                        await client.get(f"{base}/kb/documents", headers=H)
                    ).status_code == 401
        finally:
            async with db_session() as db:
                for tid in (full.id, ro.id):
                    t = await db.get(AgentToken, tid)
                    if t:
                        await db.delete(t)
                if doc_id:
                    d = await db.get(Document, uuid.UUID(doc_id))
                    if d:
                        await db.delete(d)
                a = await db.get(Agent, agent_id)
                if a:
                    await db.delete(a)
                u = await db.get(User, user_id)
                if u:
                    await db.delete(u)
                await db.commit()

    asyncio.run(_run())


def test_agent_api_operational_visibility():
    """Visibilidad operativa para agentes externos (interactions + monitor).

    Cubre:
      - 401 sin token; 403 con token sin el ámbito requerido.
      - GET /interactions: lista SOLO metadata (contact como UUID, sin nombre,
        teléfono ni contenido); filtro por status/canal; status inválido → 400.
      - GET /interactions/{id}/metadata: detalle limpio + conteos por rol; 404.
      - GET /monitor/llm-costs, /monitor/channels, /monitor/health con
        monitor:read.
    """
    import httpx

    import app.api.admin as admin
    from app.db.session import db_session
    from app.main import app
    from app.models.agent_token import AgentToken
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.models.user import User

    async def _run():
        suffix = uuid.uuid4().hex[:8]
        async with db_session() as db:
            user = User(
                email=f"apitest-{suffix}@test.local",
                password_hash="x",
                nombre="API Test",
                role="admin",
            )
            db.add(user)
            await db.flush()
            contact = Contact(
                telefono=f"+3460000{suffix}",
                nombre="Cliente Prueba",
                origen=ContactOrigen.whatsapp,
            )
            db.add(contact)
            await db.flush()
            conv = Conversation(
                contact_id=contact.id,
                canal=ConversationCanal.whatsapp,
                session_id=f"test-{suffix}",
                status=ConversationStatus.bot,
            )
            db.add(conv)
            await db.flush()
            db.add(
                Message(
                    conversation_id=conv.id,
                    rol=MessageRole.user,
                    contenido="Hola, quiero información",
                )
            )
            db.add(
                Message(
                    conversation_id=conv.id,
                    rol=MessageRole.assistant,
                    contenido="¡Hola! Claro, dime.",
                )
            )
            await db.commit()
            user_id, contact_id, conv_id = user.id, contact.id, conv.id

        async with db_session() as db:
            u = await db.get(User, user_id)
            ops = await admin.create_agent_token(
                admin.AgentTokenCreateIn(
                    name="Ops", scopes=["interactions:read", "monitor:read"]
                ),
                db=db,
                me=u,
            )
            kb_only = await admin.create_agent_token(
                admin.AgentTokenCreateIn(name="KB", scopes=["kb:read"]), db=db, me=u
            )

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                base = "/api/v1/agent-api"
                H = {"Authorization": f"Bearer {ops.token}"}
                H_KB = {"Authorization": f"Bearer {kb_only.token}"}

                # 401 sin token / 403 sin ámbito (kb:read no ve interacciones ni salud).
                assert (await client.get(f"{base}/interactions")).status_code == 401
                for path in (
                    "/interactions",
                    f"/interactions/{conv_id}/metadata",
                    "/monitor/llm-costs",
                    "/monitor/channels",
                    "/monitor/health",
                ):
                    assert (
                        await client.get(f"{base}{path}", headers=H_KB)
                    ).status_code == 403, path
                # Y el token operativo NO puede tocar la KB (sin kb:read).
                assert (
                    await client.get(f"{base}/kb/documents", headers=H)
                ).status_code == 403

                # Lista: solo metadata, contacto SOLO como UUID.
                #
                # Se filtra por `contact_id` (el contacto se crea aquí y es
                # único) en vez de rebuscar la conversación dentro de las N
                # primeras: con una base que ya tenga conversaciones, la de
                # este test no entra en el `limit` (nace sin last_message_at, o
                # sea la última del orden) y el test reventaba con un
                # StopIteration que no decía nada.
                r = await client.get(
                    f"{base}/interactions",
                    headers=H,
                    params={
                        "status": "bot",
                        "canal": "whatsapp",
                        "contact_id": str(contact_id),
                        "limit": 50,
                    },
                )
                assert r.status_code == 200, r.text
                items = r.json()["interactions"]
                encontradas = [i for i in items if i["id"] == str(conv_id)]
                assert encontradas, f"la conversación {conv_id} no aparece en el listado"
                mine = encontradas[0]
                assert mine["canal"] == "whatsapp" and mine["status"] == "bot"
                assert mine["contact_id"] == str(contact_id)
                assert mine["assigned"] is False
                assert isinstance(mine["seconds_in_status"], int)
                # Nada de PII ni contenido en NINGÚN item.
                serialized = r.text.lower()
                assert "cliente prueba" not in serialized
                assert "+3460000" not in r.text
                for it in items:
                    for prohibido in (
                        "contact_name", "contact_phone_masked", "telefono",
                        "nombre", "contenido", "resumen", "subject",
                    ):
                        assert prohibido not in it, prohibido

                # Filtro inválido → 400.
                assert (
                    await client.get(
                        f"{base}/interactions", headers=H, params={"status": "hackeada"}
                    )
                ).status_code == 400

                # Detalle: metadata + conteos por rol; sin contenido.
                r = await client.get(
                    f"{base}/interactions/{conv_id}/metadata", headers=H
                )
                assert r.status_code == 200, r.text
                meta = r.json()
                assert meta["message_count"] == 2
                assert meta["messages_by_rol"] == {"user": 1, "assistant": 1}
                assert meta["archived"] is False and meta["quarantined"] is False
                assert "información" not in r.text and "contenido" not in meta
                # 404 con un UUID inexistente.
                assert (
                    await client.get(
                        f"{base}/interactions/{uuid.uuid4()}/metadata", headers=H
                    )
                ).status_code == 404

                # Costes LLM: misma forma que el dashboard admin.
                r = await client.get(
                    f"{base}/monitor/llm-costs", headers=H, params={"range": "7d"}
                )
                assert r.status_code == 200, r.text
                body = r.json()
                for key in ("total_cost_usd", "by_agent", "by_model", "series"):
                    assert key in body, key

                # Canales: uno por canal conocido, con paused/last_inbound_at.
                r = await client.get(f"{base}/monitor/channels", headers=H)
                assert r.status_code == 200, r.text
                body = r.json()
                canales = {c["canal"]: c for c in body["channels"]}
                assert "whatsapp" in canales and "paused" in canales["whatsapp"]
                assert "last_inbound_at" in canales["whatsapp"]
                assert "recent_errors" in body

                # Salud: BD ok (estamos usándola) + estructura de colas.
                r = await client.get(f"{base}/monitor/health", headers=H)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["db"] == "ok"
                assert "celery_backlog" in body["queues"]
                assert "outbound_jobs_pending" in body["queues"]
        finally:
            async with db_session() as db:
                for tid in (ops.id, kb_only.id):
                    t = await db.get(AgentToken, tid)
                    if t:
                        await db.delete(t)
                c = await db.get(Conversation, conv_id)
                if c:
                    await db.delete(c)
                ct = await db.get(Contact, contact_id)
                if ct:
                    await db.delete(ct)
                u = await db.get(User, user_id)
                if u:
                    await db.delete(u)
                await db.commit()

    asyncio.run(_run())


def test_agent_token_invalid_scopes_rejected():
    from fastapi import HTTPException

    import app.api.admin as admin
    from app.db.session import db_session
    from app.models.user import User

    async def _run():
        async with db_session() as db:
            user = User(
                email=f"apitest-{uuid.uuid4().hex[:8]}@test.local",
                password_hash="x",
                nombre="API Test",
                role="admin",
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            try:
                with pytest.raises(HTTPException) as exc:
                    await admin.create_agent_token(
                        admin.AgentTokenCreateIn(name="X", scopes=["admin:all"]),
                        db=db,
                        me=user,
                    )
                assert exc.value.status_code == 400
            finally:
                await db.delete(user)
                await db.commit()

    asyncio.run(_run())
