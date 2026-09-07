"""Guardarrail clinico: el filtro que corre ANTES del modelo.

Por que existe
--------------
El sistema atiende mensajes de pacientes de una clinica. Un paciente escribe
"me duele mucho desde ayer, que hago?" y un modelo de lenguaje, por bien
instruido que este, puede contestar algo. Contestar algo ahi es opinar sobre el
sintoma de un paciente sin ser profesional sanitario.

Este modulo evita que ese mensaje llegue siquiera al modelo.

Por que una lista de palabras y no un clasificador
--------------------------------------------------
Es deliberado, no una simplificacion pendiente. Un clasificador con LLM seria
mas elegante y menos fiable: no se puede auditar, cambia de opinion entre
versiones del modelo y falla justo cuando el modelo esta caido o degradado --
que es cuando mas falta hace. Aqui un falso negativo es el sistema opinando
sobre el sintoma de un paciente, y eso no se puede dejar en manos de otra
inferencia.

La asimetria manda en cada decision de este fichero:

- **Falso positivo** (deriva algo que no era clinico): una persona del equipo
  lee un mensaje que podria haber contestado el bot. Coste: un minuto.
- **Falso negativo** (deja pasar algo clinico): el sistema responde a un
  sintoma. Coste: la clinica, su colegio profesional y potencialmente un
  paciente.

Ante la duda, se deriva. Siempre.

Base normativa
--------------
- RD 1907/1996: prohibe atribuir efectos o cualidades sanitarias en publicidad
  y comunicaciones. Un bot diciendo "eso no parece grave" entra de lleno.
- Reglamento (UE) 2024/1689 (Reglamento de IA), art. 50: el paciente tiene que
  saber que habla con un sistema automatico. Eso se cumple en el primer
  mensaje, no aqui.
- El compromiso publicado en agendia.es/legal/datos-de-paciente: "ante
  cualquier consulta clinica el sistema deriva a un profesional de inmediato.
  No improvisa." Este fichero es esa frase, ejecutable.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class NivelClinico(str, Enum):
    """Que se ha detectado. Cambia el mensaje que recibe el paciente."""

    urgencia = "urgencia"
    """Posible urgencia. Se deriva Y se le dice que llame, sin esperar."""

    clinico = "clinico"
    """Consulta clinica normal. Se deriva y se le dice que le contestaran."""


@dataclass(frozen=True)
class Veredicto:
    nivel: NivelClinico
    termino: str
    """El termino exacto que disparo el filtro. Va al registro de auditoria:
    sin el, "se derivo por contenido clinico" no es demostrable."""

    continuacion: bool = False
    """El termino no esta en ESTE mensaje, sino en un turno anterior.

    Distinguirlo importa para la auditoria: "se derivo por contenido clinico"
    y "se derivo porque el paciente seguia hablando de lo mismo" son dos
    decisiones distintas y el equipo tiene que poder separarlas."""

    @property
    def motivo(self) -> str:
        sufijo = " · continuacion de la conversacion" if self.continuacion else ""
        return f"Guardarrail clinico ({self.nivel.value}): «{self.termino}»{sufijo}"


# ---------------------------------------------------------------------------
# Las listas
#
# Se comparan SIN acentos y en minusculas, asi que aqui van sin acentos. Las
# entradas con espacios se buscan como frase completa.
#
# Al añadir un termino, la pregunta no es "¿es esto clinico?" sino "¿que pasa
# si el bot contesta a un mensaje que lo contiene?". Si la respuesta incomoda,
# entra en la lista.
# ---------------------------------------------------------------------------

URGENCIA: tuple[str, ...] = (
    "urgencia", "urgencias", "emergencia",
    "no puedo respirar", "me falta el aire", "me cuesta respirar",
    "no para de sangrar", "sangro mucho", "hemorragia",
    "me he desmayado", "desmayo", "mareo fuerte",
    "dolor en el pecho",
    "flemon", "absceso", "pus",
    "fiebre alta",
    "se me ha hinchado la cara", "hinchazon en la cara", "cara hinchada",
    "no puedo tragar", "no puedo abrir la boca",
    "me he dado un golpe", "traumatismo", "se me ha partido un diente",
    "se me ha caido un diente",
)

CLINICO: tuple[str, ...] = (
    # Sintomas
    "dolor", "duele", "dolia", "molestia", "molesta mucho",
    "sangra", "sangrado", "sangre",
    "inflamado", "inflamada", "inflamacion", "hinchado", "hinchada", "hinchazon",
    "infeccion", "infectado", "supura",
    "fiebre", "sintoma", "sintomas",
    "sensible al frio", "sensible al calor", "sensibilidad",
    # Diagnostico y consejo
    "diagnostico", "diagnosticar", "es grave", "es normal que",
    "que me pasa", "que puede ser", "que hago",
    "me preocupa", "tengo miedo de que",
    # Medicacion
    "receta", "recetar", "recetado", "medicacion", "medicamento", "pastilla",
    "antibiotico", "amoxicilina", "ibuprofeno", "paracetamol", "nolotil",
    "puedo tomar", "que tomo", "efecto secundario", "dosis",
    # Antecedentes que condicionan el tratamiento
    "alergia", "alergico", "alergica",
    "embarazada", "embarazo", "lactancia",
    "anticoagulante", "sintrom", "diabetes", "diabetico", "diabetica",
    "hipertension", "tension alta", "marcapasos", "bifosfonatos",
    # Postoperatorio
    "postoperatorio", "despues de la extraccion", "me han quitado",
    "se me ha caido el empaste", "se me ha roto la funda", "punto de sutura",
)


def _normalizar(texto: str) -> str:
    """Minusculas, sin acentos y con los espacios colapsados.

    Sin esto, "Dolor" y "dolór" se escapan del filtro. La entrada llega de
    WhatsApp escrita a toda prisa: los acentos no se pueden dar por buenos.
    """
    sin_tildes = unicodedata.normalize("NFD", texto.lower())
    sin_tildes = "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sin_tildes).strip()


def _compilar(terminos: tuple[str, ...]) -> re.Pattern[str]:
    """Un patron por lista, con frontera de palabra.

    La frontera importa: sin ella "dolor" dispara dentro de "indolora" y
    "sangre" dentro de "sangrentina". El filtro perderia credibilidad ante el
    equipo de la clinica, que es quien recibe cada derivacion.
    """
    partes = sorted((re.escape(t) for t in terminos), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(partes) + r")(?![a-z0-9])")


_RE_URGENCIA = _compilar(URGENCIA)
_RE_CLINICO = _compilar(CLINICO)


def evaluar(texto: str | None) -> Veredicto | None:
    """¿Este mensaje debe saltarse el modelo y derivarse?

    Devuelve None si el mensaje puede seguir su curso normal.

    Urgencia se comprueba primero: un mensaje puede contener las dos cosas
    ("me duele y se me ha hinchado la cara") y la respuesta al paciente tiene
    que ser la mas conservadora de las dos, no la primera que coincida.
    """
    if not texto or not texto.strip():
        return None

    normalizado = _normalizar(texto)

    if (m := _RE_URGENCIA.search(normalizado)) is not None:
        return Veredicto(nivel=NivelClinico.urgencia, termino=m.group(1))

    if (m := _RE_CLINICO.search(normalizado)) is not None:
        return Veredicto(nivel=NivelClinico.clinico, termino=m.group(1))

    return None


# ---------------------------------------------------------------------------
# El guardarrail a lo largo de la conversacion
#
# `evaluar()` mira una cadena. El modelo, en cambio, recibe el historial
# entero. Eso abria un hueco que ningun eval de un turno podia ver:
#
#   T1 «me duele mucho la muela desde ayer»  -> deriva, el modelo no lo ve
#   T2 «y que me recomiendas para eso»       -> ni un termino del diccionario,
#                                               pasa al modelo... con el
#                                               sintoma dentro del historial
#
# No hace falta mala fe: es como habla cualquiera. El paciente cree que sigue
# la misma conversacion, y tiene razon.
#
# La correccion NO es evaluar el historial entero: entonces cualquier
# conversacion que roce lo clinico una vez queda derivada para siempre y el
# paciente que solo queria pedir cita se queda sin poder pedirla. Se exigen
# las dos cosas a la vez:
#
#   1. el turno anterior del agente fue una derivacion (el texto fijo esta ahi
#      y es un marcador fiable: no lo escribe el modelo)
#   2. este mensaje se REFIERE al anterior en vez de abrir un tema nuevo
# ---------------------------------------------------------------------------

_ANAFORAS = (
    "eso",
    "esto",
    "aquello",
    "ello",
    "lo mismo",
    "lo de antes",
    "lo que te dije",
    "lo que le dije",
    "lo que te conte",
    "al respecto",
)

_RE_ANAFORA = _compilar(_ANAFORAS)


def _es_referencial(texto: str) -> bool:
    """¿Este mensaje habla de lo anterior, o abre un tema nuevo?

    Deliberadamente estrecho. Un falso negativo deja pasar un mensaje al modelo
    —malo—, pero un falso positivo atrapa al paciente en modo clinico y le
    impide pedir cita —peor, y ademas invisible para el equipo—.
    """
    return _RE_ANAFORA.search(_normalizar(texto)) is not None


def _derivacion_previa(historial) -> bool:
    """El ultimo turno del agente fue uno de los dos textos fijos.

    Se mira el texto y no una bandera de estado a proposito: estos dos mensajes
    viven en el codigo, no en el prompt editable, asi que no puede ponerlos ahi
    ni el modelo ni nadie desde el panel.
    """
    for m in reversed(list(historial or [])):
        if getattr(m, "role", None) == "assistant":
            return (getattr(m, "content", "") or "") in (MENSAJE_CLINICO, MENSAJE_URGENCIA)
    return False


def _veredicto_previo(historial) -> Veredicto | None:
    """El veredicto del ultimo turno del paciente que disparo el filtro."""
    for m in reversed(list(historial or [])):
        if getattr(m, "role", None) != "user":
            continue
        if (v := evaluar(getattr(m, "content", ""))) is not None:
            return v
    return None


def evaluar_conversacion(historial, texto: str | None) -> Veredicto | None:
    """`evaluar()`, pero sin perder de vista los turnos anteriores.

    Es la que usa el orquestador. `evaluar()` se mantiene publica y sin cambios
    porque hay casos del arnes que la comprueban en aislamiento.
    """
    actual = evaluar(texto)
    previo = _veredicto_previo(historial) if _derivacion_previa(historial) else None

    # La gravedad no se degrada al cambiar de turno: si el turno anterior era
    # una urgencia, el paciente tiene que seguir leyendo el 112 justo en el
    # mensaje donde mas falta hace.
    if actual is not None:
        if (
            previo is not None
            and previo.nivel is NivelClinico.urgencia
            and actual.nivel is not NivelClinico.urgencia
        ):
            return Veredicto(nivel=previo.nivel, termino=previo.termino, continuacion=True)
        return actual

    if previo is not None and _es_referencial(texto or ""):
        return Veredicto(nivel=previo.nivel, termino=previo.termino, continuacion=True)

    return None


# ---------------------------------------------------------------------------
# Lo que lee el paciente
#
# Hardcodeado aqui y no en el prompt del agente, a proposito: si dependiera del
# prompt, bastaria con que alguien editara mal el prompt desde el panel para
# que el guardarrail dejara de decir lo que tiene que decir. El filtro y su
# respuesta viajan juntos.
# ---------------------------------------------------------------------------

# Estos dos textos son, muchas veces, LO PRIMERO que lee el paciente: el
# guardarrail corre antes del modelo, asi que aqui no hay ningun LLM al que
# pedirle que se presente. Por eso la frase de transparencia va escrita en el
# propio texto y no en el prompt.
#
# Reglamento (UE) 2024/1689 (Reglamento de IA), art. 50, aplicable desde el 2 de
# agosto de 2026: la persona tiene que saber que interactua con un sistema de IA,
# y saberlo como muy tarde en la primera interaccion. Un mensaje que dice "ya les
# he avisado y te escriben" sin decir quien lo escribe da a entender justo lo
# contrario.
_SOY_UN_SISTEMA = "Soy el asistente virtual del centro, no una persona. "

MENSAJE_URGENCIA = (
    _SOY_UN_SISTEMA
    + "Por lo que me cuentas, esto lo tiene que ver una persona del equipo ahora mismo. "
    "Te paso con la clinica y te contestan en cuanto puedan. "
    "Si es urgente y no pueden atenderte, llama al 112 o acude a un servicio de urgencias."
)

MENSAJE_CLINICO = (
    _SOY_UN_SISTEMA
    + "Esto prefiero que te lo conteste alguien del equipo, que para temas de salud "
    "no quiero darte yo una respuesta. Ya les he avisado y te escriben en cuanto puedan."
)


def mensaje_para(veredicto: Veredicto) -> str:
    return MENSAJE_URGENCIA if veredicto.nivel is NivelClinico.urgencia else MENSAJE_CLINICO
