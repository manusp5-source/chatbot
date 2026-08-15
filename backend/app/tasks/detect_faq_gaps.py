"""Autoaprendizaje · Fase 3 — FAQ automática.

Tarea Celery periódica (DIARIA, ver beat_schedule en app/tasks/__init__.py).
Agrupa las PREGUNTAS DE CLIENTE más frecuentes de TODOS los canales por
similitud y, cuando una misma pregunta se repite lo suficiente (cluster ≥3),
propone una Q&A para la base de conocimiento que la operadora revisa en
"Aprendizajes" (pantalla y aprobación reutilizadas de la Fase 2).

  - Fuente: mensajes de cliente (`Message.rol == user`) en una VENTANA MÓVIL
    reciente (últimos FAQ_WINDOW_DAYS). NO mensajes del agente/operadora.
  - Frecuencia = tamaño del cluster (cuántas veces se ha preguntado lo mismo).
  - Mejor respuesta = se sintetiza (UNA llamada LLM) a partir de las RESPUESTAS
    REALES que siguieron a esas preguntas (assistant/operator justo después), de
    forma GENÉRICA y SIN DATOS PERSONALES (cero alucinación, cero PII).

Siempre con aprobación humana: aquí solo se crea un borrador en "Aprendizajes".
Como `trigger="faq"` lleva `proposal_kind="content"`, "Aprobar" sigue el camino
existente de contenido (Document + index_document_by_id) sin tocar nada.

Diseño (barato e idempotente) — VENTANA MÓVIL + DEDUPE ESTRICTO (sin watermark):
  - Una pregunta solo es "frecuente" cuando SE REPITE, así que reanalizar la
    ventana en cada corrida es lo correcto (un watermark se saltaría justo las
    repeticiones que buscamos). Lo que evita duplicados NO es un watermark sino
    el dedupe: por eso no añadimos columna/tabla de estado (sin migración).
  - Coste acotado: la corrida es DIARIA (la frecuencia no necesita ser frecuente)
    y se topa el nº de mensajes embebidos por corrida (FAQ_MAX_MESSAGES_PER_RUN,
    los más recientes de la ventana) y el nº de propuestas creadas
    (FAQ_MAX_PROPOSALS_PER_RUN).
  - Filtro de no-preguntas/smalltalk: heurística ligera (saludos, "gracias"…) +
    el paso LLM rechaza clusters que no sean FAQ de negocio.
  - Dedupe doble: (a) se saltan clusters YA cubiertos por la KB (búsqueda de
    embeddings sobre los Chunks por encima del umbral de kb_search) y (b)
    clusters que ya tienen un `KnowledgeGap` pendiente parecido (CUALQUIER
    trigger), comparando embeddings en Python.
  - Best-effort: cualquier fallo se loguea y NO rompe nada (no afecta al turno
    del agente ni a ninguna otra tarea).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import and_, desc, select

from app.core.logging import get_logger
from app.tasks.detect_correction_gaps import _centroid, _cosine, cluster_by_similarity

logger = get_logger(__name__)

# Ventana móvil: solo miramos preguntas de clientes de los últimos N días. La
# "frecuencia" se mide dentro de esta ventana (lo viejo deja de contar).
FAQ_WINDOW_DAYS = 90
# Umbral de similitud coseno para considerar dos preguntas "la misma". Algo más
# laxo que el de correcciones (0.8) porque las preguntas de clientes son más
# ruidosas/variadas en redacción.
SIMILARITY_THRESHOLD = 0.78
# Mínimo de preguntas parecidas para considerarlo una FAQ real (recurrente, no
# una pregunta puntual de una sola persona).
MIN_CLUSTER_SIZE = 3
# Top-N clusters (por frecuencia) que consideramos por corrida.
FAQ_TOP_N = 20
# Tope de mensajes a embeber por corrida (guardia de coste; los más recientes de
# la ventana). Si hay más, se analizan en la corrida del día siguiente.
FAQ_MAX_MESSAGES_PER_RUN = 400
# Tope de propuestas (huecos) creadas por corrida (guardia de coste/ruido).
FAQ_MAX_PROPOSALS_PER_RUN = 10
# Máx. de huecos pendientes (cualquier trigger) que revisamos para el dedupe
# entre corridas (son pocos; comparamos en Python sobre sus embeddings).
MAX_PENDING_FOR_DEDUPE = 200
# Cuántas respuestas reales (las que siguieron a las preguntas del cluster)
# pasamos al LLM como evidencia para sintetizar la respuesta.
MAX_EVIDENCE_ANSWERS = 8
# Umbral de cobertura por KB: reutilizamos el de kb_search (consultar_kb usa
# 0.3 en match_chunks). Si la KB ya responde por encima de esto, no proponemos.
KB_COVERAGE_THRESHOLD = 0.3

_DRAFT_MAX_LEN = 5000
_QUESTION_MAX_LEN = 2000
_MIN_QUESTION_CHARS = 12

# Heurística ligera de "no-pregunta"/smalltalk: si tras normalizar el mensaje es
# EXACTAMENTE uno de estos (o empieza por un saludo y es muy corto), se descarta
# antes de gastar embeddings. El LLM hace el filtrado fino después.
_SMALLTALK_EXACT = {
    "hola", "holaa", "holaaa", "buenas", "buenos dias", "buenos días",
    "buenas tardes", "buenas noches", "hey", "ey", "que tal", "qué tal",
    "gracias", "muchas gracias", "mil gracias", "ok", "okay", "vale",
    "perfecto", "genial", "adios", "adiós", "hasta luego", "chao", "chau",
    "si", "sí", "no", "👍", "👌", "🙏", "😊", "jaja", "jajaja", "buenas!",
}
_GREETING_PREFIXES = (
    "hola", "buenas", "buenos dias", "buenos días", "buenas tardes",
    "buenas noches", "gracias", "muchas gracias",
)


def _normalize(text: str) -> str:
    """Minúsculas + colapsa espacios + quita signos de puntuación de borde."""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip(" \t\n.!¡?¿,;:")


def is_probable_question(text: str) -> bool:
    """Heurística PURA (testeable sin BD): ¿parece una pregunta de negocio?

    Descarta smalltalk/saludos y mensajes demasiado cortos. NO intenta ser
    perfecta: el paso LLM por cluster rechaza lo que se cuele. Mejor dejar pasar
    de más aquí (recall) y filtrar fino con el LLM, que perder FAQs reales.
    """
    norm = _normalize(text)
    if not norm or len(norm) < _MIN_QUESTION_CHARS:
        return False
    if norm in _SMALLTALK_EXACT:
        return False
    # Saludo muy corto sin sustancia ("hola buenas", "gracias por todo").
    if len(norm) < 25 and any(norm.startswith(p) for p in _GREETING_PREFIXES):
        return False
    # Sin ninguna letra (solo emojis/números/símbolos) → no es pregunta.
    if not re.search(r"[a-záéíóúñ]", norm):
        return False
    return True


def select_top_clusters(
    clusters: list[list[int]],
    min_size: int = MIN_CLUSTER_SIZE,
    top_n: int = FAQ_TOP_N,
) -> list[list[int]]:
    """Función PURA (testeable sin BD): filtra por tamaño y ordena por frecuencia.

    Devuelve los `top_n` clusters con tamaño >= `min_size`, ordenados de MÁS a
    MENOS frecuente (cluster más grande = pregunta más repetida primero).
    """
    eligibles = [c for c in clusters if len(c) >= min_size]
    eligibles.sort(key=len, reverse=True)
    return eligibles[:top_n]


def build_faq_prompt(questions: list[str], answers: list[str]) -> tuple[str, str]:
    """Construye (system, user) para sintetizar la Q&A. Función PURA y testeable.

    El system instruye explícitamente: anonimizar (sin nombres, teléfonos,
    emails ni datos personales), generalizar, NO inventar (apoyarse SOLO en las
    respuestas reales) y rechazar el cluster si no es una FAQ de negocio.
    """
    q_sample = "\n".join(f"- {q}" for q in questions[:8] if q)
    if answers:
        a_sample = "\n".join(f"- {a}" for a in answers[:MAX_EVIDENCE_ANSWERS] if a)
        evidencia = (
            "Respuestas REALES que el negocio dio a esas preguntas (úsalas como "
            "ÚNICA fuente de verdad; no inventes datos que no estén aquí):\n"
            f"{a_sample}"
        )
    else:
        evidencia = (
            "No hay respuestas previas fiables para estas preguntas. En ese caso "
            'deja "answer" como cadena vacía (la operadora la redactará).'
        )

    system = (
        "Eres un asistente que destila PREGUNTAS FRECUENTES (FAQ) reales de "
        "clientes de un negocio en una entrada limpia para su base de "
        "conocimiento. La operadora revisará y aprobará antes de publicar "
        "(nunca se publica solo).\n\n"
        "Tarea: a partir de varias formulaciones de la MISMA pregunta y de las "
        "respuestas reales que se dieron, redacta UNA pregunta canónica y UNA "
        "respuesta genérica.\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. PRIVACIDAD: elimina TODO dato personal (nombres, teléfonos, emails, "
        "direcciones, números de pedido, importes concretos de una persona). La "
        "respuesta debe ser GENÉRICA y aplicable a cualquier cliente.\n"
        "2. NO INVENTES: apóyate solo en las respuestas reales aportadas. Si no "
        "puedes deducir una respuesta fiable, deja la respuesta vacía.\n"
        "3. SOLO FAQ DE NEGOCIO: si las preguntas son saludos, charla, quejas "
        "puntuales o algo que no sea una duda recurrente y útil de servicio/"
        'precio/horario/política/procedimiento, responde {"is_faq": false}.\n'
        "4. Idioma: español, tono claro y cercano.\n\n"
        "Responde SOLO con JSON válido, sin texto adicional:\n"
        '{"is_faq": true|false, "question": "<pregunta canónica>", '
        '"answer": "<respuesta genérica y anonimizada, o cadena vacía>"}'
    )
    user = (
        "Formulaciones de la pregunta (de varios clientes):\n"
        f"{q_sample}\n\n"
        f"{evidencia}"
    )
    return system, user


async def _synthesize_qa(questions: list[str], answers: list[str]) -> dict | None:
    """UNA llamada LLM: sintetiza la Q&A canónica y anonimizada.

    Devuelve {"question": str, "answer": str} si es una FAQ de negocio, o None
    (no es FAQ / error / parseo fallido).
    """
    from app.providers.llm import get_llm_provider
    from app.providers.llm.base import LLMMessage

    system, user = build_faq_prompt(questions, answers)
    try:
        llm = get_llm_provider()
        resp = await llm.complete(
            messages=[
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=user),
            ],
            temperature=0.2,
            max_tokens=500,
            source="learning",
        )
        raw = (resp.content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        data = json.loads(raw[start : end + 1])
        if not bool(data.get("is_faq")):
            return None
        question = str(data.get("question") or "").strip()
        answer = str(data.get("answer") or "").strip()
        if not question:
            return None
        return {"question": question[:_QUESTION_MAX_LEN], "answer": answer[:_DRAFT_MAX_LEN]}
    except Exception as e:  # noqa: BLE001
        logger.warning("detect_faq_gaps.llm_error", error=str(e))
        return None


async def _kb_covers(embedding: list[float]) -> bool:
    """¿La KB ya responde a esta pregunta? (búsqueda de embeddings sobre Chunks).

    Reutiliza la función match_chunks de la BD (la misma que consultar_kb) con el
    umbral de kb_search. Si hay al menos un chunk por encima del umbral, el
    agente ya sabe responder → no proponemos una FAQ redundante.
    """
    if not embedding or not any(embedding):
        return False
    from sqlalchemy import text

    from app.db.session import db_session

    emb_str = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    try:
        async with db_session() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT 1 FROM match_chunks(CAST(:emb AS vector), :threshold, :limit)"
                    ),
                    {"emb": emb_str, "threshold": KB_COVERAGE_THRESHOLD, "limit": 1},
                )
            ).fetchall()
        return len(rows) > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("detect_faq_gaps.kb_coverage_error", error=str(e))
        # En la duda, NO bloqueamos la propuesta (la operadora decide).
        return False


async def _detect_faq_gaps() -> dict:
    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.models.knowledge_gap import KnowledgeGap
    from app.models.message import Message, MessageRole
    from app.providers.embeddings import embed_texts

    window_start = datetime.now(timezone.utc) - timedelta(days=FAQ_WINDOW_DAYS)

    # 1) Preguntas de cliente en la ventana (todos los canales), recientes
    #    primero y con tope de coste. Traemos id + contenido + conversación +
    #    timestamp para luego buscar la respuesta que siguió a cada una.
    async with db_session() as db:
        rows = (
            await db.execute(
                select(
                    Message.id,
                    Message.conversation_id,
                    Message.contenido,
                    Message.created_at,
                )
                .where(
                    Message.rol == MessageRole.user,
                    Message.created_at >= window_start,
                    Message.contenido.isnot(None),
                )
                .order_by(desc(Message.created_at))
                .limit(FAQ_MAX_MESSAGES_PER_RUN)
            )
        ).all()

    if not rows:
        return {"messages": 0, "clusters": 0, "gaps_created": 0}

    # Filtro heurístico de no-preguntas ANTES de embeber (ahorra coste).
    items: list[tuple] = []  # (msg_id, question, conversation_id, created_at)
    for r in rows:
        q = (r.contenido or "").strip()
        if q and is_probable_question(q):
            items.append((r.id, q[:_QUESTION_MAX_LEN], r.conversation_id, r.created_at))

    if len(items) < MIN_CLUSTER_SIZE:
        return {"messages": len(rows), "clusters": 0, "gaps_created": 0}

    # 2) Embeddings (un solo batch) y clustering por similitud.
    questions = [it[1] for it in items]
    embeddings = await embed_texts(questions)
    if (
        not embeddings
        or len(embeddings) != len(items)
        or not any(any(e) for e in embeddings)
    ):
        # Sin embeddings reales (p. ej. sin clave): no inventamos clusters.
        logger.warning("detect_faq_gaps.no_embeddings")
        return {"messages": len(rows), "clusters": 0, "gaps_created": 0}

    clusters = cluster_by_similarity(embeddings, SIMILARITY_THRESHOLD)
    top_clusters = select_top_clusters(clusters, MIN_CLUSTER_SIZE, FAQ_TOP_N)
    if not top_clusters:
        return {"messages": len(rows), "clusters": 0, "gaps_created": 0}

    # 3) Dedupe entre corridas: embeddings de las preguntas de los huecos ya
    #    pendientes (CUALQUIER trigger), para no proponer dos veces lo mismo.
    async with db_session() as db:
        pending = (
            await db.execute(
                select(KnowledgeGap)
                .where(KnowledgeGap.status == "pendiente")
                .order_by(desc(KnowledgeGap.created_at))
                .limit(MAX_PENDING_FOR_DEDUPE)
            )
        ).scalars().all()
    pending_texts = [(g.question or "").strip() for g in pending if (g.question or "").strip()]
    pending_embeddings = await embed_texts(pending_texts) if pending_texts else []

    gaps_created = 0
    for cluster in top_clusters:
        if gaps_created >= FAQ_MAX_PROPOSALS_PER_RUN:
            break
        member_embs = [embeddings[i] for i in cluster]
        centroid = _centroid(member_embs)

        # (a) ¿Ya hay un hueco pendiente parecido? → saltar (no gastar LLM).
        if any(
            _cosine(centroid, pe) >= SIMILARITY_THRESHOLD for pe in pending_embeddings
        ):
            continue

        # (b) ¿La KB ya lo cubre? → saltar (el agente ya sabe responder).
        if await _kb_covers(centroid):
            continue

        # 4) Reunir las RESPUESTAS REALES que siguieron a estas preguntas para
        #    fundamentar la síntesis (cero alucinación).
        cluster_questions = [items[i][1] for i in cluster]
        refs = [(items[i][2], items[i][3]) for i in cluster]  # (conv_id, created_at)
        ref_conv_id = next((c for c, _ in refs if c), None)
        answers = await _gather_following_answers(refs)

        proposal = await _synthesize_qa(cluster_questions, answers)
        if not proposal:
            continue

        frequency = len(cluster)
        question = (
            f"Pregunta frecuente ({frequency} veces): {proposal['question']}"
        )[:_QUESTION_MAX_LEN]
        async with db_session() as db:
            db.add(
                KnowledgeGap(
                    trigger="faq",
                    proposal_kind="content",
                    conversation_id=ref_conv_id,
                    question=question,
                    suggested_answer=proposal["answer"] or None,
                    status="pendiente",
                )
            )
            await db.commit()
        gaps_created += 1
        # Evita que dos clusters parecidos de la MISMA corrida generen dos huecos.
        pending_embeddings.append(centroid)

    return {
        "messages": len(rows),
        "clusters": len(top_clusters),
        "gaps_created": gaps_created,
    }


async def _gather_following_answers(refs: list[tuple]) -> list[str]:
    """Para cada pregunta del cluster, recupera la PRIMERA respuesta que la siguió.

    `refs` es una lista de (conversation_id, asked_at). Devuelve los textos de los
    mensajes assistant/operator inmediatamente posteriores a cada pregunta en su
    misma conversación. Estas son la materia prima (real) para la síntesis.
    """
    from app.db.session import db_session
    from app.models.message import Message, MessageRole

    answers: list[str] = []
    seen: set[str] = set()
    async with db_session() as db:
        for conv_id, asked_at in refs:
            if conv_id is None or asked_at is None:
                continue
            row = (
                await db.execute(
                    select(Message.contenido)
                    .where(
                        and_(
                            Message.conversation_id == conv_id,
                            Message.created_at > asked_at,
                            Message.rol.in_(
                                [MessageRole.assistant, MessageRole.operator]
                            ),
                            Message.contenido.isnot(None),
                        )
                    )
                    .order_by(Message.created_at)
                    .limit(1)
                )
            ).first()
            if row and row[0]:
                txt = row[0].strip()
                key = txt[:200]
                if txt and key not in seen:
                    seen.add(key)
                    answers.append(txt[:1000])
            if len(answers) >= MAX_EVIDENCE_ANSWERS:
                break
    return answers


@shared_task(
    name="app.tasks.detect_faq_gaps.detect_faq_gaps",
    bind=True,
    max_retries=1,
)
def detect_faq_gaps(self) -> None:
    try:
        result = asyncio.run(_detect_faq_gaps())
        logger.info("detect_faq_gaps.done", **(result or {}))
    except Exception as exc:  # noqa: BLE001 — best-effort, la corrida del día
        # siguiente lo reintenta. Nunca debe tumbar el worker.
        logger.error("detect_faq_gaps.error", error=str(exc))
