# Config · Modelo y coste — eskailet-recepcion

Qué motor usa el agente y qué límites de coste/cómputo respeta. Neutral: se decide por **nivel de capacidad**, no por marca. El motor real depende de la infraestructura donde corra el agente.

**Dónde encaja en el flujo:** se rellena con la sección 9 del intake (`00-INTAKE.md`) y la sección "Modelo y coste" del `BLUEPRINT.md`, y lo consume el compilador al instalar — es un artefacto de la tabla de instalación de `INSTALAME.md`. Si esta config cambia, se cambia aquí (fuente neutral) y se recompila.

## Motor por tipo de tarea (por capacidad, no por marca)

| Nivel | Cuándo usarlo | Qué motor (según infraestructura) |
|---|---|---|
| **Alta capacidad** | criterio, decisiones, tareas complejas | el modelo más capaz disponible en tu infraestructura |
| **Media** | volumen, tareas estándar bien definidas | el equilibrado (capacidad/coste) |
| **Ligera** | clasificar, extraer, cosas rápidas y baratas | el más económico y rápido |

### Decisión para ESTE agente

| Tarea | Nivel | Por qué |
|---|---|---|
| **Agente de texto** (WhatsApp, Instagram, correo, chat web) | **Media** | Es volumen, con tool use y respuestas de dos o tres frases. Un modelo caro aquí paga criterio que no se usa |
| **Agente de voz** (llamadas) | **Media, priorizando latencia** | En una llamada el silencio se nota más que la redacción. Si hay que elegir entre matiz y velocidad, gana la velocidad |
| **Clasificador de entrada** | **Ligera** | Y solo se llama si ninguna regla dura ha disparado antes: la mayoría del spam se filtra sin tocar el modelo |
| **Alta capacidad** | **No aplica** | Lo que pide criterio en este agente **se deriva a una persona**. No hay ninguna tarea que justifique el motor caro |

Dónde se aplica en el destino: `agents.model_name`, `agents.temperature`,
`agents.max_tokens`, `agents.llm_provider_id`, más `fallback_provider_id` y `fallback_model`.
Se configura por agente desde Admin → Agentes.

**Aviso de proveedor.** Si la instalación usa Anthropic o Gemini y **no** guarda una
`openai_api_key`, la moderación de contenido queda apagada — llama directamente a la API de
moderación de OpenAI sin mirar qué proveedor usa el agente (`SECURITY.md` §9). La API de
moderación es gratuita: guardar la clave solo para eso es lo más sencillo y no encarece nada.

## Presupuesto

- **Presupuesto mensual por agente**, configurable en el panel (`0005_agent_monthly_budget`).
  El coste real de cada llamada se registra en `llm_usage_log`, y los precios de los modelos
  se refrescan a diario (`refresh-prices`, 06:40).
- **Topes de abuso por contacto**, ya activos: 10 mensajes/minuto y 60 llamadas al modelo por
  hora. Cinco infracciones en 10 minutos → bloqueo automático de 24 h, visible y reversible
  en `/admin/blocklist`.
- Mensajes truncados a 4000 caracteres antes del modelo; audios de más de 8 MB rechazados.

## Límites que NO se cruzan sin OK del responsable del arnés

| Acción | Por qué pide OK |
|---|---|
| Subir el presupuesto mensual de un agente | Es dinero recurrente de un cliente |
| Cambiar de modelo o de proveedor en una instalación viva | Cambia el comportamiento del agente que ya atiende pacientes. Exige recorrer los evals después |
| Lanzar la tanda de evals con **LLM real** | Céntimos por pasada, más levantar Postgres+pgvector y Redis. **Consultar `INFRA-LOCAL.md` antes: hay puertos ocupados** |
| Bajar el nivel de un motor para ahorrar | Se paga en calidad de respuesta, y eso se ve en el inbox antes que en la factura |

## Cuándo está bien
- Cada tipo de tarea tiene su nivel de motor asignado y justificado.
- Lo que cuesta dinero o cómputo real está marcado como "pide OK".
- La decisión está en niveles de capacidad, no atada a un proveedor concreto.
