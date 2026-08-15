"""El filtro gratis de correo automático acepta las DOS formas de la cabecera.

`email_is_automated` aparta newsletters, marketing, notificaciones y
autorespuestas ANTES de gastar una llamada al modelo. Buscaba los nombres
REALES de la cabecera (`list-unsubscribe`, `auto-submitted`), pero el proveedor
de Gmail las guarda con nombre de variable (`list_unsubscribe`,
`auto_submitted`): de los cuatro criterios solo funcionaban dos (Precedence,
que se escribe igual, y el remitente no-reply), así que todas las newsletters
con cabecera de baja y todas las autorespuestas se colaban al modelo.

El apaño vivía en el llamante. Aquí se comprueba que está arreglado de RAÍZ, en
la propia función, para que un llamante nuevo no vuelva a tropezar.
"""
from __future__ import annotations

from app.services.classifier import email_is_automated


def test_las_cabeceras_con_guion_bajo_disparan_el_filtro():
    """Tal cual las guarda el proveedor en Message.extra['email_headers']."""
    assert email_is_automated("ana@x.com", {"list_unsubscribe": "<mailto:baja@x>"})[0]
    assert email_is_automated("ana@x.com", {"list_id": "<news.x.com>"})[0]
    assert email_is_automated("ana@x.com", {"auto_submitted": "auto-replied"})[0]


def test_las_cabeceras_con_guion_siguen_disparando():
    """Los nombres del RFC, que es lo que llega si alguien las normaliza."""
    assert email_is_automated("ana@x.com", {"List-Unsubscribe": "<mailto:baja@x>"})[0]
    assert email_is_automated("ana@x.com", {"auto-submitted": "auto-generated"})[0]


def test_las_dos_formas_a_la_vez_no_se_pisan():
    """El llamante actual normaliza y manda las dos: un None de una forma no
    puede borrar el valor de la otra."""
    mezcla = {
        "list_unsubscribe": "<mailto:baja@x>",
        "list-unsubscribe": None,
        "auto_submitted": None,
        "auto-submitted": "auto-replied",
    }
    auto, motivo = email_is_automated("ana@x.com", mezcla)
    assert auto is True
    assert motivo


def test_un_correo_personal_normal_sigue_pasando():
    """El filtro es conservador a propósito: no puede cuarentenar a un cliente
    real solo porque el correo traiga cabeceras sueltas vacías."""
    limpio = {
        "list_unsubscribe": None,
        "list_id": None,
        "precedence": None,
        "auto_submitted": None,
    }
    assert email_is_automated("cliente@empresa.com", limpio)[0] is False
    # Auto-Submitted: no (RFC 3834) es EXPLÍCITAMENTE un correo de persona.
    assert email_is_automated("cliente@empresa.com", {"auto_submitted": "no"})[0] is False
