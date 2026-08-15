"""Autoaprendizaje · Fase 2 — reglas de estilo aprendidas: lectura + inyección.

Las reglas de estilo aprobadas (`LearnedRule`) se inyectan en el prompt de
sistema de TODOS los agentes conversacionales (negocio único → reglas globales:
una pauta como "sé más cálida" aplica a cualquier canal). Aquí están:

  - `render_learned_rules_block()`: compone el bloque de texto cifrado→claro,
    acotado (tope de reglas + presupuesto de caracteres) para que el prompt no se
    infle. Devuelve "" si no hay reglas activas (no se inyecta nada).
  - `build_rules_block_from_texts()`: la parte PURA (sin BD) que aplica el tope y
    el formato — testeable sin Postgres.

El bloque va claramente delimitado para que el modelo lo distinga del prompt base
y de las instrucciones del cliente.
"""
from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger(__name__)

# Topes para que el prompt no se infle sin control. La regla es corta (1-2
# líneas); con ~15 reglas y ~1500 caracteres cubrimos de sobra el caso real sin
# convertir el prompt en una lista interminable.
MAX_RULES = 15
MAX_BLOCK_CHARS = 1500
MAX_RULE_CHARS = 200

_HEADER = "Reglas aprendidas (estilo)"
_INTRO = (
    "Pautas que el equipo ha aprobado a partir de correcciones reales. Tienen "
    "prioridad sobre el estilo por defecto. Aplícalas siempre:"
)


def build_rules_block_from_texts(texts: list[str]) -> str:
    """Compone el bloque de reglas a partir de textos ya en claro (función PURA).

    Aplica: limpieza, recorte por regla, tope de nº de reglas y presupuesto de
    caracteres del bloque. Devuelve "" si no queda ninguna regla utilizable.
    """
    cleaned: list[str] = []
    for t in texts:
        rule = " ".join((t or "").split()).strip()
        if not rule:
            continue
        cleaned.append(rule[:MAX_RULE_CHARS])
        if len(cleaned) >= MAX_RULES:
            break
    if not cleaned:
        return ""

    lines: list[str] = []
    used = 0
    for rule in cleaned:
        line = f"- {rule}"
        # +1 por el salto de línea. Si una regla más se pasa del presupuesto,
        # paramos (mejor cortar que inflar el prompt).
        if used + len(line) + 1 > MAX_BLOCK_CHARS and lines:
            break
        lines.append(line)
        used += len(line) + 1

    body = "\n".join(lines)
    return f"\n\n--- {_HEADER} ---\n{_INTRO}\n{body}\n--- fin reglas aprendidas ---"


async def get_active_rule_texts() -> list[str]:
    """Devuelve los textos (en claro) de las reglas de estilo activas.

    Más recientes primero (las últimas aprobadas pesan más si hay que recortar).
    Best-effort: ante cualquier error devuelve [] para no romper el turno.
    """
    try:
        from sqlalchemy import desc, select

        from app.db.session import db_session
        from app.models.learned_rule import LearnedRule

        async with db_session() as db:
            rows = (
                await db.execute(
                    select(LearnedRule)
                    .where(LearnedRule.active.is_(True))
                    .order_by(desc(LearnedRule.created_at))
                    .limit(MAX_RULES)
                )
            ).scalars().all()
        return [r.text for r in rows if r.text]
    except Exception as e:  # noqa: BLE001 — best-effort, nunca rompe el turno
        logger.warning("learned_rules.read_error", error=str(e))
        return []


async def render_learned_rules_block() -> str:
    """Bloque de "Reglas aprendidas" listo para concatenar al prompt de sistema.

    "" si no hay reglas activas (no se inyecta nada). Nunca lanza.
    """
    texts = await get_active_rule_texts()
    if not texts:
        return ""
    return build_rules_block_from_texts(texts)
