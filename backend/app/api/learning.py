"""Autoaprendizaje · Fase 2 — pantalla "Aprendizajes" (admin).

Lista los huecos de conocimiento detectados (el agente derivó a humano, la KB no
tuvo resultados, o la operadora corrigió lo mismo varias veces) para que la
operadora los revise. Al aprobar:

  - huecos de CONTENIDO (handoff / kb_miss / correction kind="content"): se crea
    un documento de Q&A que se indexa en la base de conocimiento → el agente lo
    encuentra al responder.
  - huecos de ESTILO (correction kind="style"): se guarda una regla de estilo
    (LearnedRule) que se inyecta en el prompt del agente → cambia su tono.

Siempre con aprobación humana: nada se aprende solo. La operadora también puede
ver y desactivar las reglas de estilo activas (journey completo: lo aprobado se
aplica de verdad y se puede revertir).
"""
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.document import Document, DocumentFormato, DocumentStatus
from app.models.knowledge_gap import KnowledgeGap
from app.models.learned_rule import LearnedRule
from app.models.user import User
from app.schemas.common import OkResponse

# Reutilizamos el MISMO almacenamiento que la subida manual de documentos para
# que el indexador (que valida que el archivo esté dentro del volumen) lo
# encuentre. No inventamos una ruta nueva.
from app.api.knowledge_base import KB_STORAGE_ROOT

router = APIRouter(prefix="/admin/learning", tags=["learning"])
logger = get_logger(__name__)

_MAX_ANSWER_LEN = 5000
_MAX_RULE_LEN = 500


class GapOut(BaseModel):
    id: uuid.UUID
    trigger: str
    # "content" | "style" | None. Indica qué hace "Aprobar" (Q&A vs regla de
    # estilo). Los huecos sin valor (handoff/kb_miss) se tratan como "content".
    proposal_kind: str | None
    question: str
    suggested_answer: str | None
    status: str
    conversation_id: uuid.UUID | None
    created_at: str
    # Aviso de verificación tras aprobar contenido: null = todo OK; string = el
    # agente NO recupera la Q&A recién aprendida (p.ej. sin clave de embeddings,
    # o la respuesta quedó vacía tras chunkear). Solo se rellena en la respuesta
    # de "aprobar", no al listar.
    verification_warning: str | None = None

    class Config:
        from_attributes = True


def _gap_out(g: KnowledgeGap, verification_warning: str | None = None) -> GapOut:
    return GapOut(
        id=g.id,
        trigger=g.trigger,
        proposal_kind=g.proposal_kind,
        question=g.question or "",
        suggested_answer=g.suggested_answer,
        status=g.status,
        conversation_id=g.conversation_id,
        created_at=g.created_at.isoformat(),
        verification_warning=verification_warning,
    )


async def _verify_learning_retrievable(
    db: AsyncSession, question: str, doc_id: uuid.UUID
) -> str | None:
    """Smoke test post-aprobación: ¿el agente RECUPERA ahora la Q&A aprendida?

    Re-lanza la pregunta del hueco contra la misma búsqueda que usa el agente
    (`match_chunks_hybrid`) y comprueba que algún chunk del documento recién
    creado aparece en el top-k. Devuelve None si todo bien, o un aviso legible
    si no (indexación fallida, sin embeddings, respuesta vacía). Best-effort:
    ante cualquier error, no bloquea la aprobación."""
    try:
        from sqlalchemy import text as _sql

        from app.core.config import settings
        from app.providers.embeddings import embed_texts

        q = (question or "").strip()
        if not q:
            return None  # sin pregunta no hay nada que verificar
        embeddings = await embed_texts([q])
        vector_ok = bool(embeddings and any(embeddings[0]))
        emb_str = (
            "[" + ",".join(f"{x:.6f}" for x in embeddings[0]) + "]"
            if vector_ok
            else "[" + ",".join("0" for _ in range(1536)) + "]"
        )
        rows = (
            await db.execute(
                _sql(
                    "SELECT document_id FROM match_chunks_hybrid("
                    "  CAST(:emb AS vector), :q, :limit, :threshold, 40, 60, :ve)"
                ),
                {
                    "emb": emb_str,
                    "q": q,
                    "limit": settings.KB_TOP_K_DEFAULT,
                    "threshold": settings.KB_MATCH_THRESHOLD,
                    "ve": vector_ok,
                },
            )
        ).fetchall()
        found = any(str(r.document_id) == str(doc_id) for r in rows)
        if found:
            return None
        return (
            "La respuesta se guardó, pero el agente todavía no la recupera al "
            "hacer la pregunta original. Revisa que la clave de OpenAI "
            "(embeddings) esté configurada y que la respuesta no sea demasiado "
            "corta."
        )
    except Exception as e:
        logger.warning("learning.verify.error", error=str(e))
        return None


