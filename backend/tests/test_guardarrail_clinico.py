"""Guardarrail clinico: el filtro que corre antes del modelo.

Por que se prueba tan a fondo una lista de palabras: porque es la unica pieza
del sistema cuyo fallo no se ve. Si el RAG falla, el bot contesta mal y alguien
lo nota. Si esto falla, el bot contesta *bien* -- con soltura y educadamente --
a la pregunta de un paciente sobre su sintoma, y nadie lo nota hasta que hay un
problema.

Lo que cubre:
  - los sintomas, la medicacion y los antecedentes que tienen que derivar;
  - la urgencia gana a lo clinico cuando el mensaje trae las dos cosas, porque
    la respuesta al paciente tiene que ser la mas conservadora;
  - tildes y mayusculas, que en WhatsApp no se pueden dar por buenas;
  - frontera de palabra: "indolora" no contiene "dolor" a efectos de este
    filtro, y si lo contuviera el equipo dejaria de fiarse de las derivaciones;
  - los mensajes normales de agenda y precio NO derivan, que es lo que hace que
    el producto siga sirviendo para algo.
"""
import pytest

from app.agents.guardarrail_clinico import (
    MENSAJE_CLINICO,
    MENSAJE_URGENCIA,
    NivelClinico,
    evaluar,
    mensaje_para,
)


# --- lo que TIENE que derivar ----------------------------------------------

@pytest.mark.parametrize(
    "mensaje",
    [
        "me duele mucho la muela desde ayer",
        "tengo la encia inflamada",
        "me sangra al cepillarme",
        "se me ha infectado el implante",
        "puedo tomar ibuprofeno con el antibiotico?",
        "me han recetado amoxicilina, es normal?",
        "soy alergica a la penicilina",
        "estoy embarazada, puedo hacerme la radiografia?",
        "tomo sintrom, hay algun problema?",
        "se me ha caido el empaste",
        "que puede ser esto que me ha salido",
    ],
)
def test_contenido_clinico_deriva(mensaje: str) -> None:
    v = evaluar(mensaje)
    assert v is not None, f"NO derivo: {mensaje!r}"


@pytest.mark.parametrize(
    "mensaje",
    [
        "se me ha hinchado la cara y tengo fiebre",
        "no para de sangrar despues de la extraccion",
        "tengo un flemon enorme",
        "no puedo abrir la boca",
        "me he dado un golpe y se me ha partido un diente",
    ],
)
def test_urgencia_se_clasifica_como_urgencia(mensaje: str) -> None:
    v = evaluar(mensaje)
    assert v is not None
    assert v.nivel is NivelClinico.urgencia


def test_urgencia_gana_a_clinico_en_el_mismo_mensaje() -> None:
    """El mensaje trae "duele" (clinico) y "hinchazon en la cara" (urgencia).

    Si ganara el primero que aparece en el texto, el paciente recibiria el
    mensaje suave y no la indicacion de llamar. La prioridad es por gravedad,
    no por orden de aparicion.
    """
    v = evaluar("me duele y se me ha hinchado la cara")
    assert v is not None
    assert v.nivel is NivelClinico.urgencia


# --- normalizacion ----------------------------------------------------------

@pytest.mark.parametrize(
    "mensaje",
    ["ME DUELE MUCHO", "Me Duele", "me duéle", "tengo INFECCIÓN", "infeccion"],
)
def test_tildes_y_mayusculas_no_esquivan_el_filtro(mensaje: str) -> None:
    assert evaluar(mensaje) is not None


def test_espacios_raros_no_esquivan_el_filtro() -> None:
    assert evaluar("me    duele\n\nmucho") is not None


# --- lo que NO debe derivar -------------------------------------------------

@pytest.mark.parametrize(
    "mensaje",
    [
        "hola, quiero pedir cita para una limpieza",
        "a que hora abris los martes?",
        "cuanto cuesta un blanqueamiento?",
        "puedo cambiar mi cita del jueves?",
        "donde estais exactamente?",
        "hacen ortodoncia invisible?",
        "necesito factura de la ultima visita",
        "gracias, hasta el jueves",
    ],
)
def test_mensajes_normales_no_derivan(mensaje: str) -> None:
    v = evaluar(mensaje)
    assert v is None, f"derivo sin motivo: {mensaje!r} (por {v.termino!r})" if v else ""


@pytest.mark.parametrize(
    "mensaje",
    [
        "la limpieza fue indolora, gracias",
        "me llamo Dolores y quiero cita",
    ],
)
def test_frontera_de_palabra(mensaje: str) -> None:
    """"indolora" y "Dolores" contienen "dolor" como subcadena.

    Sin frontera de palabra, ambos derivarian. Un equipo que recibe
    derivaciones absurdas deja de mirarlas, y ese es el fallo que de verdad
    rompe el guardarrail: no que filtre de menos, sino que se le deje de hacer
    caso.
    """
    assert evaluar(mensaje) is None


def test_vacio_y_none_no_derivan() -> None:
    assert evaluar("") is None
    assert evaluar("   ") is None
    assert evaluar(None) is None


# --- lo que se registra y lo que lee el paciente ----------------------------

def test_el_motivo_nombra_el_termino_exacto() -> None:
    """El registro de auditoria tiene que poder demostrar POR QUE se derivo.

    "Se derivo por contenido clinico" no es demostrable ante nadie; "se derivo
    por «flemon» el 15/08 a las 21:04" si.
    """
    v = evaluar("tengo un flemon")
    assert v is not None
    assert "flemon" in v.motivo
    assert v.nivel.value in v.motivo


def test_el_mensaje_de_urgencia_manda_llamar() -> None:
    v = evaluar("no puedo respirar bien")
    assert v is not None
    assert mensaje_para(v) == MENSAJE_URGENCIA
    assert "112" in MENSAJE_URGENCIA


def test_el_mensaje_clinico_no_opina() -> None:
    v = evaluar("me duele la muela")
    assert v is not None
    assert mensaje_para(v) == MENSAJE_CLINICO
