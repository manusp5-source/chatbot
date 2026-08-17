# Progreso del montaje — eskailet-recepcion

Memoria de trabajo del montaje. Se actualiza al cerrar cada sesión y se lee al volver.

## Estado del recorrido (fases 0–5)

**Fase 0 · Copia** — [x] Plantilla copiada a `agentes/eskailet-recepcion/`

**Fase 1 · Intake** — [x] `00-INTAKE.md` completo, sección 0 incluida

**Fase 2 · Investigación y plan** — [x] Nicho investigado · [x] Blueprint · [x] **Aprobado**

**Fase 3 · Construcción y verificación**
- [x] Piezas 01..11 · [x] `conocimiento/` (4 fichas) · [x] los 4 esqueletos de entrenamiento adaptados
- [x] `evals.json` — **24 casos**, todos nacidos en `passes: false`
- [ ] **Evals pasados** — 12 de 13 deterministas ejecutan comportamiento y pasan; 11 sin ejecutar (piden LLM). **`passes: true` solo en 3**, los que un juez bendijo

**Fase 4 · Compilar / instalar**
- [x] Souls compilados · [x] Fuente en `eskailet-chatbot/arnes/` (36 ficheros, `compilado/` incluido)
- [x] Runner en `backend/tests/test_arnes_evals_limites.py`
- [x] Entorno de pruebas: venv CPython **3.12.14** creado con `uv` (los pines del repo no resuelven en el 3.14 del sistema)
- [ ] Prompts aplicados en Admin → Agentes de una instalación real
- [ ] Re-correr los evals sobre el agente instalado (`fase: post-instalacion`)

**Fase 5 · Mejora continua** — [ ] Crons + sensor normativo

## Ruta del arnés tras instalar
`arnes/`, relativa a la raíz de `eskailet-chatbot`. ASCII, sin espacios. Verificado: 0 rutas
absolutas de esta máquina dentro del arnés instalado.

## Piezas
- [x] 01 · [x] 02 · [x] 03 · [x] 04 · [x] 05 · [x] 06 · [x] 07 · [x] 08 · [x] 09 · [x] 10 · [x] 11

## Bitácora

**17 ago 2026 · primera vuelta.** `1 failed, 7 passed, 7 skipped`. Juez independiente:
**no listo**, por dos motivos. `L9` fallaba de verdad (transparencia, art. 50) y —esto no lo
vio el constructor— seis pruebas estaban implementadas como *grep de literales en el código
fuente*: no probaban nada aunque se ejecutaran.

**17 ago 2026 · segunda vuelta.** `1 failed, 13 passed, 0 skipped`.

- Las seis pruebas de grep, reescritas como pruebas de comportamiento con dobles de BD y de
  avisos. Corren en CI sin Postgres ni Redis. `L1` sustituye el proveedor de modelo por uno
  que revienta si se le invoca; `L5` y `L13` compilan el SQL con `literal_binds` para
  afirmar *con qué teléfono* se buscó; `L6` comprueba los tres topes con un chunk de 5000
  caracteres; `L10` trocea `SECURITY_GUARD` real para cubrir paráfrasis.
- **`L7` estaba mal, y no en la dirección cómoda.** Afirmaba que el motivo se escapa como
  HTML antes de salir al canal del equipo, copiado de `SECURITY.md` §3. El producto ya no
  notifica a Telegram/Slack (RGPD, ahora web push interno) y `html.escape` no existe en el
  fichero: la versión anterior habría **fallado**, no aprobado. Corregido contra el código,
  con la corrección escrita dentro del caso. **`SECURITY.md` §3 está desactualizado.**
- **Cambio en código del producto:** los dos textos fijos del guardarraíl empiezan ahora por
  "Soy el asistente virtual del centro, no una persona." Es la mitad A de `L9`: ese camino
  contesta sin pasar por el modelo, así que no hay ningún LLM al que pedirle que se presente.
- Suite completa: `11 failed, 847 passed, 332 skipped`. Baseline con `git stash` del único
  fichero modificado: **los mismos 10 fallos de entorno → 0 regresiones**. El único fallo
  nuevo del sistema es `L9`.

**El juez de la segunda vuelta se cortó por límite de sesión** antes de dar veredicto por
caso. Alcanzó a entregar un hallazgo, y era correcto (abajo). Por eso los nueve casos que
ahora sí ejecutan comportamiento quedan como **evidencia recogida, pendiente de juez**: la
regla dice que juzga un subagente distinto del constructor, y marcarlos yo sería
autocalificarme.

## Incidente que hay que conocer

El hook `checkpoint.ps1` hizo un `git stash` **con untracked**
(`claude-checkpoint-feat/factoria-artefactos-20260817-153558`) y se llevó `arnes/` entero y
`backend/tests/test_arnes_evals_limites.py`, los dos sin commitear. Lo detectó el juez, no
el constructor, que estuvo a punto de descartar el aviso.

No se perdió nada: la fuente neutral vive en la fábrica —que es exactamente para lo que
sirve— y el test y el cambio del guardarraíl se recuperaron del stash.

**Lección, y va a `lessons.md`: en este repositorio el trabajo sin commitear no está a salvo
entre comandos.** Commitear en la rama antes de seguir.

## Notas para retomar

Rama actual: `feat/factoria-artefactos`. **No se ha commiteado nada.**

1. **Decidir `L9` mitad B** — revelación determinista en el primer mensaje de una
   conversación normal. `conversation.py` ramifica el envío por canal (borradores de Gmail,
   troceado, voz), así que no se tocó. Es la única decisión que puede exigir código.
2. **Cerrar el juicio de los nueve casos pendientes** cuando haya sesión.
3. **Los 11 casos de `funcional` y `tono`**: piden la app levantada y clave de LLM.
   Consultar `INFRA-LOCAL.md` antes: 17 contenedores arriba.
4. **`SECURITY.md` §3**: corregir la fila de `derivar_humano`.
5. **Antes de subir**: con `L9` en rojo, el test pone la CI en rojo. Decidir `xfail` con
   motivo o resolver `L9` primero.
6. Fase 5: crons + sensor normativo.
