---
name: proponer-especialista
description: Detecta trabajo recurrente que pide un agente dedicado y empaqueta un mini-blueprint de especialista para el OK del responsable del arnes. No construye nada. La ejecuta el cron-equipo.
---

# proponer-especialista — skill de entrenamiento (motor genérico)

La dispara `cron-equipo` (ver `../../motor-mejora-continua/crons.md`). El fondo del asunto está en `../../motor-mejora-continua/proponer-especialista.md`: léelo antes de proponer. Esqueleto funcional que viene con la fábrica; al montar el arnés (Fase 3), el entrenador revisa y completa la sección "A medida de este agente".

## Pasos
1. Revisa `../../registro.md`: ¿hay una tarea que aparece mucho, con oficio propio, que estira demasiado a este agente generalista?
2. Si no la hay, este ciclo termina sin cambios. No fuerces especialistas.
3. Si la hay, redacta el mini-blueprint del especialista:
   - Su fin en una frase.
   - Las skills que necesitaría.
   - Los crons que tendría.
   - Por qué un dedicado lo hace mejor que el generalista.
4. Deja la propuesta pendiente en `../../buzon-feedback.md` y en `../../registro.md`, y avisa al responsable del arnés. Crear un agente nuevo es lo más gordo que hay (regla B): **siempre propuesta, nunca automático**.
5. Con el OK: el especialista lo construye la misma fábrica con su flujo normal (fases 0–5), y la pieza 03 (Equipo) del agente padre se actualiza para apuntar al nuevo.

## A medida de este agente

**Qué señal indicaría trabajo recurrente aquí:** un mismo tipo de conversación que acaba
siempre en derivación, en varias instalaciones, y que tiene oficio propio — no que sea
difícil, sino que sea *otro trabajo*.

**Especialistas ya descartados. No los vuelvas a proponer sin que cambie el hecho de base.**

| Candidato | Por qué NO |
|---|---|
| Anti no-show | Requiere leer la agenda del centro (el PMS). No hay integración |
| Rescate de agenda | Igual, más la lógica de encaje del hueco |
| Lista de espera | Igual |
| Revisiones periódicas | Requiere el histórico de tratamientos |
| Seguimiento de presupuestos | Requiere el presupuesto del PMS |
| Vigilancia de reseñas | Requiere integración con Google Business |

Los cinco primeros comparten el mismo hecho: **necesitan datos del programa de gestión de la
clínica, no del chatbot.** Por eso siguen siendo trabajo de integración por cliente y no
producto (`OFERTA.md`). Proponer uno desde aquí sería inventar una integración que no existe,
y el `CLAUDE.md` de la agencia lo prohíbe por escrito.

**Qué tendría que cambiar para reabrirlo:** que exista la integración con el PMS, o que tres
clientes pidan lo mismo — que es el criterio que `OFERTA.md` fija para que algo a medida pase
a producto. Antes de eso, este cron termina sin cambios y no pasa nada.

**El agente interno del panel NO es un especialista de este agente.** Es otro arnés, con su
propio presupuesto y su propio blindaje. No se coordinan y no se proponen el uno al otro.
