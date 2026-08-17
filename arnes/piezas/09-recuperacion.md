# Pieza 09 · Recuperación

> Compila a: **Memoria** (junto con la pieza 06). Cómo el agente retoma un trabajo largo sin perder el hilo.

## Qué es
La capacidad de continuar tras un corte: un reinicio, una sesión que se satura, un montaje largo. Memoria de trabajo fuera del modelo que permite retomar donde se dejó. En esta fábrica, el equivalente es `PROGRESO.md`.

> **Diferencia con la pieza 06 (Memoria):** esta pieza trata de dónde está el agente **ahora mismo** en su tarea (qué ha hecho hoy, qué le falta, en qué bloque se quedó). La 06 trata de lo que el agente **es** (hechos durables: preferencias, fuentes, aprendizajes). Recuperación = checkpoint de trabajo; Memoria = conocimiento persistente.

## Respuesta

### Qué es "un trabajo largo" aquí

No hay montajes de horas. Lo que se corta en este agente son **conversaciones**, y se cortan
de cuatro maneras distintas. Cada una tiene su forma de retomar:

**1 · La conversación se alarga.**
El resumen rodante condensa lo hablado y la ventana de contexto mantiene lo reciente. El
agente no pierde el hilo y no vuelve a preguntar el nombre a la tercera.

**2 · El paciente vuelve días o meses después.**
Se retoma desde la **ficha del contacto**, no desde la conversación anterior. Sabe quién es
y qué le interesaba; no arrastra el tono ni el contexto de aquel día. Y saluda como quien
reconoce a alguien, no como quien recita su historial.

**3 · El sistema se reinicia a mitad.**
No hay estado en memoria que perder: el mensaje entrante se vuelve a procesar y la
idempotencia por `provider_message_id` impide contestar dos veces a lo mismo. Si el paciente
mandó tres mensajes seguidos, el buffer los junta y contesta una vez.

**4 · Entra una persona del equipo.**
**Este es el caso importante y la regla es tajante: el agente se aparta y no vuelve.**
Cuando la conversación pasa a `humano` —porque derivó o porque el operador entró a
contestar—, el agente deja de hablar en ella. No "ayuda desde atrás", no añade, no corrige.
Dos voces en la misma conversación es lo que hace que el paciente pierda la confianza en las
dos. Además hay cooldown de 10 minutos para no volver a avisar al equipo por lo mismo.

### Qué lee al arrancar cada turno

En este orden: el resumen rodante y los últimos mensajes → la ficha del contacto → el
horario del centro si hace falta. **No** relee documentos de la KB "por si acaso": los busca
cuando la pregunta lo pide, que es lo que impide que arranque cargado de ruido.

### Cómo evita repetir trabajo

- No vuelve a preguntar un dato que ya está en la ficha.
- No vuelve a ofrecer un hueco que el paciente ya rechazó en esa conversación.
- No vuelve a derivar lo ya derivado (la tool es idempotente si la conversación ya está en
  manos de una persona).

### Cuando se queda sin salida

Cinco vueltas de herramientas sin llegar a una respuesta y el sistema corta. El agente
contesta el texto fijo de espera —"necesito un momento para procesar esto; si es urgente,
dímelo y aviso al equipo"— en vez de improvisar, y queda un `max_iterations_reached` en el
registro. Improvisar en la quinta vuelta es exactamente cuando se inventan los precios.

### El checkpoint del **montaje**, que es otra cosa

`PROGRESO.md` del arnés es el checkpoint de quien monta el arnés, no del agente en
producción. Se actualiza al cerrar cada sesión de trabajo y se lee al volver. Es lo que
permite retomar el montaje sin repetir fases ya cerradas.

## Cuándo está bien
- Puede retomar un trabajo largo sin perder contexto.
- Hay un sitio claro donde anota el avance y lo lee al volver.
- No repite pasos ya completados.
