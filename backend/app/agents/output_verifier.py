"""Segunda barrera contra el precio inventado: lo que se cobra tiene que estar escrito.

Por qué existe: la única defensa contra que el agente invente un importe era el prompt
(«si no está en la KB, no existe»). El caso F2 del arnés demostró que no basta: preguntado
por un implante de circonio (ausente de la base de conocimiento), el agente contestó con el
precio de la porcelana y un «sí» que confirmaba una opción que no existe. Un prompt es una
petición; esto es una comprobación.

Qué hace: extrae los importes en euros de la respuesta y mira si cada uno aparece en el
contexto que el modelo tenía delante (prompt, historial, mensaje del cliente y resultados de
las tools de este turno). Es determinista y no llama a ningún modelo: no se puede
convencer, no cambia entre versiones y funciona con el proveedor caído.

Límites asumidos, a propósito:
  - Solo importes en euros. Horarios y fechas se calculan a partir de la fecha y de la
    agenda; comprobarlos daría falsos positivos.
  - Un importe derivado (suma, descuento) no estará literalmente en el contexto y se tratará
    como no respaldado. Es el lado seguro: un falso positivo cuesta que una persona
    confirme un precio; un falso negativo, que el cliente tome por bueno uno inventado.
"""
import re
from decimal import Decimal, InvalidOperation

TEXTO_IMPORTE_NO_RESPALDADO = (
    "Ese importe no lo tengo confirmado, y prefiero no darte un dato que pueda no ser "
    "correcto. ¿Quieres que te pase con una persona del equipo para que te lo confirme?"
)

# Voz: no se puede pasar la llamada, así que la salida es tomar datos y devolverla.
TEXTO_IMPORTE_NO_RESPALDADO_SIN_DERIVAR = (
    "Ese importe no lo tengo confirmado, y prefiero no darte un dato que pueda no ser "
    "correcto. Si quieres, tomo tu nombre y tu teléfono y el equipo te devuelve la llamada "
    "para confirmártelo."
)

# Un número a la española: «1.200», «1.200,50», «49,90», «1200».
_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?"
_IMPORTE_DETRAS = re.compile(rf"(?P<n>{_NUM})\s*(?:€|euros?\b|eur\b)", re.IGNORECASE)
_IMPORTE_DELANTE = re.compile(rf"€\s*(?P<n>{_NUM})")
_CUALQUIER_NUMERO = re.compile(_NUM)
_MILES = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?")


def _normalizar(token: str) -> Decimal | None:
    """'1.200,50' → 1200.50 · '49,90' → 49.90 · '1200' → 1200. None si no es un número."""
    if _MILES.fullmatch(token):
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(",", ".")
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def extraer_importes(texto: str) -> set[Decimal]:
    """Importes en euros que aparecen en el texto. Un número sin € no cuenta."""
    encontrados: set[Decimal] = set()
    for patron in (_IMPORTE_DETRAS, _IMPORTE_DELANTE):
        for m in patron.finditer(texto or ""):
            valor = _normalizar(m.group("n"))
            if valor is not None:
                encontrados.add(valor)
    return encontrados


def _numeros(texto: str) -> set[Decimal]:
    """Todos los números del texto, con o sin símbolo: el respaldo no exige el €."""
    valores: set[Decimal] = set()
    for m in _CUALQUIER_NUMERO.finditer(texto or ""):
        valor = _normalizar(m.group(0))
        if valor is not None:
            valores.add(valor)
    return valores


def importes_no_respaldados(respuesta: str, evidencia: str) -> set[Decimal]:
    """Importes de la respuesta que no aparecen en la evidencia."""
    importes = extraer_importes(respuesta)
    if not importes:
        return set()
    return importes - _numeros(evidencia)
