# 1 · Marco legal sanitario — por qué los límites no son opcionales

Los dos primeros límites del agente parecen decisiones comerciales de `OFERTA.md`. No lo
son: los dos tienen norma detrás. Esta ficha existe para que nadie los "flexibilice" en una
instalación pensando que es una preferencia.

## Historia clínica — Ley 41/2002, arts. 14-19

**Qué dice.** El acceso a la historia clínica está reservado a los profesionales
asistenciales que intervienen en el diagnóstico o el tratamiento del paciente. Hay accesos
adicionales tasados (gestión, inspección, investigación) y todos con condiciones.

**Qué implica aquí.** Un agente automático no es profesional asistencial. No puede acceder,
y **ninguna instalación puede "activárselo"**: no es una casilla del panel, es que no
procede. La AEPD lleva años sancionando accesos no autorizados a historias clínicas por
parte de personal del propio centro; un sistema automático leyéndolas sería peor.

**Y una segunda derivada que se olvida:** tampoco puede **fingir** que la tiene. "Déjame que
mire tu ficha" o "según tu historial" son mentiras que generan una expectativa concreta en
alguien que está preguntando por su salud.

→ Pieza 01 regla 2 · pieza 05 prohibición 1 · eval `F6`.

## Publicidad sanitaria — RD 1907/1996

**Qué dice.** Prohíbe atribuir efectos o cualidades sanitarias en la publicidad y las
comunicaciones comerciales de productos, materiales y servicios con pretendida finalidad
sanitaria.

**Qué implica aquí.** Un bot que contesta "eso no parece grave", "con un enjuague se te
pasa" o "eso suele ser normal después de una extracción" entra de lleno. No hace falta que
se equivoque: basta con que lo diga.

**Por qué el filtro es una lista de palabras y no un clasificador.** Es deliberado. Un
clasificador con LLM sería más elegante y menos fiable: no se audita, cambia de opinión
entre versiones del modelo y falla justo cuando el modelo está caído o degradado, que es
cuando más falta hace. La asimetría manda: un falso positivo cuesta un minuto de una persona
del equipo; un falso negativo cuesta la clínica, su colegio profesional y potencialmente un
paciente.

→ Pieza 01 regla 1 · `guardarrail_clinico.py` · evals `L1`, `L2`, `L8`.

## Lo que esto NO cubre

Ninguna de las dos normas dice nada sobre agendar, informar de precios o captar contactos.
Ahí el agente es libre y debe ser útil. Un arnés que derive por si acaso ante cualquier cosa
convierte el producto en un contestador automático caro, y el equipo deja de mirar las
derivaciones — que es el fallo que de verdad rompe el guardarraíl.
