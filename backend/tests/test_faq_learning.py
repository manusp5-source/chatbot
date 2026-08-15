"""Autoaprendizaje · Fase 3 (FAQ automática) — tests puros (sin BD).

Cubren las funciones deterministas del detector: el filtro de no-preguntas, la
selección de los clusters más frecuentes y la construcción del prompt de síntesis
(con su instrucción de privacidad/anonimizado).
"""
from app.tasks.detect_faq_gaps import (
    build_faq_prompt,
    is_probable_question,
    select_top_clusters,
)


def test_is_probable_question_descarta_smalltalk():
    assert not is_probable_question("hola")
    assert not is_probable_question("Gracias!")
    assert not is_probable_question("👍")
    assert not is_probable_question("ok")
    assert not is_probable_question("buenas tardes equipo")  # saludo corto
    assert not is_probable_question("   ")
    assert not is_probable_question("¿qué?")  # demasiado corto


def test_is_probable_question_acepta_preguntas_reales():
    assert is_probable_question("¿Cuánto cuesta el curso de IA?")
    # Empieza por "hola" pero tiene sustancia (no se descarta).
    assert is_probable_question("Hola, ¿hacéis formación para empresas?")
    assert is_probable_question("Necesito saber el horario de atención")


def test_select_top_clusters_filtra_por_tamano_y_ordena_por_frecuencia():
    clusters = [
        [0, 1],            # tamaño 2 → descartado (< 3)
        [2, 3, 4, 5],      # tamaño 4
        [6, 7, 8],         # tamaño 3
        [9],               # tamaño 1 → descartado
    ]
    top = select_top_clusters(clusters, min_size=3, top_n=10)
    # Más frecuente primero, los pequeños fuera.
    assert [len(c) for c in top] == [4, 3]


def test_select_top_clusters_respeta_top_n():
    clusters = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11]]
    top = select_top_clusters(clusters, min_size=3, top_n=2)
    assert len(top) == 2


def test_build_faq_prompt_incluye_privacidad_y_evidencia():
    system, user = build_faq_prompt(
        questions=["¿cuánto cuesta el curso?", "precio del curso?"],
        answers=["El curso cuesta 200€ al mes."],
    )
    # Instrucción de privacidad/anonimizado presente.
    assert "PRIVACIDAD" in system
    assert "personal" in system.lower()
    # Pide JSON con is_faq (filtro fino del LLM).
    assert "is_faq" in system
    # La evidencia (respuesta real) viaja en el prompt de usuario.
    assert "El curso cuesta" in user
    # Las preguntas también.
    assert "precio del curso" in user


def test_build_faq_prompt_sin_respuestas_pide_dejar_vacia():
    system, user = build_faq_prompt(
        questions=["¿tenéis descuentos para estudiantes?"],
        answers=[],
    )
    assert "vacía" in user.lower() or "vacia" in user.lower()
