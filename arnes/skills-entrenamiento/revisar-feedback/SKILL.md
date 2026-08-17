---
name: revisar-feedback
description: Procesa el buzon de feedback del agente. Aplica los ajustes menores seguros y convierte en propuesta lo que toca soul o permisos. La ejecuta el cron-feedback.
---

# revisar-feedback — skill de entrenamiento (motor genérico)

La dispara `cron-feedback` (ver `../../motor-mejora-continua/crons.md`). Esqueleto funcional que viene con la fábrica; al montar el arnés (Fase 3), el entrenador revisa y completa la sección "A medida de este agente".

## Pasos
1. Lee `../../buzon-feedback.md` y localiza las entradas **PENDIENTE**.
2. Clasifica cada una con regla B (`../../motor-mejora-continua/regla-B.md`):
   - **Ajuste menor** (tono/criterio pequeño, reversible, no cambia quién es el agente ni qué puede tocar) → aplícalo en la fuente neutral y recompila.
   - **Cambio de fondo** (soul, permisos, skill nueva) → NO lo apliques: pásalo a `proponer-enriquecimiento` para que lo empaquete como propuesta.
   - **En caso de duda** → trátalo como de fondo y propónlo.
3. Marca cada entrada procesada como **PROCESADO** en el buzón, anotando qué hiciste con ella (aplicado / propuesto / descartado y por qué).
4. Deja constancia en `../../registro.md`. Si el buzón está vacío, este ciclo termina sin cambios.

## A medida de este agente

**Por dónde llega el feedback.** Tres vías, y dos de ellas ya corren solas en el producto:

| Vía | Origen | Cómo llega al buzón |
|---|---|---|
| Correcciones del operador | `detect_correction_gaps`, cada hora (Celery beat) | Las agrupa y propone reglas de estilo o Q&A en "Aprendizajes", **con aprobación humana**. Lo aprobado y repetido se vuelca al buzón |
| Huecos de FAQ | `detect_faq_gaps`, diario a las 06:20 | Igual: propuesta con aprobación humana |
| El responsable | Directo | Escribe en `buzon-feedback.md` |

Ojo con el matiz: el producto **ya tiene su propio ciclo de aprobación**. Lo que el equipo
de una clínica aprueba ahí afecta a esa instalación (`render_learned_rules_block`). Al buzón
del arnés solo sube lo que se repite **entre clínicas**, porque eso es señal de producto y
no de un cliente.

**Ajustes menores en ESTE agente** (se auto-aplican en la fuente y se recompila):

- Afinar una frase del tono o del formato de respuesta.
- Añadir un ejemplo a un procedimiento de la pieza 11.
- Añadir una ficha o un matiz a `conocimiento/`.
- Añadir un caso nuevo a `evals.json`.

**Cambios de fondo** (van a propuesta, siempre):

- Tocar cualquiera de las **cinco reglas duras** de la pieza 01.
- Tocar la pieza 05 (permisos) o la lista de herramientas de un agente.
- Cambiar modelo, proveedor o presupuesto.
- **Cualquier cosa que exija tocar código del producto.** En este destino, la mitad de los
  límites los impone el código: una propuesta que empiece por "habría que cambiar el
  guardarraíl" no es un ajuste menor por muy pequeña que parezca.
