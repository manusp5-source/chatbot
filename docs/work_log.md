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

## 2026-10-14 — Cierre de gaps (rama `feat/gaps-entrevista`)

### Review — pasada 1

Revisor (sonnet, contexto limpio) y crítico (opus, otro subagente), ambos sobre `git diff f341a61..HEAD`.
Hallazgo de partida: `validate-tracker.py` da «FALLA: no hay filas con ID tipo M0-IT-001»; el crítico
comprobó que falla igual sobre `f341a61` (tracker anterior a la convención), así que es previo al diff y
no se ha tocado para que la review salga limpia.

**SOSTENIDO por el crítico y arreglado, cada uno con su eval nuevo en rojo primero**

| Hallazgo | Arreglo | Prueba |
|---|---|---|
| El día del mes (1-31) y cualquier número del prompt respaldaban un precio inventado | Evidencia estricta por tipo: en prompt/historial/tools solo un importe escrito como importe; número suelto solo en fragmentos de la KB | `test_la_fecha_el_horario_y_los_telefonos_no_respaldan_un_precio` |
| Lo que escribe el cliente respaldaba un importe por eco | El mensaje del cliente ya no es evidencia | `test_lo_que_dice_el_cliente_no_respalda_un_importe` |
| El texto intermedio del propio modelo respaldaba su respuesta | Solo cuentan los resultados de las tools de este turno | `test_el_propio_texto_intermedio_del_modelo_no_respalda_su_respuesta` |
| «Corregir borrador»: el importe que dicta el equipo habría quedado bloqueado | `run_agent(evidencia_extra=...)` | `test_el_equipo_puede_dictar_un_importe_al_corregir_un_borrador` |
| Rangos, «1 200 €» y «EUR 50» no se extraían | Nuevos patrones | `test_formatos_que_se_escapaban_ahora_se_extraen` |
| Capa 2: contenido vacío derivaba TODO; «NO SÉ» pasaba; mensaje cortado a 300 caracteres; sin delimitadores | Vacío = fail-open; solo «NO» limpio pasa; mensaje entero (1500) y delimitado | `test_guardarrail_clinico_capa_llm.py` |
| Anáforas `ahí`, `la misma`, `me sigue` atrapaban peticiones normales | Frases que hablan del síntoma | `test_m7_*` y `test_m8_*` |
| El puente de `derivar_humano` no llevaba aviso de IA y desactivaba el aviso posterior | Módulo `services/aviso_ia.py`; el puente lo lleva | `test_aviso_ia_bd.py` (Postgres real) |
| Una sugerencia de Entrenamiento enviada contaba como respuesta previa | Se excluye `extra.training` | `test_aviso_ia_bd.py` |
| `handoff.sla_minutes` y `out_of_hours_notice` sin punto de escritura | `GET/PUT /admin/settings/handoff` con auditoría | `test_ajustes_derivacion.py` |
| Autorización de `/dashboard/quality` solo probada con un script desechable | Test en el repo: 401 / 403 / 200 | `test_ajustes_derivacion.py` |
| En voz, un agente legacy con `derivar_humano` ofrecía un traspaso imposible | El canal manda sobre la lista de tools | `test_en_voz_un_agente_con_todas_las_tools_...` |

**SOSTENIDO y NO arreglable con código** (se documenta, no se esconde)
- **El verificador de importes no atrapa el F2 literal.** Si el precio del producto vecino está en el
  resultado de la tool, pasa. Una prueba lo fija y DEC-15 y SECURITY.md lo dicen. La regla 7 sigue sin
  demostrarse hasta ejecutar F2 con un LLM real.
- Falsos positivos de términos cortos nuevos (`emergency`, `pain`, `algo raro`…): aceptados por la
  asimetría de costes y escritos en la cabecera de `guardarrail_clinico.py`.

**INFUNDADO** (el crítico lo desmontó): el fallo de `test_contactos_csv_fusion_telefono` era contaminación
de la BD por los scripts del propio revisor; en BD limpia pasa (17 passed). «Suite completa 261 passed»
no era la suite completa (se paró con `-x`); contra BD limpia, 1320 passed.

**Fuera de alcance de esta pasada:** el agente interno del operador tiene su propio bucle sin
verificador de importes (es de uso interno); la capa 2 con un LLM real; F2/T2/F3 contra la instalación real.

**Veredicto pasada 1**: arreglar primero. Los bloqueantes de arriba están arreglados; **falta la pasada 2**
(volver a lanzar el revisor sobre lo arreglado) antes de marcar `Review ✓`.

### Review — pasada 2 (15 oct 2026)

