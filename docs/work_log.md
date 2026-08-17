# Work Log — Chatbot

Registro cronológico del trabajo. Una entrada por tarea cerrada: qué se tocó y
qué no era obvio. `/review` escribe aquí sus hallazgos bajo `## Review`.

Lo aprendido que sirve más allá de la tarea va a
[`../KNOWLEDGE.md`](../KNOWLEDGE.md); las decisiones, a
[`../DECISIONS.md`](../DECISIONS.md).

---

## 2026-08-17 — Oleada 2 de FactorIA: artefactos reconstruidos desde el código

**Rama:** `feat/factoria-artefactos`

**Creado**
- `planning/scope.md`
- `implementation/task_tracker.md` — 12 ITs y 27 UJs, cada uno con el fichero que
  lo implementa
- `PROJECT.md`, `KNOWLEDGE.md`, `DECISIONS.md`
- `tasks/todo.md`, `tasks/lessons.md`
- `docs/project_memory.md`, `docs/decision_log.md` — punteros a la raíz

**Modificado**
- `design/design_summary.md` — añadido el bloque **Verification Commands**. No se
  reescribió el resto: estaba verificado contra el código el 12 de agosto y es la
  mejor documentación del paraguas

**Lo que no era obvio**

1. `implementation/user_journeys.md` describe la intención de diseño, no el árbol
   de hoy — lo avisa en su propia cabecera. Seis de los 27 journeys acabaron en
   otro sitio: todo el admin en un único `api/admin.py` en vez de `api/admin/*`,
   el dashboard del lado admin y no del cliente, el tiempo real como
   `hooks/useInboxSocket.ts` y no como `services/websocket.ts`, y el aviso al
   derivar dentro de `human_handoff.py` en vez de en un `providers/notifications/`
   que no existe. Marcados `[≠]`, que no es un fallo: es diseño que se movió.
2. **Alrededor de la mitad de lo que hace la aplicación no figuraba en ningún
   documento de planificación**: clasificador, difusiones, agente interno,
   autoaprendizaje, canal de voz, Instagram, Gmail, copias de seguridad, tope de
   gasto, traza del agente, RGPD, servidor MCP y notificaciones push. Queda
   inventariado al final del `task_tracker`.
3. Son 27 journeys, no 25: el documento llega hasta UJ-27.

**Corregido**
- `backend/tests/test_probar_credencial_servicios.py` — el test positivo del
  remitente de Resend usaba `hola@example.com` contra un fixture que solo
  verifica `chatbot.com`. Fallaba con razón. Corregido el remitente

**Verificado, con la salida a la vista**

| Job de `ci.yml` | Resultado |
|---|---|
| `backend` — migraciones ida y vuelta | correctas |
| `backend` — `pytest` | 1165 pasan, 11 se saltan |
| `frontend` — tipos y build | verdes |
| `imagenes` — las tres | construyen |

Reproducido en local: Python 3.12 en contenedor (la máquina solo tiene 3.14),
Postgres pgvector y Redis efímeros en red Docker propia — el 6379 del host lo
ocupa `unicornia-crm-redis`.

**Pendiente**
- `/review` con subagente de contexto limpio
