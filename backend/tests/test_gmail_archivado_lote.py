"""Archivar en Gmail: una llamada en vez de N, y fuera del cerrojo.

Lo que había: por cada correo que se descartaba se pedía a Gmail la lista
ENTERA de etiquetas del buzón (para acabar devolviendo siempre el mismo id de
una etiqueta que se crea una vez y no cambia nunca) y luego se archivaba
mensaje a mensaje, en serie, con el cerrojo de la conversación cogido. O sea
1 + N llamadas HTTP a un servicio de fuera dentro de un cerrojo cuyo único
trabajo es que no corran dos vueltas del agente a la vez.

Lo que se comprueba aquí:
  - el id de la etiqueta se cachea y la segunda vez no se pregunta a Gmail;
  - cambiar el nombre de la etiqueta en el panel tira esa caché;
  - varios mensajes se archivan en un `batchModify`;
  - si el lote falla se va de uno en uno, para que un identificador malo no se
    lleve por delante el archivado de los demás;
  - si no se archiva ninguno se olvida el id cacheado (alguien borró la
    etiqueta en Gmail), y así la próxima vez se vuelve a preguntar;
  - el archivado corre con el cerrojo YA soltado.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import gmail_quarantine


class _GmailFalso:
    """Cliente de Gmail de mentira que apunta lo que le piden."""

    def __init__(self, *, batch_ok=True, modify_ok=True):
        self.batch_ok = batch_ok
        self.modify_ok = modify_ok
        self.batches: list[list[str]] = []
        self.modificados: list[str] = []

    async def batch_modify_messages(self, ids, add=None, remove=None):
        self.batches.append(list(ids))
        return self.batch_ok

    async def modify_message(self, message_id, add=None, remove=None):
        self.modificados.append(message_id)
        return self.modify_ok


def _mensajes(n: int):
    return [(uuid.uuid4(), f"gmail-{i}") for i in range(n)]


@pytest.mark.asyncio
async def test_varios_mensajes_van_en_una_sola_llamada():
    gmail = _GmailFalso()
    mensajes = _mensajes(4)
    with patch.object(gmail_quarantine, "_marcar", AsyncMock()) as marcar:
        tocados = await gmail_quarantine._archivar_mensajes(
            gmail, mensajes, "Label_7", "Descartado por el bot"
        )
    assert tocados == 4
    assert len(gmail.batches) == 1, "seguía llamando a Gmail una vez por mensaje"
    assert gmail.modificados == []
    # Todos quedan marcados con la etiqueta con la que se archivaron: es lo que
    # permite deshacerlo después sobre esos y solo esos.
    assert marcar.await_count == 4


@pytest.mark.asyncio
async def test_con_un_solo_mensaje_no_se_monta_un_lote():
    gmail = _GmailFalso()
    with patch.object(gmail_quarantine, "_marcar", AsyncMock()):
        tocados = await gmail_quarantine._archivar_mensajes(
            gmail, _mensajes(1), "Label_7", "Etiqueta"
        )
    assert tocados == 1
    assert gmail.batches == []
    assert len(gmail.modificados) == 1


@pytest.mark.asyncio
async def test_si_el_lote_falla_se_va_de_uno_en_uno():
    """`batchModify` es todo o nada: un id malo tumbaría el archivado entero."""
    gmail = _GmailFalso(batch_ok=False, modify_ok=True)
    with patch.object(gmail_quarantine, "_marcar", AsyncMock()):
        tocados = await gmail_quarantine._archivar_mensajes(
            gmail, _mensajes(3), "Label_7", "Etiqueta"
        )
    assert tocados == 3
    assert len(gmail.batches) == 1
    assert len(gmail.modificados) == 3


@pytest.mark.asyncio
async def test_si_no_se_archiva_nada_se_olvida_la_etiqueta_cacheada():
    """El caso real: alguien borró la etiqueta en Gmail y el id ya no vale."""
    gmail = _GmailFalso(batch_ok=False, modify_ok=False)
    with patch.object(gmail_quarantine, "_marcar", AsyncMock()), patch(
        "app.providers.gmail.client.olvidar_label_id", AsyncMock()
    ) as olvidar:
        tocados = await gmail_quarantine._archivar_mensajes(
            gmail, _mensajes(2), "Label_viejo", "Descartado por el bot"
        )
    assert tocados == 0
    olvidar.assert_awaited_once_with("Descartado por el bot")


# --------------------------------------------------------------- la caché


@pytest.mark.asyncio
async def test_la_etiqueta_solo_se_pregunta_a_gmail_la_primera_vez():
    from app.providers.gmail import client as gmail_client

    guardado: dict[str, str] = {}

    class _Redis:
        async def get(self, k):
            return guardado.get(k)

        async def set(self, k, v, ex=None):
            guardado[k] = v

        async def delete(self, k):
            guardado.pop(k, None)

    llamadas = {"n": 0}

    class _Respuesta:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"labels": [{"id": "Label_9", "name": "Descartes", "type": "user"}]}

    class _HttpFalso:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            llamadas["n"] += 1
            return _Respuesta()

        async def post(self, *a, **k):
            raise AssertionError("no debería crear la etiqueta: ya existe")

    cliente = gmail_client.GmailClient()
    with patch.object(gmail_client, "get_redis", lambda: _Redis()), patch.object(
        gmail_client.httpx, "AsyncClient", lambda **k: _HttpFalso()
    ), patch.object(cliente, "_headers", AsyncMock(return_value={"Authorization": "x"})):
        assert await cliente.ensure_label("Descartes") == "Label_9"
        assert await cliente.ensure_label("Descartes") == "Label_9"

    assert llamadas["n"] == 1, "se pedía la lista entera de etiquetas en cada correo"


@pytest.mark.asyncio
async def test_olvidar_la_etiqueta_hace_que_se_vuelva_a_preguntar():
    from app.providers.gmail import client as gmail_client

    guardado: dict[str, str] = {}

    class _Redis:
        async def get(self, k):
            return guardado.get(k)

        async def set(self, k, v, ex=None):
            guardado[k] = v

        async def delete(self, k):
            guardado.pop(k, None)

    with patch.object(gmail_client, "get_redis", lambda: _Redis()):
        await gmail_client._guardar_label_id("Descartes", "Label_9")
        assert await gmail_client.label_id_cacheado("Descartes") == "Label_9"
        # El nombre no distingue mayúsculas: la clave se normaliza.
        assert await gmail_client.label_id_cacheado("descartes") == "Label_9"
        await gmail_client.olvidar_label_id("Descartes")
        assert await gmail_client.label_id_cacheado("Descartes") is None


@pytest.mark.asyncio
async def test_sin_redis_se_pregunta_a_gmail_como_siempre():
    from app.providers.gmail import client as gmail_client

    def _muerto():
        raise RuntimeError("Redis no responde")

    with patch.object(gmail_client, "get_redis", _muerto):
        assert await gmail_client.label_id_cacheado("Descartes") is None
        await gmail_client._guardar_label_id("Descartes", "Label_9")  # no revienta
        await gmail_client.olvidar_label_id("Descartes")  # tampoco


# ------------------------------------------------------------- el cerrojo


@pytest.mark.asyncio
async def test_lo_diferido_corre_con_el_cerrojo_ya_soltado():
    """Gmail tiene su propio timeout: colgado dentro del cerrojo, bloqueaba la
    conversación entera."""
    import app.services.conversation as conv_mod

    conv_id = uuid.uuid4()
    estado = {"libre_al_ejecutar": None}
    almacen: dict[str, str] = {}

    class _Redis:
        async def set(self, k, v, nx=False, ex=None):
            if nx and k in almacen:
                return None
            almacen[k] = v
            return True

        async def eval(self, *a, **k):
            almacen.pop(a[2], None)
            return 1

    async def _interior(_clave, diferidas):
        async def _accion():
            # Si el cerrojo sigue cogido, esto devolvería None.
            estado["libre_al_ejecutar"] = await conv_mod._acquire_conv_lock(conv_id) is not None

        diferidas.append(_accion)

    with patch.object(conv_mod, "get_redis", lambda: _Redis()), patch.object(
        conv_mod, "_process_buffered_locked", _interior
    ):
        await conv_mod.process_buffered_messages(f"conv:{conv_id}")

    assert estado["libre_al_ejecutar"] is True, (
        "el archivado en Gmail seguía corriendo con el cerrojo de la conversación cogido"
    )


@pytest.mark.asyncio
async def test_una_diferida_que_falla_no_se_lleva_a_las_demas():
    import app.services.conversation as conv_mod

    hechas: list[str] = []

    async def _revienta():
        raise RuntimeError("Gmail no responde")

    async def _va_bien():
        hechas.append("ok")

    await conv_mod._ejecutar_diferidas([_revienta, _va_bien])
    assert hechas == ["ok"]
