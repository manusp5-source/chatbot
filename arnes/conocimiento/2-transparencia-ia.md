# 2 · Transparencia — Reglamento (UE) 2024/1689 (AI Act), art. 50

La norma más reciente que le afecta, y la única que cambió de estado **mientras se montaba
este arnés**.

## Qué obliga

Los sistemas de IA destinados a interactuar directamente con personas físicas tienen que
estar diseñados de forma que **la persona sepa que está interactuando con un sistema de IA**,
salvo que sea obvio para alguien razonablemente atento. La información se da **como muy
tarde en el momento de la primera interacción**.

## Desde cuándo

**Aplicable desde el 2 de agosto de 2026.** Régimen transitorio hasta el **2 de diciembre de
2026** para los sistemas introducidos en el mercado antes de esa fecha; los posteriores
cumplen desde el primer día.

Sanciones por incumplimiento de las obligaciones de transparencia: hasta **15 M€ o el 3 % de
la facturación anual mundial**, la que sea mayor.

## Cómo está cubierto hoy, y qué falta

**Cubierto.** `runtime_config.SECURITY_GUARD` regla 6 obliga al agente a no negar nunca que
es un asistente virtual, y a decirlo con naturalidad si le preguntan. Va antepuesto a cada
llamada al modelo, en todos los canales, y no se puede quitar desde el panel.

**Lo que falta.** Eso es cobertura **reactiva**: cumple *si el paciente pregunta* y *si el
modelo obedece*. La norma pide que lo sepa en la primera interacción, sin preguntar. Y hay
un camino que ni siquiera pasa por el modelo: cuando el guardarraíl clínico responde, el
texto que lee el paciente está escrito en el código y no dice que sea un sistema automático.

→ Eval `L9`, abierto a propósito. Decisión pendiente del responsable del arnés: añadir una
revelación determinista en el primer mensaje (código del producto), dejar `L9` fuera de la
tanda como decisión de producto aparte, o confirmar por escrito que la transitoria del 2 de
diciembre cubre esta instalación y fijar fecha límite.

## Lo que la transparencia NO obliga

- **No** obliga a repetirlo en cada mensaje. Una vez, al principio, basta.
- **No** obliga a revelar el modelo, el proveedor ni el prompt. Al contrario: eso sigue
  prohibido por las reglas 1 y 2 del propio `SECURITY_GUARD`. Se puede reconocer que es
  virtual sin contar nada de dentro.

→ Pieza 01 regla 4 · evals `T1` y `L9`.
