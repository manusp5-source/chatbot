# Regla B — qué se aplica solo y qué se propone

La regla que gobierna toda la mejora continua. Auto-aplica lo seguro; propone lo gordo. El agente mejora solo en lo pequeño y te pregunta en lo grande.

## Se auto-aplica (bajo riesgo, reversible)
- Añadir un caso de eval a partir de un fallo real.
- Anotar un aprendizaje en la base de conocimiento.
- Ajustes menores de formato o de un sensor de dominio.
- Cualquier cosa fácil de revertir y que no cambia quién es el agente ni qué puede tocar.

→ Se aplica en la fuente neutral, se recompila y se deja constancia en el registro. No necesita OK.

## Se propone (alto riesgo o de fondo)
- Tocar el **soul** (identidad, tono, reglas de oro).
- Tocar los **permisos** (lo que puede hacer solo).
- Crear una **skill nueva**.
- Crear un **agente especialista** (lo más gordo de todo).
- Cualquier cosa irreversible o con coste real.

→ El motor deja una **propuesta de enriquecimiento** pendiente (un mini-blueprint) y avisa al responsable del arnés (definido en `../00-INTAKE.md`, sección 0). No se aplica hasta su OK. Aprobado → se aplica en la fuente neutral y se recompila.

## En caso de duda
Si no está claro si algo es seguro o gordo → trátalo como gordo y propónlo. El cuello de botella humano es preferible a la deriva silenciosa.

## Siempre, en los dos casos
- Escribir en la fuente neutral, nunca en el compilado.
- Pasar evals (juez aparte) antes de dar la mejora por buena.
- Dejar huella en el registro de qué se cambió y por qué.
