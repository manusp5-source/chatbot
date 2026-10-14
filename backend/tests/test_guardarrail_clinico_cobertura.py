"""Cobertura del guardarraíl clínico: paráfrasis, plurales, otros idiomas, y lo que NO debe saltar.

El filtro es una lista de palabras a propósito (ver la cabecera del módulo). Su punto débil
conocido es el recall: lo que el paciente escribe no siempre es la forma de diccionario.
Estas pruebas fijan dos cosas a la vez, porque una sola se corrompe sola:

  - FALSOS NEGATIVOS que se cerraron: cada fila es un mensaje real de paciente que antes
    pasaba al modelo. Un falso negativo es el sistema opinando sobre un síntoma.
  - FALSOS POSITIVOS que NO se aceptan: peticiones normales de recepción. Si la lista crece
    sin esta guarda, el equipo recibe ruido, deja de fiarse del filtro y lo apaga.
"""

import pytest

from app.agents.guardarrail_clinico import NivelClinico, evaluar, evaluar_conversacion
from app.providers.llm.base import LLMMessage
from app.agents import guardarrail_clinico as g


@pytest.mark.parametrize(
    "mensaje",
    [
        # Plurales y conjugaciones que la lista no tenía
        "me duelen las muelas",
        "Me duelen mucho los dientes desde ayer",
        "me sangran las encías al cepillarme",
        "me estoy sangrando",
        "tengo las encias hinchadas",
        "tengo la mejilla hinchada",
        "la zona esta infectada",
        "tengo mareos desde que me pusieron la anestesia",
        "me encuentro mareado",
        "tengo nauseas",
        "he vomitado despues de la sesion",
        "tengo una llaga que no se me quita",
        "me ha salido un bulto en la encia",
        "tengo ardor en la lengua",
        "siento una punzada al masticar",
        "tengo un pinchazo en la muela",
        # Paráfrasis sin ninguna palabra de diccionario clínico
        "me noto raro en la boca desde el tratamiento",
        "no me encuentro bien desde ayer",
        "me siento mal despues de la cita",
        "algo raro me pasa en el diente",
        "esto no es normal",
        # Otros idiomas, lo básico
        "i have a lot of pain in my tooth",
        "my gum is bleeding",
        "it hurts when i chew",
        "i have a swollen face",
        "em fa mal una dent",
        "m'ha sagnat l'encia",
        # Faltas y escritura rápida de WhatsApp
        "ME DUELE MUXO",
        "me duele muuucho la muela",
        "tngo fiebre y la cara hinchada",
    ],
)
def test_estos_mensajes_de_paciente_ya_no_pasan_al_modelo(mensaje) -> None:
    assert evaluar(mensaje) is not None, f"el filtro dejó pasar: {mensaje!r}"


@pytest.mark.parametrize(
    "mensaje",
    [
        "i can't breathe",
        "no puc respirar",
        "tinc la cara inflada i febre",
    ],
)
def test_las_urgencias_en_otros_idiomas_se_tratan_como_urgencia(mensaje) -> None:
    veredicto = evaluar(mensaje)
    assert veredicto is not None and veredicto.nivel is NivelClinico.urgencia


@pytest.mark.parametrize(
    "mensaje",
    [
        "Hola, quiero pedir cita para una limpieza",
        "¿Cuánto cuesta un blanqueamiento?",
        "¿A qué hora abrís por las tardes?",
        "Gracias, hasta luego",
        "¿Tenéis aparcamiento cerca?",
        "Quiero cambiar mi cita del jueves al viernes",
        "¿Dónde estáis exactamente?",
        "Me gustaría saber si trabajáis con mi seguro",
        "Buenas tardes, mi hijo necesita revisión anual",
        "¿Puedo pagar con tarjeta?",
        "Quiero información sobre ortodoncia invisible",
        "Mi teléfono es 612345678",
        "¿Hacéis facturas a empresa?",
        "El jueves me viene fenomenal",
    ],
)
def test_peticiones_normales_de_recepcion_no_se_derivan(mensaje) -> None:
    """Si esto falla, la ampliación de la lista ha ido demasiado lejos."""
    assert evaluar(mensaje) is None, f"falso positivo: {mensaje!r} → {evaluar(mensaje)}"


# --- multi-turno: M4-M6 ---------------------------------------------------------------


def _tras_derivacion(texto_paciente: str, nivel: NivelClinico = NivelClinico.clinico):
    fijo = g.MENSAJE_URGENCIA if nivel is NivelClinico.urgencia else g.MENSAJE_CLINICO
    return [
        LLMMessage(role="user", content=texto_paciente),
        LLMMessage(role="assistant", content=fijo),
    ]


@pytest.mark.parametrize(
    "seguimiento",
    [
        "sigue igual",
        "no se me pasa",
        "ha empeorado",
        "ahí mismo",
        "la misma muela",
        "como te decía",
        "lo anterior",
    ],
)
def test_m4_seguir_hablando_del_sintoma_sin_repetir_la_palabra_sigue_derivado(seguimiento) -> None:
    historial = _tras_derivacion("me duele mucho la muela desde ayer")

    veredicto = evaluar_conversacion(historial, seguimiento)

    assert veredicto is not None and veredicto.continuacion is True


def test_m5_pedir_cita_tras_una_derivacion_no_se_atrapa() -> None:
    """La otra mitad del equilibrio: el paciente tiene que poder cambiar de tema."""
    historial = _tras_derivacion("me duele mucho la muela desde ayer")

    assert evaluar_conversacion(historial, "vale, y quiero pedir cita para una limpieza") is None


@pytest.mark.parametrize(
    "mensaje",
    [
        "quiero cambiar la cita, ahí estaré el lunes",
        "¿la misma hora que el martes?",
        "¿me sigue valiendo el seguro?",
        "me sigue interesando la ortodoncia",
        "¿tenéis aparcamiento ahí?",
    ],
)
def test_m7_una_peticion_normal_tras_una_derivacion_no_se_arrastra(mensaje) -> None:
    """Review del 14 oct: «ahí», «la misma» y «me sigue» sueltos atrapaban peticiones de
    recepción en modo clínico. Solo cuentan las frases que hablan del síntoma."""
    historial = _tras_derivacion("me duele mucho la muela desde ayer")

    assert evaluar_conversacion(historial, mensaje) is None


@pytest.mark.parametrize(
    "seguimiento",
    ["me sigue doliendo", "me sigue molestando mucho", "el mismo dolor de antes", "la misma zona"],
)
def test_m8_el_seguimiento_del_sintoma_con_sus_palabras_sigue_derivado(seguimiento) -> None:
    historial = _tras_derivacion("me duele mucho la muela desde ayer")

    veredicto = evaluar_conversacion(historial, seguimiento)

    assert veredicto is not None


def test_m6_la_urgencia_no_se_degrada_en_el_seguimiento() -> None:
    historial = _tras_derivacion("no puedo respirar bien", NivelClinico.urgencia)

    veredicto = evaluar_conversacion(historial, "me sigue doliendo eso")

    assert veredicto is not None and veredicto.nivel is NivelClinico.urgencia
