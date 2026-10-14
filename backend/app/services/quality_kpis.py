"""KPIs de calidad de la atención: lo que el dashboard de volumen no enseña.

El dashboard ya da volumen, tasa de derivación y tasa de resolución por IA. Esos números
dicen cuánto trabaja el agente, no cómo de bien. Estos tres sí:

  - Huecos de conocimiento por cada 100 conversaciones: cuántas veces el agente consultó la KB
    y no encontró nada (`trigger='kb_miss'`). Es la señal más directa de qué falta documentar.
  - Tiempo hasta la primera respuesta humana tras una derivación: mediana y p90. Una derivación
    es una promesa («te escriben en cuanto puedan»); esto mide cuánto tarda en cumplirse.
  - Derivaciones sin respuesta humana todavía: las promesas que siguen sin cumplirse.

No hay CSAT: no existe en el producto ningún punto donde el cliente valore la atención, y
fabricar ese dato desde otras señales sería inventarlo. Queda anotado como funcionalidad
pendiente, no como métrica aproximada.
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def minutos_entre(desde: datetime, hasta: datetime) -> float:
    return max(0.0, (hasta - desde).total_seconds() / 60.0)


def percentil(valores: list[float], p: float) -> float | None:
    """Percentil por rango más cercano (sin interpolar): con pocas muestras, un valor real."""
    if not valores:
        return None
    ordenados = sorted(valores)
    rango = max(1, math.ceil(p / 100.0 * len(ordenados)))
    return ordenados[rango - 1]


def resumen_primera_respuesta(
    pares: list[tuple[datetime, datetime | None]],
) -> dict:
    """`pares` = (derivada_en, primera_respuesta_humana_en | None) por conversación."""
    tiempos = [minutos_entre(d, r) for d, r in pares if r is not None]
    mediana = percentil(tiempos, 50)
    p90 = percentil(tiempos, 90)
    return {
        "derivaciones": len(pares),
        "respondidas": len(tiempos),
        "sin_respuesta": sum(1 for _, r in pares if r is None),
        "mediana_min": round(mediana, 1) if mediana is not None else None,
        "p90_min": round(p90, 1) if p90 is not None else None,
    }


async def calcular(db: AsyncSession, desde: datetime) -> dict:
    total = (
        await db.execute(
            text("SELECT COUNT(*) FROM conversations WHERE started_at >= :desde"),
            {"desde": desde},
        )
    ).scalar_one()

    huecos = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM knowledge_gaps "
                "WHERE trigger = 'kb_miss' AND created_at >= :desde"
            ),
            {"desde": desde},
        )
    ).scalar_one()

    filas = (
        await db.execute(
            text(
                """
                SELECT c.derivada_a_humano_at AS derivada,
                       (
                         SELECT MIN(m.created_at)
                         FROM messages m
                         WHERE m.conversation_id = c.id
                           AND m.rol::text = 'operator'
                           AND m.created_at > c.derivada_a_humano_at
                       ) AS respondida
                FROM conversations c
                WHERE c.derivada_a_humano_at >= :desde
                """
            ),
            {"desde": desde},
        )
    ).fetchall()

    total = int(total or 0)
    huecos = int(huecos or 0)
    return {
        "conversaciones": total,
        "huecos_kb": huecos,
        "huecos_kb_por_100": round(huecos * 100.0 / total, 1) if total else 0.0,
        "primera_respuesta_humana": resumen_primera_respuesta(
            [(f.derivada, f.respondida) for f in filas]
        ),
    }
