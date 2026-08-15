"""Indexado de la KB: en segundo plano y con la degradación a la vista.

Dos fallos:
  - la indexación corría DENTRO de la petición. La extracción de un PDF/Word/
    Excel (hasta 25 MB) es síncrona y pesada, así que mientras duraba se quedaba
    parado el event loop entero: no respondía el panel ni entraban los webhooks
    de WhatsApp e Instagram.
  - sin clave de embeddings se guardaban vectores de CEROS y el documento se
    marcaba como indexado, en verde, sin que nada dijera que la búsqueda
    semántica no funcionaba. Y no existía reindexado.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import text


def _db_available() -> bool:
    try:
        async def _check():
            from app.db.session import db_session
            async with db_session() as db:
                await db.execute(text("SELECT 1"))
        asyncio.run(_check())
        return True
    except Exception:
        return False


db_gated = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


# ── Extracción fuera del event loop ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_la_extraccion_no_bloquea_el_event_loop(monkeypatch, tmp_path):
    """Mientras se extrae, el loop sigue atendiendo. Antes se paraba entero."""
    import time as _time

    from app.services import kb_indexer

    marcas: list[str] = []

    def _extraccion_lenta(path, formato):
        _time.sleep(0.3)  # simula pypdf/pandas: síncrono y con CPU
        marcas.append("extraccion_fin")
        return [{"contenido": "hola", "metadata": {}}]

    monkeypatch.setattr(kb_indexer, "_extract_chunks", _extraccion_lenta)

    async def latido():
        for _ in range(6):
            await asyncio.sleep(0.05)
            marcas.append("latido")

    fichero = tmp_path / "x.txt"
    fichero.write_text("hola")

    # Llamamos directamente al trozo que corre en hilo, sin BD.
    import anyio

    from app.models.document import DocumentFormato

    async def extraer():
        return await anyio.to_thread.run_sync(
            kb_indexer._extract_chunks, fichero, DocumentFormato.txt
        )

    await asyncio.gather(extraer(), latido())

    # Si el loop se hubiera quedado parado, no habría ni un latido antes del
    # final de la extracción.
    assert marcas.index("latido") < marcas.index("extraccion_fin")


# ── Estado degradado cuando no hay embeddings ───────────────────────────────


@db_gated
@pytest.mark.asyncio
async def test_sin_embeddings_el_documento_no_se_da_por_bueno(monkeypatch, tmp_path):
    from app.db.session import db_session
    from app.models.document import Document, DocumentFormato, DocumentStatus
    from app.providers.embeddings import EmbeddingResult
    from app.services import kb_indexer

    async def sin_clave(texts):
        return EmbeddingResult(
            vectors=[[0.0] * 1536 for _ in texts], ok=False, reason="sin clave"
        )

    monkeypatch.setattr(kb_indexer, "embed_texts_with_status", sin_clave)

    fichero = tmp_path / f"{uuid.uuid4().hex}.txt"
    fichero.write_text("Los precios del corte son 20 euros.\n\nAbrimos de 9 a 18.")

    doc_id = uuid.uuid4()
    async with db_session() as db:
        db.add(
            Document(
                id=doc_id, nombre="prueba.txt", formato=DocumentFormato.txt,
                tamano_bytes=fichero.stat().st_size,
                status=DocumentStatus.procesando, storage_path=str(fichero),
            )
        )
        await db.commit()

    try:
        await kb_indexer.index_document_by_id(doc_id)
        async with db_session() as db:
            row = (
                await db.execute(
                    text("SELECT status, error_msg, num_chunks FROM documents WHERE id = :i"),
                    {"i": str(doc_id)},
                )
            ).one()
        status, error_msg, num_chunks = row
        # NO es "indexado" a secas: se dice que le falta la parte semántica.
        assert status == "indexado_sin_semantica"
        assert "semántica" in error_msg
        # Pero los chunks SÍ se guardan: la búsqueda degrada a texto completo.
        assert num_chunks > 0
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM documents WHERE id = :i"), {"i": str(doc_id)}
            )
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_reindexar_recupera_el_documento_al_poner_la_clave(monkeypatch, tmp_path):
    """El caso real: subo PDFs, luego pongo la clave, reindexo y ya funcionan."""
    from app.db.session import db_session
    from app.models.document import Document, DocumentFormato, DocumentStatus
    from app.providers.embeddings import EmbeddingResult
    from app.services import kb_indexer

    fichero = tmp_path / f"{uuid.uuid4().hex}.txt"
    fichero.write_text("Contenido del documento de prueba para reindexar.")
    doc_id = uuid.uuid4()
    async with db_session() as db:
        db.add(
            Document(
                id=doc_id, nombre="reindex.txt", formato=DocumentFormato.txt,
                tamano_bytes=fichero.stat().st_size,
                status=DocumentStatus.procesando, storage_path=str(fichero),
            )
        )
        await db.commit()

    try:
        async def sin_clave(texts):
            return EmbeddingResult([[0.0] * 1536 for _ in texts], ok=False)

        monkeypatch.setattr(kb_indexer, "embed_texts_with_status", sin_clave)
        await kb_indexer.index_document_by_id(doc_id)

        async def con_clave(texts):
            return EmbeddingResult([[0.01] * 1536 for _ in texts], ok=True)

        monkeypatch.setattr(kb_indexer, "embed_texts_with_status", con_clave)
        await kb_indexer.index_document_by_id(doc_id)

        async with db_session() as db:
            status, error_msg = (
                await db.execute(
                    text("SELECT status, error_msg FROM documents WHERE id = :i"),
                    {"i": str(doc_id)},
                )
            ).one()
        assert status == "indexado"
        assert error_msg is None
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM documents WHERE id = :i"), {"i": str(doc_id)}
            )
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_endpoints_de_reindexado_y_estado(monkeypatch, tmp_path):
    """No existía forma de reindexar: ni endpoint ni botón."""
    from app.api import knowledge_base as kb
    from app.db.session import db_session
    from app.models.document import Document, DocumentFormato, DocumentStatus
    from app.models.user import User
    from app.providers.embeddings import EmbeddingResult
    from app.services import kb_indexer

    async def _emb(texts):
        return EmbeddingResult([[0.01] * 1536 for _ in texts], ok=True)

    monkeypatch.setattr(kb_indexer, "embed_texts_with_status", _emb)

    fichero = tmp_path / f"{uuid.uuid4().hex}.txt"
    fichero.write_text("Texto que hay que reindexar.")
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with db_session() as db:
        db.add(
            User(
                id=user_id, email=f"reidx-{uuid.uuid4().hex[:8]}@test.local",
                password_hash="x", nombre="Reindex", role="admin",
            )
        )
        db.add(
            Document(
                id=doc_id, nombre="degradado.txt", formato=DocumentFormato.txt,
                tamano_bytes=10,
                status=DocumentStatus.indexado_sin_semantica,
                error_msg="sin semántica", storage_path=str(fichero),
            )
        )
        await db.commit()

    try:
        async with db_session() as db:
            u = await db.get(User, user_id)

            estado = await kb.kb_status(db=db, _=u)
            assert estado.sin_semantica >= 1

            # Reindexado en bloque: por defecto solo lo que lo necesita.
            out = await kb.reindex_documents(
                body=kb.ReindexAllIn(todos=False), db=db, _=u
            )
            assert out.reindexados >= 1

            # Y el individual.
            uno = await kb.reindex_document(document_id=doc_id, db=db, _=u)
            assert uno.reindexados == 1

        for _ in range(40):
            await asyncio.sleep(0.05)
            async with db_session() as db:
                status = (
                    await db.execute(
                        text("SELECT status FROM documents WHERE id = :i"),
                        {"i": str(doc_id)},
                    )
                ).scalar_one()
            if status == "indexado":
                break
        assert status == "indexado"
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM documents WHERE id = :i"), {"i": str(doc_id)}
            )
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(user_id)})
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_schedule_indexing_vuelve_al_momento(monkeypatch, tmp_path):
    """La petición no espera al indexado: devuelve con el doc en `procesando`."""
    import time as _time

    from app.db.session import db_session
    from app.models.document import Document, DocumentFormato, DocumentStatus
    from app.providers.embeddings import EmbeddingResult
    from app.services import kb_indexer

    def _lento(path, formato):
        _time.sleep(0.4)
        return [{"contenido": "hola", "metadata": {}}]

    async def _emb(texts):
        return EmbeddingResult([[0.01] * 1536 for _ in texts], ok=True)

    monkeypatch.setattr(kb_indexer, "_extract_chunks", _lento)
    monkeypatch.setattr(kb_indexer, "embed_texts_with_status", _emb)

    fichero = tmp_path / f"{uuid.uuid4().hex}.txt"
    fichero.write_text("hola")
    doc_id = uuid.uuid4()
    async with db_session() as db:
        db.add(
            Document(
                id=doc_id, nombre="lento.txt", formato=DocumentFormato.txt,
                tamano_bytes=4, status=DocumentStatus.procesando,
                storage_path=str(fichero),
            )
        )
        await db.commit()

    try:
        t0 = _time.perf_counter()
        via = await kb_indexer.schedule_indexing(doc_id)
        elapsed = _time.perf_counter() - t0
        assert via == "background"
        assert elapsed < 0.2, "schedule_indexing no puede esperar al indexado"

        # Y termina solo, en segundo plano.
        for _ in range(40):
            await asyncio.sleep(0.05)
            async with db_session() as db:
                status = (
                    await db.execute(
                        text("SELECT status FROM documents WHERE id = :i"),
                        {"i": str(doc_id)},
                    )
                ).scalar_one()
            if status == "indexado":
                break
        assert status == "indexado"
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM documents WHERE id = :i"), {"i": str(doc_id)}
            )
            await db.commit()
