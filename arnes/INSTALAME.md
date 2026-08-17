# INSTÁLAME — compilador portátil, infraestructura-agnóstico

Este archivo viaja con el arnés. El arnés es **neutral**: no lleva dentro la estructura de ninguna plataforma. Instalarlo = mapear los artefactos de la tabla de instalación a las convenciones de la infraestructura donde caigas, sea cual sea. Como el destino es a su vez un agente, el propio agente lee esto y se instala.

No hay plataforma privilegiada. La fábrica no está hecha "para Claude" ni "para Hermes": está hecha para crear el arnés con independencia de la infraestructura.

## La tabla de instalación (lo que hay que colocar — piezas, artefactos y permisos, sin huecos)

Una sola tabla: cubre las 11 piezas, las semillas y las configs. Los **permisos son un artefacto de primera clase**: si tu plataforma no tiene config de permisos, se llevan al soul como reglas duras — pero nunca se omiten.

| Artefacto | Sale de | Qué es / dónde cae |
|---|---|---|
| **Soul** | piezas 01, 02, 03 | identidad + contexto + equipo del agente |
| **Permisos y herramientas** | piezas 04, 05 | qué toca y qué NO hace solo: config de herramientas + reglas allow/deny; lo irreversible y lo costoso en "pide OK" o denegado |
| **Memoria** | piezas 06, 08, 09 + semilla `registro.md` | qué recuerda, su registro (el log que lee el motor) y cómo retoma |
| **Base de conocimiento** | `conocimiento/` | el saber de dominio |
| **Skills** | piezas 07, 11 + `skills-entrenamiento/` | oficio de ejecución + verificación + entrenamiento continuo |
| **Crons** | `motor-mejora-continua/` + semilla `buzon-feedback.md` | los carriles de mejora (el buzón es la entrada del cron-feedback) |
| **Config de modelo y coste** | `config-modelo-coste.md` (intake §9 + blueprint) | qué motor por tipo de tarea y qué límites de coste piden OK; se aplica en la config del runtime |
| **Evals** | pieza 10 + `evals.json` | no se colocan: se **corren** tras instalar (juez aparte) y cada ejecución se guarda en el histórico de `evals.json` |

---

## Método general (vale para CUALQUIER infraestructura)

Esto basta para instalar el arnés en cualquier sitio, conocido o no:

1. **Averigua las convenciones de tu plataforma.** ¿Dónde guarda la identidad del agente? ¿Dónde las skills? ¿Cómo declara permisos? ¿Dónde la memoria? ¿Cómo programa tareas en el tiempo (crons)?
2. **Mapea cada artefacto de la tabla de instalación** a esos sitios. Todos, permisos incluidos: un artefacto sin sitio es un agujero, no un detalle.
3. **Colócalos, y fija la RUTA del arnés.** La fuente neutral (esta carpeta del arnés, entera) **viaja con el agente**: cópiala dentro del destino, en una subcarpeta de la raíz del agente instalado (recomendado: `<raiz-del-agente>/arnes/`, sin espacios ni acentos). Todo lo que el agente compilado referencie del arnés (evals, motor, conocimiento, `registro.md`, `buzon-feedback.md`) apunta a esa ruta **relativa a la raíz del agente**, nunca a una ruta absoluta de la máquina donde se montó: esa máquina no existirá en el destino. Anota la ruta elegida en `PROGRESO.md`. Regla de oro innegociable: lo irreversible y lo costoso siempre pide OK humano.
4. **Corre los `evals.json`** (con juez aparte) antes de dar el agente por vivo, y guarda la ejecución en el histórico de `evals.json`.
5. **Anota en `PROGRESO.md`** qué convenciones usaste y la ruta del arnés, para poder repetir la instalación igual la próxima vez (y, si quieres, convertirla en una receta concreta).

Si tu infraestructura no aparece en las recetas de abajo, este método la instala igual. Las recetas son solo atajos; el método general es lo que de verdad hace el trabajo.

---

## Recetas concretas (atajos para plataformas conocidas, en igualdad)

Son concreciones del método general. Ninguna es "la principal"; usa la que aplique a tu destino.

### Receta A — runtime con archivo de identidad raíz + carpeta de skills
(p. ej. el patrón `CLAUDE.md` + `.claude/`)
- **Soul** → archivo de identidad en la raíz del proyecto.
- **Permisos y herramientas** → archivo de settings con reglas allow/deny; lo irreversible/costoso en "ask" o deny.
- **Memoria** (incluido `registro.md`) → carpeta de memoria del proyecto.
- **Base de conocimiento** → carpeta junto al archivo de identidad, referenciada desde él.
- **Skills** → una carpeta por skill, con su frontmatter (`name`, `description`).
- **Crons** (+ `buzon-feedback.md`) → el sistema de tareas programadas del runtime.
- **Config de modelo y coste** → la selección de modelo/límites del runtime, según `config-modelo-coste.md`.
- **Fuente del arnés** → subcarpeta `arnes/` en la raíz del proyecto (ruta relativa, sin espacios ni acentos).

### Receta B — runtime con `soul.md`
(p. ej. el patrón Hermes y similares)
- **Soul** → `soul.md`.
- **Permisos y herramientas** → su archivo de config propio.
- **Memoria** (incluido `registro.md`) → su mecanismo de persistencia.
- **Base de conocimiento** → donde ese runtime cargue contexto de dominio, referenciada desde el soul.
- **Skills** → la carpeta de skills que use ese runtime, con su convención.
- **Crons** (+ `buzon-feedback.md`) → su scheduler propio.
- **Config de modelo y coste** → su config de modelos, según `config-modelo-coste.md`.
- **Fuente del arnés** → subcarpeta `arnes/` junto al `soul.md` (ruta relativa, sin espacios ni acentos).

### Receta C — cualquier otra infraestructura
- Usa el **método general** de arriba. Es exactamente para esto.

---

## Reglas comunes a toda instalación
- La **fuente neutral es la única verdad.** Si algo cambia, cambia en la fuente y se reinstala; nunca se edita el compilado a mano.
- La **fuente viaja con el arnés.** Tras instalar, la fuente neutral vive dentro del destino (la subcarpeta anotada en `PROGRESO.md`), no en la máquina donde se montó. Ninguna referencia del compilado puede depender de una ruta absoluta de origen, ni de rutas con espacios o acentos.
- **No inventes contenido al instalar:** solo colocas lo que ya está en el arnés.
- **Lo irreversible y lo costoso siempre pide OK**, en la infraestructura que sea. Los permisos (pieza 05) se instalan siempre: si no hay dónde, van al soul como reglas duras.
- Tras instalar, **corre los evals con juez aparte** antes de dar el agente por vivo, y **guarda la ejecución en el histórico** de `evals.json`.
