"""Canal web: el session_token y la lista de dominios permitidos (sin BD).

Fallos que cubre la auditoría:

  B5 — desactivar el canal o regenerar la api_key no cortaba nada. El token era
       un HMAC de conv_id + visitor_id SIN versión ni caducidad, y
       `POST /webchat/messages` no consultaba el `Channel` en ningún momento.
       Resultado: apagabas el widget desde el panel y los visitantes con sesión
       abierta seguían gastando modelo; regenerabas la clave porque se te había
       filtrado y todas las sesiones emitidas seguían vivas. La única forma de
       cortar era borrar el canal o cambiar JWT_SECRET (que tira la sesión del
       panel de todo el mundo).

  I16 — `models/channel.py` documentaba `allowed_domains[]` y no había una sola
        línea que lo implementara ni lo leyera. La api_key viaja en el HTML
        público de la web del cliente: sin lista de dominios, cualquiera copia
        el snippet a su sitio y gasta el presupuesto de modelo ajeno.

Aquí van las piezas puras (firma, caducidad, parsing de dominios). El recorrido
completo contra la BD está en test_webchat_api.py.
"""
from __future__ import annotations

import time

import pytest

from app.api import webchat as wc

# ---------------------------------------------------------------------------
# session_token: versión, huella de la clave y caducidad
# ---------------------------------------------------------------------------


def test_el_token_lleva_version_huella_y_caducidad():
    token, exp = wc._make_session_token("11111111-1111-4111-8111-111111111111", "clave-secreta")
    partes = token.split(".")
    assert len(partes) == 4
    assert partes[0] == "v1"
    assert partes[1] == wc._api_key_fingerprint("clave-secreta")
    assert int(partes[2]) == exp
    assert exp > int(time.time())
    # La api_key NO viaja dentro del token, solo su huella.
    assert "clave-secreta" not in token


def test_la_huella_cambia_al_regenerar_la_clave():
    """Es lo que hace que regenerar la api_key mate las sesiones emitidas."""
    assert wc._api_key_fingerprint("vieja") != wc._api_key_fingerprint("nueva")


def test_el_token_esta_ligado_al_visitante():
    a, _ = wc._make_session_token("11111111-1111-4111-8111-111111111111", "k", now=1000)
    b, _ = wc._make_session_token("22222222-2222-4222-8222-222222222222", "k", now=1000)
    assert a != b


def test_la_caducidad_no_es_infinita():
    assert 0 < wc.WEBCHAT_SESSION_TTL_SECONDS <= 24 * 3600


def test_la_firma_cubre_la_caducidad():
    """Que nadie pueda alargarse la sesión editando el campo de caducidad."""
    vid = "11111111-1111-4111-8111-111111111111"
    token, exp = wc._make_session_token(vid, "k")
    _v, fp, _exp, sig = token.split(".")
    assert wc._sign_session(vid, fp, exp) == sig
    assert wc._sign_session(vid, fp, exp + 99999) != sig


# ---------------------------------------------------------------------------
# allowed_domains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("https://Ejemplo.com/pagina", "ejemplo.com"),
        ("http://ejemplo.com:8080", "ejemplo.com"),
        ("  ejemplo.com  ", "ejemplo.com"),
        ("ejemplo.com:443", "ejemplo.com"),
        ("https://sub.ejemplo.com", "sub.ejemplo.com"),
        ("", None),
        (None, None),
    ],
)
def test_normalizar_host(entrada, esperado):
    """El administrador escribe el dominio como le sale; el navegador manda un
    Origin con esquema. Los dos tienen que acabar en lo mismo."""
    assert wc._normalize_host(entrada) == esperado


def test_dominio_exacto():
    assert wc.host_matches_domain("ejemplo.com", "ejemplo.com")
    assert not wc.host_matches_domain("otro.com", "ejemplo.com")
    # Un subdominio NO entra por la puerta del dominio exacto.
    assert not wc.host_matches_domain("sub.ejemplo.com", "ejemplo.com")
    # Y el truco clásico: un dominio que TERMINA en el permitido.
    assert not wc.host_matches_domain("malejemplo.com", "ejemplo.com")


def test_comodin_de_subdominios():
    assert wc.host_matches_domain("ejemplo.com", "*.ejemplo.com")
    assert wc.host_matches_domain("www.ejemplo.com", "*.ejemplo.com")
    assert wc.host_matches_domain("a.b.ejemplo.com", "*.ejemplo.com")
    assert not wc.host_matches_domain("ejemplo.com.malo.com", "*.ejemplo.com")
    assert not wc.host_matches_domain("malejemplo.com", "*.ejemplo.com")


def test_lectura_de_la_lista_del_canal():
    class _Ch:
        config = {
            "allowed_domains": [
                " https://Ejemplo.com/ ",
                "*.Otro.com",
                "",
                None,
                123,
            ]
        }

    assert wc.channel_allowed_domains(_Ch()) == ["ejemplo.com", "*.otro.com"]


def test_lista_admite_texto_separado_por_comas():
    """El panel puede mandar un textarea; no queremos que eso lo tire todo."""

    class _Ch:
        config = {"allowed_domains": "ejemplo.com, *.otro.com\ntercero.com"}

    assert wc.channel_allowed_domains(_Ch()) == [
        "ejemplo.com",
        "*.otro.com",
        "tercero.com",
    ]


def test_sin_lista_no_se_restringe_nada():
    class _Ch:
        config: dict = {}

    assert wc.channel_allowed_domains(_Ch()) == []
