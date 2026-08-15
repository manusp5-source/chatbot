"""Los clientes de OpenAI tienen que tener timeout y reutilizarse.

Dos fallos que cubre este test:

  - `AsyncOpenAI(api_key=...)` a secas usa los defaults del SDK: 600 s de
    lectura y 2 reintentos → hasta 30 MINUTOS colgado en una sola llamada. Con
    la concurrencia de los workers en 4, cuatro llamadas atascadas dejan al bot
    mudo sin que nada avise.
  - Se creaba un cliente NUEVO en cada llamada, y con él un pool de conexiones
    httpx que nunca se cierra: fuga de sockets y un handshake TLS por llamada.
"""
from __future__ import annotations

import pytest

# Tope de cordura: cualquier timeout por encima de esto ya no protege de nada
# (el default del SDK son 600 s).
_TOPE_SEGUNDOS = 180


def _assert_acotado(client, nombre: str) -> None:
    timeout = client.timeout
    assert timeout is not None, f"{nombre}: sin timeout (el SDK usaría 600 s)"
    for campo in ("connect", "read", "write", "pool"):
        valor = getattr(timeout, campo, None)
        assert valor is not None, f"{nombre}: timeout.{campo} sin límite"
        assert valor <= _TOPE_SEGUNDOS, f"{nombre}: timeout.{campo}={valor}s es demasiado"
    assert client.max_retries <= 1, (
        f"{nombre}: max_retries={client.max_retries} multiplica el tiempo colgado"
    )


@pytest.fixture
def con_clave(monkeypatch):
    async def fake_get_credential(name: str):
        return "sk-de-prueba"

    for modulo in (
        "app.providers.embeddings.openai_embeddings",
        "app.providers.transcription.whisper_client",
        "app.services.moderation",
    ):
        monkeypatch.setattr(f"{modulo}.get_credential", fake_get_credential)
    yield
    # Los clientes cacheados de la clave de prueba no deben sobrevivir al test.
    from app.providers.openai_factory import reset_openai_clients

    reset_openai_clients()


@pytest.mark.asyncio
async def test_cliente_de_chat_acotado(con_clave):
    from app.providers.llm.openai_client import OpenAIProvider

    provider = OpenAIProvider(api_key_credential="openai_api_key", label="openai")
    client = provider._client("sk-de-prueba", None)
    _assert_acotado(client, "chat")


@pytest.mark.asyncio
async def test_cliente_de_embeddings_acotado(con_clave):
    from app.providers.embeddings.openai_embeddings import _get_client

    _assert_acotado(await _get_client(), "embeddings")


@pytest.mark.asyncio
async def test_cliente_de_whisper_acotado(con_clave):
    from app.providers.transcription.whisper_client import _get_client

    _assert_acotado(await _get_client(), "whisper")


@pytest.mark.asyncio
async def test_cliente_de_moderacion_acotado(con_clave):
    from app.services.moderation import _get_client

    _assert_acotado(await _get_client(), "moderación")


@pytest.mark.asyncio
async def test_los_clientes_se_reutilizan(con_clave):
    """Mismo cliente entre llamadas: si no, cada llamada abre un pool httpx
    que nadie cierra."""
    from app.providers.embeddings.openai_embeddings import _get_client as emb
    from app.providers.transcription.whisper_client import _get_client as whi

    assert await emb() is await emb()
    assert await whi() is await whi()
    # Cada uso tiene su propio timeout → no comparten cliente entre sí.
    assert await emb() is not await whi()


@pytest.mark.asyncio
async def test_cambiar_de_clave_da_un_cliente_nuevo(con_clave):
    """Cambiar la credencial en el panel sigue aplicando sin reiniciar."""
    from app.providers.openai_factory import get_async_openai

    primero = get_async_openai(api_key="sk-una")
    assert get_async_openai(api_key="sk-una") is primero
    assert get_async_openai(api_key="sk-otra") is not primero


@pytest.mark.asyncio
async def test_close_libera_los_clientes(con_clave):
    from app.providers.embeddings.openai_embeddings import _get_client as emb
    from app.providers.openai_factory import close_openai_clients

    primero = await emb()
    await close_openai_clients()
    assert primero.is_closed()
    assert await emb() is not primero
