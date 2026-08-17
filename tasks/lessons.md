# LESSONS — reglas aprendidas trabajando aquí

Reglas extraídas de correcciones del usuario y de errores cometidos en este
proyecto. **Se lee al empezar cualquier sesión en este directorio.**

Una regla entra aquí solo si costó algo aprenderla. Lo que se deduce leyendo el
código no es una lección: es documentación, y va en `CLAUDE.md`.

Formato: la regla, y debajo de dónde sale.

---

## L-01 · Verificar es haber ejecutado el comando, no que el fichero exista

No se marca nada como hecho sin una prueba, un log o un diff que lo demuestre.
"Creo que funciona" no cuenta.

*De: regla 6 del `CLAUDE.md` global y criterio de `/review`.*

## L-02 · El plan describe la intención; el árbol de ficheros, la realidad

`implementation/user_journeys.md` avisa de ello en su cabecera y aun así seis de
27 journeys estaban en otro sitio del que decía. Antes de afirmar dónde vive
algo, abrir el fichero.

*De: reconstruir el `task_tracker` el 17 de agosto de 2026.*

## L-03 · Nada de `--autogenerate` en las migraciones

Propone borrar cuatro tablas y catorce índices correctos. Se escribe a mano.

*De: `CLAUDE.md`, comprobado contra una base al día.*

## L-04 · Antes de levantar un servicio, mirar el mapa de puertos

`INFRA-LOCAL.md` en la raíz del paraguas. Hay trece contenedores arriba y doce
puertos ocupados; los choques ya han pasado. El síntoma típico de hablar con el
Postgres equivocado es `FATAL: database "test" does not exist`.

*De: regla 1 de la casa, y confirmado el 17 de agosto: el 5173 lo sirve el panel
de este proyecto, así que los e2e del CRM apuntaban aquí sin darse cuenta.*

## L-05 · No inventar integraciones entre las tres aplicaciones

El chatbot y `eskailet-crm` **no** están integrados. Si alguien pregunta si los
contactos del chatbot llegan al CRM, hoy la respuesta es no.

*De: regla 7 del `CLAUDE.md` del paraguas y de `OFERTA.md`.*

## L-06 · Las bitácoras no vienen en el paquete

Si un comando busca `docs/project_memory.md`, `implementation/task_tracker.md` o
`docs/work_log.md` y no están, no es un error: se crean con lo que se pueda leer
del repositorio y se sigue.

*De: `CLAUDE.md`, sección Bitácoras.*

## L-07 · Un `[x]` de oficio es peor que un `[ ]` honesto

Al inventariar, lo que no se localiza queda pendiente o marcado como "está, pero
en otro sitio". Marcar por lo que dice el plan convierte el tracker en ficción.

*De: reconstruir el `task_tracker` el 17 de agosto de 2026.*
