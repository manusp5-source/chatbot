---
name: proponer-enriquecimiento
description: Empaqueta un cambio de fondo del arnes (soul, permisos, skill nueva) como propuesta para el OK del responsable del arnes. No aplica nada por su cuenta. La llaman las otras skills del motor cuando detectan algo gordo.
---

# proponer-enriquecimiento — skill de entrenamiento (motor genérico)

No tiene cron propio: la llaman `recoger-fallos`, `revisar-feedback` o el sensor de dominio cuando una señal pide un cambio de fondo (regla B, ver `../../motor-mejora-continua/regla-B.md`). Esqueleto funcional que viene con la fábrica; al montar el arnés (Fase 3), el entrenador revisa y completa la sección "A medida de este agente".

## Pasos
1. Redacta la propuesta como mini-blueprint, con estos campos:
   - **Qué cambia** (pieza afectada y texto propuesto).
   - **Por qué** (la señal que lo origina: fallo, feedback, novedad).
   - **Riesgo** y **cómo se revierte**.
   - **Qué evals lo verificarían** si se aprueba.
2. Deja la propuesta anotada como pendiente en `../../buzon-feedback.md` (entrada `PENDIENTE | motor | propuesta: ...`) y en `../../registro.md`.
3. Avisa al responsable del arnés (definido en `../../00-INTAKE.md`, sección 0) por su canal de avisos.
4. **NO apliques nada hasta su OK.** Con el OK: aplica en la fuente neutral, recompila, corre los evals (juez aparte) y guarda la ejecución en el histórico de `evals.json`.
5. Si el responsable lo rechaza, márcalo PROCESADO con el motivo y no insistas con la misma propuesta.

## A medida de este agente

**Canal de aviso al responsable:** la propia sesión de trabajo. La propuesta se escribe en
`buzon-feedback.md` y en `registro.md` del arnés, y se lee al abrir sesión. **No se manda al
canal de Telegram del chatbot**: ese es el canal de alertas de producción de un cliente
(`[SEGURIDAD] …`), y mezclar ahí propuestas de diseño hace que se dejen de leer las dos cosas.

**Umbral de "cambio de fondo" en este agente.** Además de lo genérico (soul, permisos, skill
nueva), aquí cuenta como fondo:

- Cualquier cambio en las **cinco reglas duras** de la pieza 01, incluido suavizar una.
- Cualquier cambio en `tools_enabled` de un agente, en cualquier dirección.
- **Cualquier propuesta que exija tocar código del producto.** Es el caso más frecuente en
  este destino, porque la mitad de los límites viven en código. La propuesta tiene que
  decirlo en la primera línea: *"esto no se arregla en el arnés"*.
- Cualquier cosa que afecte a los **tres NO de `OFERTA.md`**. Eso no es diseño de agente: es
  producto vendido, y cambiarlo cambia lo que se prometió por contrato.

**Campo obligatorio extra en la propuesta**, además de los cinco genéricos:

> **¿Se arregla en el arnés o en el producto?** — y si es en el producto, qué fichero y qué
> test lo cubriría. Sin eso, la propuesta no se puede evaluar: parece un cambio de prompt y
> es un cambio de código.

**Precedente vivo:** el caso `L9` (revelación determinista de que es una IA en la primera
interacción, art. 50 del AI Act). Está redactado exactamente con este formato y sigue
esperando decisión. Úsalo de plantilla.
