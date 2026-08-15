"""Correcciones marcadas como "procesadas" sin haberlas analizado.

El fallo que cubre este test: la corrida horaria marcaba TODAS las correcciones
como procesadas al final, incluso las de un grupo cuya llamada al LLM había
fallado. Como el detector solo mira las correcciones con `processed_at IS NULL`,
esas correcciones no se volvían a analizar NUNCA: una hora de caída de OpenAI
se comía en silencio el autoaprendizaje de esa hora.

Ahora solo se marcan las que se completaron; las demás quedan pendientes para
el siguiente ciclo, con un contador de intentos para no reintentar en bucle una
corrección que siempre falla.

Sin Postgres ni OpenAI: dobles para la sesión de BD, los embeddings y el LLM.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.tasks import detect_correction_gaps as mod

# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class _Correction:
    def __init__(self, instruction: str, *, attempts: int = 0) -> None:
        self.id = uuid.uuid4()
        self.instruction = instruction
        self.conversation_id = uuid.uuid4()
        self.created_at = datetime.now(timezone.utc)
        self.analysis_attempts = attempts
        self.processed_at = None


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, corrections) -> None:
        self.corrections = corrections
        self.added: list = []

    async def execute(self, stmt):
        from app.models.agent_correction import AgentCorrection

        entity = stmt.column_descriptions[0]["entity"]
        if entity is AgentCorrection:
            return _FakeResult(self.corrections)
        return _FakeResult([])  # huecos pendientes (dedupe)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def _patch_all(monkeypatch, corrections, *, proposal):
    """Cablea BD, embeddings y LLM. Devuelve (marcadas, intentos, db)."""
    import app.db.session as db_mod
    import app.providers.embeddings as emb_mod

    db = _FakeDB(corrections)

    @asynccontextmanager
    async def fake_db_session():
        yield db

    monkeypatch.setattr(db_mod, "db_session", fake_db_session)

    async def fake_embed(texts):
        # Todo se parece a todo → un solo cluster con todas las correcciones.
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(emb_mod, "embed_texts", fake_embed)

    async def fake_draft(_instructions):
        return proposal

    monkeypatch.setattr(mod, "_draft_proposal_for_cluster", fake_draft)

    marcadas: list = []
    intentos: list = []

    async def fake_mark(ids):
        marcadas.extend(ids)

    async def fake_bump(ids):
        intentos.extend(ids)

    monkeypatch.setattr(mod, "_mark_processed", fake_mark)
    monkeypatch.setattr(mod, "_bump_attempts", fake_bump, raising=False)
    return marcadas, intentos, db


_PROPUESTA_OK = {"kind": "style", "summary": "sé más cálida", "proposal": "Sé más cálida."}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_llm_caido_no_marca_las_correcciones_como_procesadas(monkeypatch):
    """EL FALLO: una caída del LLM borraba el aprendizaje de esa hora."""
    corr = [_Correction("sé más cálida"), _Correction("sé más cálida al saludar")]
    marcadas, intentos, _ = _patch_all(monkeypatch, corr, proposal=None)

    result = await mod._detect_correction_gaps()

    assert result["gaps_created"] == 0
    ids = {c.id for c in corr}
    assert not (ids & set(marcadas)), (
        "se marcaron como procesadas correcciones que nadie llegó a analizar"
    )
    # Y queda constancia del intento fallido (para no reintentar en bucle).
    assert set(intentos) == ids


async def test_analisis_correcto_si_marca_procesadas(monkeypatch):
    """Contrapeso: cuando el análisis sale bien, no se re-analizan."""
    corr = [_Correction("sé más cálida"), _Correction("sé más cálida al saludar")]
    marcadas, intentos, db = _patch_all(monkeypatch, corr, proposal=_PROPUESTA_OK)

    result = await mod._detect_correction_gaps()

    assert result["gaps_created"] == 1
    assert {c.id for c in corr} <= set(marcadas)
    assert intentos == []
    assert db.added  # el hueco propuesto


async def test_correccion_que_siempre_falla_acaba_cerrandose(monkeypatch):
    """Sin bucle infinito: al agotar los intentos se marca (con log de error)."""
    ultimo = mod.MAX_ANALYSIS_ATTEMPTS - 1
    corr = [
        _Correction("sé más cálida", attempts=ultimo),
        _Correction("sé más cálida al saludar", attempts=ultimo),
    ]
    marcadas, _intentos, _ = _patch_all(monkeypatch, corr, proposal=None)

    await mod._detect_correction_gaps()

    assert {c.id for c in corr} <= set(marcadas)


async def test_ediciones_manuales_se_marcan_aunque_el_llm_falle(monkeypatch):
    """Las ediciones a mano no se analizan nunca: no tienen nada que esperar."""
    from app.models.agent_correction import MANUAL_EDIT_INSTRUCTION

    manual = _Correction(MANUAL_EDIT_INSTRUCTION)
    corr = [manual, _Correction("sé más cálida"), _Correction("sé más cálida ya")]
    marcadas, _intentos, _ = _patch_all(monkeypatch, corr, proposal=None)

    await mod._detect_correction_gaps()

    assert manual.id in marcadas


async def test_cluster_sin_repeticion_se_marca_procesado(monkeypatch):
    """Una corrección suelta (sin repetirse) sigue cerrándose como antes."""
    corr = [_Correction("sé más cálida")]
    marcadas, _intentos, _ = _patch_all(monkeypatch, corr, proposal=None)

    await mod._detect_correction_gaps()

    assert corr[0].id in marcadas


@pytest.mark.parametrize("attempts", [0, 1])
async def test_intentos_previos_no_agotados_siguen_pendientes(monkeypatch, attempts):
    corr = [
        _Correction("sé más cálida", attempts=attempts),
        _Correction("sé más cálida al saludar", attempts=attempts),
    ]
    marcadas, _intentos, _ = _patch_all(monkeypatch, corr, proposal=None)

    await mod._detect_correction_gaps()

    assert not ({c.id for c in corr} & set(marcadas))
