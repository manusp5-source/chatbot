"""Autoaprendizaje · Fase 2 — detector de "correcciones repetidas" (disparador #3).

Tarea Celery periódica (cada hora, ver beat_schedule en app/tasks/__init__.py).
Lee `agent_corrections` (Fase 1: las instrucciones en lenguaje natural con que la
operadora corrigió al agente), AGRUPA por similitud y, cuando lo mismo se ha
corregido ≥2 veces, propone un APRENDIZAJE para que la operadora lo apruebe:

  - corrección de ESTILO/tono ("sé más cálida", "no ofrezcas descuentos")
    → propone una REGLA de estilo (al aprobar se inyecta en el prompt).
  - corrección de CONTENIDO/dato → propone una Q&A (al aprobar va a la KB).

Siempre con aprobación humana: aquí solo se crea un borrador en "Aprendizajes".

Diseño (barato e idempotente):
  - Solo embebe/agrupa las correcciones SIN procesar (`processed_at IS NULL`), de
    modo que el coste de cada corrida es proporcional a lo NUEVO, no al histórico.
  - Agrupa con clustering voraz por similitud coseno (umbral ~0.8).
  - Dedupe entre corridas: antes de gastar un LLM, descarta los clusters que ya
    están representados por un hueco "correction" pendiente (compara el embedding
    del centroide con el de la pregunta de esos huecos).
  - UNA llamada LLM por cluster nuevo: clasifica (style/content) + redacta.
  - Marca como procesadas las correcciones ANALIZADAS (generen o no hueco), para
    no volver a embeberlas. Las que NO se pudieron analizar (el LLM falló, o el
    tope de clusters por corrida las dejó fuera) quedan PENDIENTES para la
    corrida siguiente: marcarlas era perder el aprendizaje en silencio — una
    hora de caída de OpenAI se comía las correcciones de esa hora para siempre.
    Contador `analysis_attempts` para que una corrección que siempre falla no
    se reintente en bucle: a los MAX_ANALYSIS_ATTEMPTS intentos se cierra con
    un log de error.
  - Best-effort: cualquier fallo se loguea y NO rompe nada (no afecta al turno
    del agente).
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import desc, select

from app.core.logging import get_logger

logger = get_logger(__name__)

# Umbral de similitud coseno para considerar dos correcciones "la misma".
SIMILARITY_THRESHOLD = 0.8
# Mínimo de correcciones parecidas para proponer un aprendizaje ("repetidas").
MIN_CLUSTER_SIZE = 2
# Tope de correcciones a analizar por corrida (guardia de coste/tiempo). Si hay
# más pendientes, las restantes se analizan en la siguiente corrida horaria.
MAX_CORRECTIONS_PER_RUN = 200
# Tope de clusters nuevos que mandamos al LLM por corrida (guardia de coste).
MAX_NEW_CLUSTERS_PER_RUN = 10
# Máx. de huecos "correction" pendientes que revisamos para el dedupe entre
# corridas (son pocos; comparamos en Python sobre sus embeddings).
MAX_PENDING_FOR_DEDUPE = 100
# Intentos de análisis por corrección antes de darla por imposible. Evita el
# bucle infinito si una corrección concreta hace fallar siempre al LLM (texto
# raro, filtro de contenido…): al agotarlos se marca procesada con log de error.
MAX_ANALYSIS_ATTEMPTS = 3

_DRAFT_MAX_LEN = 2000


def _cosine(a: list[float], b: list[float]) -> float:
    """Coseno entre dos vectores. 0.0 si alguno es nulo/cero."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def cluster_by_similarity(
    embeddings: list[list[float]], threshold: float = SIMILARITY_THRESHOLD
) -> list[list[int]]:
    """Clustering voraz por similitud coseno (función PURA, testeable sin BD).

    Devuelve una lista de clusters; cada cluster es una lista de índices sobre
    `embeddings`. Cada elemento entra en el primer cluster cuyo PRIMER miembro
    (semilla) supere el umbral; si no, abre uno nuevo. Suficiente y barato para
    el volumen esperado (decenas de correcciones por corrida).
    """
    clusters: list[list[int]] = []
    seeds: list[list[float]] = []
    for i, emb in enumerate(embeddings):
        placed = False
        for ci, seed in enumerate(seeds):
            if _cosine(emb, seed) >= threshold:
                clusters[ci].append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
            seeds.append(emb)
    return clusters


