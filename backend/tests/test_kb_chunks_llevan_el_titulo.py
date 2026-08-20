"""Un trozo de documento tiene que saber de qué documento es.

El primer chunk de un documento arrastra su título porque es el arranque del
texto; los siguientes no llevaban nada. Resultado: preguntando "cuánto cuesta un
blanqueamiento", el trozo que contiene literalmente `Blanqueamiento en clínica |
290 €` puntuaba POR DEBAJO del documento de horarios, y el agente contestaba que
no tenía ese precio. El peor síntoma posible para el negocio: decir que no se
tiene un dato que la clínica sí tiene.
"""
from __future__ import annotations

from app.services.kb_indexer import CHUNK_SIZE, _chunk_text, _titulo_del_documento


def test_el_titulo_sale_de_la_primera_linea_de_encabezado():
    assert _titulo_del_documento("# Servicios y precios\n\nTexto") == "Servicios y precios"
    assert _titulo_del_documento("\n\n## Horario\n\nTexto") == "Horario"


def test_un_encabezado_a_mitad_de_texto_no_es_el_titulo():
    """`#` después de que empiece el cuerpo es una sección, no el título."""
    texto = "Esto es el cuerpo del documento.\n\n# Una sección de más abajo\n\nMás cuerpo."
    assert _titulo_del_documento(texto) == ""


def test_documento_sin_encabezado_no_rompe_nada():
    texto = "Un documento en texto plano, sin markdown.\n\n" + ("x " * 50)
    assert _titulo_del_documento(texto) == ""
    chunks = _chunk_text(texto, source="plano.txt")
    assert chunks
    assert all(c["contenido"].strip() for c in chunks)


def _documento_de_varios_chunks() -> str:
    """Un documento con el dato buscado LEJOS del título, como el real."""
    relleno = "\n\n".join(
        f"Párrafo de relleno número {i} con texto suficiente para ocupar sitio. " * 4
        for i in range(12)
    )
    return (
        "# Servicios y precios — Clínica Dental Miralba\n\n"
        f"{relleno}\n\n"
        "## Estética dental\n\n"
        "| Tratamiento | Precio |\n"
        "|---|---|\n"
        "| Blanqueamiento en clínica | 290 € |\n"
    )


def test_los_chunks_posteriores_llevan_el_titulo_delante():
    chunks = _chunk_text(_documento_de_varios_chunks(), source="1-servicios.md")

    assert len(chunks) > 1, "el documento de prueba tiene que partirse en varios chunks"
    for c in chunks[1:]:
        assert c["contenido"].startswith("Servicios y precios — Clínica Dental Miralba"), (
            "un chunk que no es el primero se queda sin contexto del documento"
        )


def test_el_primer_chunk_no_repite_el_titulo():
    """Ya lo lleva dentro: repetirlo lo duplicaría en el embedding."""
    chunks = _chunk_text(_documento_de_varios_chunks(), source="1-servicios.md")
    primero = chunks[0]["contenido"]
    assert primero.count("Servicios y precios — Clínica Dental Miralba") == 1


def test_el_chunk_del_precio_queda_identificable():
    """El caso concreto que motivó el cambio, comprobado de punta a punta."""
    chunks = _chunk_text(_documento_de_varios_chunks(), source="1-servicios.md")
    con_precio = [c for c in chunks if "290" in c["contenido"]]

    assert con_precio, "el precio tiene que estar en algún chunk"
    contenido = con_precio[0]["contenido"]
    assert "Blanqueamiento" in contenido
    assert "Miralba" in contenido, (
        "sin el nombre del centro, el embedding de este trozo no compite con el "
        "del primer chunk cuando preguntan por el blanqueamiento de esa clínica"
    )


def test_el_titulo_no_dispara_chunks_gigantes():
    """El prefijo suma tamaño: no puede desbordar el límite sin control."""
    chunks = _chunk_text(_documento_de_varios_chunks(), source="1-servicios.md")
    titulo = "Servicios y precios — Clínica Dental Miralba"
    holgura = len(titulo) + 2
    for c in chunks:
        assert len(c["contenido"]) <= CHUNK_SIZE + holgura


def test_la_metadata_sigue_intacta():
    chunks = _chunk_text(_documento_de_varios_chunks(), source="1-servicios.md", page=3)
    for c in chunks:
        assert c["metadata"]["source"] == "1-servicios.md"
        assert c["metadata"]["page"] == 3
