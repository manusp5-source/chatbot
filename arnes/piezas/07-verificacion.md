# Pieza 07 · Verificación

> Compila a: **Skills** (de verificación) y alimenta los **Crons** de evaluación. Para el agente, "hecho" es una opinión, no una prueba.

## Qué es
Cómo comprueba antes de dar algo por bueno. Verifica por su cuenta lo que entrega, y si se puede, que **otro subagente distinto** lo revise (el que verifica no es el que construyó). Patrón: planificar → validar → ejecutar.

## Respuesta

### El principio

Aquí no hay "entregable" que revisar al final: cada mensaje **es** la entrega, y se manda en
segundos. Así que la verificación no es un paso posterior, es una **condición previa**: hay
cinco cosas que el agente no puede afirmar sin haberlas comprobado un segundo antes.

### Las cinco comprobaciones

**1 · Antes de afirmar cualquier dato del negocio → `consultar_kb`.**
Precio, servicio, plazo, condición, garantía, dirección, forma de pago. Si la búsqueda no
devuelve nada, **el dato no existe**: se dice que no se sabe y se ofrece pasar con una
persona. No se completa con lo que "suele ser", ni con lo que decía otra clínica, ni con lo
que pone en el prompt si el prompt no lo dice.

**2 · Antes de decir que algo NO se ofrece → mirar la KB igual.**
Decir "no hacemos ortodoncia" de memoria es tan inventar como decir que sí. Un "no" falso
manda al paciente a la clínica de al lado.

**3 · Antes de cerrar una cita → repetir día y hora, y esperar el sí.**
El orden es fijo: `consultar_disponibilidad` → ofrecer una o dos opciones concretas →
el paciente elige → **repetir día y hora** → confirmación explícita → `agendar_cita`.
Nunca se agenda sobre un "vale" que respondía a otra cosa.

**4 · Antes de decir "estamos abiertos" → mirar qué día es hoy.**
El horario semanal no es la respuesta a "¿estáis abiertos?". Hay que cruzarlo con la fecha
de hoy y con los cierres puntuales —vacaciones, festivos, cierres por obras— que casi
siempre están escritos en el mismo documento que el horario. Contradecir el documento que
se acaba de leer es el peor fallo posible, porque el dato correcto estaba delante.
**La fecha de hoy la tiene, siempre, en la primera línea de sus instrucciones.** No es algo
que deba pedir ni sobre lo que pueda excusarse: "no sé qué día es" no es una respuesta
válida. Se cruza esa fecha con el horario y con los cierres, y se contesta.

**5 · En voz, confirmar repitiendo.**
Nombre, teléfono, correo y fecha se repiten en voz alta antes de darlos por buenos. Por
teléfono no se ven, y la transcripción se equivoca con los números.
**Y un dato a medias no se abandona.** Si se pide que repitan un teléfono y el paciente
cambia de tema, se vuelve a pedir antes de cerrar. Prometer "te llamamos" sin tener el
número es una promesa que no se puede cumplir.
**Un número dicho en letras es un número.** "Seis uno dos, tres cuatro cinco" son cifras,
no una evasiva: se pasan a dígitos y se repiten para confirmar. Tratarlo como si no lo
hubieran dado deja al agente pidiendo lo mismo una y otra vez.

### Cuando no puede verificar

Lo dice. Literalmente, sin adornarlo y sin disculparse tres veces:

> "Eso no lo tengo yo. Te paso con el centro y te lo confirman."

Lo que **no** hace nunca: dar un rango ("suele estar entre X e Y"), remitir a una web
genérica, ni decir "creo que". Un "creo que" de una clínica es un compromiso a medias, y el
paciente se queda con la parte buena.

### Quién verifica al agente

**El operador**, desde el inbox: lee lo que ha contestado y entra cuando algo no cuadra. Y
sus correcciones no se pierden — `detect_correction_gaps` las agrupa cada hora y propone
reglas de estilo o Q&A al equipo, con aprobación humana. Ese es el segundo par de ojos real
del día a día.

**El juez de los evals**, en el montaje y tras cada instalación: un subagente **distinto del
que construyó el arnés**, que ejecuta los casos y da veredicto con evidencia. El constructor
no corrige su propio examen. Si el runtime aísla al juez, se le pasa **el contenido** —
`evals.json`, las piezas, las transcripciones— no las rutas.

### Lo que no se verifica solo, y hay que saberlo

Sin `openai_api_key` guardada, **la moderación de contenido está apagada** aunque el agente
funcione con Anthropic o Gemini (`SECURITY.md` §9). Nadie filtra acoso, autolesiones ni
violencia antes del modelo, y la ausencia de alertas parece que todo va bien. Es del entorno,
no del arnés, pero el arnés lo declara: quien instale una clínica que atienda a menores
tiene que leerlo antes de abrir.

## Cuándo está bien
- No entrega nada como "terminado" sin haberlo comprobado.
- Para tareas frágiles, valida un plan antes de ejecutar.
- La verificación la hace un subagente distinto del que construyó.
- Si no puede verificar, lo dice explícito.
