# Non-Functional Requirements — Chatbot

## Performance

- **Latencia respuesta del agente:** <15s end-to-end (desde llegada del webhook hasta envío de respuesta a WhatsApp) en p95 con carga normal.
- **Latencia transcripción audio:** <10s para audios <60s de duración.
- **Latencia búsqueda RAG:** <500ms por query.
- **Concurrencia objetivo:** 30 conversaciones simultáneas sin degradación significativa.
- **Latencia WebSocket:** mensajes nuevos visibles en el inbox <1s desde envío.
- **Throughput webhook YCloud:** capaz de absorber ráfagas de 100 mensajes/min sin pérdida (vía cola Celery).

## Security

- **Autenticación:** JWT HS256 con `JWT_SECRET` mínimo 256 bits. Expiración 24h. Passwords bcrypt cost ≥12.
- **Autorización:** RBAC con dos roles (admin, cliente). Cada endpoint declara su requirement. Tests unitarios sobre autorización.
- **Cifrado credenciales:** Fernet con `ENCRYPTION_KEY` en `.env`. Nunca expuestas en logs ni respuestas API.
- **Validación firma webhook YCloud:** HMAC SHA256 con `YCLOUD_WEBHOOK_SECRET`. Webhook sin firma válida → 401.
- **Rate limiting:**
  - `/auth/login`: 5 req/min por IP
  - Webhook: 100 req/min por IP
  - Resto autenticados: 300 req/min por usuario
- **CORS:** restringido a dominio del frontend.
- **HTTPS obligatorio:** EasyPanel + Let's Encrypt.
- **Protección prompt injection:** sección dedicada en system prompt + validación de salida del agente. Heredada de la primera versión del sistema.
- **PII fuera de logs:** filtro centralizado en `core/logging.py` que enmascara teléfonos, emails y mensajes completos.
- **Niveles de credenciales:** Level 1 (.env), Level 2 (deployment), Level 3 (admin panel cifradas en DB).
- **Backups:** dump diario de Postgres a volumen separado. Retención 30 días.

## Accessibility

- **WCAG 2.1 AA** en todo el panel.
- **Navegación teclado:** todos los interactivos accesibles con `Tab`. Foco visible.
- **Lectores de pantalla:** live regions para mensajes nuevos. Etiquetas ARIA en componentes custom.
- **Contraste:** mínimo 4.5:1 en texto normal, 3:1 en texto grande.
- **Atajos:** `/` enfoca buscador, `↑↓` navega lista, `Esc` cierra modals.

## Observability

- **Logging:** structlog con formato JSON. Niveles: DEBUG, INFO, WARNING, ERROR.
- **Correlation ID:** cada request HTTP / tarea Celery / mensaje WhatsApp lleva un `request_id` o `trace_id` para trazar el flujo completo.
- **Errores:** capturados centralizadamente. Últimos 100 visibles en `/admin/health`.
- **Métricas:** conteo de mensajes procesados, tareas Celery (en cola / completadas / fallidas), tiempo medio de respuesta, llamadas a OpenAI, tokens consumidos. Visibles en dashboard cliente y en `/admin/health`.
- **Alertas (opcional Fase 1):** notificación a Telegram si Celery tiene >50 tareas en cola o >5 fallos en 5 minutos.

## Scalability

- **Crecimiento esperado:** ~5-10 mensajes/min en pico para un cliente típico (un comercio pequeño). Para clientes grandes, hasta 50/min.
- **Estrategia escalado:** vertical (más vCPU al VPS) en primera instancia; añadir más workers Celery si la cola se acumula. Postgres puede subir a réplica de lectura si hace falta (Fase 3+).
- **Volumen DB:** ~10MB/mes de mensajes para cliente típico. Para 1000 clientes activos: ~10GB/mes, manejable con Postgres estándar.
- **Storage audios:** estimado ~50KB por audio, ~100 audios/mes para cliente típico = 5MB/mes. Negligible.

## Reliability

- **Uptime objetivo:** 99% (Fase 1 — VPS único, sin redundancia activa).
- **Recuperación de fallos:** Celery con retry exponential backoff (3 intentos) y dead-letter queue. Tareas fallidas reintentables manualmente desde panel admin.
- **Idempotencia:** webhook YCloud puede recibir duplicados — procesamiento idempotente por `message_id` de YCloud.
- **Graceful shutdown:** workers Celery esperan a terminar tareas en curso antes de cerrarse (max 30s).

## Maintainability

- **Tests:**
  - Backend: pytest con cobertura mínima 70% en código nuevo. Tests para tools del agente, providers, endpoints críticos (auth, conversations, kb).
  - Frontend: Vitest + Testing Library para componentes interactivos clave (ChatView, MessageBubble, formularios).
- **Linting:** ruff + black (Python), eslint + prettier (TS).
- **Type checking:** mypy estricto en backend. TypeScript strict en frontend.
- **Documentación:** OpenAPI auto-generada por FastAPI. Storybook opcional para componentes UI (Fase 2).

## Compliance

- **RGPD:** todos los datos personales (mensajes, contactos, audios) se manejan según el Reglamento. El cliente final firma DPA con sus usuarios. La app facilita:
  - Exportación de todos los datos de un contacto
  - Borrado completo de un contacto y todos sus mensajes/audios
  - Aviso visible al subir documentos sobre datos personales
- **Cookies / Analytics:** el panel no usa analytics de terceros. Solo cookie de sesión necesaria.
- **Retención:** configurable (futuro Fase 2). Default sin caducidad automática.
