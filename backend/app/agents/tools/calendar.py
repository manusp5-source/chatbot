"""Tools de Google Calendar para el agente (F8b).

Dos tools:
  - `consultar_disponibilidad(fecha, duracion_min)` — busca slots libres en
    el calendario del negocio para una fecha dada.
  - `agendar_cita(start, end, motivo, email_cliente)` — crea el evento.

Cuando no hay canal Google Calendar conectado, ambas devuelven un error
claro para que el agente sepa que no puede agendar y derive a humano.
"""
from __future__ import annotations

import re
import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.tools.registry import Tool, register_tool
from app.agents.tools.schedule_config import (
    DEFAULT_TZ_NAME,
    ScheduleConfig,
    get_schedule_config,
    now_in,
)
from app.core.logging import get_logger
from app.providers.llm.base import LLMToolSchema
from app.services.google_calendar import create_event, list_busy_slots

logger = get_logger(__name__)

# Solo para parsear entradas sin zona cuando aún no se ha leído la config (y
# como red de seguridad). El horario REAL sale de `schedule_config`.
DEFAULT_TZ = ZoneInfo(DEFAULT_TZ_NAME)


def _parse_date(value: str, tz: ZoneInfo) -> datetime | None:
    """Acepta varios formatos comunes: '2026-05-20', '20/05/2026', etc."""
    value = (value or "").strip()
    formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


async def _calendar_connected() -> bool:
    """¿Hay una cuenta de Google conectada?

    `list_busy_slots` devuelve lista vacía tanto si el calendario está libre
    como si NO HAY calendario. Sin esta comprobación, con Google sin conectar el
    agente ofrecía el día entero libre y el fallo solo aparecía al final, al
    intentar crear el evento, cuando el cliente ya había elegido hora.
    """
    try:
        from app.services.google_oauth import get_access_token

        return bool(await get_access_token("google_calendar"))
    except Exception as e:  # noqa: BLE001 — ante la duda, no bloqueamos
        logger.warning("calendar.tool.connection_check_failed", error=str(e))
        return True


