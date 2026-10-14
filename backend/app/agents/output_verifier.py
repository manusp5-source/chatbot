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

# Un número a la española: «1.200», «1 200», «1.200,50», «49,90», «1200».
_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d{1,3}(?:[  ]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?"
_MONEDA_DETRAS = r"\s*(?:€|euros?\b|eur\b)"
_IMPORTE_DETRAS = re.compile(rf"(?P<n>{_NUM}){_MONEDA_DETRAS}", re.IGNORECASE)
# Delante solo «€» y «EUR»: con «euros» delante, «60 euros 30 minutos» leería un 30 como precio.
_IMPORTE_DELANTE = re.compile(rf"(?:€|\bEUR\b)\s*(?P<n>{_NUM})", re.IGNORECASE)
# «100-200 €», «de 100 a 200 €», «entre 100 y 200 euros»: el límite inferior también es un precio.
_RANGO = re.compile(
    rf"(?P<a>{_NUM})\s*(?:-|–|—|\ba\b|\by\b)\s*(?P<b>{_NUM}){_MONEDA_DETRAS}", re.IGNORECASE
)
_CUALQUIER_NUMERO = re.compile(_NUM)
_MILES_PUNTO = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?")
_MILES_ESPACIO = re.compile(r"\d{1,3}(?:[  ]\d{3})+(?:,\d+)?")


def _normalizar(token: str) -> Decimal | None:
    """'1.200,50' → 1200.50 · '1 200' → 1200 · '49,90' → 49.90 · '1200' → 1200."""
    if _MILES_PUNTO.fullmatch(token):
        token = token.replace(".", "").replace(",", ".")
    elif _MILES_ESPACIO.fullmatch(token):
        token = token.replace(" ", "").replace(" ", "").replace(",", ".")
    else:
        token = token.replace(",", ".")
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def extraer_importes(texto: str) -> set[Decimal]:
    """Importes en euros que aparecen en el texto. Un número sin € no cuenta."""
    encontrados: set[Decimal] = set()
    for patron, grupos in (
        (_IMPORTE_DETRAS, ("n",)),
        (_IMPORTE_DELANTE, ("n",)),
        (_RANGO, ("a", "b")),
    ):
        for m in patron.finditer(texto or ""):
            for grupo in grupos:
                valor = _normalizar(m.group(grupo))
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
    """Importes de la respuesta que no aparecen en la evidencia (cualquier número cuenta)."""
    return importes_no_respaldados_en(respuesta, con_moneda=evidencia, numeros_sueltos=evidencia)


def importes_no_respaldados_en(
    respuesta: str, *, con_moneda: str, numeros_sueltos: str
) -> set[Decimal]:
    """Importes de la respuesta sin respaldo, con dos grados de evidencia.

    `con_moneda`: textos donde solo vale un importe escrito como importe (prompt del negocio,
    respuestas ya dadas, instrucciones del equipo). Un número desnudo ahí no respalda nada:
    el prompt lleva horarios y teléfonos, y la fecha antepone el día del mes, así que si
    contara cualquier número, cualquier precio entre 1 y 31 € quedaría respaldado.

    `numeros_sueltos`: contenido de la base de conocimiento, donde una tabla trae «Limpieza,60»
    sin símbolo. Ahí sí vale un número desnudo (límite asumido: un número coincidente en un
    documento largo puede respaldar un importe inventado).
    """
    importes = extraer_importes(respuesta)
    if not importes:
        return set()
    respaldo = extraer_importes(con_moneda) | _numeros(numeros_sueltos)
    return importes - respaldo