class ApproveGapBody(BaseModel):
    answer: str


class RuleOut(BaseModel):
    id: uuid.UUID
    text: str
    active: bool
    source_gap_id: uuid.UUID | None
    created_at: str

    class Config:
        from_attributes = True


@router.get("/gaps")
async def list_gaps(
    status: str = "pendiente",
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[GapOut]:
    """Lista los huecos de conocimiento (más recientes primero)."""
    rows = (
        await db.execute(
            select(KnowledgeGap)
            .where(KnowledgeGap.status == status)
            .order_by(desc(KnowledgeGap.created_at))
        )
    ).scalars().all()
    return [_gap_out(g) for g in rows]


class CorrectionOut(BaseModel):
    id: uuid.UUID
    canal: str | None
    instruction: str
    manual: bool  # True si vino de editar el borrador a mano
    original_preview: str | None
    resulting_preview: str | None
    processed: bool  # ya analizada por el detector de reglas
    promoted_to: str | None  # "style" | "content" | None (convertida a mano)
    created_at: str


class CorrectionsResponse(BaseModel):
    total: int
    pending: int
    items: list[CorrectionOut]


def _preview(s: str | None, n: int = 200) -> str | None:
    if not s:
        return s
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


@router.get("/corrections", response_model=CorrectionsResponse)
async def list_corrections(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> CorrectionsResponse:
    """Correcciones EN CRUDO de la operadora (materia prima del aprendizaje).

    Da feedback inmediato de que CADA corrección se guarda, sin esperar a que el
    detector horario agrupe ≥2 parecidas en una regla. Incluye tanto las de
    instrucción ("Corregir") como las ediciones a mano del borrador.
    """
    from app.models.agent_correction import AgentCorrection, MANUAL_EDIT_INSTRUCTION

    limit = max(1, min(limit, 200))
    total = (
        await db.execute(select(func.count()).select_from(AgentCorrection))
    ).scalar_one()
    pending = (
        await db.execute(
            select(func.count())
            .select_from(AgentCorrection)
            .where(AgentCorrection.processed_at.is_(None))
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(AgentCorrection)
            .order_by(desc(AgentCorrection.created_at))
            .limit(limit)
        )
    ).scalars().all()
    items = [
        CorrectionOut(
            id=c.id,
            canal=c.canal,
            instruction=c.instruction or "",
            manual=(c.instruction or "") == MANUAL_EDIT_INSTRUCTION,
            original_preview=_preview(c.original_text),
            resulting_preview=_preview(c.resulting_text),
            processed=c.processed_at is not None,
            promoted_to=c.promoted_to,
            created_at=c.created_at.isoformat(),
        )
        for c in rows
    ]
    return CorrectionsResponse(total=total, pending=pending, items=items)


class PromoteCorrectionBody(BaseModel):
    kind: str  # "style" (→ prompt) | "content" (→ base de conocimiento)
    text: str


@router.post("/corrections/{correction_id}/promote")
async def promote_correction(
    correction_id: uuid.UUID,
    body: PromoteCorrectionBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> CorrectionOut:
    """Convierte UNA corrección en aprendizaje al instante (sin esperar a que se
    repita ≥2 veces). La operadora elige el destino y SIEMPRE lo aprueba:

      - kind="style"   → regla de estilo (se inyecta en el prompt del agente).
      - kind="content" → documento Q&A en la base de conocimiento (indexado).

    Marca la corrección como promocionada (badge "a dónde fue") y procesada para
    que el detector horario no vuelva a sugerirla.
    """
    from app.models.agent_correction import AgentCorrection, MANUAL_EDIT_INSTRUCTION

    kind = "style" if body.kind == "style" else "content"
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El texto no puede estar vacío")

    c = (
        await db.execute(select(AgentCorrection).where(AgentCorrection.id == correction_id))
    ).scalar_one_or_none()
    if not c:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Corrección no encontrada")
    if c.promoted_to:
        raise HTTPException(status.HTTP_409_CONFLICT, "Esta corrección ya se convirtió en aprendizaje")

    if kind == "style":
        db.add(LearnedRule(text=text[:_MAX_RULE_LEN], source_gap_id=None, active=True, created_by=user.id))
    else:
        # Q&A → Document .txt en el almacenamiento de la KB + indexado (mismo
        # camino que la subida manual y que aprobar un hueco de contenido).
        doc_id = uuid.uuid4()
        storage_path = str((KB_STORAGE_ROOT / f"{doc_id}.txt").resolve())
        if not storage_path.startswith(str(KB_STORAGE_ROOT)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta inválida")
        content = text[:_MAX_ANSWER_LEN].encode("utf-8")
        with open(storage_path, "wb") as f:
            f.write(content)
        doc = Document(
            id=doc_id,
            nombre=(f"Aprendizaje · {text[:50]}").strip()[:255] + ".txt",
            formato=DocumentFormato.txt,
            tamano_bytes=len(content),
            status=DocumentStatus.procesando,
            storage_path=storage_path,
            uploaded_by=user.id,
        )
        db.add(doc)
        await db.commit()
        from app.services.kb_indexer import index_document_now
        await index_document_now(doc_id)

    c.promoted_to = kind
    if c.processed_at is None:
        c.processed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(c)

    try:
        from app.services.audit import record_audit
        await record_audit(
            db,
            user_id=user.id,
            action="learning.correction.promoted",
            entity="agent_correction",
            entity_id=c.id,
            after={"kind": kind},
        )
        await db.commit()
    except Exception:
        pass

    return CorrectionOut(
        id=c.id,
        canal=c.canal,
        instruction=c.instruction or "",
        manual=(c.instruction or "") == MANUAL_EDIT_INSTRUCTION,
        original_preview=_preview(c.original_text),
        resulting_preview=_preview(c.resulting_text),
        processed=c.processed_at is not None,
        promoted_to=c.promoted_to,
        created_at=c.created_at.isoformat(),
    )


@router.post("/gaps/{gap_id}/approve")
async def approve_gap(
    gap_id: uuid.UUID,
    body: ApproveGapBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> GapOut:
    """Aprueba un hueco. El destino depende de `proposal_kind`:

    - "style": la propuesta es una REGLA de estilo → se guarda como LearnedRule
      (sin Document) y queda activa, de modo que se inyecta en el prompt del
      agente en cada turno.
    - resto ("content" / handoff / kb_miss): la operadora escribe la respuesta;
      con la pregunta del hueco montamos "P: …\\nR: …", lo guardamos como Document
      (.txt) en el almacenamiento de la KB y lo indexamos (chunks + embeddings) —
      exactamente el mismo camino que la subida manual de documentos.
    """
    answer = (body.answer or "").strip()
    if not answer:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "La respuesta no puede estar vacía")

    gap = (
        await db.execute(select(KnowledgeGap).where(KnowledgeGap.id == gap_id))
    ).scalar_one_or_none()
    if not gap:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hueco no encontrado")
    if gap.status != "pendiente":
        raise HTTPException(status.HTTP_409_CONFLICT, "El hueco ya está resuelto")

    # ── Rama ESTILO: crear una regla aprendida (no va a la KB) ──────────────
    if gap.proposal_kind == "style":
        return await _approve_style_gap(db, gap, answer[:_MAX_RULE_LEN], user)

    # ── Rama CONTENIDO: Q&A → Document + índice (camino existente) ──────────
    answer = answer[:_MAX_ANSWER_LEN]
    question = (gap.question or "").strip()
    qa_text = f"P: {question}\nR: {answer}"

    # Document + archivo .txt en el mismo almacenamiento que /kb/documents, para
    # que el indexador lo encuentre dentro del volumen compartido.
    doc_id = uuid.uuid4()
    storage_path = str((KB_STORAGE_ROOT / f"{doc_id}.txt").resolve())
    if not storage_path.startswith(str(KB_STORAGE_ROOT)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta inválida")
    nombre = f"Aprendizaje · {question[:60] or 'Q&A'}.txt"
    content = qa_text.encode("utf-8")
    with open(storage_path, "wb") as f:
        f.write(content)

    doc = Document(
        id=doc_id,
        nombre=nombre[:255],
        formato=DocumentFormato.txt,
        tamano_bytes=len(content),
        status=DocumentStatus.procesando,
        storage_path=storage_path,
        uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()

    # AQUÍ SÍ se espera, a diferencia de la subida de archivos: justo después
    # corre el smoke test `_verify_learning_retrievable`, que comprueba que el
    # agente ya recupera la Q&A recién aprobada. Si la indexación fuera en
    # segundo plano, la verificación miraría antes de que existieran los chunks
    # y avisaría de un fallo inexistente. El coste de bloquear es despreciable:
    # es un .txt de unos KB (sin extracción de PDF/Word), y la única espera real
    # es la llamada de embeddings, que es asíncrona y no ocupa el event loop.
    from app.services.kb_indexer import index_document_now

    await index_document_now(doc.id)

    gap.status = "aprobado"
    gap.suggested_answer = answer
    gap.created_document_id = doc.id
    gap.approved_by = user.id
    gap.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(gap)

    # Cierra el loop del autoaprendizaje: comprueba que el agente RECUPERA la
    # Q&A recién aprobada. Si no (embeddings sin clave, respuesta muy corta), lo
    # avisamos en la respuesta en vez de dar por buena una aprobación que no
    # tendrá efecto en las respuestas del agente.
    verification = await _verify_learning_retrievable(db, question, doc.id)

    try:
        from app.services.audit import record_audit

        await record_audit(
            db,
            user_id=user.id,
            action="learning.gap.approved",
            entity="knowledge_gap",
            entity_id=gap.id,
            after={"document_id": str(doc.id), "trigger": gap.trigger, "kind": "content"},
        )
        await db.commit()
    except Exception:
        pass

    return _gap_out(gap, verification_warning=verification)


async def _approve_style_gap(
    db: AsyncSession, gap: KnowledgeGap, rule_text: str, user: User
) -> GapOut:
    """Aprueba un hueco de ESTILO: persiste una LearnedRule activa.

    La regla (lo que la operadora dejó/editó en el campo de propuesta) se guarda
    cifrada y activa. A partir de ese momento `get_runtime_for_channel` la inyecta
    en el prompt de todos los agentes conversacionales. No se crea Document.
    """
    rule = LearnedRule(
        text=rule_text,
        source_gap_id=gap.id,
        active=True,
        created_by=user.id,
    )
    db.add(rule)

    gap.status = "aprobado"
    gap.suggested_answer = rule_text
    gap.approved_by = user.id
    gap.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(gap)
    await db.refresh(rule)

    try:
        from app.services.audit import record_audit

        await record_audit(
            db,
            user_id=user.id,
            action="learning.gap.approved",
            entity="knowledge_gap",
            entity_id=gap.id,
            after={"learned_rule_id": str(rule.id), "trigger": gap.trigger, "kind": "style"},
        )
        await db.commit()
    except Exception:
        pass

    return _gap_out(gap)


# ── Reglas de estilo aprendidas (gestión por la operadora) ──────────────────


@router.get("/rules")
async def list_rules(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[RuleOut]:
    """Lista las reglas de estilo aprendidas (las activas por defecto)."""
    stmt = select(LearnedRule).order_by(desc(LearnedRule.created_at))
    if active_only:
        stmt = stmt.where(LearnedRule.active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [
        RuleOut(
            id=r.id,
            text=r.text or "",
            active=r.active,
            source_gap_id=r.source_gap_id,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


class RulePreviewIn(BaseModel):
    """Regla candidata + agente contra el que se quiere previsualizar."""

    text: str = Field(..., min_length=1, max_length=2000)
    agent_id: uuid.UUID | None = None


class RulePreviewOut(BaseModel):
    # Bloque de reglas tal cual quedaría (con la candidata incluida).
    rules_block: str
    # Prompt de sistema EFECTIVO completo: seguridad + prompt del agente + reglas.
    effective_prompt: str
    agent_name: str
    # Las reglas son GLOBALES: se inyectan en TODOS los agentes, no solo en este.
    global_scope: bool = True
    affected_agents: list[str]
    # Frases del prompt del agente que la regla podría estar contradiciendo.
    posibles_conflictos: list[str]


# Palabras que suelen marcar una prohibición o una obligación en el prompt. Si
# la regla candidata habla de lo mismo que una línea imperativa del prompt, la
# enseñamos para que quien aprueba lo mire con ojos.
_IMPERATIVE_MARKERS = (
    "no ", "nunca", "jamás", "jamas", "prohibido", "evita", "siempre",
    "obligatorio", "debes", "no uses", "sin ",
)
_STOPWORDS = {
    "para", "como", "cuando", "porque", "pero", "esto", "esta", "este", "esos",
    "cliente", "usuario", "agente", "mensaje", "respuesta", "responde", "siempre",
    "nunca", "tono", "más", "mas", "menos", "todo", "todos", "cada", "hacer",
    "sobre", "entre", "desde", "hasta", "muy", "que", "los", "las", "del", "con",
    "por", "una", "uno", "les", "sus", "sin", "sea", "ser",
}


def _detect_conflicts(rule_text: str, prompt: str) -> list[str]:
    """Líneas imperativas del prompt que comparten vocabulario con la regla.

    Heurística deliberadamente simple y barata: no decide nada, solo pone
    delante lo que un humano tiene que leer antes de aprobar. Las reglas se
    montan AL FINAL del prompt (la posición de más peso) y su propio texto dice
    que tienen prioridad, así que una regla puede anular en silencio una
    instrucción del negocio.
    """
    words = {
        w
        for w in re.findall(r"[a-záéíóúñü]{4,}", (rule_text or "").lower())
        if w not in _STOPWORDS
    }
    if not words:
        return []
    out: list[str] = []
    for raw in (prompt or "").splitlines():
        line = raw.strip(" -•\t")
        if len(line) < 12:
            continue
        low = line.lower()
        if not any(m in low for m in _IMPERATIVE_MARKERS):
            continue
        if words & set(re.findall(r"[a-záéíóúñü]{4,}", low)):
            out.append(line[:200])
        if len(out) >= 5:
            break
    return out


@router.post("/rules/preview", response_model=RulePreviewOut)
async def preview_rule(
    body: RulePreviewIn,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> RulePreviewOut:
    """Cómo queda el prompt CON la regla aplicada, antes de aprobarla.

    Al aprobar una regla no se comprobaba nada contra el prompt: se montaba al
    final (la posición de más peso), diciendo de sí misma que tiene prioridad, y
    se aplicaba a TODOS los agentes a la vez. Este endpoint enseña el resultado
    exacto y señala las instrucciones del prompt que podría estar contradiciendo.
    """
    from app.models.agent import Agent
    from app.services.learned_rules import (
        build_rules_block_from_texts,
        get_active_rule_texts,
    )
    from app.services.runtime_config import SECURITY_GUARD

    candidate = " ".join((body.text or "").split()).strip()
    if not candidate:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "La regla no puede estar vacía")

    agents = (
        await db.execute(select(Agent).where(Agent.is_active.is_(True)).order_by(Agent.created_at))
    ).scalars().all()
    target = next((a for a in agents if a.id == body.agent_id), None) if body.agent_id else None
    if target is None:
        target = agents[0] if agents else None

    base_prompt = target.prompt_system if target else ""
    agent_name = target.name if target else "(sin agentes activos)"

    # La candidata va la PRIMERA: `get_active_rule_texts` ordena por fecha
    # descendente, así que una regla recién aprobada encabeza el bloque.
    existing = await get_active_rule_texts()
    block = build_rules_block_from_texts([candidate, *existing])

    return RulePreviewOut(
        rules_block=block,
        effective_prompt=SECURITY_GUARD + base_prompt + block,
        agent_name=agent_name,
        global_scope=True,
        affected_agents=[a.name for a in agents],
        posibles_conflictos=_detect_conflicts(candidate, base_prompt),
    )


class RuleUpdateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@router.put("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    body: RuleUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> RuleOut:
    """Corrige el texto de una regla aprendida.

    Antes solo se podía desactivar: una regla mal redactada obligaba a tirarla y
    esperar a que el sistema volviera a proponer algo parecido.
    """
    rule = (
        await db.execute(select(LearnedRule).where(LearnedRule.id == rule_id))
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Regla no encontrada")
    nuevo = " ".join((body.text or "").split()).strip()
    if not nuevo:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "La regla no puede estar vacía")
    rule.text = nuevo
    await db.commit()
    await db.refresh(rule)

    try:
        from app.services.audit import record_audit

        await record_audit(
            db,
            user_id=user.id,
            action="learning.rule.edited",
            entity="learned_rule",
            entity_id=rule.id,
            after={"text": nuevo[:200]},
        )
        await db.commit()
    except Exception:
        pass

    return RuleOut(
        id=rule.id,
        text=rule.text or "",
        active=rule.active,
        source_gap_id=rule.source_gap_id,
        created_at=rule.created_at.isoformat(),
    )


@router.post("/rules/{rule_id}/deactivate", response_model=OkResponse)
async def deactivate_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> OkResponse:
    """Desactiva una regla de estilo: deja de inyectarse en el prompt (no la
    borra, para conservar la traza). Reversible reactivándola sería trivial, pero
    en este incremento basta con poder quitarla del prompt."""
    rule = (
        await db.execute(select(LearnedRule).where(LearnedRule.id == rule_id))
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Regla no encontrada")
    if not rule.active:
        return OkResponse()
    rule.active = False
    await db.commit()

    try:
        from app.services.audit import record_audit

        await record_audit(
            db,
            user_id=user.id,
            action="learning.rule.deactivated",
            entity="learned_rule",
            entity_id=rule.id,
        )
        await db.commit()
    except Exception:
        pass

    return OkResponse()


@router.post("/gaps/{gap_id}/discard", response_model=OkResponse)
async def discard_gap(
    gap_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> OkResponse:
    """Descarta un hueco: no aporta nada que aprender."""
    gap = (
        await db.execute(select(KnowledgeGap).where(KnowledgeGap.id == gap_id))
    ).scalar_one_or_none()
    if not gap:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hueco no encontrado")
    if gap.status != "pendiente":
        raise HTTPException(status.HTTP_409_CONFLICT, "El hueco ya está resuelto")

    gap.status = "descartado"
    gap.resolved_at = datetime.now(timezone.utc)
    await db.commit()

    try:
        from app.services.audit import record_audit

        await record_audit(
            db,
            user_id=user.id,
            action="learning.gap.discarded",
            entity="knowledge_gap",
            entity_id=gap.id,
        )
        await db.commit()
    except Exception:
        pass

    return OkResponse()
