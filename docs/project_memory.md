# project_memory — puntero

El estado del proyecto vive en **[`../PROJECT.md`](../PROJECT.md)**, en la raíz.

Está ahí y no aquí porque el hook `session-start.ps1` inyecta `PROJECT.md` como
contexto al abrir cada sesión. Un único fichero, un único sitio donde
actualizarlo.

`/session-start`, `/review` y `/start-execution` piden este fichero por su nombre
de FactorIA: siguen el enlace y leen `PROJECT.md`.
