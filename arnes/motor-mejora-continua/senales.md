# Señales — de dónde aprende el agente

El motor no inventa mejoras: las saca de tres fuentes reales. Si una fuente no tiene material, ese cron no hace nada ese ciclo.

## 1. Fallos
- **De dónde:** el registro del agente (pieza 08; semilla `../registro.md`, existe desde el arranque). Los fallos quedan marcados ahí con la palabra FALLO.
- **Qué produce:** casos de eval nuevos a partir de errores reales, y a veces un aprendizaje para la base de conocimiento.
- **Riesgo:** bajo. Añadir un eval o anotar un aprendizaje es seguro → se auto-aplica (regla B).

## 2. Feedback
- **De dónde:** el buzón donde el responsable del arnés deja correcciones (semilla `../buzon-feedback.md`, existe desde el arranque). Si el blueprint define otro canal (Telegram, chat, etc.), lo que llegue por ahí se vuelca al buzón antes de procesarse.
- **Qué produce:** ajustes de comportamiento. Si son de tono/criterio menores → seguros. Si tocan el soul o los permisos → propuesta.

## 3. Novedades del nicho
- **De dónde:** un sensor a medida que escanea el dominio del agente (qué es "novedad" lo define la pieza 11 / el blueprint para ESTE agente). El sensor es **opcional con criterio**: si el blueprint decide que este agente no tiene nicho que vigilar, no se genera y esta señal no aplica (y el `cron-nicho` no se registra).
- **Qué produce:** entradas nuevas en la base de conocimiento, y a veces la idea de una skill o un especialista nuevo.
- **Riesgo:** anotar conocimiento es seguro; crear skill/especialista es gordo → propuesta.

## Qué hace el sensor de dominio (la parte a medida)
El motor genérico sabe "recoge novedades del nicho", pero no sabe QUÉ es el nicho. Eso lo aporta un sensor generado con skill-creator durante la construcción:
- Para un agente de contenido: escanear tendencias, formatos, qué funciona.
- Para un agente de ventas: escanear deals perdidos, objeciones nuevas.
- Para el agente que sea: lo que su destino necesite vigilar.
