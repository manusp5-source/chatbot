"""Búsqueda híbrida en la KB: vector + full-text (RRF)

Revision ID: 0036_hybrid_search
Revises: 0035_llm_providers
Create Date: 2026-07-01

La búsqueda de la KB era 100% vectorial: los embeddings fallan con nombres
propios, precios exactos, SKUs y siglas (el significado semántico no captura
la cadena literal). Añadimos `match_chunks_hybrid`, que combina el ranking
vectorial (coseno) con full-text en español (`to_tsvector('spanish', …)`)
mediante Reciprocal Rank Fusion — sin necesidad de normalizar puntuaciones.

- Índice GIN funcional sobre `to_tsvector('spanish', contenido)` para el FTS.
- La función antigua `match_chunks` se conserva (compat).
- `vector_enabled=false` → modo solo-texto: la KB sigue respondiendo aunque
  falte la clave de OpenAI (sin embeddings), degradada pero útil.
"""
from alembic import op


revision = "0036_hybrid_search"
down_revision = "0035_llm_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_fts_es
        ON chunks USING GIN (to_tsvector('spanish', contenido));
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION match_chunks_hybrid(
          query_embedding vector(1536),
          query_text text,
          match_count int DEFAULT 5,
          match_threshold float DEFAULT 0.3,
          candidate_pool int DEFAULT 40,
          rrf_k int DEFAULT 60,
          vector_enabled boolean DEFAULT true
        )
        RETURNS TABLE (
          id uuid,
          document_id uuid,
          contenido text,
          metadata jsonb,
          similarity float
        )
        LANGUAGE sql STABLE
        AS $$
          WITH vector_ranked AS (
            SELECT
              c.id,
              1 - (c.embedding <=> query_embedding) AS vsim,
              ROW_NUMBER() OVER (ORDER BY c.embedding <=> query_embedding) AS rank
            FROM chunks c
            WHERE vector_enabled
              AND 1 - (c.embedding <=> query_embedding) > match_threshold
            ORDER BY c.embedding <=> query_embedding
            LIMIT candidate_pool
          ),
          text_ranked AS (
            SELECT
              c.id,
              ROW_NUMBER() OVER (
                ORDER BY ts_rank_cd(
                  to_tsvector('spanish', c.contenido),
                  plainto_tsquery('spanish', query_text)
                ) DESC
              ) AS rank
            FROM chunks c
            WHERE query_text <> ''
              AND to_tsvector('spanish', c.contenido) @@ plainto_tsquery('spanish', query_text)
            ORDER BY ts_rank_cd(
              to_tsvector('spanish', c.contenido),
              plainto_tsquery('spanish', query_text)
            ) DESC
            LIMIT candidate_pool
          ),
          fused AS (
            SELECT
              COALESCE(v.id, t.id) AS id,
              COALESCE(1.0 / (rrf_k + v.rank), 0.0)
                + COALESCE(1.0 / (rrf_k + t.rank), 0.0) AS score,
              COALESCE(v.vsim, 0.0) AS vsim
            FROM vector_ranked v
            FULL OUTER JOIN text_ranked t ON v.id = t.id
          )
          SELECT
            c.id,
            c.document_id,
            c.contenido,
            c.metadata,
            f.vsim AS similarity
          FROM fused f
          JOIN chunks c ON c.id = f.id
          ORDER BY f.score DESC
          LIMIT match_count;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS match_chunks_hybrid(vector, text, int, float, int, int, boolean);")
    op.execute("DROP INDEX IF EXISTS idx_chunks_fts_es;")