def _slots_for_day(
    date: datetime,
    duration_min: int,
    busy: list[dict[str, str]],
    cfg: ScheduleConfig,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Huecos libres de un día, descontando ocupados, horario y lo ya pasado.

    Tres cosas que antes no hacía:
      - respeta los TRAMOS configurados (y admite partido de mañana y tarde),
      - descarta lo que YA HA PASADO (a las 17:00 seguía ofreciendo las 9:30 de
        esa misma mañana) y aplica la antelación mínima,
      - los días no laborables ni llegan aquí (los corta `consultar_disponibilidad`).
    """
    now = now or now_in(cfg.tz)
    floor = now + timedelta(minutes=cfg.min_notice_min)

    busy_ranges = []
    for b in busy:
        try:
            bs = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
            be = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
            busy_ranges.append((bs.astimezone(cfg.tz), be.astimezone(cfg.tz)))
        except (KeyError, ValueError):
            continue

    slots: list[dict[str, str]] = []
    delta = timedelta(minutes=duration_min)
    step = timedelta(minutes=cfg.slot_step_min)
    for start_t, end_t in cfg.work_hours:
        block_start = date.replace(
            hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0
        )
        block_end = date.replace(
            hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0
        )
        cursor = block_start
        while cursor + delta <= block_end:
            end_cursor = cursor + delta
            if cursor < floor:  # ya pasó (o no da la antelación mínima)
                cursor += step
                continue
            overlap = any(
                not (end_cursor <= bs or cursor >= be) for bs, be in busy_ranges
            )
            if not overlap:
                slots.append(
                    {
                        "start": cursor.isoformat(),
                        "end": end_cursor.isoformat(),
                        "label": cursor.strftime("%H:%M"),
                    }
                )
            cursor += step
    return slots


async def consultar_disponibilidad(args: dict[str, Any], _ctx: dict[str, Any]) -> str:
    fecha_str = args.get("fecha")
    if not fecha_str:
        return json.dumps({"error": "Falta 'fecha'"})
    try:
        duracion = int(args.get("duracion_min", 30))
    except (TypeError, ValueError):
        duracion = 30
    duracion = max(15, min(duracion, 120))

    cfg = await get_schedule_config()
    fecha = _parse_date(fecha_str, cfg.tz)
    if not fecha:
        return json.dumps({
            "error": "No entendí la fecha. Usa formato AAAA-MM-DD o DD/MM/AAAA.",
        })

    now = now_in(cfg.tz)
    # Día pasado: no hay nada que ofrecer.
    if fecha.date() < now.date():
        return json.dumps({
            "error": "Esa fecha ya ha pasado. Pregunta al cliente por un día futuro.",
            "hoy": now.strftime("%Y-%m-%d"),
        })

    # Día no laborable: se dice por qué, en vez de devolver una lista vacía que
    # el agente no sabe interpretar.
    cerrado = cfg.closed_reason(fecha.date())
    if cerrado:
        return json.dumps({
            "fecha": fecha.strftime("%Y-%m-%d"),
            "slots_libres": [],
            "total_libres": 0,
            "cerrado": True,
            "motivo": cerrado,
        })

    if not await _calendar_connected():
        return json.dumps({
            "error": "El calendario del negocio no está conectado, así que no puedo "
            "ver los huecos libres. No propongas horas: ofrece que una persona "
            "del equipo confirme la cita.",
        })

    day_start = fecha.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    try:
        busy = await list_busy_slots(day_start, day_end)
    except Exception as e:
        logger.error("calendar.tool.busy_failed", error=str(e))
        return json.dumps({
            "error": "No pude consultar el calendario ahora. Avisa a un humano para agendar manualmente.",
        })

    slots = _slots_for_day(fecha, duracion, busy, cfg, now=now)
    return json.dumps({
        "fecha": fecha.strftime("%Y-%m-%d"),
        "zona_horaria": cfg.tz_name,
        "duracion_min": duracion,
        "slots_libres": slots[:8],  # tope para no abrumar al cliente
        "total_libres": len(slots),
    })


_MAX_CITAS_POR_CONV_DIA = 3
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


async def agendar_cita(args: dict[str, Any], _ctx: dict[str, Any]) -> str:
    start_str = args.get("start")
    end_str = args.get("end")
    motivo = (args.get("motivo") or "Cita").strip()[:200]
    email_cliente = (args.get("email_cliente") or "").strip()
    nombre_cliente = (args.get("nombre_cliente") or "").strip()

    # Rate-limit por conversación/día: sin él, un cliente (o una inyección)
    # podía llenar el calendario de eventos basura dentro de su cuota LLM/hora.
    conv_id = (_ctx or {}).get("conversation_id")
    if conv_id:
        try:
            from app.core.redis import get_redis

            key = f"calendar:citas:{conv_id}"
            r = get_redis()
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, 24 * 3600)
            if count > _MAX_CITAS_POR_CONV_DIA:
                logger.warning("calendar.tool.rate_limited", conversation_id=str(conv_id))
                return json.dumps({
                    "error": "Ya se han creado varias citas en esta conversación hoy. "
                    "Deriva a una persona si el cliente necesita más cambios.",
                })
        except Exception:
            pass  # Redis caído: no bloqueamos la cita por eso

    # El email lo da el cliente sin verificar: si no tiene pinta de email, no
    # lo invitamos al evento (Google enviaría la invitación a esa dirección).
    if email_cliente and not _EMAIL_RE.match(email_cliente):
        email_cliente = ""

    if not start_str or not end_str:
        return json.dumps({"error": "Faltan 'start' o 'end' (ISO 8601)"})
    try:
        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str)
    except ValueError:
        return json.dumps({"error": "Formato de fecha/hora inválido. Usa ISO 8601."})

    cfg = await get_schedule_config()
    if start.tzinfo is None:
        start = start.replace(tzinfo=cfg.tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=cfg.tz)
    if end <= start:
        return json.dumps({"error": "La hora de fin debe ser posterior al inicio."})

    # EN EL PASADO NO. Antes solo se comprobaba que el fin fuera posterior al
    # inicio: "apúntame el 3 de enero" creaba el evento en el año pasado, y el
    # cliente se quedaba con una cita que nadie iba a ver.
    now = now_in(cfg.tz)
    if start <= now:
        return json.dumps({
            "error": "Esa fecha y hora ya han pasado. Confirma con el cliente un "
            "momento futuro (y el año, si te ha dado solo el día y el mes).",
            "ahora": now.isoformat(),
        })
    if start < now + timedelta(minutes=cfg.min_notice_min):
        return json.dumps({
            "error": f"El negocio necesita al menos {cfg.min_notice_min} minutos de "
            "antelación. Propón una hora más tarde.",
        })

    cerrado = cfg.closed_reason(start.astimezone(cfg.tz).date())
    if cerrado:
        return json.dumps({"error": f"{cerrado} Propón otro día."})

    description_parts = ["Reservado por el agente (datos del cliente SIN verificar)."]
    if nombre_cliente:
        description_parts.append(f"Cliente: {nombre_cliente}")
    if email_cliente:
        description_parts.append(f"Email: {email_cliente}")
    description = "\n".join(description_parts)

    attendees = [email_cliente] if email_cliente else None
    try:
        event = await create_event(
            start=start,
            end=end,
            summary=motivo,
            description=description,
            attendee_emails=attendees,
        )
    except Exception as e:
        logger.error("calendar.tool.create_failed", error=str(e))
        return json.dumps({
            "error": "No pude crear la cita ahora. Avisa a un humano para agendarla manualmente.",
        })
    if not event:
        return json.dumps({
            "error": "Google Calendar no está conectado. Avisa al administrador para conectarlo.",
        })
    return json.dumps({
        "ok": True,
        "event_id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "start": start.isoformat(),
        "end": end.isoformat(),
    })


register_tool(
    Tool(
        schema=LLMToolSchema(
            name="consultar_disponibilidad",
            description=(
                "Consulta los huecos libres en el calendario del negocio para una fecha dada. "
                "Devuelve hasta 8 slots libres con su hora de inicio. Úsalo cuando un cliente "
                "pida cita para saber cuándo proponerle hora."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fecha": {
                        "type": "string",
                        "description": "Fecha a consultar en formato AAAA-MM-DD o DD/MM/AAAA",
                    },
                    "duracion_min": {
                        "type": "integer",
                        "description": "Duración esperada de la cita en minutos. Default 30.",
                        "default": 30,
                    },
                },
                "required": ["fecha"],
            },
        ),
        handler=consultar_disponibilidad,
    )
)

register_tool(
    Tool(
        schema=LLMToolSchema(
            name="agendar_cita",
            description=(
                "Crea una cita en el calendario del negocio. Solo úsalo DESPUÉS de haber "
                "confirmado con el cliente la fecha, hora y motivo. Si tienes su email se "
                "le manda invitación de Google Calendar."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "Inicio en ISO 8601 con zona, ej. 2026-05-20T10:00:00+02:00",
                    },
                    "end": {
                        "type": "string",
                        "description": "Fin en ISO 8601 con zona",
                    },
                    "motivo": {
                        "type": "string",
                        "description": "Breve descripción de la cita (max 200 chars)",
                    },
                    "nombre_cliente": {
                        "type": "string",
                        "description": "Nombre del cliente (opcional, va en la descripción del evento)",
                    },
                    "email_cliente": {
                        "type": "string",
                        "description": "Email del cliente (si lo tienes, se le manda invitación)",
                    },
                },
                "required": ["start", "end", "motivo"],
            },
        ),
        handler=agendar_cita,
    )
)
