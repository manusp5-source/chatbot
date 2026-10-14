# Intent 002 — Aviso de IA determinista en el primer mensaje de texto

**Fecha:** 14 oct 2026 · **Rama:** `feat/gaps-entrevista` · **Tipo:** gap (cierre de L9 mitad B)

## Problema
El art. 50 del Reglamento (UE) 2024/1689 (en vigor desde el 2 ago 2026) obliga a informar
de que se habla con un sistema de IA. Hoy, en los canales de texto, el aviso depende de que
el modelo obedezca `SECURITY_GUARD` regla 6, y esa regla solo le obliga a no negarlo si le
preguntan. `PLATAFORMAS-Y-OPERACION.md` lo llama «el incumplimiento abierto más claro». El
caso L9 del arnés lo registra como `xfail(strict=True)` a propósito.

## Resultado esperado
La primera respuesta del agente en una conversación nueva de **WhatsApp, Instagram y chat
web** empieza por un aviso fijo escrito en código, antes de lo que diga el modelo, una sola
vez por conversación. Sin migración: «primera vez» = no existe ningún mensaje previo del
agente (`MessageRole.assistant`, sin `is_draft`) en esa conversación.

## Fuera de alcance
- Voz (ya saluda de forma determinista en `api/voice.py`).
- Email (el agente solo deja borrador y lo revisa una persona) y modo Entrenamiento
  (nada se envía solo).
- Cambiar los prompts, el guardarraíl clínico o el texto de `SECURITY_GUARD`.
- Hacer el aviso editable desde el panel (queda como mejora posterior).

## Criterio de aceptación (eval)
`test_L9_el_paciente_sabe_que_habla_con_un_sistema_en_la_primera_respuesta` deja de ser
`xfail` y **pasa**, y hay pruebas nuevas que ejecutan el comportamiento (no `grep` del
fuente, lección 2 de `tasks/lessons.md`): primera respuesta lleva el aviso; segunda no;
email y entrenamiento no lo llevan; si ya hubo un mensaje del agente no se repite.
`arnes/evals.json`: el caso L9 pasa a `passes: true` con su evidencia.

## Verificación
Suite completa de `backend/tests` sin regresiones respecto a la línea base.
