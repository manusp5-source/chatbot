# Motor de mejora continua

Los carriles que hacen que el agente se haga más capaz con el tiempo hacia su destino. El **motor es genérico** (igual para todos los agentes); lo que se genera a medida con skill-creator son los **sensores de dominio** (qué mira cada agente).

El ciclo, siempre el mismo:

```
recoger señal → decidir (regla B) → aplicar lo seguro / proponer lo gordo
   → escribir en la FUENTE NEUTRAL → recompilar
   → (cuando procede) proponer un especialista
```

## Los archivos de este motor
- `senales.md` — de dónde saca el material: fallos (`../registro.md`), feedback (`../buzon-feedback.md`), novedades del nicho.
- `regla-B.md` — qué se auto-aplica solo y qué va a propuesta para el OK del responsable del arnés (definido en `../00-INTAKE.md`, sección 0).
- `crons.md` — los crons concretos, su cadencia y qué skill ejecuta cada uno.
- `proponer-especialista.md` — la segunda salida del motor: hacer crecer el equipo.

Las skills que ejecutan estos crons existen desde el arranque como esqueletos en `../skills-entrenamiento/` (una carpeta por skill); el entrenador las adapta al montar el arnés (Fase 3).

## Reglas de oro del motor
- **Escribe siempre en la fuente neutral y recompila.** Nunca edita el compilado del runtime a mano. Así las plataformas no divergen.
- **Lo de fondo pasa por una persona.** Tocar soul, permisos o crear skill/agente nuevo → propuesta, nunca automático.
- **La memoria del día a día vive en el runtime; las mejoras del arnés vuelven al plano neutral.** Son cosas distintas.
- **Nada se da por mejorado sin evals.** Una mejora que no pasa por verificación no es una mejora.
