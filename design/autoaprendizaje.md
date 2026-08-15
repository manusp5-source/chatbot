# Autoaprendizaje — Plan (con aprobación humana)

> El agente aprende de conversaciones reales, pero **quien atiende aprueba** lo que
> aprende. **Nunca se cambia solo** (descartado por seguridad: manipulación /
> drift). Tres disparadores; se construye por fases.

## Estado

- **Fase 1 — "Corrige y re-redacta": DISPONIBLE.**
  Botón **"Corregir"** en sugerencias de Entrenamiento y borradores de email: la
  quien atiende da una instrucción en lenguaje natural ("sé más cálida, no prometas
  plazos de entrega") y el agente **re-redacta** (sigue sin enviarse). Endpoint
  `POST /conversations/{id}/drafts/{message_id}/refine`. Cada corrección se guarda
  en `agent_corrections` (cifrada). → **Esto ya genera la materia prima de la Fase 2.**
- **Fase 2 — Aprendizaje permanente: PENDIENTE** (este plan).
- **Fase 3 — FAQ automática: PENDIENTE.**

---

## Fase 2 — De correcciones + huecos → conocimiento permanente

**Objetivo:** que de (a) las **correcciones repetidas** y (b) los **huecos** (cuando
el agente no supo responder) salgan **propuestas** de entradas de base de
conocimiento / reglas de estilo que quien atiende **aprueba con un clic** → y el
agente deja de repetir el mismo fallo.

### Señales (qué cuenta como "hueco"/aprendizaje)
1. **Deriva a humano** — el agente llamó a `derivar_humano` (no supo resolver). Señal fuerte.
   - Hook: traza `tool_invocation` con `tool_name=derivar_humano`, o `Conversation.status→humano`.
   - **Mejor respuesta propuesta = lo que quien atiende respondió de verdad tras la derivación** (cero alucinación).
2. **KB sin resultados** — `consultar_kb` devolvió 0 hits (buscó y no había nada).
   - Hook: traza `kb_lookup` con `payload.hits == 0` (ya se loguea en `kb_search.py`).
3. **Correcciones repetidas** (Fase 1) — agrupar `agent_corrections` por similitud; si quien atiende corrige lo mismo varias veces → proponer regla/KB.

### Flujo
1. **Detección + captura** — tarea Celery periódica que lee `agent_trace_event` (y `agent_corrections`) y crea/actualiza registros `knowledge_gap`. (Periódica = simple y sin añadir latencia al turno.)
2. **Propuesta** —
   - Hueco por derivación → respuesta propuesta = la respuesta humana posterior.
   - Hueco por KB-miss / correcciones → el LLM redacta un borrador de Q&A o de regla (marcado "revísalo"); **nunca se aplica solo**.
3. **Aprobar** — nueva pantalla **"Aprendizajes"** (admin) que lista los pendientes: Pregunta + Respuesta/Regla propuesta (editable) → **Añadir a KB** / **Descartar**. Reutiliza el patrón de `DraftCard`.
4. **Aplicar** — al aprobar una Q&A → crear `Document` + `index_document_by_id()` (chunks + embeddings) → el agente ya lo encuentra. Reglas de estilo → bloque de "reglas aprendidas" inyectado en el prompt.

### Modelo de datos (nuevo)
`knowledge_gap`: `id`, `conversation_id` (FK), `trigger` ("handoff"|"kb_miss"|"correction"),
`unanswered_query` (cifrado), `suggested_answer` (cifrado, editable),
`status` ("detectado"|"sugerido"|"aprobado"|"descartado"|"indexado"),
`created_document_id` (FK Document, nullable), `approved_by`, timestamps.
(`agent_corrections` de Fase 1 ya existe; Fase 2 la consume.)

### Endpoints (boceto)
- `GET /admin/learning/gaps?status=…` — lista para revisar.
- `POST /admin/learning/gaps/{id}/approve` — crea Document + indexa.
- `POST /admin/learning/gaps/{id}/discard`.
- (opc.) `POST /admin/learning/gaps/{id}/regenerate` — re-proponer respuesta.

### Building blocks que YA existen (para ir rápido)
- KB ingest: `backend/app/services/kb_indexer.py` → `index_document_by_id()`; modelos `Document`/`Chunk`; `POST /kb/documents`.
- `consultar_kb`: `backend/app/agents/tools/kb_search.py` (umbral 0.3; loguea `log_kb_lookup` con nº de hits).
- `derivar_humano`: `backend/app/agents/tools/human_handoff.py`.
- Trazas: tabla `agent_trace_event` (`kb_lookup`, `tool_invocation`); `backend/app/services/trace_logger.py`.
- Correcciones (Fase 1): tabla `agent_corrections` (modelo `backend/app/models/agent_correction.py`).
- UI de aprobación: `frontend/src/components/DraftCard.tsx` + endpoints de borradores en `conversations.py`.
- KB admin: `frontend/src/pages/client/KnowledgeBase.tsx`.

### Decisiones abiertas (resolver al planificar)
- Detección periódica (recomendado) vs en caliente tras cada turno.
- Reglas de estilo: empezar por KB (Q&A); bloque de reglas en prompt solo si hace falta.
- **Dedupe** de huecos parecidos por similitud de embedding (no repetir la misma pregunta N veces).

---

## Fase 3 — FAQ automática (pendiente)

Agrupar las preguntas más frecuentes (todos los canales) por similitud → proponer
las top-N a la KB con su mejor respuesta. Reutiliza la pantalla "Aprendizajes" y el
ingest de la Fase 2.

---

## Apunte relacionado: procesar audios entrantes (IG + WhatsApp)

Las **notas de voz** que mandan los clientes deben transcribirse (Whisper) y tratarse
como un mensaje más (el agente las "lee" y responde; quedan en el historial).
- **WhatsApp:** la transcripción Whisper ya existe (M6, `audio_url`/`audio_transcript` en `Message`) — **verificar** que sigue funcionando con notas de voz entrantes.
- **Instagram:** `meta.parse_webhook` hoy procesa **texto**; los DM con audio NO se transcriben → **añadir** descarga del adjunto de audio + transcripción Whisper (mismo pipeline que WhatsApp).
- Reutilizar el flujo de audio existente (`backend/app/services/media.py` + transcripción) y exponer el `audio_transcript` en el inbox como ya se hace.
