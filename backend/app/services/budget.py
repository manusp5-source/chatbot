"""Control de presupuesto mensual del LLM.

Si el admin ha configurado `AgentConfig.monthly_budget_usd`, este modulo:
- Calcula el coste acumulado del mes actual (llm_usage_log + el consumo que no
  llego a escribirse en BD, ver `usage_tracker.untracked_usage_tokens`, + los
  MINUTOS de las llamadas de voz).
- Si supera el tope, pausa el agente globalmente y notifica al admin.
- Idempotencia: solo notifica una vez por mes (Redis flag).

Se llama best-effort tras cada llamada exitosa al LLM, sin bloquear la
respuesta al usuario si la verificacion falla.

FALLAR CERRADO (lo razonable). El guardarraíl solo servia cuando todo iba bien:
cualquier excepcion salia en silencio y "no se cuanto llevo gastado" acababa
comportandose igual que "llevo gastado 0". Ahora:

- El consumo que no se pudo escribir en BD SI cuenta (contador de Redis).
- Cada comprobacion buena deja el ultimo coste/tope conocido en Redis.
- Si nos quedamos CIEGOS (revienta la consulta del coste o la lectura de la
  config) se loguea como error, se avisa al admin y se pausa el agente SI lo
  ultimo que sabiamos era que ibamos en zona de riesgo (>= `_BLIND_PAUSE_RATIO`
  del tope). Sin ninguna referencia previa NO pausamos: apagar el bot ante un
  hipo de BD con el mes recien empezado seria peor que el riesgo que evita.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, text as sql_text

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.agent_config import AgentConfig
from app.services.agent_pause import is_agent_paused, set_agent_paused
from app.services.llm_pricing import estimate_cost_usd, get_price_map
from app.services.runtime_logs import push_runtime_log
from app.services.security_alerts import notify_security
from app.services.usage_tracker import untracked_usage_tokens

logger = get_logger(__name__)

_NOTIFIED_KEY = "budget:notified:{ym}"  # TTL 33 dias
_WARNED80_KEY = "budget:warned80:{ym}"  # aviso temprano al 80%, una vez por mes
_LASTKNOWN_KEY = "budget:lastknown:{ym}"  # ultimo coste/tope medido con exito
_BLIND_KEY = "budget:blind:{ym}"  # aviso de "no puedo medir", una vez por mes
# Marca de "la pausa activa la puso el presupuesto". La pone al pausar y la
# quita al no estar superado. Sirve para que una pausa por tope no se pueda
# saltar (la whitelist demo SI se salta la pausa manual, que es lo que quiere,
# pero saltarse el corte por gasto es seguir gastando por encima del tope).
_BUDGET_PAUSE_KEY = "budget:pause_active"
_AGENT_NOTIFIED_KEY = "budget:agent_notified:{ym}:{agent_id}"
_STATE_TTL = 33 * 24 * 3600
# A partir de que fraccion del tope, quedarse ciego se considera peligroso.
_BLIND_PAUSE_RATIO = 0.8


def _ym() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


# ---------------------- Telefonía (canal de voz) ----------------------
#
# Los MINUTOS de Retell no son tokens. Se facturan por tiempo de conversación,
# así que no caben en `llm_model_price` (tarifas por millón de tokens) ni en
# `llm_usage_log` (contadores de tokens): meterlos ahí con unos "tokens"
# inventados habría falseado el desglose de consumo por modelo del panel.
#
# Modelo elegido: coste PLANO por conversación en `conversations.call_cost_usd`
# (lo escribe el webhook de fin de llamada, prefiriendo el importe real que
# factura Retell sobre la estimación duración × tarifa). Aquí se suma como un
# sumando aparte del coste de tokens. El tope mensual pasa así a medir el canal
# entero y no solo su mitad barata.
#
# Qué fecha manda: `ended_at` si la hay, si no `started_at`. Una llamada se
# paga cuando ocurre, y `ended_at` puede faltar si el webhook nunca llegó.

_VOICE_MONTH_SQL = """
    SELECT COALESCE(SUM(call_cost_usd), 0) AS cost,
           COALESCE(SUM(call_duration_seconds), 0) AS secs,
           COUNT(*) FILTER (
               WHERE call_cost_usd IS NULL
                 AND COALESCE(call_duration_seconds, 0) > 0
           ) AS sin_coste
    FROM conversations
    WHERE canal = 'retell_voice'
      AND COALESCE(ended_at, started_at) >= :since
