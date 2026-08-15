"""Provider Retell AI Voz (F6).

Retell hace llamadas de voz por teléfono y delega la "inteligencia" al
LLM custom de nuestro backend. Flujo de una llamada:

  1. Cliente llama al número virtual de Retell.
  2. Retell hace ASR (voz → texto) en streaming en su lado.
  3. Para cada turno del cliente, Retell hace POST a nuestro endpoint
     `/voice/retell/llm` con `{call_id, transcript: [...], interaction_type}`.
  4. Nuestro endpoint construye contexto (prompt del agent + historial de la
     llamada), llama al LLM y devuelve `{response: "texto del bot"}`.
  5. Retell hace TTS (texto → voz) con la voice_id del canal y lo dice.

Configuración del canal (se lee con `channel_config`, que junta la config en
plano con los secretos cifrados de `credentials_encrypted`):
  - api_key:          Retell API key. CIFRADA. Es además la clave con la que
                      Retell firma el webhook de la llamada, así que con ella
                      se verifica la firma (`verify_call_webhook_signature`).
  - agent_id_retell:  ID del Agent en Retell (lo creas en su panel y le
                      pones nuestra URL como LLM webhook).
  - voice_id:         voz de Retell a usar.
  - phone_number:     número virtual que compras en Retell.

Aquí NO hay ningún "secreto del webhook" que elija el cliente: Retell no tiene
ese campo, firma siempre con la API key. Hubo uno hasta ago-2026 (obligatorio
para dar el canal por configurado, y sin usar en ninguna comprobación); si
alguna instalación lo tiene guardado, se ignora.

Latencia: Retell tolera 200-800ms por turno antes de notarse. v1 hace
respuesta NO-streaming (espera al LLM completo); cuando llegue F6-bis se
puede meter streaming token-a-token con `text/event-stream`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from dataclasses import dataclass

import httpx

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.channel import Channel, ChannelType
from app.services.channel_secrets import channel_config
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)


@dataclass
class RetellCredentials:
    api_key: str
    agent_id_retell: str
    # Informativos: no los usa nadie del runtime. Van con default para que un
    # canal sin número (o sin voz apuntada) siga estando "configurado".
    voice_id: str = ""
    phone_number: str = ""


async def get_retell_credentials() -> RetellCredentials | None:
    async with db_session() as db:
        # El más antiguo, y solo uno: con dos canales de voz activos esto era un
        # MultipleResultsFound que dejaba el teléfono mudo. Mismo criterio que
        # usa la provisión del panel, para que se escriba y se lea la misma fila.
        ch = (
            await db.execute(
                select(Channel)
                .where(
                    Channel.type == ChannelType.retell_voice,
                    Channel.enabled.is_(True),
                )
                .order_by(Channel.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    if not ch:
        return None
    cfg = channel_config(ch)
    api_key = cfg.get("api_key")
    agent_id_retell = cfg.get("agent_id_retell")
    voice_id = cfg.get("voice_id")
    phone_number = cfg.get("phone_number")
    # Solo la API key y el agente hacen falta para hablar con Retell. Exigir
    # también voz y número dejaba el teléfono mudo por un dato decorativo.
    if not (api_key and agent_id_retell):
        return None
    return RetellCredentials(
        api_key=api_key,
        agent_id_retell=agent_id_retell,
        voice_id=voice_id or "",
        phone_number=phone_number or "",
    )


RETELL_API_BASE = "https://api.retellai.com"


_WS_ATTEMPT_KEY = "voice:ws:attempts:{ip}"


async def register_ws_attempt(ip: str) -> bool:
    """Cuenta un intento de conexión al WebSocket de voz desde `ip` y dice si
    todavía está por debajo del tope (`RETELL_WS_MAX_ATTEMPTS_PER_MIN`).

    Por qué existe (B5): validar un `call_id` cuesta 1-2 peticiones a la API de
    Retell —contra la cuota del CLIENTE— más un segundo de espera del reintento
    reteniendo la conexión. Sin tope, cualquiera puede quemar esa cuota con un
    bucle de conexiones basura. El contador va en Redis (compartido entre
    workers y réplicas) con ventana de 60s.

    Devuelve True = sigue permitido; False = tope superado, hay que cerrar SIN
    llamar a Retell. Si Redis falla, devuelve True (el límite es una defensa
    añadida; la autenticación real la hace `is_call_authentic`).
    """
    from app.core.config import settings

    limit = settings.RETELL_WS_MAX_ATTEMPTS_PER_MIN
    if limit <= 0:
        return True
    try:
        r = get_redis()
        key = _WS_ATTEMPT_KEY.format(ip=ip)
        count = await r.incr(key)
        if int(count) == 1:
            await r.expire(key, 60)
        if int(count) > limit:
            await push_runtime_log(
                level="warn",
                event="voice.ws.rate_limited",
                message=(
                    f"Demasiados intentos de conexión al WebSocket de voz desde {ip} "
                    f"({count} en el último minuto, tope {limit}). Conexión rechazada "
                    "sin consultar a Retell."
                ),
                channel="retell_voice",
            )
            logger.warning("retell.ws.rate_limited", ip=ip, count=int(count), limit=limit)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - Redis caído no debe tirar llamadas reales
        logger.warning("retell.ws.rate_limit_unavailable", error=str(e))
        return True


async def is_call_authentic(call_id: str) -> bool:
    """Valida que `call_id` corresponde a una llamada REAL de nuestra cuenta de
    Retell, consultando `v2/get-call/{call_id}` con la api_key.

    Política FAIL-CLOSED: si no podemos validar, NO servimos el WS. Antes era
    fail-open "para no tirar llamadas legítimas", pero eso permitía a cualquiera
    abrir el WS del LLM (gasto de tokens + tools) cronometrando una caída de
    Retell o un 401 por api_key mal configurada. Ahora:
      - sin credenciales de voz → False (el canal no está operativo, el WS no debe servir).
      - 404 → False (la llamada no existe en nuestra cuenta → conexión ilegítima).
      - 200 con estado "ended"/"error" → False (replay de un call_id viejo).
      - 200 ok → True.
      - 401/403/5xx/timeout/red → UN reintento; si persiste → False. (Se pierde
        una llamada real durante una caída de Retell — pero si Retell está caído
        la llamada tampoco funcionaría — a cambio de no dejar el agente abierto.)

    I7: cada motivo de rechazo se publica también en "Logs en vivo" del panel.
    Antes solo iba al log estructurado del contenedor, así que para el operador
    "las llamadas no funcionan y el panel dice que todo va bien".
    """
    creds = await get_retell_credentials()
    if not creds:
        logger.warning("retell.ws.no_creds", call_id=call_id)
        await push_runtime_log(
            level="error",
            event="voice.ws.no_creds",
            message=(
                "Llamada entrante rechazada: el canal Retell no está conectado o le "
                "faltan credenciales (API key, agent, voz, teléfono o secret)."
            ),
            channel="retell_voice",
            call_id=call_id,
        )
        return False

    async def _check() -> bool | None:
        """True/False = veredicto firme; None = fallo transitorio (reintentable)."""
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{RETELL_API_BASE}/v2/get-call/{call_id}",
                    headers={"Authorization": f"Bearer {creds.api_key}"},
                )
        except Exception as e:  # noqa: BLE001
            logger.error("retell.ws.validate_error", call_id=call_id, error=str(e))
            await push_runtime_log(
                level="warn",
                event="voice.ws.validate_error",
                message=f"No se pudo contactar con la API de Retell para validar la llamada: {e}",
                channel="retell_voice",
                call_id=call_id,
            )
            return None
        if r.status_code == 404:
            logger.warning("retell.ws.call_not_found", call_id=call_id)
            await push_runtime_log(
                level="warn",
                event="voice.ws.call_not_found",
                message=(
                    "Conexión al WebSocket de voz con un call_id que no existe en la "
                    "cuenta de Retell. Rechazada."
                ),
                channel="retell_voice",
                call_id=call_id,
            )
            return False
        if r.status_code != 200:
            logger.error("retell.ws.validate_http", call_id=call_id, status=r.status_code)
            await push_runtime_log(
                level="warn",
                event="voice.ws.validate_http",
                message=(
                    f"La API de Retell respondió {r.status_code} al validar la llamada. "
                    "Si es 401, revisa la API key del canal."
                ),
                channel="retell_voice",
                call_id=call_id,
            )
            return None
        try:
            status = str((r.json() or {}).get("call_status") or "").lower()
        except Exception:
            await push_runtime_log(
                level="warn",
                event="voice.ws.validate_bad_json",
                message="Respuesta ilegible de la API de Retell al validar la llamada.",
                channel="retell_voice",
                call_id=call_id,
            )
            return None
        if status in ("ended", "error"):
            logger.warning("retell.ws.call_ended", call_id=call_id, status=status)
            await push_runtime_log(
                level="warn",
                event="voice.ws.call_ended",
                message=(
                    f"Conexión al WebSocket de voz de una llamada ya terminada "
                    f"(estado {status}). Rechazada."
                ),
                channel="retell_voice",
                call_id=call_id,
            )
            return False
        return True

    verdict = await _check()
    if verdict is None:
        await asyncio.sleep(1.0)
        verdict = await _check()
    if verdict is None:
        logger.error("retell.ws.validate_failed_closed", call_id=call_id)
        await push_runtime_log(
            level="error",
            event="voice.ws.validate_failed_closed",
            message=(
                "No se pudo validar la llamada contra Retell tras reintentar: conexión "
                "rechazada (fail-closed). Si se repite, revisa la API key del canal y "
                "el estado de Retell."
            ),
            channel="retell_voice",
            call_id=call_id,
        )
        return False  # fail-closed tras el reintento
    return verdict


async def enable_signed_recordings(api_key: str, agent_id: str) -> tuple[bool, str]:
    """Activa `opt_in_signed_url` en el Agent de Retell y PUBLICA el agente.

    Sin esto, la URL de la grabación que Retell manda en el webhook es PÚBLICA:
    quien la tenga escucha la llamada entera sin credenciales, para siempre. Con
    `opt_in_signed_url` la URL va firmada y caduca.

    OJO — NO ES RETROACTIVO: las grabaciones ya creadas siguen siendo públicas
    aunque se active ahora. Solo protege a las nuevas.

    Se llama al provisionar/guardar el canal, con la API key que acaba de
    introducir el cliente. Es best-effort: si falla no se aborta la conexión del
    canal (se devuelve el motivo para enseñarlo en el panel), porque el canal
    funciona igual — solo que con grabaciones públicas.

    Devuelve (ok, detalle_legible).
    """
    if not api_key or not agent_id:
        return False, "Faltan la API key o el Agent ID de Retell."
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.patch(
                f"{RETELL_API_BASE}/update-agent/{agent_id}",
                headers=headers,
                json={"opt_in_signed_url": True},
            )
            if r.status_code >= 400:
                detail = f"Retell respondió {r.status_code} al activar las grabaciones firmadas."
                logger.warning("retell.signed_url.update_failed", status=r.status_code)
                await push_runtime_log(
                    level="warn",
                    event="voice.signed_url.failed",
                    message=detail + " Las grabaciones seguirán siendo URLs públicas.",
                    channel="retell_voice",
                )
                return False, detail
            # Publicar: en Retell los cambios del agente no aplican a las
            # llamadas nuevas hasta que se publica una versión.
            pub = await client.post(
                f"{RETELL_API_BASE}/publish-agent/{agent_id}", headers=headers, json={}
            )
            if pub.status_code >= 400:
                detail = (
                    f"Grabaciones firmadas activadas, pero al publicar el agente Retell "
                    f"respondió {pub.status_code}. Publícalo a mano en su panel."
                )
                await push_runtime_log(
                    level="warn",
                    event="voice.signed_url.publish_failed",
                    message=detail,
                    channel="retell_voice",
                )
                return False, detail
    except Exception as e:  # noqa: BLE001
        detail = f"No se pudo contactar con la API de Retell: {e}"
        logger.warning("retell.signed_url.error", error=str(e))
        await push_runtime_log(
            level="warn",
            event="voice.signed_url.error",
            message=detail + " Las grabaciones seguirán siendo URLs públicas.",
            channel="retell_voice",
        )
        return False, detail
    await push_runtime_log(
        level="info",
        event="voice.signed_url.enabled",
        message=(
            "Grabaciones firmadas activadas en el agente de Retell y agente publicado. "
            "No es retroactivo: las grabaciones anteriores siguen siendo públicas."
        ),
        channel="retell_voice",
    )
    return True, "Grabaciones firmadas activadas y agente publicado."


async def delete_call(call_id: str) -> bool:
    """Borra en Retell una llamada y su grabación. Devuelve si se consiguió.

    Es el cierre del derecho de supresión (RGPD Art. 17) para el canal de voz:
    el audio de la llamada NO vive en nuestro disco, lo guarda Retell, y su URL
    era pública y sin caducidad. Borrar el contacto de nuestra BD dejaba la voz
    del cliente accesible para siempre. `services/data_erasure.py` busca esta
    función por nombre y la llama con cada `call_id` del contacto.

    API: `DELETE /v2/delete-call/{call_id}` con la API key del canal en
    `Authorization: Bearer`. Devuelve 204 sin cuerpo.
    https://docs.retellai.com/api-references/delete-call

    TOLERANTE A FALLOS a propósito: nunca propaga. Un 500 de Retell no puede
    abortar el borrado del contacto entero (que es lo que el cliente ha pedido
    y lo que tiene plazo legal). Devuelve False y el informe de supresión ya
    cuenta cuántas grabaciones quedan pendientes en vez de callárselo.

    Casos:
      - 204/200 → True (borrada).
      - 404 → True: la llamada ya no está en Retell. El objetivo del borrado es
        que no exista, y no existe. Contarla como pendiente haría que el
        informe pidiera borrar a mano algo que ya no está.
      - sin credenciales / 401 / 5xx / red → False (queda pendiente y se avisa).
    """
    if not call_id:
        return False
    creds = await get_retell_credentials()
    if not creds:
        logger.error("retell.delete_call.no_creds", call_id=call_id)
        await push_runtime_log(
            level="error",
            event="voice.erasure.no_creds",
            message=(
                "No se pudo borrar la grabación de una llamada en Retell: el canal de "
                "voz no está conectado o le faltan credenciales. La grabación sigue "
                "ahí; bórrala a mano desde el panel de Retell."
            ),
            channel="retell_voice",
            call_id=call_id,
        )
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.delete(
                f"{RETELL_API_BASE}/v2/delete-call/{call_id}",
                headers={"Authorization": f"Bearer {creds.api_key}"},
            )
    except Exception as e:  # noqa: BLE001 — el borrado del contacto sigue adelante
        logger.error("retell.delete_call.error", call_id=call_id, error=str(e))
        await push_runtime_log(
            level="error",
            event="voice.erasure.error",
            message=(
                f"No se pudo contactar con Retell para borrar la grabación de una "
                f"llamada: {e}. La grabación sigue ahí."
            ),
            channel="retell_voice",
            call_id=call_id,
        )
        return False

    if r.status_code in (200, 202, 204, 404):
        logger.info("retell.delete_call.ok", call_id=call_id, status=r.status_code)
        await push_runtime_log(
            level="info",
            event="voice.erasure.deleted",
            message=(
                "Grabación de llamada borrada en Retell por derecho de supresión"
                + (" (ya no existía)." if r.status_code == 404 else ".")
            ),
            channel="retell_voice",
            call_id=call_id,
        )
        return True

    logger.error("retell.delete_call.http", call_id=call_id, status=r.status_code)
    await push_runtime_log(
        level="error",
        event="voice.erasure.failed",
        message=(
            f"Retell respondió {r.status_code} al borrar la grabación de una llamada. "
            "Si es 401, revisa la API key del canal. La grabación sigue ahí."
        ),
        channel="retell_voice",
        call_id=call_id,
    )
    return False


class RetellProvider:
    async def verify_call_webhook_signature(
        self, headers: dict[str, str], body: bytes
    ) -> bool:
        """Verifica la firma del **webhook de la llamada** (call_started /
        call_ended / call_analyzed).

        Retell firma con la **API key** del canal —no hay ningún otro secreto—
        y el header `X-Retell-Signature` tiene el formato
        `v={timestamp_ms},d={hex_digest}`, donde el digest es
        HMAC-SHA256(api_key, body + str(timestamp)). Replicamos exactamente el
        algoritmo del SDK oficial de Retell (`Retell.verify`).

        Además rechazamos firmas con timestamp fuera de ±5 min (anti-replay),
        como recomienda Retell. Devuelve True solo si todo cuadra.
        """
        creds = await get_retell_credentials()
        if not creds:
            await push_runtime_log(
                level="warn",
                event="retell.call_webhook.no_config",
                message="Llegó webhook de llamada Retell pero no hay canal configurado",
            )
            return False
        sig = headers.get("x-retell-signature") or headers.get("X-Retell-Signature")
        if not sig:
            await push_runtime_log(
                level="warn",
                event="retell.call_webhook.no_header",
                message="Webhook de llamada Retell sin X-Retell-Signature",
            )
            return False
        match = re.search(r"v=(\d+),d=(.*)", sig)
        if not match:
            await push_runtime_log(
                level="warn",
                event="retell.call_webhook.bad_header",
                message="X-Retell-Signature con formato inesperado (se esperaba v=..,d=..)",
            )
            return False
        timestamp = match.group(1)
        received = match.group(2)
        # Anti-replay: el timestamp viene en milisegundos. Toleramos ±5 min.
        try:
            ts_ms = int(timestamp)
            if abs(time.time() * 1000 - ts_ms) > 5 * 60 * 1000:
                await push_runtime_log(
                    level="warn",
                    event="retell.call_webhook.stale",
                    message="Webhook Retell con timestamp fuera de ±5 min (posible replay)",
                )
                return False
        except (TypeError, ValueError):
            return False
        expected = hmac.new(
            creds.api_key.encode(), body + timestamp.encode(), hashlib.sha256
        ).hexdigest()
        ok = hmac.compare_digest(received, expected)
        if not ok:
            await push_runtime_log(
                level="error",
                event="retell.call_webhook.mismatch",
                message="Firma del webhook de llamada Retell no coincide. Revisa la API key del canal.",
            )
        return ok

    @staticmethod
    def extract_user_turn(payload: dict) -> tuple[str | None, list[dict]]:
        """Saca el último turno del usuario y el transcript completo.

        Retell envía algo como:
        {
          "call_id": "...",
          "interaction_type": "response_required" | "reminder_required" | ...,
          "transcript": [
            {"role": "agent", "content": "Hola, ¿en qué puedo ayudarte?"},
            {"role": "user", "content": "Quería información..."},
            ...
          ]
        }
        Devuelve (último_mensaje_user, transcript_completo).
        """
        transcript = payload.get("transcript") or []
        last_user = None
        for turn in reversed(transcript):
            if turn.get("role") == "user" and turn.get("content"):
                last_user = turn["content"]
                break
        return last_user, transcript


_provider_singleton: RetellProvider | None = None


def get_retell_provider() -> RetellProvider:
    global _provider_singleton
    if _provider_singleton is None:
        _provider_singleton = RetellProvider()
    return _provider_singleton
