"""Tests del autoaprendizaje Fase 2 — correcciones repetidas (disparador #3).

Cobertura (PURA, sin BD):
  - El clustering por similitud agrupa correcciones parecidas y separa las
    distintas, respetando el umbral.
  - El bloque de "Reglas aprendidas" se compone, se acota (nº de reglas y
    presupuesto de caracteres) y queda vacío cuando no hay reglas.

La parte que toca BD/LLM (creación del hueco, marcado processed_at) se ejerce en
CI con Postgres y mocks; aquí nos centramos en la lógica determinista, que es
donde está el riesgo de regresión.
"""
from __future__ import annotations


def test_cluster_groups_similar_and_splits_different():
    from app.tasks.detect_correction_gaps import cluster_by_similarity

    # Dos vectores casi idénticos + uno ortogonal → 2 clusters.
    embs = [
        [1.0, 0.0, 0.0],
        [0.98, 0.02, 0.0],
        [0.0, 1.0, 0.0],
    ]
    clusters = cluster_by_similarity(embs, threshold=0.8)
    # El primer cluster contiene los dos parecidos; el ortogonal va solo.
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]
    big = max(clusters, key=len)
    assert set(big) == {0, 1}


def test_cluster_threshold_separates_when_high():
    from app.tasks.detect_correction_gaps import cluster_by_similarity

    embs = [[1.0, 0.0], [0.9, 0.1]]
    # Coseno ~0.992 → con umbral 0.999 NO se agrupan.
    clusters = cluster_by_similarity(embs, threshold=0.999)
    assert sorted(len(c) for c in clusters) == [1, 1]
    # Con umbral 0.8 SÍ se agrupan.
    clusters2 = cluster_by_similarity(embs, threshold=0.8)
    assert sorted(len(c) for c in clusters2) == [2]


def test_cluster_handles_zero_vectors():
    from app.tasks.detect_correction_gaps import cluster_by_similarity

    # Vectores nulos (p. ej. sin clave de embeddings): no deben agruparse por
    # coseno (que es 0.0), cada uno abre su propio cluster.
    embs = [[0.0, 0.0], [0.0, 0.0]]
    clusters = cluster_by_similarity(embs, threshold=0.8)
    assert sorted(len(c) for c in clusters) == [1, 1]


def test_rules_block_renders_and_caps():
    from app.services.learned_rules import (
        MAX_RULES,
        build_rules_block_from_texts,
    )

    block = build_rules_block_from_texts(["  sé   más  cálida ", "", "no ofrezcas descuentos"])
    assert "Reglas aprendidas (estilo)" in block
    assert "sé más cálida" in block  # normaliza espacios
    assert "no ofrezcas descuentos" in block
    assert "fin reglas aprendidas" in block

    # Tope de nº de reglas: con más de MAX_RULES solo entran MAX_RULES líneas.
    many = [f"regla número {i}" for i in range(MAX_RULES + 10)]
    big_block = build_rules_block_from_texts(many)
    assert big_block.count("\n- ") == MAX_RULES


def test_rules_block_empty_when_no_rules():
    from app.services.learned_rules import build_rules_block_from_texts

    assert build_rules_block_from_texts([]) == ""
    assert build_rules_block_from_texts(["", "   "]) == ""


def test_rules_block_char_budget():
    from app.services.learned_rules import MAX_BLOCK_CHARS, build_rules_block_from_texts

    # Reglas largas: el cuerpo del bloque no debe dispararse sin control.
    rules = [("x" * 150) for _ in range(15)]
    block = build_rules_block_from_texts(rules)
    # El bloque incluye cabecera/intro/cierre; el cuerpo de reglas se acota al
    # presupuesto. Comprobamos que no excede de forma desbocada.
    assert len(block) <= MAX_BLOCK_CHARS + 400
