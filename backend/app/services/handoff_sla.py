"""Escalado por tiempo de las derivaciones a humano que nadie ha contestado.

Una derivación es una promesa: el bot le dice al cliente «te contestan en cuanto puedan».
Hasta ahora nada vigilaba que se cumpliera: si la derivación pasaba sin que nadie mirase el
inbox, la conversación se quedaba esperando sin que el equipo recibiera ningún segundo aviso.

Esto es el primer escalón, deliberadamente pequeño: pasado un plazo configurable sin una sola
respuesta humana, se manda UN aviso interno (web push) por derivación. No reasigna, no
escribe al cliente y no cambia el estado: avisar al equipo es lo seguro, lo demás son
decisiones de producto (a quién reasignar, con qué texto) que no se toman solas.

APAGADO por defecto (`handoff.sla_minutes` = 0): un negocio sin nadie de guardia recibiría
avisos que nadie puede atender.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SETTING_SLA_MINUTES = "handoff.sla_minutes"
SETTING_AVISO_FUERA_DE_HORARIO = "handoff.out_of_hours_notice"

TEXTO_FUERA_DE_HORARIO = (
    " Ahora mismo estamos fuera de horario, así que te contestarán cuando volvamos a abrir."
)

# Tope por pasada: si hay una avalancha, el resto se avisa en la siguiente (cada 5 min).
LIMITE_POR_PASADA = 50


def sla_minutos(valor: str | None) -> int:
    """Lee el ajuste. Vacío, no numérico o ≤ 0 → 0 (apagado)."""
    try:
        minutos = int((valor or "0").strip())
    except ValueError:
        return 0
    return max(0, minutos)


def aviso_fuera_de_horario_activo(valor: str | None) -> bool:
    return (valor or "").strip().lower() in {"1", "true", "si", "sí", "on"}


async def derivaciones_vencidas(
    db: AsyncSession, sla_min: int, ahora: datetime, limite: int = LIMITE_POR_PASADA
) -> list[dict]:
    """Conversaciones en `humano` derivadas hace más de `sla_min` sin ningún mensaje de operador.

    «Sin respuesta» = ningún mensaje `operator` posterior a la derivación: el mismo criterio
    que usa el KPI de tiempo hasta la primera respuesta humana (`quality_kpis`).
    """
    if sla_min <= 0:
        return []
    filas = (
        await db.execute(
            text(
                """
                SELECT c.id AS id, c.canal::text AS canal, c.derivada_a_humano_at AS derivada
                FROM conversations c
                WHERE c.status::text = 'humano'
                  AND c.derivada_a_humano_at IS NOT NULL
                  AND c.derivada_a_humano_at <= :corte
                  AND NOT EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.conversation_id = c.id
                        AND m.rol::text = 'operator'
                        AND m.created_at > c.derivada_a_humano_at
                  )
                ORDER BY c.derivada_a_humano_at ASC
                LIMIT :limite
                """
            ),
            {"corte": ahora - timedelta(minutes=sla_min), "limite": limite},
        )
    ).fetchall()
    return [
        {
            "conversation_id": str(f.id),
            "canal": f.canal,
            "derivada": f.derivada,
            "esperando_min": int((ahora - f.derivada).total_seconds() // 60),
        }
        for f in filas
    ]
