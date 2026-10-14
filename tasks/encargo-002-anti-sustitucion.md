# Encargo 002 — Regla anti-sustitución de producto

**Para:** Codex · **Revisa:** Claude · **Rama:** `feat/gaps-entrevista` · **Intent:** `planning/intent-003.md` (léelo).
Codex no ve más contexto que este. Comentarios y nombres de test en **español**, explicando el
porqué. **No hagas commit.** Si no puedes ejecutar los tests en tu sandbox, no pasa nada:
dilo; Claude los ejecuta. No intentes crear venvs ni instalar nada.

## Alcance de ficheros (fuera de esta lista = hallazgo)
- `backend/app/services/runtime_config.py` — solo la constante `SECURITY_GUARD` (líneas ~63-91).
- `backend/tests/test_security_guard_anti_sustitucion.py` — NUEVO.
- `arnes/evals.json` — solo el campo `evidencia` del caso F2 (y su duplicado en el bloque de
  estado si existe). **`passes` de F2 sigue en `false`.**
Nada más.

## Qué hacer
1. Añade la regla **7** a `SECURITY_GUARD`, sin tocar las 1-6 ni su texto, antes de
   `[FIN DE LAS REGLAS DE SEGURIDAD]`. Mismo tono y estilo que las demás (castellano, frases
   completas). Contenido: un precio, plazo, horario o condición solo se afirma si el dato
   recuperado corresponde EXACTAMENTE al servicio o producto por el que se pregunta; si lo
   recuperado es de otro servicio parecido, no se usa como respuesta: se dice con naturalidad
   que ese dato no se tiene y se ofrece pasar con una persona del equipo (o, si no se puede
   pasar la llamada, tomar nombre y teléfono para que devuelvan la llamada); no se empieza
   con «sí» una respuesta que no se puede confirmar; se puede mencionar la alternativa
   cercana solo si se aclara que es otra cosa.
2. Prueba nueva (rojo primero, si puedes ejecutarla): llama a `assemble_system_prompt`
   (`runtime_config.py`, ~línea 167) con un prompt de agente cualquiera y comprueba, sobre el
   texto ensamblado, que: contiene la regla 7 con las ideas clave (usa varias comprobaciones
   de subcadena específicas y estables, no una frase entera); aparece ANTES del prompt del
   agente; las reglas 1 a 6 siguen presentes y en orden. Mira cómo se llama y qué devuelve
   esa función antes de escribir la prueba.
3. `arnes/evals.json`, caso F2: actualiza `evidencia` añadiendo al final que el 14 oct 2026
   se añadió la regla 7 de `SECURITY_GUARD` (anti-sustitución) y que **falta ejecutar F2
   contra la instalación real con LLM** para cerrarlo. No cambies `passes`.

## Informe final
Ficheros tocados, el texto exacto de la regla 7, la prueba, y lo que no pudiste ejecutar.
