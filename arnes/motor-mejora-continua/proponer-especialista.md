# Proponer un especialista — la segunda salida del motor

El motor de mejora continua tiene dos salidas: "me mejoro a mí mismo" y "propongo un especialista para mi equipo". Esta es la segunda, y es lo que hace al sistema recursivo: la fábrica llamándose a sí misma.

## Cuándo se dispara
Cuando el agente vivo detecta **trabajo recurrente que se serviría mejor con un dedicado**: una tarea que aparece mucho, que tiene su propio oficio, y que estira demasiado al agente generalista.

> Ejemplo: un agente de contenido que cada semana acaba haciendo guiones de reels nota que eso es un oficio en sí. Propone un especialista solo en guiones.

## Qué propone (no construye)
Un **mini-blueprint** del especialista:
- Su fin en una frase.
- Las skills que necesitaría.
- Los crons que tendría.
- Por qué un dedicado lo hace mejor que el generalista.

## Quién aprueba
El responsable del arnés (definido en `../00-INTAKE.md`, sección 0). Crear un agente nuevo es lo más gordo que hay → siempre va a propuesta (regla B), nunca automático.

## Quién lo construye
La **misma fábrica**, con su flujo normal: intake → blueprint → OK → rellenar piezas → evals → compilar/instalar. El especialista nace en `agentes/<especialista>/` como arnés completo.

## Cómo se conecta
Al aprobarse, la **pieza 03 (Equipo)** del agente padre se actualiza para apuntar al especialista y definir cómo se coordinan (quién pide qué, quién verifica a quién).

## Regla
No hay mecanismo nuevo: es el flujo de siempre, disparado por el agente en vez de por una persona. Mismo rigor, misma parada en el blueprint, mismos evals.
