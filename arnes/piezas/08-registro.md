# Pieza 08 · Registro

> Compila a: **Memoria/log** del runtime. Es el **sustrato que el motor de mejora continua lee** para detectar fallos.

## Qué es
La huella de lo que el agente hace: qué tareas corrió, qué salió bien, qué falló. No es burocracia: es el material del que se alimenta la mejora continua. Sin registro, los crons no tienen de qué aprender.

El arnés trae una **semilla ya creada**: `../registro.md`, con el formato de entrada y la marca FALLO definidos. Existe desde el arranque para que el motor siempre tenga de dónde leer. Esta pieza decide si ese formato/ubicación por defecto vale para ESTE agente o se redefine (si se redefine, anótalo en la propia semilla).

## Respuesta

### Dos registros que no se mezclan

Esta es la decisión de la pieza, y equivocarla sería el error clásico: **el agente en
producción no escribe en `registro.md`.**

| | Registro del **producto** | `registro.md` del **arnés** |
|---|---|---|
| Quién escribe | La aplicación, sola | El responsable y el motor de mejora continua |
| Qué guarda | Lo que pasó en cada conversación real | Lo que se ha cambiado en el diseño del agente y por qué |
| Dónde | Base de datos del cliente | `arnes/registro.md`, en el repositorio |
| Quién lo lee | El operador, el panel, las tareas de autoaprendizaje | El motor, al abrir sesión |

Un agente que escribiera en su propio diseño desde producción se estaría modificando sin
que nadie lo aprobara. Eso es exactamente lo que la regla B impide.

### Lo que ya deja el producto — y es mucho

No hay que construir nada. Lo que hay:

| Dónde | Qué guarda | Retención |
|---|---|---|
| `agent_trace_event` | Paso a paso del agente: decisión de router, cada tool con sus argumentos, el guardarraíl con **el término exacto** que disparó. PII redactada | 90 días, purga diaria a las 04:10 |
| `llm_usage_log` | Cada llamada al modelo con su coste real y el agente que la hizo | 90 días |
| `audit_log` | Cambios de prompt (incluidos los hechos por MCP), credenciales, usuarios | Sin límite hoy — crece indefinido (`SECURITY.md` §5) |
| Conversaciones y mensajes | Todo lo dicho, con su estado (`bot`, `humano`, `cerrada`) | Del negocio |
| `agent_correction` | Cuando el operador reescribe lo que dijo el bot | Hasta procesarse |
| `knowledge_gap` | Lo que preguntan los pacientes y la KB no contesta | Hasta aprobarse o descartarse |

### Cómo se marca un fallo para que el motor lo recoja

Tres marcas, y las tres existen ya:

1. **`log_router_decision` con `TraceLevel.warn`** — el sistema sabe que algo no fue normal.
   Hoy lo emiten el guardarraíl clínico (`decision="guardarrail_clinico"`) y el tope de
   iteraciones (`max_iterations_reached`).
2. **Una corrección del operador** — es el fallo mejor etiquetado que existe: una persona ha
   dicho, con su reescritura, qué habría estado bien.
3. **Una derivación cuya causa era evitable** — la KB no tenía algo que debería tener.
   `detect_faq_gaps` ya las agrupa a diario.

El motor del arnés lee esas tres y, cuando un fallo es real y repetido, **añade un caso
nuevo a `evals.json`** (eso se auto-aplica, regla B) y anota la entrada en `registro.md`.

### Formato de la semilla

El de la plantilla vale tal cual. Una entrada por cambio de diseño, con fecha, qué se
cambió, por qué señal, y si se auto-aplicó o fue a OK. La marca `FALLO` se reserva para lo
que llegó a producción y salió mal.

### Purga

La del producto ya está resuelta y es diaria. **La deuda conocida**: `audit_log` no tiene
política de retención y crece sin límite. Está documentada en `SECURITY.md` §5; no es de
este arnés, pero el motor la vigila como señal.

## Cuándo está bien
- Cada tarea relevante deja huella verificable.
- Los fallos quedan marcados de forma que un cron los pueda leer.
- El registro es legible y no crece sin control.
