# Pieza 11 · Skills

> Compila a: **Skills** del runtime (la carpeta de skills de la infraestructura que sea). Incluye las skills de ejecución Y las de entrenamiento continuo (estas últimas: motor genérico + sensores a medida, ver `../skills-entrenamiento/`).

## Qué es
El arnés empaquetado. Cuando una tarea se repite mucho y siempre igual, se mete en una skill: una carpeta con sus instrucciones, sus herramientas y sus pasos, lista para reutilizar mil veces. Carga progresivamente (solo se lee cuando la tarea la pide).

## Respuesta

### Dónde caen las skills en este runtime

**No hay carpeta de skills.** El destino no es Claude Code ni Hermes: es una aplicación cuyo
agente se configura con un prompt en base de datos y una lista blanca de herramientas. Así
que las skills de ejecución **se compilan como procedimientos numerados dentro del soul**, y
la carga progresiva la da el propio diseño: cada procedimiento es corto y el modelo solo
"entra" en el que la conversación pide.

Es la receta C de `INSTALAME.md` — método general — y no es una degradación: un artefacto sin
sitio sería un agujero, pero este lo tiene, solo que dentro del soul.

### Las cuatro skills de ejecución

**`responder-con-la-kb`** — *cuando el paciente pregunta cualquier dato del centro.*
`consultar_kb` con la pregunta tal cual → si hay resultado, contestar **solo** con eso, en
dos o tres frases → si no hay resultado, decir que no se sabe y ofrecer pasar con una
persona. Nunca se completa un resultado parcial con lo que "suele ser".
Herramientas: `consultar_kb`, `schedule_config`, `derivar_humano`.

**`captar-contacto`** — *cuando hay interés real: pregunta por un servicio concreto, pide
presupuesto, quiere reservar o da sus datos.*
`buscar_contacto` primero → y después `crear_actualizar_contacto` **siempre**, con lo que
acaben de dar, exista ya la ficha o no → **sin contarlo**. Que la ficha exista no significa
que esté rellena: el canal la crea vacía al recibir el primer mensaje, así que "ya existe"
nunca es motivo para no guardar. Si dan un nombre y no se guarda, se ha perdido el lead que
justifica todo lo demás. El teléfono no se pide ni se acepta del mensaje: lo pone el sistema.
Herramientas: `buscar_contacto`, `crear_actualizar_contacto`.

**`agendar-cita`** — *cuando el paciente quiere hora.*
`consultar_disponibilidad` → ofrecer **una o dos** opciones concretas, nunca una lista →
el paciente elige → **repetir día y hora** → confirmación explícita → `agendar_cita` →
confirmar en una frase. Si no hay hueco que le sirva, ofrecer que le llamen.
Herramientas: `consultar_disponibilidad`, `agendar_cita`, `buscar_contacto`.

**`derivar-a-una-persona`** — *cuando la KB no llega, lo piden, hay compromiso de dinero o
plazo, el paciente está enfadado, o asoma cualquier tema clínico.*
`derivar_humano` con motivo claro y mensaje puente → **y callarse**: la conversación pasa a
una persona y el agente no vuelve a hablar en ella.
**Decirlo y hacerlo son el mismo acto.** Si la respuesta va a contener "te paso con el
equipo", "te lo confirman ellos" o cualquier promesa de que alguien retoma, `derivar_humano`
va en ese mismo turno. Anunciar un traspaso que no se ejecuta deja al paciente esperando a
alguien a quien nadie ha avisado, y eso es peor que no ofrecerlo.
Herramienta: `derivar_humano`. **En voz no existe**: ahí solo se puede decir al paciente que
hable con el centro.

### Las skills de entrenamiento continuo

Son del **arnés**, no del agente: viven en `arnes/skills-entrenamiento/`, las ejecutan los
crons y **no viajan al prompt**. Los cuatro esqueletos de la plantilla se adaptan a este
agente:

| Skill | Qué mira aquí |
|---|---|
| `recoger-fallos` | `agent_trace_event` con nivel `warn`, `max_iterations_reached`, derivaciones cuya causa era evitable |
| `revisar-feedback` | `agent_correction` (lo que el operador reescribió), `knowledge_gap` (lo que la KB no contestó) y `buzon-feedback.md` |
| `proponer-enriquecimiento` | Qué falta en `conocimiento/` y qué caso nuevo merece entrar en `evals.json` |
| `proponer-especialista` | Hoy no propone nada: los candidatos necesitan datos del PMS, que no existe (pieza 03) |

### El sensor de dominio: sí procede

Es opcional con criterio, y aquí el criterio es claro: **este nicho cambia por boletín
oficial, no por moda**, y una de las normas que le afectan entró en vigor hace dos semanas.
Se genera con `skill-creator` y se engancha al `cron-nicho`, cadencia **mensual**, vigilando
cuatro cosas y solo cuatro:

1. Desarrollos del art. 50 del AI Act — guías de la Comisión, la transitoria del 2 de
   diciembre de 2026, criterio de las autoridades nacionales.
2. Pronunciamientos de la AEPD sobre datos de salud, asistentes virtuales o clínicas.
3. Cambios en la Ley 41/2002 y en el RD 1907/1996.
4. Criterio de los colegios profesionales sobre comunicación automatizada con pacientes.

Lo que encuentre **no se aplica solo**: una novedad normativa que toque los límites es
justo lo que la regla B manda proponer al responsable.

## Cuándo está bien
- Las tareas repetitivas críticas están empaquetadas como skills.
- Cada skill tiene nombre claro y un "cuándo se usa" inequívoco.
- Tiene definidos sus sensores de entrenamiento continuo a medida de su destino.
