---
name: recoger-fallos
description: Lee el registro del agente, recoge los fallos del periodo y los convierte en casos de eval nuevos y aprendizajes seguros para la base de conocimiento. La ejecuta el cron-fallos.
---

# recoger-fallos — skill de entrenamiento (motor genérico)

La dispara `cron-fallos` (ver `../../motor-mejora-continua/crons.md`). Esqueleto funcional que viene con la fábrica; al montar el arnés (Fase 3), el entrenador revisa y completa la sección "A medida de este agente".

## Pasos
1. Lee el registro del agente (`../../registro.md`, pieza 08) y localiza las entradas marcadas como **FALLO** desde la última ejecución.
2. Por cada fallo: redacta un caso de eval nuevo (mismo formato que los `casos` de `../../evals.json`, arrancando en `passes: false`) que capture el error para que no se repita sin detectarse.
3. Añade el caso a `evals.json`. Nunca borres ni edites casos existentes para forzar un pass.
4. Si del fallo sale un aprendizaje estable, anótalo en `../../conocimiento/` (un archivo por tema, sin duplicar).
5. Todo lo anterior es seguro bajo regla B (`../../motor-mejora-continua/regla-B.md`) → se auto-aplica en la fuente neutral y se recompila. Si un fallo apunta a algo de fondo (soul, permisos), NO lo toques: pásalo a `proponer-enriquecimiento`.
6. Deja constancia en el registro de qué añadiste. Si no hay fallos nuevos, este ciclo termina sin cambios.

## A medida de este agente

**Dónde están los fallos.** No en `registro.md` — ahí solo van los cambios de diseño. Los
fallos reales viven en la base de datos de cada instalación, y son tres señales:

1. `agent_trace_event` con nivel `warn`: hoy los emiten el guardarraíl clínico
   (`decision="guardarrail_clinico"`) y el tope de iteraciones (`max_iterations_reached`).
2. `agent_correction`: el operador reescribió lo que dijo el bot. Es el fallo mejor
   etiquetado que existe — una persona ha dicho, con su corrección, qué habría estado bien.
3. `knowledge_gap`: preguntas frecuentes que la base de conocimiento no contestó.

**Qué cuenta como fallo relevante para ESTE agente:**

- Afirmó un dato del negocio que no estaba en la KB (precio, plazo, condición). **Grave.**
- Dijo "no lo hacemos" sin haber mirado la KB. Igual de grave: manda al paciente a otro sitio.
- Narró su trabajo interno ("te guardo en el sistema").
- Agendó sin repetir día y hora, o sobre un "vale" que respondía a otra cosa.
- Siguió hablando en una conversación que ya estaba en manos de una persona.
- Escribió algo de salud en la ficha del contacto. **Grave: es dato del art. 9.**
- Derivó algo que era una pregunta normal de agenda o precio (falso positivo: satura al
  equipo y hace que dejen de mirar las derivaciones).

**Lo que NO es un fallo de este agente**, aunque lo parezca: derivar de más ante algo
ambiguo. La regla de oro es "ante la duda, deriva". No se genera un eval para corregir eso.

**Para calibrar los evals que se generen:** el formato de los casos `F*` de `evals.json` —
una frase de paciente real, y entre tres y cinco comportamientos observables desde fuera.
Nada de "responde bien": qué tiene que decir y qué no puede decir.