"""


async def voice_month_telephony_usage() -> dict:
    """Minutos y coste de telefonía del mes en curso (canal de voz).

    Devuelve {"cost_usd", "minutes", "calls_without_cost"}. `calls_without_cost`
    son llamadas con duración pero sin importe: ni Retell lo mandó ni hay tarifa
    por minuto configurada. Se cuentan aparte para no dar por bueno un total que
    en realidad está incompleto.
    """
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    async with db_session() as db:
        row = (
            await db.execute(sql_text(_VOICE_MONTH_SQL), {"since": month_start})
        ).first()
    if row is None:
        return {"cost_usd": 0.0, "minutes": 0.0, "calls_without_cost": 0}
    return {
        "cost_usd": round(float(row.cost or 0), 4),
        "minutes": round(float(row.secs or 0) / 60.0, 2),
        "calls_without_cost": int(row.sin_coste or 0),
    }


async def voice_month_telephony_cost_usd() -> float:
    """Solo el coste. Atajo para el cálculo del tope."""
    return (await voice_month_telephony_usage())["cost_usd"]


async def current_month_cost_usd() -> float:
    """Coste estimado de TODO el consumo del mes actual.

    Suma tres fuentes:
      - la tabla `llm_usage_log` (tokens del modelo),
      - el consumo que NO se pudo escribir en ella (contador de Redis),
      - los MINUTOS de las llamadas de voz (`conversations.call_cost_usd`).

    Si Redis no responde, ese segundo sumando no se puede leer: se loguea como
    error y se sigue con lo que hay (la BD es la fuente principal), pero el fallo
    queda a la vista.

    La telefonía NO se envuelve en un try/except a propósito: si la consulta
    revienta, la excepción sube y `check_budget_and_maybe_pause` la trata como
    "estoy ciego" (que es la verdad), en vez de devolver un total que se calla
    la mitad cara del canal más caro.
    """
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    async with db_session() as db:
        rows = (
            await db.execute(
                sql_text(
                    """
                    SELECT model,
                           COALESCE(SUM(prompt_tokens), 0) AS pt,
                           COALESCE(SUM(completion_tokens), 0) AS ct
                    FROM llm_usage_log
                    WHERE created_at >= :since
                    GROUP BY model
                    """
                ),
                {"since": month_start},
            )
        ).fetchall()
        prices = await get_price_map(db)
    total = 0.0
    for r in rows:
        total += estimate_cost_usd(r.model or "", int(r.pt or 0), int(r.ct or 0), prices)
    try:
        for model, (pt, ct) in (await untracked_usage_tokens()).items():
            total += estimate_cost_usd(model, pt, ct, prices)
    except Exception as e:
        logger.error("budget.untracked.read_failed", error=str(e))
    total += await voice_month_telephony_cost_usd()
    return round(total, 4)


# ---------------------- Memoria de la ultima medicion ----------------------


async def _remember_last_known(cost: float, budget: float) -> None:
    """Guarda el ultimo coste/tope medido con exito (referencia para decidir
    si pausar cuando nos quedemos ciegos). Nunca lanza."""
    try:
        await get_redis().set(
            _LASTKNOWN_KEY.format(ym=_ym()),
            json.dumps({"cost": round(float(cost), 4), "budget": float(budget)}),
            ex=_STATE_TTL,
        )
    except Exception as e:
        logger.warning("budget.lastknown.write_failed", error=str(e))


async def _last_known() -> dict | None:
    """Ultimo coste/tope conocido de este mes, o None si no hay (o Redis no
    responde). Nunca lanza."""
    try:
        raw = await get_redis().get(_LASTKNOWN_KEY.format(ym=_ym()))
        if not raw:
            return None
        data = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        return {"cost": float(data["cost"]), "budget": float(data["budget"])}
    except Exception as e:
        logger.warning("budget.lastknown.read_failed", error=str(e))
        return None


async def _handle_blind(*, reason: str, error: str, budget: float | None) -> None:
    """No podemos saber cuanto llevamos gastado. Ruido + decision conservadora.

    Pausa SOLO si la ultima medicion buena del mes ya estaba en zona de riesgo.
    Con el mes recien empezado (o sin referencia) no pausamos: un fallo puntual
    de BD no puede apagar el bot. Nunca propaga.
    """
    try:
        await _handle_blind_inner(reason=reason, error=error, budget=budget)
    except Exception as e:  # pragma: no cover - defensivo
        logger.error("budget.blind.handler_failed", reason=reason, error=str(e))


async def _handle_blind_inner(*, reason: str, error: str, budget: float | None) -> None:
    last = await _last_known()
    ref_budget = budget if (budget and budget > 0) else (last or {}).get("budget") or 0.0
    last_cost = (last or {}).get("cost")
    ratio = (last_cost / ref_budget) if (last_cost is not None and ref_budget > 0) else None
    should_pause = ratio is not None and ratio >= _BLIND_PAUSE_RATIO

    paused_now = False
    if should_pause:
        try:
            await _set_budget_pause_flag(True)
            if not await is_agent_paused():
                await set_agent_paused(True)
                paused_now = True
                await push_runtime_log(
                    level="error",
                    event="budget.blind.paused",
                    message=(
                        f"No se puede medir el gasto del LLM ({reason}) y el ultimo dato "
                        f"conocido iba al {ratio:.0%} del tope. Agente pausado por precaucion."
                    ),
                )
        except Exception as e:
            logger.error("budget.blind.pause_failed", error=str(e))

    # Aviso al admin: una vez por mes y motivo (si Redis esta caido tampoco
    # podemos deduplicar; en ese caso simplemente no avisamos por aqui, el log
    # de error ya esta puesto).
    try:
        was_set = await get_redis().set(
            _BLIND_KEY.format(ym=_ym()) + f":{reason}", "1", ex=_STATE_TTL, nx=True
        )
    except Exception:
        was_set = False
    if was_set:
        try:
            await notify_security(
                kind="budget_unmeasurable",
                title=f"No se puede comprobar el presupuesto LLM ({_ym()})",
                details={
                    "motivo": reason,
                    "error": error[:300],
                    "ultimo_coste_conocido_usd": (
                        f"{last_cost:.4f}" if last_cost is not None else "desconocido"
                    ),
                    "tope_mensual_usd": f"{ref_budget:.2f}" if ref_budget else "desconocido",
                    "agente_pausado": "si" if paused_now else "no",
                    "accion_requerida": "Revisar la BD de consumo: el tope de gasto esta ciego",
                },
                throttle_key=f"budgetblind:{_ym()}:{reason}",
            )
        except Exception as e:
            logger.error("budget.blind.notify_failed", error=str(e))


async def _get_budget_usd() -> float | None:
    """Tope GLOBAL de la instalación (`agent_config`, pantalla Tarifas y límites).

    Es la suma de TODO el consumo del mes: agentes, clasificador, agente interno,
    embeddings, transcripciones y los MINUTOS de telefonía del canal de voz. Al
    superarlo se pausa el bot entero (y la pausa global apaga también la voz).
    """
    async with db_session() as db:
        cfg = (
            await db.execute(select(AgentConfig).where(AgentConfig.is_active.is_(True)))
        ).scalar_one_or_none()
        if not cfg or cfg.monthly_budget_usd is None:
            return None
        return float(cfg.monthly_budget_usd)


# ---------------------- Tope POR AGENTE ----------------------
#
# `agents.monthly_budget_usd` se editaba en el panel, viajaba en el runtime… y no
# lo leia NADIE: el unico tope que se aplicaba era el global de `agent_config`.
# Dos pantallas para lo mismo y solo funcionaba una. Aqui esta el consumidor.
#
# Al superar SU tope, el agente se DESACTIVA (is_active=False) en vez de pausar
# la instalacion entera: es su presupuesto, no el de los demas. La desactivacion
# se ve en la lista de Agentes, se revierte con un clic y `runtime_config` ya
# filtra por `is_active`, asi que ese agente deja de atender canales al momento.


# Minutos de voz atribuidos a UN agente. `conversations` no guarda qué agente
# atendió la llamada, pero `llm_usage_log` sí (cada turno deja su agent_id junto
# al conversation_id), así que la llamada se le imputa al agente que la
# contestó. Sin esto el tope POR AGENTE seguiría midiendo solo tokens y un
# agente de voz podría pasarse de presupuesto sin que su tope se enterase — el
# mismo agujero del tope global, a menor escala.
_AGENT_VOICE_MONTH_SQL = """
    SELECT COALESCE(SUM(c.call_cost_usd), 0) AS cost
    FROM conversations c
    WHERE c.canal = 'retell_voice'
      AND COALESCE(c.ended_at, c.started_at) >= :since
      AND c.call_cost_usd IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM llm_usage_log u
          WHERE u.conversation_id = c.id AND u.agent_id = :aid
      )
