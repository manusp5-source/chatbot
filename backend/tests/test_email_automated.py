"""Tests del filtro determinista de correo automático (anti-tokens).

`email_is_automated` aparta newsletters / marketing / notificaciones /
autorespuestas SIN gastar LLM, por cabeceras estándar (List-Unsubscribe,
List-Id, Precedence: bulk, Auto-Submitted) o por remitente no-reply. Lo
importante: NO debe marcar correos personales normales (riesgo de falso
positivo bajo y recuperable, pero igualmente lo cubrimos).
"""
from app.services.classifier import email_is_automated


def test_list_unsubscribe_is_automated():
    auto, reason = email_is_automated("ofertas@tienda.com", {"List-Unsubscribe": "<https://x/u>"})
    assert auto is True
    assert "lista" in reason


def test_list_id_is_automated():
    auto, _ = email_is_automated("hello@news.com", {"List-Id": "<news.example.com>"})
    assert auto is True


def test_precedence_bulk_is_automated():
    auto, reason = email_is_automated("info@empresa.com", {"Precedence": "bulk"})
    assert auto is True
    assert "masivo" in reason


def test_auto_submitted_is_automated():
    auto, _ = email_is_automated("soporte@empresa.com", {"Auto-Submitted": "auto-generated"})
    assert auto is True


def test_auto_submitted_no_is_not_automated():
    # Auto-Submitted: no → correo normal, NO automático.
    auto, _ = email_is_automated("juan@gmail.com", {"Auto-Submitted": "no"})
    assert auto is False


def test_noreply_sender_is_automated():
    auto, reason = email_is_automated("no-reply@facturas.com", None)
    assert auto is True
    assert "no-reply" in reason


def test_real_person_is_not_automated():
    auto, reason = email_is_automated(
        "cliente@example.com", {"Subject": "Consulta sobre presupuesto"}
    )
    assert auto is False
    assert reason == ""


def test_info_sender_without_headers_is_not_automated():
    # info@ es buzón de persona real: NO debe filtrarse sin señal de cabecera.
    auto, _ = email_is_automated("info@clientecorp.com", {})
    assert auto is False


def test_headers_case_insensitive():
    auto, _ = email_is_automated("x@y.com", {"list-unsubscribe": "<mailto:u@y.com>"})
    assert auto is True


def test_no_sender_no_headers():
    auto, reason = email_is_automated(None, None)
    assert auto is False
    assert reason == ""
