# Intent 004 — Verificador determinista de importes en la salida del agente

**Fecha:** 14 oct 2026 · **Rama:** `feat/gaps-entrevista` · **Tipo:** gap (alucinación)

## Problema
La barrera contra inventar precios es solo el prompt («si no está en la KB, no existe»). F2
demostró que no basta: el agente dio el precio de un producto vecino. No hay ninguna
comprobación posterior a la salida del modelo.

## Resultado esperado
Antes de devolver la respuesta final, `run_agent` extrae los **importes en euros** que el
modelo ha escrito y comprueba que cada uno aparece en algún sitio del contexto de la
conversación (prompt de sistema, historial, mensaje del cliente, resultados de las tools de
este turno). Si algún importe no está respaldado, la respuesta se sustituye por un texto
fijo escrito en código («no tengo confirmado ese importe…») y se deja una decisión
`salida_importe_no_respaldado` en las trazas. Fail-closed a propósito: el coste de un falso
positivo es una consulta que un humano confirma; el de un falso negativo es un precio
inventado que el cliente toma por bueno. Si el agente no puede derivar (voz), el texto fijo
ofrece tomar datos y devolver la llamada.

Además, `consultar_kb` sin resultados lo dice de forma explícita en su respuesta
(`sin_resultados` + instrucción), para no depender de que el modelo deduzca de una lista
vacía que debe decir «no lo tengo».

## Límites asumidos (no se esconden)
- Solo importes en euros. Horarios, fechas y plazos quedan fuera: se calculan a partir de la
  fecha y de la agenda y producirían falsos positivos.
- Un importe derivado por el modelo (suma, descuento) no estará literalmente en el contexto y
  se tratará como no respaldado. Es el lado seguro.
- No se cambia el umbral de similitud (0,3) ni la función SQL: ajustarlos sin una KB real y
  evals de recall sería tocar a ciegas. Queda anotado como trabajo con datos reales.

## Fuera de alcance
Cambios de migración, del prompt plantilla, y del guardarraíl clínico.

## Criterio de aceptación
Pruebas que ejecutan el comportamiento, en rojo antes y en verde después: extracción de
importes en formatos españoles; respaldo contra la evidencia; `run_agent` sustituye una
respuesta con precio inventado, deja pasar una con precio respaldado por la tool, deja
pasar una sin importes, y cambia el texto según pueda derivar o no.
