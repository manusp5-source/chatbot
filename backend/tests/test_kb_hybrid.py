"""Búsqueda híbrida de la KB (vector + full-text, RRF). DB-gated.

Verifica que una coincidencia LITERAL exacta (sigla/SKU) la rescata el
full-text aunque el ranking vectorial apunte a otro chunk, y que el modo
solo-texto funciona sin embeddings.
"""
import asyncio
import uuid

import pytest


def _db_available() -> bool:
    try:
        async def _check():
            from sqlalchemy import text
            from app.db.session import db_session
            async with db_session() as db:
                await db.execute(text("SELECT 1"))
        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


def _emb(dim: int) -> str:
    v = ["0.0"] * 1536
    v[dim] = "1.0"
    return "[" + ",".join(v) + "]"


@pytestmark_db
@pytest.mark.asyncio
async def test_hybrid_rescues_exact_keyword():
    from sqlalchemy import text

    from app.db.session import db_session

    doc = uuid.uuid4()
    async with db_session() as db:
        await db.execute(
            text(
                "INSERT INTO documents (id, nombre, formato, tamano_bytes, status, num_chunks) "
                "VALUES (:i,'catalogo.txt','txt',10,'indexado',2)"
            ),
            {"i": str(doc)},
        )
        chunks = [
            ("El producto con referencia SKU-ZX9 cuesta 49 euros.", _emb(0)),
            ("Nuestro horario de atención es de lunes a viernes.", _emb(900)),
        ]
        for c, e in chunks:
            await db.execute(
                text(
                    "INSERT INTO chunks (id, document_id, contenido, embedding, metadata) "
                    "VALUES (:i,:d,:c,CAST(:e AS vector),'{}')"
                ),
                {"i": str(uuid.uuid4()), "d": str(doc), "c": c, "e": e},
            )
        await db.commit()

    try:
        # Embedding que apunta al chunk del HORARIO (dim 900), pero buscamos la
        # sigla exacta: el full-text debe rescatar el chunk del SKU.
        qemb = "[" + ",".join("1.0" if i == 900 else "0.0" for i in range(1536)) + "]"
        async with db_session() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT contenido FROM match_chunks_hybrid("
                        "CAST(:e AS vector), :q, 3, 0.3, 40, 60, true)"
                    ),
                    {"e": qemb, "q": "SKU-ZX9"},
                )
            ).fetchall()
        assert any("SKU-ZX9" in r.contenido for r in rows)

        # Modo solo-texto (vector_enabled=false): sin embeddings sigue habiendo
        # recuperación por coincidencia de palabras.
        zero = "[" + ",".join("0" for _ in range(1536)) + "]"
        async with db_session() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT contenido FROM match_chunks_hybrid("
                        "CAST(:e AS vector), :q, 3, 0.3, 40, 60, false)"
                    ),
                    {"e": zero, "q": "horario atención"},
                )
            ).fetchall()
        assert any("horario" in r.contenido.lower() for r in rows)
    finally:
        async with db_session() as db:
            await db.execute(text("DELETE FROM documents WHERE id = :i"), {"i": str(doc)})
            await db.commit()
