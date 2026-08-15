"""Agente Interno: chat read-only para el equipo admin.

Responde preguntas en lenguaje natural sobre el estado del sistema
(conversaciones, contactos, canales, uso del bot). NO escribe en BD,
NO accede al bot publico, NO modifica nada.

Componentes:
- service.py: leer/guardar config singleton
- prompts.py: system prompt
- tools.py: registry de tools read-only
- budget.py: rate limit por usuario admin + tope mensual
- agent.py: loop tool-use con streaming SSE
"""
