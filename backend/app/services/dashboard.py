"""Lógica de agregación para el dashboard/Home del operador.

Funciones **puras** (sin I/O) para que sean unit-testables sin base de datos.
El endpoint en `app/api/admin.py` ejecuta las queries y delega aquí el cálculo
de buckets por hora, clasificación de "necesita atención" y previews.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def minutes_between(earlier: datetime | None, later: datetime) -> int:
    """Minutos enteros transcurridos entre `earlier` y `later` (>= 0).

    Devuelve 0 si `earlier` es None o si es posterior a `later`.
    """
    if earlier is None:
        return 0
    delta = later - earlier
    return max(0, int(delta.total_seconds() // 60))


def build_hourly_series(
    today_counts: dict[int, int],
    last7_counts: dict[int, int],
) -> list[dict[str, float | int]]:
    """Construye 24 buckets (hora local 0-23) para el gráfico de actividad.

    - `today`: conteo de hoy en esa hora.
    - `avg_7d`: media de los 7 días previos (conteo acumulado / 7).
    Rellena con 0 las horas sin actividad para que el eje X esté completo.
    """
    out: list[dict[str, float | int]] = []
    for hour in range(24):
        out.append(
            {
                "hour": hour,
                "today": int(today_counts.get(hour, 0)),
                "avg_7d": round(last7_counts.get(hour, 0) / 7, 2),
            }
        )
    return out


def preview_text(contenido: str | None, media_type: str | None, max_len: int = 80) -> str:
    """Resumen corto del último mensaje para la tarjeta de atención.

    Colapsa espacios/saltos y trunca con elipsis. Si no hay texto pero sí
    adjunto, muestra el tipo de media. Nunca expone campos cifrados (resumen,
    transcripción de audio): solo `contenido` en claro.
    """
    if contenido:
        text = " ".join(contenido.split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "…"
    if media_type:
        return f"[{media_type}]"
    return ""


def classify_attention(
    rows: list[dict[str, Any]],
    now: datetime,
    stale_minutes: int,
) -> dict[str, list[dict[str, Any]]]:
    """Separa las conversaciones que necesitan atención en dos grupos.

    - **handoffs**: derivadas a humano (``status == "humano"``). El operador
      debe atenderlas; se ordenan por antigüedad de la espera (más urgente
      primero).
    - **waiting**: el último mensaje es del cliente (``last_rol == "user"``) y
      lleva ``>= stale_minutes`` sin respuesta. Señal de que el bot no contestó
      (pausado, error, etc.) y alguien debería mirar.

    Cada `row` es un dict con los campos del JOIN (conversation/contact/último
    mensaje). Función pura: no toca BD ni Redis.
    """
    handoffs: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []

    for row in rows:
        status = row.get("status")
        last_rol = row.get("last_rol")
        last_message_at = row.get("last_message_at")
        derivada_at = row.get("derivada_a_humano_at")

        base: dict[str, Any] = {
            "conversation_id": str(row.get("conversation_id")),
            "canal": row.get("canal"),
            "status": status,
            "contact_name": row.get("contact_name"),
            "contact_phone": row.get("contact_phone"),
            "assigned_to": str(row["asignada_a"]) if row.get("asignada_a") else None,
            "last_message_at": last_message_at.isoformat() if last_message_at else None,
            "derivada_a_humano_at": derivada_at.isoformat() if derivada_at else None,
            "preview": preview_text(row.get("last_contenido"), row.get("last_media_type")),
        }

        if status == "humano":
            item = dict(base)
            # Para handoffs medimos la espera desde que se derivó (o, en su
            # defecto, desde el último mensaje).
            item["waiting_minutes"] = minutes_between(derivada_at or last_message_at, now)
            handoffs.append(item)
        elif last_rol == "user" and last_message_at is not None:
            mins = minutes_between(last_message_at, now)
            if mins >= stale_minutes:
                item = dict(base)
                item["waiting_minutes"] = mins
                waiting.append(item)

    handoffs.sort(key=lambda x: x["waiting_minutes"], reverse=True)
    waiting.sort(key=lambda x: x["waiting_minutes"], reverse=True)
    return {"handoffs": handoffs, "waiting": waiting}
