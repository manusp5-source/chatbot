"""Tests de la edición de documentos de la KB (notas, edición, versionado).

Cobertura:
  - create_note: crea un Document .txt indexado a partir de título + texto.
  - update_document_content: guarda la versión anterior, reescribe y reindexa
    SIN duplicar chunks (el indexador borra los antiguos).
  - list/restore versions: restaurar recupera el contenido y guarda el actual
    como versión nueva (restaurar también es reversible).
  - Formatos no editables (pdf) → 409.

DB-gated con skipif (mismo patrón que test_knowledge_gap.py). Los embeddings se
mockean (vectores deterministas) para no depender de la clave de OpenAI.
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
        email=f"kbtest-{uuid.uuid4().hex[:8]}@test.local",
        password_hash="x",
        nombre="KB Test",
        role="admin",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def test_kb_note_edit_restore_cycle():
    """Ciclo completo: nota → edición → historial → restauración → borrado."""
    from sqlalchemy import select

    import app.api.knowledge_base as kb
    from app.db.session import db_session
    from app.models.chunk import Chunk
    from app.models.document import DocumentVersion
    from app.models.user import User

    async def _run():
        async with db_session() as db:
            user = await _make_admin(db)
            user_id = user.id

        with patch("app.services.kb_indexer.embed_texts_with_status", new=AsyncMock(side_effect=_fake_embed)):
            # 1) Nota
            async with db_session() as db:
                u = await db.get(User, user_id)
                out = await kb.create_note(
                    kb.NoteIn(titulo="Envíos a Canarias", contenido="Tardan 5 días."),
                    db=db,
                    user=u,
                )
            assert out.status == "indexado", out.error_msg
            assert out.num_chunks >= 1
            doc_id = out.id

            # 2) Edición → versión + reindexado sin duplicados
            async with db_session() as db:
                u = await db.get(User, user_id)
                out2 = await kb.update_document_content(
                    doc_id, kb.DocumentContentIn(contenido="Tardan 3 días laborables."), db=db, user=u
                )
            assert out2.status == "indexado", out2.error_msg
            async with db_session() as db:
                chunks = (
                    await db.execute(select(Chunk).where(Chunk.document_id == doc_id))
                ).scalars().all()
                assert len(chunks) == out2.num_chunks  # sin chunks huérfanos del contenido viejo
                assert all("3 días" in c.contenido for c in chunks)

            # 3) Historial contiene el contenido anterior
            async with db_session() as db:
                u = await db.get(User, user_id)
                versions = await kb.list_document_versions(doc_id, db=db, _=u)
            assert len(versions) == 1
            assert "5 días" in versions[0].contenido

            # 4) Restaurar → contenido vuelve, la edición queda como versión
            async with db_session() as db:
                u = await db.get(User, user_id)
                await kb.restore_document_version(doc_id, versions[0].id, db=db, user=u)
            async with db_session() as db:
                u = await db.get(User, user_id)
                content = await kb.get_document_content(doc_id, db=db, _=u)
                versions2 = await kb.list_document_versions(doc_id, db=db, _=u)
            assert "5 días" in content.contenido
            assert len(versions2) == 2

            # 5) Borrado → CASCADE limpia versiones
            async with db_session() as db:
                u = await db.get(User, user_id)
                # `user` (antes `_`): el borrado ahora deja rastro en auditoría y
                # necesita saber quién lo hizo.
                await kb.delete_document(doc_id, db=db, user=u)
            async with db_session() as db:
                left = (
                    await db.execute(
                        select(DocumentVersion).where(DocumentVersion.document_id == doc_id)
                    )
                ).scalars().all()
                assert not left

        async with db_session() as db:
            u = await db.get(User, user_id)
            if u:
                await db.delete(u)
                await db.commit()

    asyncio.run(_run())


def test_kb_non_text_document_not_editable():
    """Un PDF no se edita desde el panel: content/update devuelven 409."""
    from fastapi import HTTPException

    import app.api.knowledge_base as kb
    from app.db.session import db_session
    from app.models.document import Document
    from app.models.user import User

    async def _run():
        async with db_session() as db:
            user = await _make_admin(db)
            user_id = user.id
            doc = Document(
                nombre="manual.pdf",
                formato="pdf",
                tamano_bytes=10,
                status="indexado",
                storage_path=None,
                uploaded_by=user_id,
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)
            doc_id = doc.id

        try:
            async with db_session() as db:
                u = await db.get(User, user_id)
                with pytest.raises(HTTPException) as exc:
                    await kb.get_document_content(doc_id, db=db, _=u)
                assert exc.value.status_code == 409
                with pytest.raises(HTTPException) as exc:
                    await kb.update_document_content(
                        doc_id, kb.DocumentContentIn(contenido="nuevo"), db=db, user=u
                    )
                assert exc.value.status_code == 409
        finally:
            async with db_session() as db:
                d = await db.get(Document, doc_id)
                if d:
                    await db.delete(d)
                u = await db.get(User, user_id)
                if u:
                    await db.delete(u)
                await db.commit()

    asyncio.run(_run())
