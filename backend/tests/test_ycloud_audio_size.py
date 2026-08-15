"""La nota de voz de WhatsApp NO se carga entera en RAM antes de mirar el tamaño.

Fallo que cubre (auditoría #12): `YCloudProvider.download_audio` hacía un
`client.get(...)` y devolvía `r.content`, o sea el fichero COMPLETO en memoria
del worker. El tope (`MAX_AUDIO_BYTES`) se comprobaba después, en
`audio_processor`: para entonces los bytes ya estaban dentro. Con un media de
varios cientos de MB (o un servidor que responde sin fin) el worker se come la
RAM del contenedor y se lo lleva por delante — y el worker es único, así que
para el bot entero.

El provider de Instagram ya lo hace bien: `client.stream` + corte por
Content-Length + corte durante la descarga. Esto replica ese patrón; el test
comprueba las tres cosas:
  1. corte inmediato si el servidor declara un tamaño excesivo (sin descargar);
  2. corte DURANTE la descarga aunque no venga Content-Length;
  3. el camino normal sigue devolviendo (bytes, mime).
"""
from __future__ import annotations

import pytest

from app.services.agent_guardrails import MAX_AUDIO_BYTES

_URL = "https://api.ycloud.com/media/abc.ogg"


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], headers: dict, status_code: int = 200) -> None:
        self._chunks = chunks
        self.headers = headers
        self.status_code = status_code
        self.leidos = 0  # bytes que el cliente llegó a traerse de la red

    async def aiter_bytes(self):
        for c in self._chunks:
            self.leidos += len(c)
            yield c

    async def aread(self) -> bytes:
        data = b"".join(self._chunks)
        self.leidos += len(data)
        return data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeStreamCtx:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_exc):
        return False


class _FakeClient:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, _method: str, _url: str, **_kw):
        return _FakeStreamCtx(self._response)

    async def get(self, *_a, **_kw):  # la implementación vieja usaba esto
        return self._response


def _patch(monkeypatch, response: _FakeStreamResponse):
    from app.providers.whatsapp import ycloud as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **_kw: _FakeClient(response))
    monkeypatch.setattr(mod, "_host_allowed", lambda _h: True)

    async def _no_private(_h):
        return False

    monkeypatch.setattr(mod, "_resolves_to_private", _no_private)
    return mod


def _provider(mod):
    p = mod.YCloudProvider()

    async def _key():
        return "k"

    p._api_key = _key  # type: ignore[method-assign]
    return p


@pytest.mark.asyncio
async def test_content_length_excesivo_corta_sin_descargar(monkeypatch):
    grande = MAX_AUDIO_BYTES * 3
    resp = _FakeStreamResponse(
        chunks=[b"x" * 65536] * 10,
        headers={"content-length": str(grande), "content-type": "audio/ogg"},
    )
    mod = _patch(monkeypatch, resp)

    with pytest.raises(ValueError):
        await _provider(mod).download_audio(_URL)

    # Lo importante: no se ha traído ni un byte del cuerpo.
    assert resp.leidos == 0


@pytest.mark.asyncio
async def test_sin_content_length_se_corta_durante_la_descarga(monkeypatch):
    """Un servidor con `Transfer-Encoding: chunked` no declara tamaño."""
    chunk = b"y" * (1024 * 1024)
    n_chunks = (MAX_AUDIO_BYTES // len(chunk)) + 20
    resp = _FakeStreamResponse(
        chunks=[chunk] * n_chunks, headers={"content-type": "audio/ogg"}
    )
    mod = _patch(monkeypatch, resp)

    with pytest.raises(ValueError):
        await _provider(mod).download_audio(_URL)

    # Se paró en cuanto pasó del tope: no se leyó el stream entero.
    assert resp.leidos <= MAX_AUDIO_BYTES + len(chunk)
    assert resp.leidos < n_chunks * len(chunk)


@pytest.mark.asyncio
async def test_audio_normal_se_descarga_entero(monkeypatch):
    resp = _FakeStreamResponse(
        chunks=[b"ogg-", b"datos"],
        headers={"content-length": "9", "content-type": "audio/ogg; codecs=opus"},
    )
    mod = _patch(monkeypatch, resp)

    data, mime = await _provider(mod).download_audio(_URL)
    assert data == b"ogg-datos"
    assert mime.startswith("audio/ogg")


@pytest.mark.asyncio
async def test_host_no_permitido_sigue_bloqueado(monkeypatch):
    """El guard anti-SSRF no se pierde con el cambio a streaming."""
    resp = _FakeStreamResponse(chunks=[b"x"], headers={})
    from app.providers.whatsapp import ycloud as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **_kw: _FakeClient(resp))
    with pytest.raises(ValueError):
        await _provider(mod).download_audio("https://malicioso.example.com/a.ogg")
    assert resp.leidos == 0
