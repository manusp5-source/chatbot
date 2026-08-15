"""Elegir proveedor de WhatsApp por canal, no por variable de entorno.

Cada instalación es de un cliente distinto: unos tendrán contratado YCloud y
otros irán directos a Meta. Por eso la elección vive en el canal
(`Channel.config["provider"]`) y se cambia desde el panel, y la variable de
entorno se queda solo como valor de partida para instalaciones antiguas.

El envío pasa por una fachada porque `get_whatsapp_provider()` es síncrona y la
llaman una docena de sitios (el worker de difusión, el envío del agente, la
subida de adjuntos): resolver el proveedor dentro de cada método asíncrono
evita convertir a `async` esa docena de llamadas.
"""
from __future__ import annotations

import asyncio

import pytest

from app.providers.whatsapp import selector as mod
from app.providers.whatsapp.meta import MetaCloudProvider
from app.providers.whatsapp.selector import (
    PROVIDER_META,
    PROVIDER_YCLOUD,
    WhatsAppRouter,
    get_whatsapp_provider,
    provider_by_name,
)
from app.providers.whatsapp.ycloud import YCloudProvider


def _sin_cache(monkeypatch) -> None:
    """Quita Redis de en medio: aquí se prueba la decisión, no la caché."""

    class _SinRedis:
        async def get(self, *_a, **_k):
            raise RuntimeError("sin redis en este test")

        async def setex(self, *_a, **_k):
            raise RuntimeError("sin redis en este test")

        async def delete(self, *_a, **_k):
            raise RuntimeError("sin redis en este test")

    monkeypatch.setattr("app.core.redis.get_redis", lambda: _SinRedis())


def _canal_dice(monkeypatch, valor: object | None) -> None:
    """Finge lo que tiene guardado el canal de WhatsApp."""

    async def _fake() -> str | None:
        return mod._clean(valor) if valor is not None else None

    monkeypatch.setattr(mod, "_read_provider_from_db", _fake)


# ---------------------------------------------------------------------------


def test_por_defecto_ycloud(monkeypatch):
    """Instalación de siempre: el canal no tiene el campo todavía."""
    _sin_cache(monkeypatch)
    _canal_dice(monkeypatch, None)
    monkeypatch.setattr(mod.settings, "WHATSAPP_PROVIDER", "ycloud")
    assert asyncio.run(mod.get_provider_name()) == PROVIDER_YCLOUD
    assert isinstance(asyncio.run(mod.resolve_whatsapp_provider()), YCloudProvider)


def test_el_canal_manda_sobre_la_variable_de_entorno(monkeypatch):
    """Es el punto de todo el cambio: lo elige el panel, no el despliegue."""
    _sin_cache(monkeypatch)
    _canal_dice(monkeypatch, "meta")
    monkeypatch.setattr(mod.settings, "WHATSAPP_PROVIDER", "ycloud")
    assert asyncio.run(mod.get_provider_name()) == PROVIDER_META
    assert isinstance(asyncio.run(mod.resolve_whatsapp_provider()), MetaCloudProvider)


def test_sin_canal_se_respeta_la_variable_de_entorno(monkeypatch):
    _sin_cache(monkeypatch)
    _canal_dice(monkeypatch, None)
    monkeypatch.setattr(mod.settings, "WHATSAPP_PROVIDER", "meta")
    assert asyncio.run(mod.get_provider_name()) == PROVIDER_META


def test_un_valor_raro_no_deja_el_canal_mudo(monkeypatch):
    """Un typo en la configuración no puede tumbar los envíos: se cae al de
    siempre en vez de reventar."""
    _sin_cache(monkeypatch)
    _canal_dice(monkeypatch, "twilio")
    monkeypatch.setattr(mod.settings, "WHATSAPP_PROVIDER", "loquesea")
    assert asyncio.run(mod.get_provider_name()) == PROVIDER_YCLOUD


def test_si_no_se_puede_leer_la_base_de_datos_se_sigue_enviando(monkeypatch):
    """Quedarse sin mandar por no poder leer una preferencia sería peor que
    mandar por el proveedor de siempre."""
    _sin_cache(monkeypatch)

    async def _revienta() -> str | None:
        raise RuntimeError("base de datos caída")

    monkeypatch.setattr(mod, "_read_provider_from_db", _revienta)
    monkeypatch.setattr(mod.settings, "WHATSAPP_PROVIDER", "ycloud")
    assert asyncio.run(mod.get_provider_name()) == PROVIDER_YCLOUD


def test_las_instancias_se_reutilizan():
    assert provider_by_name("meta") is provider_by_name("meta")
    assert provider_by_name("ycloud") is not provider_by_name("meta")


# ---------------------------------------------------------------------------
# La fachada
# ---------------------------------------------------------------------------


def test_la_fachada_delega_en_el_proveedor_del_canal(monkeypatch):
    _sin_cache(monkeypatch)
    _canal_dice(monkeypatch, "meta")

    enviados: list[tuple[str, str]] = []

    async def _fake_send(self, to_phone: str, body: str) -> str:
        enviados.append((to_phone, body))
        return "wamid.OUT"

    monkeypatch.setattr(MetaCloudProvider, "send_text", _fake_send)

    wa = get_whatsapp_provider()
    assert isinstance(wa, WhatsAppRouter)
    assert asyncio.run(wa.send_text("+34600111222", "Hola")) == "wamid.OUT"
    assert enviados == [("+34600111222", "Hola")]


def test_la_fachada_conserva_la_firma_de_send_template():
    """`outbound_send` INSPECCIONA la firma antes de llamar para no pasar
    argumentos que el proveedor no entienda. Con un proxy dinámico vería la
    firma equivocada y dejaría de mandar la cabecera o los botones."""
    import inspect

    esperados = set(inspect.signature(YCloudProvider.send_template).parameters) - {"self"}
    reales = set(inspect.signature(get_whatsapp_provider().send_template).parameters)
    assert esperados == reales


def test_la_fachada_no_adivina_quien_manda_un_webhook():
    """Cada proveedor tiene su propia URL justo para no tener que adivinarlo
    por la forma del payload, que sería una fuente de fallos silenciosos."""
    with pytest.raises(NotImplementedError) as exc:
        get_whatsapp_provider().parse_webhook({})
    assert "/webhooks/ycloud" in str(exc.value)
    assert "/webhooks/whatsapp/meta" in str(exc.value)


def test_los_errores_se_siguen_importando_del_sitio_de_siempre():
    """Medio backend hace `from ...ycloud import WhatsAppNotConfiguredError`.
    Al pasar a base.py se re-exportan para no romper esos imports."""
    from app.providers.whatsapp.base import (
        TemplateListError as BaseTemplate,
    )
    from app.providers.whatsapp.base import (
        WhatsAppNotConfiguredError as BaseNotConf,
    )
    from app.providers.whatsapp.ycloud import (
        TemplateListError,
        WhatsAppNotConfiguredError,
        _normalize_phone,
    )

    assert WhatsAppNotConfiguredError is BaseNotConf
    assert TemplateListError is BaseTemplate
    assert _normalize_phone("34600111222") == "+34600111222"
    # Y un identificador `wa:` no se convierte en un teléfono inventado.
    assert _normalize_phone("wa:ES.123") == "wa:ES.123"
