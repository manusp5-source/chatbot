# Stack Selection — Chatbot

## Selecciones

| Capa | Tecnología | Razón |
|---|---|---|
| Backend | Python 3.12 + FastAPI | Ecosistema IA maduro, async I/O, OpenAPI gratis, más fácil de leer para perfiles no programadores |
| ORM | SQLAlchemy 2.x (async) | Estándar, soporte async, juega bien con FastAPI |
| Migraciones | Alembic | Estándar SQLAlchemy, versionado limpio |
| Validación | Pydantic v2 | Integrado con FastAPI, validación rápida y declarativa |
| Cola de tareas | Celery 5.x | Probado a escala, integración Redis sólida |
| Broker / Cache | Redis 7 | Cola, buffer mensajes, pub/sub interno si hace falta |
| Base de datos | PostgreSQL 16 | Sólido, fiable, una sola DB para todo |
| Vector store | pgvector | Sin servicio extra, búsqueda híbrida en SQL |
| LLM principal | OpenAI GPT-5.4 mini | Tool use maduro, ecosistema único con embeddings y Whisper |
| Embeddings | OpenAI `text-embedding-3-small` | Baratos, calidad muy buena (1536 dims) |
| Transcripción | OpenAI Whisper API | Estándar para audio WhatsApp |
| WhatsApp | YCloud (abstraído) | Ya dominado, setup rápido, plantillas y contactos gestionados |
| Email | Resend | API simple, DX cuidada, free tier suficiente para start |
| Notificaciones | Telegram Bot API + Slack Incoming Webhooks | Lo que ya se usa en el ecosistema actual |
| Auth | JWT (HS256) + bcrypt | Estándar para apps single-tenant |
| Cifrado credenciales | Fernet (cryptography) | Simple, autenticado, suficiente para credenciales de proveedores |
| Frontend | React 18 + TypeScript + Vite | Estándar moderno, dev experience rápida |
| Estado frontend | Zustand | Ligero, sin boilerplate, suficiente para esta complejidad |
| Estilos | Tailwind CSS + shadcn/ui | Diseño rápido y consistente, accesible |
| Routing | React Router v6 | Estándar |
| WebSocket cliente | Nativo del navegador (con reconnect manual) | Sin librería extra |
| Tests backend | pytest + pytest-asyncio + httpx | Estándar Python async |
| Tests frontend | Vitest + Testing Library | Estándar Vite |
| Contenedores | Docker + Docker Compose | Estándar del proyecto |
| Deploy | EasyPanel | Decisión del equipo — gestiona reverse proxy, SSL, dominios |

## Alternativas consideradas

### Backend — Python vs Node vs Bun

- **Node + TypeScript + BullMQ**: mejor I/O paralelo pero ecosistema IA va por detrás de Python. SDK Anthropic / LangChain / Whisper salen primero en Python. Python es más fácil de leer para quien no programa a diario.
- **Bun + Hono + BullMQ**: muy rápido, runtime moderna, pero bleeding edge. No para producción de cliente.

### RAG — pgvector vs servicios dedicados

- **Supabase**: heredado de la primera versión. Mismo motor (pgvector) pero con servicio externo + facturación + setup extra por cliente.
- **Pinecone / Qdrant**: vector search dedicado, más potente a gran escala. Innecesario para volumen Fase 1, añade servicio externo y coste.

### CRM — nativo vs Airtable

- **Airtable**: UI gratis y vistas tipo Kanban, pero dependencia externa, coste por seats, setup extra por cliente. El cliente final no quiere otra herramienta.

### WhatsApp — YCloud vs Meta vs Twilio

- **Meta Cloud API directo**: más barato a escala, control total, pero setup laborioso (Business Manager, verificaciones) — más fricción al montarlo.
- **Twilio**: muy fiable, docs perfectas, pero más caro y sin valor diferencial vs YCloud.
- **Wati / Wapichat**: similares a YCloud, no aportan vs lo conocido.

### LLM — OpenAI vs Anthropic

- **Claude (Anthropic)**: excelente tool use y razonamiento. Descartado por preferir un único proveedor de IA (LLM + embeddings + Whisper todo OpenAI), lo que simplifica monitoreo, facturación y debug.

## Decisiones por defecto

- Autenticación: JWT + bcrypt → mantenido
- AI integration: SDK oficial OpenAI → mantenido como provider principal
- UI: Glassmorphism con light/dark mode, responsive, accessible → se sigue
- Credenciales: 3 niveles (.env → deployment → admin panel encrypted) → respetado