def _centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for j in range(min(dim, len(v))):
            acc[j] += v[j]
    n = float(len(vectors))
    return [x / n for x in acc]


async def _draft_proposal_for_cluster(instructions: list[str]) -> dict | None:
    """UNA llamada LLM: clasifica el cluster y redacta la propuesta.

    Devuelve {"kind": "style"|"content", "summary": str, "proposal": str} o None
    si no se pudo (sin clave, error de parseo…). El llamante decide qué hacer.
    """
    from app.providers.llm import get_llm_provider
    from app.providers.llm.base import LLMMessage

    sample = "\n".join(f"- {t}" for t in instructions[:8] if t)
    system = (
        "Eres un asistente que destila APRENDIZAJES a partir de correcciones "
        "que una operadora ha hecho repetidamente a un agente de atención al "
        "cliente. Clasifica la corrección repetida y redacta UNA propuesta breve "
        "que la operadora revisará antes de aplicarla (nunca se aplica sola).\n\n"
        "Decide el tipo:\n"
        "- \"style\": es una pauta de TONO o de comportamiento del agente "
        "(p. ej. 'sé más cálida', 'no ofrezcas descuentos', 'usa tuteo'). En "
        "caso 'proposal' debe ser una REGLA en imperativo, de 1-2 líneas, "
        "redactada como instrucción permanente para el agente.\n"
        "- \"content\": es un DATO o hecho del negocio que el agente debería "
        "saber (precios, horarios, políticas, disponibilidad). En este caso "
        "'proposal' debe ser una Q&A en formato exactamente 'P: <pregunta>\\nR: "
        "<respuesta>'. Si no puedes deducir la respuesta con seguridad a partir "
        "de las correcciones, deja la R lo más fiel posible a lo corregido y NO "
        "inventes datos.\n\n"
        "Responde SOLO con JSON válido, sin texto adicional:\n"
        '{"kind": "style"|"content", "summary": "<resumen muy breve de qué se '
        'corrige>", "proposal": "<la regla o la Q&A>"}'
    )
    user = f"Correcciones repetidas de la operadora:\n{sample}"
    try:
        llm = get_llm_provider()
        resp = await llm.complete(
            messages=[
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=user),
            ],
            temperature=0.2,
            max_tokens=400,
            source="learning",
        )
        raw = (resp.content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        data = json.loads(raw[start : end + 1])
        kind = "style" if str(data.get("kind")).lower() == "style" else "content"
        summary = str(data.get("summary") or "").strip()
        proposal = str(data.get("proposal") or "").strip()
        if not proposal:
            return None
        return {"kind": kind, "summary": summary[:500], "proposal": proposal[:_DRAFT_MAX_LEN]}
    except Exception as e:  # noqa: BLE001
        logger.warning("detect_correction_gaps.llm_error", error=str(e))
        return None


async def _detect_correction_gaps() -> dict:
    from app.db.session import db_session
    from app.models.agent_correction import AgentCorrection
    from app.models.knowledge_gap import KnowledgeGap
    from app.providers.embeddings import embed_texts

    # 1) Correcciones sin procesar (las más recientes primero, con tope).
    async with db_session() as db:
        rows = (
            await db.execute(
                select(AgentCorrection)
                .where(AgentCorrection.processed_at.is_(None))
                .order_by(desc(AgentCorrection.created_at))
                .limit(MAX_CORRECTIONS_PER_RUN)
            )
        ).scalars().all()

    if not rows:
        return {"corrections": 0, "clusters": 0, "gaps_created": 0}

    # Snapshot fuera de la sesión: id, instrucción descifrada, conversación.
    # Las ediciones a mano se registran como corrección (visibilidad) pero se
    # EXCLUYEN del clustering: de un texto editado no se destila una pauta de
    # estilo. Se marcan procesadas igualmente para no re-embeberlas.
    from app.models.agent_correction import MANUAL_EDIT_INSTRUCTION

    items: list[tuple] = []
    analyzable_ids: set = set()
    for r in rows:
        instr = (r.instruction or "").strip()
        if instr and instr != MANUAL_EDIT_INSTRUCTION:
            items.append((r.id, instr, r.conversation_id, r.analysis_attempts or 0))
            analyzable_ids.add(r.id)
    # Las que NO se analizan (ediciones a mano, texto vacío) se cierran ya: no
    # están esperando ningún análisis.
    processed_ids: list = [r.id for r in rows if r.id not in analyzable_ids]

    if not items:
        await _mark_processed(processed_ids)
        return {"corrections": len(rows), "clusters": 0, "gaps_created": 0}

    # 2) Embeddings (un solo batch) y clustering.
    instructions = [it[1] for it in items]
    embeddings = await embed_texts(instructions)
    if not embeddings or len(embeddings) != len(items) or not any(any(e) for e in embeddings):
        # Sin embeddings reales (p. ej. sin clave): no inventamos clusters. NO
        # marcamos procesadas para reintentar cuando haya clave.
        logger.warning("detect_correction_gaps.no_embeddings")
        return {"corrections": len(rows), "clusters": 0, "gaps_created": 0}

    clusters = cluster_by_similarity(embeddings, SIMILARITY_THRESHOLD)
    repeated = [c for c in clusters if len(c) >= MIN_CLUSTER_SIZE]
    # Las que no llegan a "repetidas" se cierran como siempre: ya se han mirado.
    repeated_members = {i for c in repeated for i in c}
    processed_ids.extend(
        items[i][0] for i in range(len(items)) if i not in repeated_members
    )

    # 3) Dedupe entre corridas: embeddings de las preguntas de los huecos
    #    "correction" ya pendientes, para no proponer dos veces lo mismo.
    async with db_session() as db:
        pending = (
            await db.execute(
                select(KnowledgeGap)
                .where(
                    KnowledgeGap.status == "pendiente",
                    KnowledgeGap.trigger == "correction",
                )
                .order_by(desc(KnowledgeGap.created_at))
                .limit(MAX_PENDING_FOR_DEDUPE)
            )
        ).scalars().all()
    pending_texts = [(g.question or "").strip() for g in pending if (g.question or "").strip()]
    pending_embeddings = await embed_texts(pending_texts) if pending_texts else []

    gaps_created = 0
    new_clusters = 0
    deferred = 0        # sin analizar por tope de corrida → siguiente vuelta
    failed_ids: list = []   # el LLM falló → siguiente vuelta (con intento contado)
    given_up_ids: list = []  # intentos agotados → se cierran con log de error
    for cluster in repeated:
        cluster_ids = [items[i][0] for i in cluster]
        if new_clusters >= MAX_NEW_CLUSTERS_PER_RUN:
            # Tope de coste: NO se marcan procesadas (nadie las ha analizado);
            # las coge la corrida siguiente.
            deferred += len(cluster_ids)
            continue
        member_embs = [embeddings[i] for i in cluster]
        centroid = _centroid(member_embs)

        # ¿Ya hay un hueco pendiente parecido? → saltar (no gastar LLM). Estas
        # SÍ están analizadas: su propuesta ya existe esperando aprobación.
        if any(
            _cosine(centroid, pe) >= SIMILARITY_THRESHOLD for pe in pending_embeddings
        ):
            processed_ids.extend(cluster_ids)
            continue

        new_clusters += 1
        cluster_instructions = [items[i][1] for i in cluster]
        # conversation_id de referencia (el del primer miembro, si lo tiene).
        ref_conv_id = next((items[i][2] for i in cluster if items[i][2]), None)

        proposal = await _draft_proposal_for_cluster(cluster_instructions)
        if not proposal:
            # El LLM no respondió (caído, sin clave, respuesta ilegible). Estas
            # correcciones NO se han analizado: quedan pendientes, salvo que ya
            # hayan agotado los intentos.
            for i in cluster:
                cid, attempts = items[i][0], items[i][3]
                if attempts + 1 >= MAX_ANALYSIS_ATTEMPTS:
                    given_up_ids.append(cid)
                else:
                    failed_ids.append(cid)
            continue

        summary = proposal["summary"] or cluster_instructions[0]
        question = (
            f"Corrección repetida ({len(cluster)} veces): {summary}"
        )[:2000]
        async with db_session() as db:
            db.add(
                KnowledgeGap(
                    trigger="correction",
                    proposal_kind=proposal["kind"],
                    conversation_id=ref_conv_id,
                    question=question,
                    suggested_answer=proposal["proposal"],
                    status="pendiente",
                )
            )
            await db.commit()
        gaps_created += 1
        processed_ids.extend(cluster_ids)
        # Añadimos el centroide al set de pendientes para que dos clusters
        # nuevos parecidos dentro de la MISMA corrida no generen dos huecos.
        pending_embeddings.append(centroid)

    # 4) Marca procesadas SOLO las correcciones cuyo análisis se completó.
    if failed_ids or given_up_ids:
        # El intento cuenta aunque se reintente: es lo que corta el bucle.
        await _bump_attempts(failed_ids + given_up_ids)
    if given_up_ids:
        logger.error(
            "detect_correction_gaps.analysis_given_up",
            count=len(given_up_ids),
            attempts=MAX_ANALYSIS_ATTEMPTS,
        )
        processed_ids.extend(given_up_ids)
    if failed_ids:
        logger.warning(
            "detect_correction_gaps.analysis_deferred",
            count=len(failed_ids),
            reason="llm_error",
        )
    if deferred:
        logger.info("detect_correction_gaps.over_budget", count=deferred)
    await _mark_processed(processed_ids)

    return {
        "corrections": len(rows),
        "clusters": len(repeated),
        "gaps_created": gaps_created,
        # Correcciones que quedan pendientes para la corrida siguiente.
        "pending_next_run": len(failed_ids) + deferred,
    }


async def _bump_attempts(correction_ids: list) -> None:
    """+1 intento de análisis (sin marcarlas procesadas).

    Es el contador que impide el bucle infinito: una corrección que hace
    fallar siempre al LLM se cierra a los MAX_ANALYSIS_ATTEMPTS intentos.
    """
    if not correction_ids:
        return
    from sqlalchemy import update

    from app.db.session import db_session
    from app.models.agent_correction import AgentCorrection

    async with db_session() as db:
        await db.execute(
            update(AgentCorrection)
            .where(AgentCorrection.id.in_(correction_ids))
            .values(analysis_attempts=AgentCorrection.analysis_attempts + 1)
        )
        await db.commit()


async def _mark_processed(correction_ids: list) -> None:
    if not correction_ids:
        return
    from sqlalchemy import update

    from app.db.session import db_session
    from app.models.agent_correction import AgentCorrection

    async with db_session() as db:
        await db.execute(
            update(AgentCorrection)
            .where(AgentCorrection.id.in_(correction_ids))
            .values(processed_at=datetime.now(timezone.utc))
        )
        await db.commit()


@shared_task(
    name="app.tasks.detect_correction_gaps.detect_correction_gaps",
    bind=True,
    max_retries=1,
)
def detect_correction_gaps(self) -> None:
    try:
        result = asyncio.run(_detect_correction_gaps())
        logger.info("detect_correction_gaps.done", **(result or {}))
    except Exception as exc:  # noqa: BLE001 — best-effort, la corrida de la
        # próxima hora lo reintenta. Nunca debe tumbar el worker.
        logger.error("detect_correction_gaps.error", error=str(exc))
