# Crons — los carriles en el tiempo

Los crons son los que despiertan al motor cada cierto tiempo. Cadencias de partida; se ajustan por agente en el blueprint. El compilador los registra en el sistema de tareas programadas de la infraestructura destino, sea cual sea.

## Crons de partida

Cada cron ejecuta una skill del arnés (existen desde el arranque como esqueletos en `../skills-entrenamiento/`; el entrenador las adapta en la Fase 3). Ningún cron se registra apuntando a algo que no exista.

### cron-fallos (diario) → ejecuta `../skills-entrenamiento/recoger-fallos/`
- Lee el registro (semilla `../registro.md`, pieza 08), recoge los fallos del día.
- Convierte fallos en casos de eval nuevos. Anota aprendizajes seguros en la base de conocimiento.
- Todo bajo regla B (esto es seguro → se auto-aplica).

### cron-feedback (cada vez que hay buzón nuevo, o diario) → ejecuta `../skills-entrenamiento/revisar-feedback/`
- Revisa el buzón de feedback (semilla `../buzon-feedback.md`).
- Ajustes menores → aplica. Cambios de soul/permisos → propuesta (vía `proponer-enriquecimiento`).

### cron-nicho (semanal) → ejecuta el sensor de dominio a medida
- Lanza el sensor generado con skill-creator en la Fase 3. El sensor es **opcional con criterio**: si no se generó, este cron NO se registra.
- Anota novedades en la base de conocimiento (seguro). Si detecta necesidad de skill/especialista → propuesta (vía `proponer-enriquecimiento` / `proponer-especialista`).

### cron-evals (semanal, tras cambios) → corre `../evals.json`
- Corre los evals con juez aparte sobre el agente vivo y guarda la ejecución en el histórico de `evals.json`.
- Si algo ha bajado de nivel respecto al histórico, lo marca y propone arreglo.

### cron-equipo (mensual) → ejecuta `../skills-entrenamiento/proponer-especialista/`
- Revisa si el trabajo recurrente pide un especialista.
- Si sí → propuesta de agente nuevo (el fondo, en `proponer-especialista.md` de esta carpeta).

## Reglas
- Ningún cron aplica cambios de fondo sin OK (regla B).
- Cada cron deja constancia de lo que hizo en el registro (`../registro.md`).
- Las rutas de arriba son relativas al arnés: tras instalar valen igual, porque la fuente viaja con el agente (ver `INSTALAME.md`, punto 3 del método general).
- Si un cron no tiene material (sin fallos, sin feedback), no fuerza cambios: ese ciclo no hace nada.
- Las cadencias son orientativas: súbelas o bájalas según cuánto vive el agente.
