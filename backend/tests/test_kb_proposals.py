"""Tests de las propuestas de KB del agente interno (M16 fase 2).

El agente interno solo INSERTA propuestas (propose_kb_update); aplicar y
descartar son endpoints admin. Cobertura:
  - propose create → apply → documento indexado; doble apply → 409.
  - propose edit → apply → contenido reemplazado + versión anterior guardada.
  - discard no aplica nada.
  - Validaciones de la tool: pdf no editable, crear sin título, contenido vacío.

DB-gated con skipif (patrón test_knowledge_gap.py). Embeddings mockeados.
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


async def _make_admin(db):
    from app.models.user import User

    user = User(
        email=f"kbprop-{uuid.uuid4().hex[:8]}@test.local",
        password_hash="x",
        nombre="KB Prop",
        role="admin",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def test_kb_proposal_full_cycle():
    from fastapi import HTTPException
    from sqlalchemy import select

    import app.api.admin as admin
    import app.api.knowledge_base as kb
    from app.agents.internal.tools import propose_kb_update
    from app.db.session import db_session
    from app.models.document import Document, DocumentVersion
    from app.models.kb_edit_proposal import KBEditProposal
    from app.models.user import User

    async def _run():
        async with db_session() as db:
            user = await _make_admin(db)
            user_id = user.id

        created_doc_id = None
        proposal_ids: list[uuid.UUID] = []
        try:
            with patch(
                "app.services.kb_indexer.embed_texts_with_status", new=AsyncMock(side_effect=_fake_embed)
            ):
                # propose CREATE → apply → doc indexado
                async with db_session() as db:
                    r = await propose_kb_update(
                        {"titulo": "Horario", "contenido": "De 9 a 18h.", "motivo": "test"},
                        {"db": db, "actor_user_id": user_id},
                    )
                assert r.get("ok") and r["__proposal__"]["kind"] == "create"
                pid = uuid.UUID(r["proposal_id"])
                proposal_ids.append(pid)
                async with db_session() as db:
                    u = await db.get(User, user_id)
                    out = await admin.apply_kb_proposal(pid, db=db, me=u)
                assert out.status == "aplicada" and out.applied_document_id
                created_doc_id = out.applied_document_id
                async with db_session() as db:
                    doc = await db.get(Document, created_doc_id)
                    assert doc.status.value == "indexado" and doc.num_chunks >= 1

                # doble apply → 409
                async with db_session() as db:
                    u = await db.get(User, user_id)
                    with pytest.raises(HTTPException) as exc:
                        await admin.apply_kb_proposal(pid, db=db, me=u)
                    assert exc.value.status_code == 409

                # propose EDIT → apply → contenido nuevo + versión
                async with db_session() as db:
                    r2 = await propose_kb_update(
                        {"document_id": str(created_doc_id), "contenido": "De 9 a 20h."},
                        {"db": db, "actor_user_id": user_id},
                    )
                assert r2.get("ok") and r2["__proposal__"]["kind"] == "edit"
                pid2 = uuid.UUID(r2["proposal_id"])
                proposal_ids.append(pid2)
                async with db_session() as db:
                    u = await db.get(User, user_id)
                    out2 = await admin.apply_kb_proposal(pid2, db=db, me=u)
                assert out2.status == "aplicada"
                async with db_session() as db:
                    u = await db.get(User, user_id)
                    content = await kb.get_document_content(created_doc_id, db=db, _=u)
                    versions = (
                        await db.execute(
                            select(DocumentVersion).where(
                                DocumentVersion.document_id == created_doc_id
                            )
                        )
                    ).scalars().all()
                assert "20h" in content.contenido
                assert len(versions) == 1 and "18h" in versions[0].contenido

                # discard → nada aplicado
                async with db_session() as db:
                    r3 = await propose_kb_update(
                        {"titulo": "Basura", "contenido": "no aplicar"},
                        {"db": db, "actor_user_id": user_id},
                    )
                pid3 = uuid.UUID(r3["proposal_id"])
                proposal_ids.append(pid3)
                async with db_session() as db:
                    u = await db.get(User, user_id)
                    out3 = await admin.discard_kb_proposal(pid3, db=db, me=u)
                assert out3.status == "descartada"
        finally:
            async with db_session() as db:
                for pid_ in proposal_ids:
                    p = await db.get(KBEditProposal, pid_)
                    if p:
                        await db.delete(p)
                await db.commit()
            async with db_session() as db:
                if created_doc_id:
                    d = await db.get(Document, created_doc_id)
                    if d:
                        await db.delete(d)
                u = await db.get(User, user_id)
                if u:
                    await db.delete(u)
                await db.commit()

    asyncio.run(_run())


def test_kb_proposal_tool_validations():
    from sqlalchemy import select

    from app.agents.internal.tools import propose_kb_update
    from app.db.session import db_session
    from app.models.document import Document
    from app.models.kb_edit_proposal import KBEditProposal
    from app.models.user import User

    async def _run():
        async with db_session() as db:
            user = await _make_admin(db)
            user_id = user.id

        try:
            async with db_session() as db:
                pdf = Document(
                    nombre="m.pdf",
                    formato="pdf",
                    tamano_bytes=1,
                    status="indexado",
                    storage_path=None,
                    uploaded_by=user_id,
                )
                db.add(pdf)
                await db.commit()
                await db.refresh(pdf)
                pdf_id = pdf.id

                ctx = {"db": db, "actor_user_id": user_id}
                r = await propose_kb_update({"document_id": str(pdf_id), "contenido": "x"}, ctx)
                assert "error" in r and "txt/md" in r["error"]
                r = await propose_kb_update({"contenido": "sin titulo"}, ctx)
                assert "error" in r
                r = await propose_kb_update({"titulo": "t", "contenido": "  "}, ctx)
                assert "error" in r
                r = await propose_kb_update({"document_id": "no-uuid", "contenido": "x"}, ctx)
                assert "error" in r
                # Ninguna validación fallida debe haber creado propuestas.
                pendientes = (
                    await db.execute(
                        select(KBEditProposal).where(KBEditProposal.created_by == user_id)
                    )
                ).scalars().all()
                assert not pendientes
        finally:
            async with db_session() as db:
                d = await db.get(Document, pdf_id)
                if d:
                    await db.delete(d)
                u = await db.get(User, user_id)
                if u:
                    await db.delete(u)
                await db.commit()

    asyncio.run(_run())
