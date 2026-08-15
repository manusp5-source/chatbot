import re

import pytest

from app.services.conversation import _split_response


def _rejoin(parts: list[str]) -> str:
    """Une los trozos como texto plano comparable (colapsa los separadores
    \\n\\n internos a un espacio) para verificar que no se perdió nada."""
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_split_by_paragraphs():
    text = "Hola, ¿cómo estás?\n\nGracias por escribirnos.\n\nDime en qué puedo ayudarte."
    parts = _split_response(text, max_parts=3)
    assert len(parts) == 3


def test_split_single_short_no_split():
    text = "Te respondo enseguida."
    parts = _split_response(text, max_parts=3)
    assert parts == [text]


def test_split_long_by_sentences():
    text = ("Vale. Te explico. Tenemos servicios distintos. Cada uno con su precio. "
            "Cuéntame cuál te interesa. Lo miramos juntos.")
    parts = _split_response(text, max_parts=3)
    assert len(parts) <= 3
    assert all(p.strip() for p in parts)


# ---------- I1: NUNCA se pierde contenido ----------


@pytest.mark.parametrize(
    "text,max_parts",
    [
        # Caso reportado: 5 frases, max_parts=3 (antes perdía "Cuatro. Cinco.").
        ("Uno. Dos. Tres. Cuatro. Cinco.", 3),
        # 4 párrafos, max_parts=3 (antes descartaba el 4º con parts[:max_parts]).
        ("Párrafo uno.\n\nPárrafo dos.\n\nPárrafo tres.\n\nPárrafo cuatro.", 3),
        # Muchas más frases que trozos.
        ("A. B. C. D. E. F. G. H. I. J.", 3),
        # Justo en el límite por frases.
        ("Uno. Dos. Tres.", 3),
        # Una sola frase.
        ("Una sola frase sin puntos internos", 3),
        # max_parts=2.
        ("Uno. Dos. Tres. Cuatro.", 2),
        # Párrafos justo en el límite.
        ("P1.\n\nP2.\n\nP3.", 3),
        # Más párrafos que max_parts, con max_parts=2.
        ("P1.\n\nP2.\n\nP3.\n\nP4.\n\nP5.", 2),
    ],
)
def test_split_never_loses_content(text, max_parts):
    parts = _split_response(text, max_parts=max_parts)
    # Invariante 1: no se supera max_parts.
    assert len(parts) <= max_parts, f"len={len(parts)} > {max_parts}"
    # Invariante 2: unir los trozos recupera TODO el texto (sin pérdida).
    assert _rejoin(parts) == _normalize(text)
    # Invariante 3: ningún trozo vacío.
    assert all(p.strip() for p in parts)


def test_split_paragraph_overflow_merges_into_last():
    """El exceso de párrafos se fusiona en el ÚLTIMO trozo, no se descarta."""
    text = "P1.\n\nP2.\n\nP3.\n\nP4."
    parts = _split_response(text, max_parts=3)
    assert len(parts) == 3
    # El último trozo contiene P3 y P4 fusionados.
    assert "P3." in parts[-1] and "P4." in parts[-1]


def test_split_sentence_overflow_into_last_chunk():
    """5 frases / max_parts=3 → ceil(5/3)=2 frases por trozo: [2,2,1]."""
    parts = _split_response("Uno. Dos. Tres. Cuatro. Cinco.", max_parts=3)
    assert len(parts) == 3
    assert _rejoin(parts) == "Uno. Dos. Tres. Cuatro. Cinco."
