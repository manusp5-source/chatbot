# TODO — tarea en curso

Plan de la tarea que se está haciendo ahora, con la revisión al cerrarla. Cuando
termina, se vacía y entra la siguiente. El inventario permanente de lo que existe
está en [`../implementation/task_tracker.md`](../implementation/task_tracker.md).

---

## Tarea: Oleada 2 de FactorIA — reconstruir los artefactos del método

**Rama:** `feat/factoria-artefactos` · **Abierta:** 17 de agosto de 2026

Los proyectos ya existen, así que no se usa `/init-project`: se reconstruye desde
el código lo que falta y se entra por `/start-execution`.

- [x] `planning/scope.md` — dentro y fuera, derivado del código
- [x] `implementation/task_tracker.md` — 12 ITs y 27 UJs con evidencia en fichero
- [x] `design/design_summary.md` — bloque **Verification Commands** que `/review` necesita
- [x] `PROJECT.md` — estado, bloqueadores, siguiente paso
- [x] `KNOWLEDGE.md` — trampas que no se deducen del código
- [x] `DECISIONS.md` — 13 decisiones con su alternativa descartada
- [x] `tasks/todo.md` y `tasks/lessons.md`
- [x] Punteros en `docs/` para que `/session-start` y `/review` encuentren lo suyo
- [x] Verificación: los tres jobs de `ci.yml` reproducidos en local
- [ ] `/review` con subagente de contexto limpio

## Revisión al cerrar

**Verificación ejecutada el 17 de agosto de 2026** (Python 3.12 en contenedor —
la máquina solo tiene 3.14; Postgres pgvector y Redis efímeros en su propia red
Docker, porque el 6379 del host lo tiene `unicornia-crm-redis`):

| Job de `ci.yml` | Resultado |
|---|---|
| `backend` — migraciones ida y vuelta | correctas, hasta `0054_channel_secrets_encrypted` |
| `backend` — `pytest` | **1165 pasan, 11 se saltan** |
| `frontend` — `tsc --noEmit` y `npm run build` | verdes |
| `imagenes` — backend, panel y MCP | las tres construyen |

**Un fallo real encontrado y arreglado.**
`tests/test_probar_credencial_servicios.py::test_remitente_con_dominio_verificado`
guardaba `hola@example.com` mientras su propio fixture `RESEND_VERIFICADO` solo
declara `chatbot.com` como verificado. El servicio respondía `ok: False` — y
hacía bien, es lo que cubre el test de al lado. Bug del test, no del producto:
corregido el remitente a `hola@chatbot.com`.

**Lo que quedó fuera:** el arnés del agente (Oleada 5) y el remoto de git
(Oleada 0). Ninguno es de esta tarea.

---

## Después de esto

No en esta tarea, y en este orden:

1. **Oleada 0 del paraguas** — remoto privado. Hoy no hay copia fuera de esta
   máquina; es el riesgo mayor del proyecto.
2. **Oleada 5** — arnés del agente (`entrenando-agentes`), que cierra el hueco
   del guardarraíl clínico sin evals.