Revisor (sonnet, contexto limpio) sobre los arreglos de la pasada 1, con casos hostiles propios y BD real.
Arreglos 1-6 (verificador, capa LLM, anáforas, aviso de IA, endpoints, voz): **CORRECTO** con sus ataques.
Hallazgos nuevos, todos MEDIO o BAJO, **arreglados con su prueba en rojo primero**:

| Hallazgo | Arreglo |
|---|---|
| `mensaje_al_cliente` que el modelo escribe al llamar a `derivar_humano` esquivaba el verificador | Un puente que contenga un importe se descarta entero y se usa el texto por defecto |
| `PUT /admin/settings/handoff` no era parcial: `{"sla_minutes": 45}` apagaba el otro ajuste | Campos opcionales; ausente = se conserva |
| U+202F (espacio fino) como separador de miles | Reconocido |
| El historial de contexto de la capa LLM podía imitar el delimitador | Saneado como el mensaje |
| `sigue doliendo`, `me siguen doliendo`, `no mejora` ya no se reconocían tras estrechar las anáforas | Añadidos |

**Aceptado y documentado, sin arreglar:** `evidencia_extra` respalda un importe citado en negativo por el
equipo (lo teclea un usuario autenticado que luego revisa el borrador); falsos positivos residuales de
`como te decía` / `lo anterior` / `ahí mismo` tras una derivación; el agente interno del operador sin
verificador (DEC-15).

**Suite completa** (Postgres y Redis reales, `pytest tests` desde `backend/`):
`2 failed, 1396 passed, 1 skipped`. Los dos fallos son ajenos al diff y se conocían:
- `test_internal_agent.py::test_internal_package_does_not_import_writers`: `UnicodeDecodeError` cp1252, falla igual en la base `f341a61`.
- `test_calendar_scheduling.py::test_sin_google_conectado_no_se_ofrecen_huecos`: el test usa «hoy + 30 días»,
  que el 15 oct 2026 cae en sábado (2026-11-14) y el negocio cierra; ni el test ni `calendar.py` están en el diff.

Los «8 fallos de pandas» que citó el revisor eran de su entorno: el antivirus iba poniendo en cuarentena
binarios (`.pyd`) del venv temporal (pandas, lxml); con el venv reinstalado pasan.

**No ejecutado en ninguna pasada:** la capa LLM con un modelo real; F2, T2 y F3 contra la instalación
real con LLM; entrega real por WhatsApp/Instagram/web; web push real. El crítico (opus) no se relanzó en la
pasada 2.

**Veredicto pasada 2**: avanzar, con esas salvedades. La regla 7 sigue sin demostrarse hasta ejecutar F2.

### 2026-10-15 — Los dos fallos previos de la suite, arreglados

Eran fallos de los propios tests, no del producto, y estaban fuera del diff de la review:
- `test_internal_agent.py`: `open()` sin `encoding` leía con cp1252 en Windows y fallaba con `UnicodeDecodeError` ante las tildes de los comentarios. Ahora `encoding="utf-8"`.
- `test_calendar_scheduling.py::test_sin_google_conectado_no_se_ofrecen_huecos`: pedía «hoy + 30 días», que depende del día de la semana (el 15 oct 2026 cae en sábado y el negocio cierra). Ahora avanza al primer día laborable.

Ejecutados: `25 passed, 1 skipped`. La suite completa debería quedar sin fallos; no la he repetido entera tras este cambio.

### 2026-10-15 — Auditoría de la plantilla de prompt de texto

Leída `PROMPT_PLANTILLA_TEXTO` (`backend/app/scripts/seed.py`). Hallazgo arreglado: la sección 4.2 ordenaba
«nunca cuentes que estás guardando nada en un sistema», lo que choca con la transparencia del RGPD (art. 13/14);
la plantilla de voz sí decía que se explicara para qué se piden los datos. Ahora: no anunciar cada guardado, pero
responder con sinceridad si el cliente pregunta qué se hace con sus datos.

**Pendiente de recompilar, no editado a mano:** `arnes/compilado/prompt-texto.md` y `prompt-voz.md` llevan la frase
antigua. Los compilados no se editan a mano: hay que cambiar la fuente y recompilar con la Fábrica de Arneses.
**Sin comprobar con LLM real:** el cambio es de prompt; ningún test determinista demuestra que el modelo lo cumpla.
Solo afecta a instalaciones nuevas (el prompt vive en la base de datos y los agentes ya creados no cambian).

Otros puntos vistos y NO tocados: al derivar, la plantilla dice «dile que un compañero le contesta», y además sale
el mensaje puente escrito en código (posible duplicado); el fuera de horario depende solo del prompt.
