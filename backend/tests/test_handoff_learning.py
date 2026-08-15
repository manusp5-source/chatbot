"""Tests del aprendizaje por derivación a humano (hueco de conocimiento).

Cobertura (funciones PURAS, sin BD ni red):
  - extract_handoff_question: en canales de chat devuelve el mensaje tal cual.
  - En email LIMPIA el hilo citado (">"/"El ... escribió:") y las firmas, y
    antepone el asunto como contexto — el bug era que el aprendizaje guardaba
    el HILO entero del correo en lugar del último mensaje relevante.
  - Correo que era solo cita/firma: no se pierde (clean_email_body devuelve el
    original).
"""
from __future__ import annotations

from app.agents.tools.human_handoff import extract_handoff_question


def test_chat_message_passthrough():
    q = extract_handoff_question("¿Cuánto cuesta el plan premium?")
    assert q == "¿Cuánto cuesta el plan premium?"


def test_empty_message_returns_empty():
    assert extract_handoff_question(None) == ""
    assert extract_handoff_question("   ") == ""
    assert extract_handoff_question("", es_email=True, subject="Hola") == ""


def test_email_strips_quoted_thread_and_signature():
    body = (
        "Hola, ¿me podéis decir si el plan premium incluye soporte por email?\n"
        "\n"
        "Gracias,\n"
        "Marta\n"
        "\n"
        "El lun, 14 jul 2026 a las 10:02, Soporte escribió:\n"
        "> Hola Marta,\n"
        "> Gracias por escribirnos...\n"
        "> \n"
        "> El dom, 13 jul 2026, Marta escribió:\n"
        ">> Primera pregunta del hilo\n"
    )
    q = extract_handoff_question(body, es_email=True, subject="Duda plan premium")
    assert q.startswith("Asunto: Duda plan premium\n\n")
    assert "¿me podéis decir si el plan premium incluye soporte por email?" in q
    # El hilo citado NO va al aprendizaje.
    assert "escribió:" not in q
    assert "Primera pregunta del hilo" not in q
    assert ">" not in q


def test_email_without_subject_no_prefix():
    q = extract_handoff_question("Hola, una duda rápida.", es_email=True, subject=None)
    assert not q.startswith("Asunto:")
    assert q == "Hola, una duda rápida."


def test_email_only_quote_keeps_original():
    # Si tras limpiar no queda nada, clean_email_body devuelve el original:
    # mejor un aprendizaje con ruido que perder la señal.
    body = "> mensaje citado sin contenido nuevo\n"
    q = extract_handoff_question(body, es_email=True)
    assert "mensaje citado sin contenido nuevo" in q