"""


async def agent_month_cost_usd(agent_id) -> float:
    """Coste estimado del mes atribuido a UN agente concreto.

    Tokens del modelo + los minutos de las llamadas de voz que atendió.
    """
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    async with db_session() as db:
        rows = (
            await db.execute(
                sql_text(
                    """
                    SELECT model,
                           COALESCE(SUM(prompt_tokens), 0) AS pt,
                           COALESCE(SUM(completion_tokens), 0) AS ct
                    FROM llm_usage_log
                    WHERE created_at >= :since AND agent_id = :aid
                    GROUP BY model
                    """
                ),
                {"since": month_start, "aid": str(agent_id)},
            )
        ).fetchall()
        prices = await get_price_map(db)
        voice = (
            await db.execute(
                sql_text(_AGENT_VOICE_MONTH_SQL),
                {"since": month_start, "aid": str(agent_id)},
            )
        ).first()
    total = 0.0
    for r in rows:
        total += estimate_cost_usd(r.model or "", int(r.pt or 0), int(r.ct or 0), prices)
    total += float((voice.cost if voice is not None else 0) or 0)
    return round(total, 4)


async def _check_agent_budget(agent_id) -> None:
    """Aplica el tope propio del agente. Nunca propaga."""
    from app.models.agent import Agent

    async with db_session() as db:
        agent = (
            await db.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if agent is None or agent.monthly_budget_usd is None:
            return
        budget = float(agent.monthly_budget_usd)
        name = agent.name
        already_off = not agent.is_active
    if budget <= 0 or already_off:
        return

    cost = await agent_month_cost_usd(agent_id)
    if cost < budget:
        return

    async with db_session() as db:
        agent = (
            await db.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if agent is None or not agent.is_active:
            return
        agent.is_active = False
        await db.commit()

    logger.warning(
        "budget.agent.exceeded.deactivated",
        agent_id=str(agent_id),
        cost_usd=cost,
        budget_usd=budget,
    )
    await push_runtime_log(
        level="error",
        event="budget.agent.exceeded",
        message=(
            f"El agente «{name}» ha superado su tope mensual "
            f"({cost:.2f} $ / {budget:.2f} $) y se ha DESACTIVADO. Sube su tope o "
            "vuelve a activarlo desde Agentes."
        ),
        agent=name,
    )
    try:
        was_set = await get_redis().set(
            _AGENT_NOTIFIED_KEY.format(ym=_ym(), agent_id=str(agent_id)),
            "1",
            ex=_STATE_TTL,
            nx=True,
        )
    except Exception:
        was_set = False
    if was_set:
        try:
            await notify_security(
                kind="budget_agent_exceeded",
                title=f"Tope del agente «{name}» superado ({_ym()})",
                details={
                    "agente": name,
                    "coste_agente_usd": f"{cost:.4f}",
                    "tope_agente_usd": f"{budget:.2f}",
                    "agente_desactivado": "si",
                    "accion_requerida": "Subir su tope o reactivarlo en /admin/agents",
                },
                throttle_key=f"budgetagent:{_ym()}:{agent_id}",
            )
        except Exception as e:
            logger.error("budget.agent.notify.error", error=str(e))


# ---------------------- Pausa por presupuesto ----------------------


async def is_budget_pause_active() -> bool:
    """True si la pausa vigente la puso el corte por presupuesto.

    La whitelist demo se salta la pausa a propósito (para poder enseñar el bot
    con los canales apagados), pero saltarse el CORTE POR GASTO es seguir
    gastando por encima del tope. Quien consulte la whitelist debe mirar esto
    antes. Nunca lanza: si Redis no responde, decimos que no (la pausa manual
    sigue funcionando igual).
    """
    try:
        return bool(await get_redis().get(_BUDGET_PAUSE_KEY))
    except Exception as e:  # noqa: BLE001
        logger.warning("budget.pause_flag.read_failed", error=str(e))
        return False


async def _set_budget_pause_flag(active: bool) -> None:
    try:
        r = get_redis()
        if active:
            await r.set(_BUDGET_PAUSE_KEY, "1", ex=_STATE_TTL)
        else:
            await r.delete(_BUDGET_PAUSE_KEY)
    except Exception as e:  # noqa: BLE001
        logger.warning("budget.pause_flag.write_failed", error=str(e))


async def check_budget_and_maybe_pause(agent_id=None) -> None:
    """Llamar tras cada uso del LLM. Pausa el agente si se ha superado el tope.

    Dos topes independientes:
      - el GLOBAL de la instalación (`agent_config`) → pausa el bot entero.
      - el propio del AGENTE que ha gastado (`agents.monthly_budget_usd`) → lo
        desactiva solo a él. `agent_id` viene del contexto de traza de la
        llamada; si es None, solo se comprueba el global.

    Nunca propaga (no bloquea el flujo del agente), pero tampoco se calla: si
    no se puede leer la config o calcular el coste, va por `_handle_blind` en
    vez de salir como si el gasto fuera 0.
    """
    if agent_id is not None:
        try:
            await _check_agent_budget(agent_id)
        except Exception as e:  # noqa: BLE001 — el tope global es lo prioritario
            logger.error("budget.agent.check_failed", agent_id=str(agent_id), error=str(e))

    # 1) ¿Hay tope configurado? Si ni eso se puede leer, estamos ciegos.
    try:
        budget = await _get_budget_usd()
    except Exception as e:
        logger.error("budget.check.config_unavailable", error=str(e))
        await _handle_blind(reason="config", error=str(e), budget=None)
        return
    if budget is None or budget <= 0:
        return

    # 2) ¿Cuanto llevamos gastado? Idem: no poder medirlo NO es gastar 0.
    try:
        cost = await current_month_cost_usd()
    except Exception as e:
        logger.error("budget.check.cost_unavailable", error=str(e))
        await _handle_blind(reason="coste", error=str(e), budget=budget)
        return
    await _remember_last_known(cost, budget)

    try:
        if cost < budget:
            # Aviso TEMPRANO al 80% (una vez por mes): hasta ahora el primer
            # aviso era el corte en seco — con esto da tiempo a reaccionar
            # (subir tope, revisar consumo anómalo) antes de que el bot pare.
            if cost >= 0.8 * budget:
                now = datetime.now(timezone.utc)
                ym = f"{now.year:04d}-{now.month:02d}"
                r = get_redis()
                was_set = await r.set(
                    _WARNED80_KEY.format(ym=ym), "1", ex=33 * 24 * 3600, nx=True
                )
                if was_set:
                    await push_runtime_log(
                        level="warn",
                        event="budget.warning80",
                        message=(
                            f"Presupuesto LLM al {cost / budget:.0%} "
                            f"({cost:.2f} $ / {budget:.2f} $). Al 100% el agente se pausa."
                        ),
                    )
                    try:
                        await notify_security(
                            kind="budget_warning",
                            title=f"Presupuesto LLM al {cost / budget:.0%} ({ym})",
                            details={
                                "coste_acumulado_usd": f"{cost:.4f}",
                                "tope_mensual_usd": f"{budget:.2f}",
                                "accion": "Revisar consumo o subir el tope antes del corte",
                            },
                            throttle_key=f"budget80:{ym}",
                        )
                    except Exception as e:
                        logger.error("budget.warn80.notify.error", error=str(e))
            # Por debajo del tope: si quedaba marca de corte por gasto (mes
            # nuevo, tope subido), se retira. La pausa manual no se toca.
            await _set_budget_pause_flag(False)
            return

        # Pausar si no estaba ya pausado (idempotente — si ya esta pausado,
        # set_agent_paused(True) es no-op).
        await _set_budget_pause_flag(True)
        already_paused = await is_agent_paused()
        if not already_paused:
            await set_agent_paused(True)
            logger.warning(
                "budget.exceeded.paused", cost_usd=cost, budget_usd=budget
            )
            await push_runtime_log(
                level="error",
                event="budget.exceeded",
                message=f"Presupuesto superado ({cost:.2f} $ / {budget:.2f} $). Agente pausado.",
            )

        # Notificar una sola vez por mes (Redis flag con TTL 33 dias para que
        # cubra el cambio de mes natural).
        now = datetime.now(timezone.utc)
        ym = f"{now.year:04d}-{now.month:02d}"
        flag_key = _NOTIFIED_KEY.format(ym=ym)
        r = get_redis()
        was_set = await r.set(flag_key, "1", ex=33 * 24 * 3600, nx=True)
        if was_set:
            try:
                await notify_security(
                    kind="budget_exceeded",
                    title=f"Presupuesto LLM superado ({ym})",
                    details={
                        "coste_acumulado_usd": f"{cost:.4f}",
                        "tope_mensual_usd": f"{budget:.2f}",
                        "agente_pausado": "si",
                        "accion_requerida": "Subir el tope o reactivar manualmente desde /admin/agent/dashboard",
                    },
                    throttle_key=f"budget:{ym}",
                )
            except Exception as e:
                logger.error("budget.notify.error", error=str(e))
    except Exception as e:
        # Sabemos lo que se ha gastado pero algo ha fallado al avisar/pausar:
        # error, no warning — puede significar que el tope NO se ha aplicado.
        logger.error("budget.check.error", cost_usd=cost, budget_usd=budget, error=str(e))
