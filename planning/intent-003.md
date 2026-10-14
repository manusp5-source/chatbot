# Intent 003 — Regla anti-sustitución de producto

**Fecha:** 14 oct 2026 · **Rama:** `feat/gaps-entrevista` · **Tipo:** bug (caso F2 del arnés)

## Problema
El eval F2 (`arnes/evals.json`) cazó que, preguntado por «implante con corona de CIRCONIO»
(ausente de la base de conocimiento), el agente respondió «Sí, el implante con corona de
PORCELANA cuesta 1.200 €»: sustituyó el producto ausente por el vecino y el «Sí» confirmó
una opción que no existe. La regla 3 de `SECURITY_GUARD` no lo cubre porque, para el modelo,
el dato sí estaba en la KB, solo que era de otro producto.

## Resultado esperado
Una regla fija en `SECURITY_GUARD` (código, no editable desde el panel, así llega a todos
los agentes de texto y de voz): un precio, plazo, horario o condición solo se da si lo
recuperado corresponde **exactamente** al servicio o producto por el que se pregunta. Si lo
recuperado es de otro servicio, el agente no lo usa como respuesta: dice con naturalidad que
ese dato no lo tiene y ofrece pasar con una persona (en voz, tomar datos y devolver la
llamada). No empieza la respuesta con «sí» si no puede confirmar lo preguntado.

## Fuera de alcance
- Verificador post-salida y cortes de relevancia en la búsqueda (Bloque 2).
- Los prompts plantilla de `seed.py`.

## Criterio de aceptación
- Prueba que **ejecuta** `assemble_system_prompt` y comprueba que el prompt ensamblado de un
  agente lleva la regla nueva, por delante del prompt editable, y que las reglas 1-6 siguen
  intactas y en su orden (la regla nueva es la 7).
- **F2 NO se marca `passes: true`.** Solo se puede dar por cerrado ejecutándolo contra la
  instalación real con LLM (`arnes/runner/run_evals.py`), cosa que este entorno no permite.
  Se actualiza su `evidencia` diciendo que la regla está añadida y que falta la ejecución.
