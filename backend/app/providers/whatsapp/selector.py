"""Quién habla con WhatsApp: YCloud o la API Cloud oficial de Meta.

La elección NO es una variable de entorno. Cada instalación es de un negocio
distinto y unos tendrán contratado YCloud y otros irán directos a Meta, así que
la decisión vive con el canal —`Channel.config["provider"]`— y se cambia desde
Conexiones. `WHATSAPP_PROVIDER` del entorno queda solo como valor por defecto
para instalaciones antiguas, donde el canal aún no tiene el campo.

El problema práctico: `get_whatsapp_provider()` es SÍNCRONA y la llaman una
docena de sitios (el worker de difusión, el envío del agente, la subida de
adjuntos…). Leer el canal es ir a la base de datos, que es asíncrono. En vez de
convertir a `async` esa docena de llamadas —y romper de paso los tests que la
sustituyen por un doble— devolvemos un ENVOLTORIO que cumple el mismo contrato
y resuelve el proveedor de verdad dentro de cada método, que sí es asíncrono.

Los webhooks NO pasan por aquí: cada proveedor tiene su propia URL
(`/webhooks/ycloud` y `/webhooks/whatsapp/meta`) porque lo que se pega en el
panel de YCloud y lo que se pega en el de Meta son cosas distintas, y así el
que recibe un mensaje sabe sin ambigüedad quién se lo manda.
"""
from __future__ import annotations

from typing import Literal

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.whatsapp.base import (
    IncomingMessage,
    MediaKind,
    WhatsAppProvider,
)

logger = get_logger(__name__)

ProviderName = Literal["ycloud", "meta"]

PROVIDER_YCLOUD = "ycloud"
PROVIDER_META = "meta"
VALID_PROVIDERS = (PROVIDER_YCLOUD, PROVIDER_META)

# Nombre legible para los mensajes de error del panel. Que un aviso diga
# "meta" a secas no le dice nada a quien está configurando el canal.
PROVIDER_LABELS = {
    PROVIDER_YCLOUD: "YCloud",
    PROVIDER_META: "API Cloud de Meta",
}

# Caché del nombre del proveedor. Se comparte entre procesos por Redis igual que
# las credenciales (app/services/credentials.py) para que un cambio en el panel
# lo vean también el worker y el beat sin reiniciar nada.
_CACHE_KEY = "whatsapp:provider"
_CACHE_TTL_SECONDS = 60


async def invalidate_provider_cache() -> None:
    """Olvida el proveedor cacheado. La llama el alta/edición del canal."""
    try:
        from app.core.redis import get_redis

        await get_redis().delete(_CACHE_KEY)
    except Exception as e:  # noqa: BLE001 — sin Redis se vive, solo tarda 60s más
        logger.warning("whatsapp.provider.cache_invalidate_failed", error=str(e))


def _clean(name: object) -> str | None:
    value = str(name or "").strip().lower()
    return value if value in VALID_PROVIDERS else None


