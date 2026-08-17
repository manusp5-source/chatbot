# Pieza 06 · Memoria

> Compila a: **Memoria** (junto con la pieza 09). Define qué recuerda el agente entre sesiones y dónde lo guarda, en el mecanismo de persistencia de la infraestructura destino.

## Qué es
Lo que el agente recuerda fuera del modelo: hechos del negocio, preferencias, decisiones tomadas, aprendizajes. La memoria persistente del día a día vive en el runtime del agente; las mejoras estructurales del arnés vuelven a la fuente neutral (eso lo gestiona el motor, no esta pieza).

> **Diferencia con la pieza 09 (Recuperación):** esta pieza trata de lo que el agente **es** (hechos durables: quién es el negocio, qué tono funciona, qué fuentes son fiables). La 09 trata de **dónde está** el agente en su tarea ahora mismo (estado de trabajo, checkpoint del día). Memoria = identidad persistente; Recuperación = estado de avance.

## Respuesta

### Qué recuerda, y dónde

Cuatro capas, todas ya existentes en el runtime. El agente no elige dónde va cada cosa: el
sistema lo decide por él.

| Capa | Qué guarda | Cuánto dura |
|---|---|---|
| Ventana de contexto | Los últimos mensajes de la conversación, tal cual | La conversación |
| Resumen rodante | Lo hablado cuando la conversación se alarga | La conversación |
| **Ficha del contacto** | Nombre, teléfono, email, servicio de interés, etiquetas | **Para siempre**, entre conversaciones |
| Reglas aprendidas | Estilo y Q&A que el equipo ha aprobado, inyectadas tras el soul | Hasta que el equipo las retire |

La memoria larga de verdad es **la ficha**: es lo que hace que el paciente que vuelve en
marzo no tenga que repetir quién es.

### Qué NO guarda — la regla que añade este arnés

**El agente no escribe contenido clínico en la ficha del contacto.** Ni síntomas, ni
medicación, ni antecedentes, ni diagnósticos. No en `notas_internas`, no en el nombre, no en
`servicio_interes`, no en ningún campo libre.

Por qué, y no es un escrúpulo:

- Lo que el paciente cuenta es **dato de salud** en cuanto se guarda: categoría especial del
  art. 9 del RGPD. Recibirlo no se puede evitar; **propagarlo sí**, y propagarlo es lo que
  convierte un mensaje en un fichero de salud.
- La ficha del contacto es una **ficha comercial**, y en el panel la ve cualquier persona con
  rol `cliente` — que ve toda la base de contactos, notas internas incluidas
  (`SECURITY.md` §1). Un síntoma escrito ahí queda a la vista de todo el equipo para siempre.
- La trazabilidad de la derivación **ya existe donde debe**: `agent_trace_event` guarda el
  término exacto que disparó el guardarraíl, con su conversación y su hora, y se purga a los
  90 días. Eso es auditoría con retención; la ficha no.

En la práctica el caso casi no se da, porque el guardarraíl aparta el mensaje clínico
**antes** de que el modelo lo vea. Esta regla cubre lo que se cuela de refilón: el paciente
que menciona que es diabético mientras pide hora para una limpieza.

Tampoco guarda: nada que el paciente no haya dicho, nada deducido ("parece molesto"),
opiniones sobre la persona, ni el contenido de sus mensajes en campos de ficha.

### Cómo distingue un hecho durable de algo de una conversación

Tres preguntas, en este orden:

1. **¿Lo ha dicho el paciente explícitamente?** Si es deducción, no se guarda.
2. **¿Sirve la próxima vez que escriba?** Su nombre sí; que hoy tenía prisa, no.
3. **¿Es clínico?** Entonces no se guarda, por muy durable que sea.

Solo lo que pasa las tres va a la ficha. El resto vive y muere en la conversación.

### Lo que no es esta memoria

`registro.md` y `buzon-feedback.md` del arnés **no son memoria del agente**: son la bitácora
del arnés para el motor de mejora continua. Viven en `arnes/`, los lee el responsable y el
agente en producción no los toca. Confundirlos llevaría a un agente escribiendo en su propio
diseño sin que nadie lo apruebe, que es exactamente lo que la regla B impide.

## Cuándo está bien
- Lo que debe recordar está definido y tiene un sitio.
- No guarda lo efímero ni lo sensible que no debe.
- La memoria persistente del runtime y las mejoras del arnés están separadas.
