# Requirements — Chatbot

## Functional Requirements (Fase 1)

### Mensajería y agente

- **FR-01** — La app recibe mensajes entrantes de WhatsApp vía webhook YCloud con validación HMAC de firma.
- **FR-02** — Mensajes del mismo número que llegan en ráfaga se agrupan con buffer Redis (5s configurables) antes de procesarse.
- **FR-03** — Los audios entrantes se transcriben con OpenAI Whisper. La transcripción se persiste junto al audio original.
- **FR-04** — El agente principal (OpenAI GPT-5.4 mini) procesa la conversación con tool use: `consultar_kb`, `buscar_contacto`, `crear_actualizar_contacto`, `derivar_humano`.
- **FR-05** — La memoria conversacional persiste en Postgres usando el teléfono como `session_id`. Context window configurable (default 20 mensajes).
- **FR-06** — La respuesta del agente puede dividirse en hasta N partes (default 3) enviadas con espera para parecer natural.
- **FR-07** — Cuando el agente decide derivar a humano, la conversación cambia a estado `humano`, el bot se calla, y se notifica al equipo por Telegram y/o Slack.

### Base de Conocimiento (RAG)

- **FR-08** — El cliente puede subir documentos al panel en formatos: PDF, DOCX, TXT, MD, CSV, XLSX.
- **FR-09** — Cada documento se chunkea, se generan embeddings (OpenAI `text-embedding-3-small`) y se almacenan en Postgres con pgvector.
- **FR-10** — El cliente puede borrar documentos. Al borrar, todos sus chunks se eliminan.
- **FR-11** — El cliente puede hacer "búsqueda de prueba" en el panel para ver qué chunks devuelve el RAG ante una pregunta.
- **FR-12** — La indexación se ejecuta en background (Celery). El estado del documento es visible: procesando, indexado, error.

### CRM nativo

- **FR-13** — Cada conversación crea o actualiza automáticamente un contacto en el CRM (por teléfono).
- **FR-14** — El cliente puede editar manualmente datos del contacto: nombre, email, estado, etiquetas, notas internas.
- **FR-15** — El cliente puede crear y editar etiquetas (nombre + color) y asignarlas a contactos.

### Inbox (panel cliente)

- **FR-16** — Lista de conversaciones con búsqueda, filtros (estado bot/humano/cerrada, etiquetas, no leídas) y paginación.
- **FR-17** — Vista de chat tipo WhatsApp Web con burbujas, agrupación temporal, reproductor de audios inline y transcripción debajo.
- **FR-18** — Ficha de contacto lateral con datos, etiquetas editables, notas internas y botones "Tomar control" / "Devolver al bot" / "Cerrar conversación".
- **FR-19** — Caja de respuesta manual del operador cuando la conversación está en humano. Si ha pasado más de 24h desde el último mensaje del usuario, el botón se desactiva con aviso.
- **FR-20** — El inbox se actualiza en tiempo real vía WebSocket (mensajes nuevos, cambios de estado).

### Email resumen

- **FR-21** — Al cerrar una conversación, opción de enviar resumen automático al cliente final por email (Resend).

### Panel admin

- **FR-22** — CRUD de usuarios y asignación de roles.
- **FR-23** — Gestión de credenciales: YCloud, OpenAI, Resend, Telegram, Slack. Cifradas en DB con Fernet. UI con máscara y test de conexión.
- **FR-24** — Editor del prompt del agente con preview, versionado básico y restaurar versión anterior.
- **FR-25** — Configuración del modelo: modelo OpenAI (default GPT-5.4 mini), temperatura, max_tokens, buffer_seconds.
- **FR-26** — Audit log filtrable por usuario, acción, fecha.
- **FR-27** — Panel de salud del sistema: estado servicios (DB, Redis, Celery workers), últimos errores.

## Non-Functional Requirements

Ver `docs/nfr.md` para detalle (concurrencia, latencia, disponibilidad, seguridad).

## User Roles

- **Admin** — desarrollador o responsable técnico que despliega la app. Acceso completo: credenciales, prompts, configuración técnica, audit log, salud del sistema. NO ve el inbox del cliente final.
- **Cliente** — el negocio que usa la app (una clínica, una asesoría…). Acceso a: inbox, contactos, etiquetas, base de conocimiento, dashboard. NO ve credenciales ni configuración técnica.