async def _read_provider_from_db() -> str | None:
    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType

    async with db_session() as db:
        cfg = (
            await db.execute(
                select(Channel.config)
                .where(Channel.type == ChannelType.whatsapp)
                .order_by(Channel.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    return _clean((cfg or {}).get("provider"))


async def get_provider_name() -> str:
    """Nombre del proveedor configurado para el canal de WhatsApp.

    Orden: lo que diga el canal → `WHATSAPP_PROVIDER` del entorno → YCloud.
    Cualquier fallo de lectura cae al valor por defecto en vez de tumbar el
    envío: quedarse sin mandar por no poder leer una preferencia sería peor
    que mandar por el proveedor de siempre.
    """
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        cached = await redis.get(_CACHE_KEY)
        if cached is not None:
            raw = cached if isinstance(cached, str) else cached.decode()
            return _clean(raw) or PROVIDER_YCLOUD
    except Exception:  # noqa: BLE001 — sin caché se lee de la BD y ya
        redis = None

    name: str | None = None
    try:
        name = await _read_provider_from_db()
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp.provider.db_read_failed", error=str(e))

    if name is None:
        name = _clean(settings.WHATSAPP_PROVIDER) or PROVIDER_YCLOUD

    if redis is not None:
        try:
            await redis.setex(_CACHE_KEY, _CACHE_TTL_SECONDS, name)
        except Exception:  # noqa: BLE001
            pass
    return name


async def resolve_whatsapp_provider() -> WhatsAppProvider:
    """El proveedor CONCRETO que toca ahora mismo."""
    return provider_by_name(await get_provider_name())


def provider_by_name(name: str) -> WhatsAppProvider:
    """Instancia (cacheada) del proveedor pedido por nombre."""
    from app.providers.whatsapp.meta import MetaCloudProvider
    from app.providers.whatsapp.ycloud import YCloudProvider

    key = _clean(name) or PROVIDER_YCLOUD
    inst = _instances.get(key)
    if inst is None:
        inst = MetaCloudProvider() if key == PROVIDER_META else YCloudProvider()
        _instances[key] = inst
    return inst


_instances: dict[str, WhatsAppProvider] = {}


class WhatsAppRouter(WhatsAppProvider):
    """Fachada: mismo contrato que un proveedor, delega en el que toque.

    Repite las firmas una a una a propósito, sin `__getattr__` mágico: hay
    llamantes que INSPECCIONAN la firma antes de llamar (`outbound_send` mira
    qué argumentos acepta `send_template` para no pasarle los que no entiende)
    y con un proxy dinámico verían la firma equivocada.
    """

    async def _p(self) -> WhatsAppProvider:
        return await resolve_whatsapp_provider()

    # --- entrada -----------------------------------------------------------

    async def verify_webhook_signature(self, headers: dict[str, str], body: bytes) -> bool:
        return await (await self._p()).verify_webhook_signature(headers, body)

    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        # Sin `await` no hay forma de saber qué proveedor manda, y adivinarlo
        # por la forma del payload sería una fuente de errores silenciosos. Los
        # webhooks tienen endpoint propio por proveedor justo para no pasar por
        # aquí (ver app/api/webhooks.py).
        raise NotImplementedError(
            "parse_webhook no se resuelve por la fachada: usa el endpoint del "
            "proveedor (/webhooks/ycloud o /webhooks/whatsapp/meta)."
        )

    # --- salida ------------------------------------------------------------

    async def send_text(self, to_phone: str, body: str) -> str:
        return await (await self._p()).send_text(to_phone, body)

    async def upload_media(self, file_bytes: bytes, mime: str, filename: str) -> str:
        return await (await self._p()).upload_media(file_bytes, mime, filename)

    async def send_media(
        self,
        to_phone: str,
        media_id: str,
        media_kind: MediaKind,
        *,
        caption: str | None = None,
        filename: str | None = None,
    ) -> str:
        return await (await self._p()).send_media(
            to_phone, media_id, media_kind, caption=caption, filename=filename
        )

    async def send_template(
        self,
        to_phone: str,
        template_name: str,
        language: str,
        body_variables: list[str] | None = None,
        *,
        header: dict | None = None,
        button_variables: list[dict] | None = None,
        param_format: str = "positional",
        variable_names: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        return await (await self._p()).send_template(  # type: ignore[attr-defined]
            to_phone,
            template_name,
            language,
            body_variables,
            header=header,
            button_variables=button_variables,
            param_format=param_format,
            variable_names=variable_names,
            idempotency_key=idempotency_key,
        )

    # --- consultas y descargas --------------------------------------------

    async def list_templates(self) -> list[dict]:
        return await (await self._p()).list_templates()  # type: ignore[attr-defined]

    async def check_credentials(self) -> None:
        await (await self._p()).check_credentials()  # type: ignore[attr-defined]

    async def download_audio(self, audio_url: str) -> tuple[bytes, str]:
        return await (await self._p()).download_audio(audio_url)

    async def download_media(self, media_url: str, *, max_bytes: int) -> tuple[bytes, str]:
        return await (await self._p()).download_media(media_url, max_bytes=max_bytes)  # type: ignore[attr-defined]


_router: WhatsAppRouter | None = None


def get_whatsapp_provider() -> WhatsAppProvider:
    """Punto de entrada de todo el backend para enviar por WhatsApp."""
    global _router
    if _router is None:
        _router = WhatsAppRouter()
    return _router
