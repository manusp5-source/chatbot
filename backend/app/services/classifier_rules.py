"""Evaluación de las reglas duras del clasificador (sin LLM, coste cero).

Corren ANTES del filtro de correo automático y antes del clasificador con IA.
Lo que cazan va a cuarentena ('Descartados') sin gastar modelo ni generar
borrador, y el motivo que se guarda dice qué regla fue, para que en la bandeja
se pueda distinguir "lo bloqueaste tú" de "lo decidió la IA".

Semántica (fija, ver `models/classifier_rule`):
  remitente  igualdad exacta con el email / @usuario
  dominio    el email termina en @dominio (o en .dominio → subdominios)
  asunto     el asunto contiene el texto

CACHÉ. Las reglas se consultaban en la base EN CADA MENSAJE ENTRANTE, y son un
puñado de filas que cambian una vez al mes. Ahora van en Redis, compartidas por
todos los workers, y se tiran al crear, editar o borrar una regla desde el
panel. El TTL corto es una red de seguridad, no el mecanismo: si alguien tocara
la tabla por fuera, la caché se cura sola en unos minutos.

Nada de esto puede hacer que se filtre de más: si Redis no está, o la caché
viene rota, se lee de la base exactamente como antes.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.classifier_rule import ClassifierRule

logger = get_logger(__name__)

# La clave lleva dentro el número de GENERACIÓN, y invalidar es subir ese
# número. Con una clave fija había una carrera de manual: un worker leía las
# reglas de la base, la operadora borraba una y llamaba a invalidar (que no
# borraba nada, porque la clave aún no existía), y acto seguido el worker
# escribía la caché con la regla ya borrada dentro. Cinco minutos comiéndose
# correo bueno con la regla desaparecida del panel. Escribiendo en la clave de
# la generación vieja, ese escaparate se queda sin nadie que lo mire.
_GENERACION_KEY = "classifier:rules:generacion"
_CACHE_KEY = "classifier:rules:activas:{gen}"
_CACHE_TTL_S = 300


@dataclass(frozen=True)
class _Regla:
    """Una regla ya sacada de la base, lista para comparar.

    Se cachea esto y no el objeto de SQLAlchemy: para decidir solo hacen falta
    cuatro campos, y así lo cacheado no depende de ninguna sesión abierta.
    """

    id: uuid.UUID
    campo: str
    valor: str
    canal: str | None


@dataclass
class RuleHit:
    rule_id: uuid.UUID
    campo: str
    valor: str

    @property
    def reason(self) -> str:
        etiqueta = {
            "remitente": "remitente bloqueado",
            "dominio": "dominio bloqueado",
            "asunto": "asunto bloqueado",
        }.get(self.campo, "regla")
        return f"{etiqueta}: {self.valor}"


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _sender_matches_domain(sender: str, dominio: str) -> bool:
    """True si `sender` es una dirección de ese dominio.

    Se acepta el dominio escrito de las tres formas en que lo escribe una
    persona: "ejemplo.com", "@ejemplo.com" y "correo@ejemplo.com" (de esta
    última nos quedamos con lo de después de la arroba). Los subdominios
    cuentan: una regla para "ejemplo.com" caza "news.ejemplo.com", que es de
    donde suele salir el envío masivo.
    """
    dominio = dominio.rsplit("@", 1)[-1].lstrip("@").strip(".")
    if not dominio or "@" not in sender:
        return False
    host = sender.rsplit("@", 1)[-1]
    return host == dominio or host.endswith("." + dominio)


def rule_matches(
    rule: ClassifierRule, senders: list[str | None], subject: str | None
) -> bool:
    """¿Encaja esta regla con el mensaje? Sin tocar la base de datos.

    `senders` es la lista de formas de identificar a quien escribe, porque no
    es la misma en cada canal: en email la dirección, en Instagram el @usuario,
    en WhatsApp el teléfono. Basta con que una encaje.
    """
    valor = _norm(rule.valor)
    if not valor:
        return False
    limpios = [_norm(s) for s in senders if _norm(s)]
    if rule.campo == "remitente":
        return valor in limpios
    if rule.campo == "dominio":
        return any(_sender_matches_domain(s, valor) for s in limpios)
    if rule.campo == "asunto":
        return bool(subject) and valor in _norm(subject)
    return False


async def _leer_de_la_base() -> list[_Regla]:
    async with db_session() as db:
        filas = (
            (
                await db.execute(
                    select(ClassifierRule)
                    .where(ClassifierRule.enabled.is_(True))
                    .order_by(ClassifierRule.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [
        _Regla(id=f.id, campo=f.campo, valor=f.valor, canal=f.canal) for f in filas
    ]


async def _generacion() -> str | None:
    """Número de generación de las reglas. None si Redis no responde."""
    try:
        valor = await get_redis().get(_GENERACION_KEY)
    except Exception as e:  # noqa: BLE001 — sin Redis se lee de la base
        logger.warning("classifier_rules.cache_read_error", error=str(e))
        return None
    return str(valor) if valor else "0"


async def _leer_de_la_cache(gen: str) -> list[_Regla] | None:
    """Reglas cacheadas, o None si no hay nada utilizable.

    Ojo con la diferencia entre "no hay caché" (None) y "no hay reglas" (lista
    vacía): sin distinguirlas, una instalación sin reglas iría a la base en
    cada mensaje, que es justo lo que se quería evitar.
    """
    try:
        crudo = await get_redis().get(_CACHE_KEY.format(gen=gen))
    except Exception as e:  # noqa: BLE001 — sin Redis se lee de la base
        logger.warning("classifier_rules.cache_read_error", error=str(e))
        return None
    if crudo is None:
        return None
    try:
        datos = json.loads(crudo)
        return [
            _Regla(
                id=uuid.UUID(r["id"]),
                campo=r["campo"],
                valor=r["valor"],
                canal=r.get("canal"),
            )
            for r in datos
        ]
    except Exception as e:  # noqa: BLE001 — caché rota = como si no hubiera
        logger.warning("classifier_rules.cache_corrupta", error=str(e))
        return None


async def _guardar_en_cache(reglas: list[_Regla], gen: str) -> None:
    try:
        await get_redis().set(
            _CACHE_KEY.format(gen=gen),
            json.dumps(
                [
                    {"id": str(r.id), "campo": r.campo, "valor": r.valor, "canal": r.canal}
                    for r in reglas
                ]
            ),
            ex=_CACHE_TTL_S,
        )
    except Exception as e:  # noqa: BLE001 — no cachear nunca es un error grave
        logger.warning("classifier_rules.cache_write_error", error=str(e))


async def invalidar_cache() -> None:
    """Sube la generación. La llaman el alta, la edición y el borrado.

    Sin esto, desactivar una regla que se está comiendo correo bueno tardaría
    hasta cinco minutos en surtir efecto, y quien la desactiva está mirando el
    buzón. Es un INCR y no un DELETE a propósito: así una lectura que estuviera
    a medio camino no puede resucitar lo borrado (ver `_GENERACION_KEY`).
    """
    try:
        await get_redis().incr(_GENERACION_KEY)
    except Exception as e:  # noqa: BLE001
        logger.warning("classifier_rules.cache_invalidate_error", error=str(e))


async def reglas_activas() -> list[_Regla]:
    """Reglas activas, de la caché si las hay y de la base si no."""
    gen = await _generacion()
    if gen is None:  # Redis caído: a la base, como siempre
        return await _leer_de_la_base()
    cacheadas = await _leer_de_la_cache(gen)
    if cacheadas is not None:
        return cacheadas
    reglas = await _leer_de_la_base()
    await _guardar_en_cache(reglas, gen)
    return reglas


async def match_rules(
    senders: list[str | None], subject: str | None, canal: str
) -> RuleHit | None:
    """Primera regla activa que encaja, o None.

    Ante cualquier fallo devuelve None: igual que el resto del clasificador, lo
    seguro es NO filtrar. Un error de base de datos no puede acabar tragándose
    los mensajes de clientes reales.
    """
    if not any(_norm(s) for s in senders) and not _norm(subject):
        return None
    try:
        for rule in await reglas_activas():
            # Regla de un canal concreto: no aplica a los demás.
            if rule.canal and rule.canal != canal:
                continue
            if rule_matches(rule, senders, subject):
                return RuleHit(rule_id=rule.id, campo=rule.campo, valor=rule.valor)
    except Exception as e:
        logger.warning("classifier_rules.error", error=str(e))
    return None


async def register_hit(rule_id: uuid.UUID) -> None:
    """Suma un acierto a la regla. Best-effort: si falla, no pasa nada."""
    try:
        async with db_session() as db:
            await db.execute(
                update(ClassifierRule)
                .where(ClassifierRule.id == rule_id)
                .values(hits=ClassifierRule.hits + 1, last_hit_at=datetime.now(timezone.utc))
            )
            await db.commit()
    except Exception as e:
        logger.warning("classifier_rules.hit_failed", error=str(e))
