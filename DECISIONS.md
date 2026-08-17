# DECISIONS — Chatbot

Decisiones de arquitectura con su razón y lo que se descartó. Una decisión sin
alternativa descartada no es una decisión: es una descripción, y esa va en
[`design/design_summary.md`](design/design_summary.md).

Reconstruido el 17 de agosto de 2026 a partir del código y de `CLAUDE.md`. Las
fechas son aproximadas salvo donde se indica.

---

## DEC-01 · Un cliente por instalación, no multi-inquilino

**Decidido:** se clona el repositorio, se configura desde el panel y se despliega
en el servidor de ese cliente. Nada se comparte entre clientes.

**Por qué:** son datos de salud. El aislamiento por infraestructura no se puede
equivocar; un `WHERE tenant_id` sí.

**Descartado:** multi-inquilino con columna discriminante — un fallo de filtro
expone historiales clínicos de otra clínica. Y esquema por cliente en la misma
base: sigue habiendo una credencial que abre todo.

**Coste asumido:** cada instalación se actualiza por separado.

---

## DEC-02 · Todo proveedor externo detrás de una interfaz

**Decidido:** ningún endpoint llama a una API de fuera. Todo pasa por
`backend/app/providers/` (`LLMProvider`, `WhatsAppProvider`…).

**Por qué:** los proveedores se caen, cambian de precio y de contrato. Hoy hay
tres de LLM (OpenAI, Anthropic, Gemini), dos de WhatsApp (YCloud, Meta) y dos de
correo. Cambiar uno es cambiar un fichero.

**Descartado:** llamar al SDK desde el endpoint. Más rápido de escribir y
imposible de sustituir.

---

## DEC-03 · Los prompts viven en la base de datos

**Decidido:** los prompts se editan desde el panel y se versionan en
`agent_prompt_history`. En código solo está el blindaje de seguridad del agente
interno.

**Por qué:** quien afina el tono del agente es el cliente, no quien despliega.
Un prompt en un fichero exige un despliegue para cambiar una coma.

**Descartado:** prompts en ficheros versionados. Buscarlos ahí hoy es perder el
tiempo.

**Excepción deliberada:** el blindaje anti-inyección del agente interno **sí**
está fijo en el código, porque es una medida de seguridad y no debe poder
desactivarse desde el panel.

---

## DEC-04 · Cifrado en reposo solo donde duele

**Decidido:** `nif`, `direccion`, `notas_internas`, transcripciones y credenciales
van cifrados con Fernet (`core/encrypted_type.py`). `telefono`, `email` y
`nombre`, en claro.

**Por qué:** cifrar una columna la deja fuera del alcance de SQL — ni `WHERE`, ni
`LIKE`, ni índices. La app necesita buscar por teléfono, correo y nombre; sin
ellos en claro no hay lista de contactos que funcione.

**Descartado:** cifrarlo todo (rompe la aplicación) y no cifrar nada (son datos
de salud).

**Riesgo aceptado:** perder `ENCRYPTION_KEY` deja esos campos ilegibles para
siempre, y en silencio: el tipo devuelve `None` en vez de romper.

---

## DEC-05 · El clasificador falla hacia dentro

**Decidido:** ante cualquier error, el mensaje **pasa** al agente.

**Por qué:** los dos fallos posibles no cuestan lo mismo. Tragarse un spam es una
molestia; descartar a un paciente real que pregunta por una cita es perder un
cliente y no enterarse.

**Descartado:** fallar hacia fuera (descartar ante la duda). Más limpio en el
inbox, invisible cuando se equivoca.

---

## DEC-06 · Dos pasos en el clasificador, reglas antes que LLM

**Decidido:** primero reglas duras (remitente, dominio, asunto), y solo si
ninguna dispara, una llamada al LLM.

**Por qué:** el 90 % del spam lo caza una regla que cuesta cero. Llamar al modelo
para cada boletín es pagar por lo evidente.

---

## DEC-07 · `/health` devuelve 200 con el worker caído

**Decidido:** el código de estado no depende del worker; el detalle va en el
cuerpo, campo `worker`.

**Por qué:** si `/health` fallara con el worker caído, el orquestador reiniciaría
la API en bucle — y la API funciona perfectamente sin worker, solo se retrasan
las tareas de fondo.

**Descartado:** 503 cuando algo va mal. Convierte una degradación en una caída.

---

## DEC-08 · Migraciones a mano, sin `--autogenerate`

**Decidido:** `upgrade()` y `downgrade()` escritos a mano, con la cabecera
explicando **por qué**.

**Por qué:** `--autogenerate`, comprobado contra una base al día, propone borrar
cuatro tablas y catorce índices correctos. No los ve porque hay modelos sin
importar en `__init__.py` y muchos índices son SQL a mano (parciales, GIN, IVFFlat).

**Descartado:** arreglar el autogenerado cada vez. Es más trabajo y una sola
distracción borra una tabla en producción.

**Regla que se sigue:** una migración aplicada no se edita nunca; se corrige con
otra encima. Y toda migración lleva `downgrade()` de verdad — la CI lo comprueba.

---

## DEC-09 · El código habla español

**Decidido:** columnas, campos, valores de enum, comentarios, textos del panel y
nombres de prueba, en español. Nombres de tabla en inglés por herencia; nombres
de componente React en inglés y PascalCase.

**Por qué:** quien mantiene esto y quien lee los mensajes de error trabaja en
español. Un `estado: derivada` se entiende sin diccionario.

**Descartado:** traducirlo todo al inglés — y también renombrar las tablas, que
habría costado una migración por nada.

---

## DEC-10 · Buffer de ráfaga en Redis, salvo en voz

**Decidido:** los mensajes que llegan seguidos del mismo remitente se acumulan
unos segundos antes de procesarse. La voz va sin buffer.

**Por qué:** la gente escribe en tres mensajes lo que es una sola pregunta.
Contestar a cada trozo sale caro y queda mal. En voz no aplica: es síncrona.

---

## DEC-11 · Dos clases de agente, texto y voz, no intercambiables

**Decidido:** `kind` es `text` o `voice`, y canal y agente tienen que coincidir.

**Por qué:** un prompt pensado para escribir no sirve al teléfono, donde no hay
listas ni enlaces y el turno de palabra manda.

---

## DEC-12 · El agente interno tiene presupuesto propio

**Decidido:** el agente del operador tiene límite diario y presupuesto separados
de los agentes que atienden a clientes, y un blindaje fijo contra órdenes
escondidas en los mensajes que lee.

**Por qué:** lee mensajes de terceros; es superficie de inyección de prompt. Y si
un operador se pone a preguntarle cosas, no puede consumir el presupuesto que
atiende a los pacientes.

---

## DEC-13 · Búsqueda híbrida en la base de conocimiento

**Decidido:** vector (pgvector) **más** texto completo en español —
`match_chunks_hybrid`.

**Por qué:** el vector solo falla con nombres propios, referencias y códigos, que
es justo lo que pregunta la gente ("¿tenéis el tratamiento X?"). El texto completo
solo falla con sinónimos.

---

## Pendiente de decidir

| # | Qué | Dónde |
|---|---|---|
| 1 | ¿El chatbot sustituye o complementa los workflows de n8n? | Decisión 3 del `CLAUDE.md` del paraguas. Bloquea construir dos veces lo mismo |
| 2 | ¿EasyPanel o adaptar los compose al Caddy propio? | Decisión 2 del paraguas |
