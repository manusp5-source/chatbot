"""Tarea periódica: avisa al equipo de las derivaciones que siguen sin respuesta humana.

Cada 5 minutos (ver beat_schedule en app/tasks/__init__.py). Si `handoff.sla_minutes` es 0 (el
valor por defecto) no hace nada. La lógica de selección vive en `services/handoff_sla.py`.

Un solo aviso por derivación: la clave de Redis lleva la marca de tiempo de la derivación, así
que si la conversación se deriva otra vez más adelante, vuelve a poder avisar.
"""
import asyncio
from datetime import datetime, timezone

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)

_CLAVE = "handoff:sla_alert:{conv_id}:{ts}"
_TTL_S = 7 * 24 * 3600


@shared_task(name="app.tasks.escalate_handoffs.escalate_handoffs", bind=True, max_retries=1)
def escalate_handoffs(self) -> int:
    try:
        return asyncio.run(_escalar())
    except Exception as exc:  # best-effort: un fallo del aviso no debe romper el beat
        logger.warning("escalate_handoffs.error", error=str(exc))
        return 0


async def _escalar() -> int:
    from app.core.redis import get_redis
    from app.db.session import db_session
    from app.services.app_settings import get_app_setting
    from app.services.handoff_sla import SETTING_SLA_MINUTES, derivaciones_vencidas, sla_minutos
    from app.services.runtime_logs import push_runtime_log
    from app.services.web_push import notify

    sla = sla_minutos(await get_app_setting(SETTING_SLA_MINUTES, "0"))
    if sla <= 0:
        return 0

    ahora = datetime.now(timezone.utc)
    async with db_session() as db:
        vencidas = await derivaciones_vencidas(db, sla, ahora)

    avisadas = 0
    redis = get_redis()
    for v in vencidas:
        clave = _CLAVE.format(
            conv_id=v["conversation_id"], ts=int(v["derivada"].timestamp())
        )
        # SET NX: solo el primero que llega avisa; los siguientes ciclos lo ven ya avisado.
        if not await redis.set(clave, "1", nx=True, ex=_TTL_S):
            continue
        await notify(
            "Cliente esperando a una persona",
            f"Lleva {v['esperando_min']} min derivado sin respuesta ({v['canal']}).",
            f"/inbox/{v['conversation_id']}",
        )
        await push_runtime_log(
            level="warn",
            event="handoff.sla_vencido",
            message=f"Derivación sin respuesta humana tras {v['esperando_min']} min (SLA {sla} min)",
            conversation_id=v["conversation_id"],
            channel=v["canal"],
        )
        avisadas += 1
    return avisadas
