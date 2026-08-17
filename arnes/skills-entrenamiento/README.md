# Skills de entrenamiento continuo

Las skills que ejecutan los crons del motor de mejora continua. Aquí se ve la decisión de diseño clave: **motor genérico + sensores a medida**.

## Motor genérico (igual para todos)
El ciclo recoger-señal → regla B → escribir en fuente → recompilar es el mismo para cualquier agente. Estas skills vienen con la fábrica como **esqueletos funcionales**, una carpeta por skill:
- `recoger-fallos/` — lee el registro (`../registro.md`), hace evals de los errores.
- `revisar-feedback/` — procesa el buzón (`../buzon-feedback.md`), aplica o propone.
- `proponer-enriquecimiento/` — empaqueta un cambio gordo como propuesta para el OK del responsable del arnés.
- `proponer-especialista/` — empaqueta un mini-blueprint de agente nuevo.

Los esqueletos funcionan tal cual (los crons pueden llamarlos desde el arranque), pero cada uno lleva una sección **"A medida de este agente"** marcada con "GENERAR AQUÍ": en la Fase 3 (construcción), el entrenador la completa para el destino concreto. No des el motor por activo sin haber revisado las cuatro.

## Sensores a medida (uno por agente, generados con skill-creator — opcional con criterio)
Lo que cambia entre un agente de contenido y uno de ventas es QUÉ miran. Eso se genera durante la construcción con la skill `skill-creator`, a partir del destino del agente:
- Qué fuentes escanea.
- Qué cuenta como "novedad del nicho".
- Qué es una "mejora buena" para su fin.

El sensor es **opcional con criterio**: si el nicho del agente no tiene novedades que vigilar (o el blueprint decide que no compensa), no se genera — y entonces el `cron-nicho` no se registra. Lo que no vale es registrar el cron sin sensor: llamaría a algo que no existe.

## Cómo se generan los sensores
En la Fase 3 (construcción), tras rellenar la pieza 11:
1. Lee el destino del agente y su nicho (intake + blueprint).
2. Decide si compensa (opcional con criterio; el porqué queda en el blueprint).
3. Si sí: usa `skill-creator` para escribir la skill-sensor a medida, en su carpeta aquí dentro.
4. Engánchala al `cron-nicho` correspondiente.
5. Verifícala como una pieza más (cuándo está bien).

Así el motor no se reinventa nunca, pero cada agente se enriquece mirando lo suyo.
