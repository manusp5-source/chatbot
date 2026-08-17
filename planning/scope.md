# Alcance — Chatbot

Reconstruido a partir del código el 17 de agosto de 2026. No es el alcance que
se planeó en su día: es el que la aplicación tiene hoy. Cuando el código y este
documento discrepen, manda el código.

Los requisitos numerados están en [`requirements.md`](requirements.md); aquí solo
se dice qué entra, qué no, y por qué.

---

## Dentro

### Canales de entrada

Cinco, todos con su proveedor detrás de una interfaz en `backend/app/providers/`:

| Canal | Proveedor | Dónde |
|---|---|---|
| WhatsApp | YCloud y Meta, con selector | `providers/whatsapp/` |
| Instagram DM | Meta | `providers/instagram/` |
| Correo | Gmail (entrada por sondeo) y SMTP/Resend (salida) | `providers/gmail/`, `providers/email/` |
| Chat web | propio | `api/webchat.py` |
| Voz | Retell | `providers/voice/`, `api/voice.py` |

El requisito original hablaba solo de WhatsApp. Los otros cuatro llegaron después
y están en producción.

### El agente

- Orquestador con *tool use* (`agents/orchestrator.py`) y siete herramientas
  registradas en `agents/tools/registry.py`.
- **Dos clases de agente**: `text` y `voice`. Canal y agente tienen que coincidir
  de tipo.
- **Agente interno** aparte, para el operador, con presupuesto y límite diario
  propios y un blindaje fijo en código contra órdenes escondidas en los mensajes
  que lee (`agents/internal/`).
- Los prompts viven en base de datos y se editan desde el panel. No hay prompts
  en ficheros.

### Base de conocimiento

Documentos (PDF, DOCX, TXT, MD, CSV, XLSX) troceados e indexados en background
con pgvector. Búsqueda **híbrida**: vector más texto completo en español
(`match_chunks_hybrid`). Búsqueda de prueba desde el panel.

### CRM propio

Contactos con teléfono único y BSUID único parcial, notas, etiquetas, actividad,
fusión de fichas duplicadas, importación y exportación CSV, borrado RGPD.

### Inbox del operador

Listado con filtros, vista de chat, WebSocket en tiempo real
(`hooks/useInboxSocket.ts`), ficha lateral, toma de control y devolución al bot,
respuesta manual con la ventana de 24 h de WhatsApp, cierre con resumen por
correo opcional.

### Difusiones

Envío masivo por WhatsApp con plantilla aprobada por Meta, de uno en uno y con
pausas aleatorias, comprobando bajas antes de cada envío. Seguimiento en directo
y cancelable.

### Clasificador de entrada

Aparta spam, boletines y autorrespuestas antes de que lleguen al agente. Dos
pasos: reglas duras primero, LLM solo si ninguna dispara. **Ante error, deja
pasar** — perder un cliente real es peor que tragarse un spam.

### Autoaprendizaje

Detección de huecos de conocimiento y de correcciones, propuestas de edición de
la base de conocimiento y reglas aprendidas, todo revisable desde el panel.

### Panel de administración

Conexiones y credenciales cifradas, agentes y prompts con historial, clasificador,
usuarios, difusiones, copias de seguridad, proveedores y precios de LLM, auditoría,
salud, registros del sistema y listas de bloqueo.

### Coste y observabilidad

Registro de uso por token y por minuto de voz, tope mensual en dólares con pausa
automática del agente, traza paso a paso del agente por conversación, auditoría
de quién hizo qué.

---

## Fuera

| Qué | Por qué |
|---|---|
| **Multi-inquilino** | Es un producto de **un cliente por instalación**: se clona, se configura y se despliega en el servidor de ese cliente. No hay nada compartido entre clientes |
| **Integración con `eskailet-crm`** | Las dos aplicaciones no están integradas. `OFERTA.md` lo dice por escrito y la regla 7 del `CLAUDE.md` del paraguas prohíbe inventarlo. Los contactos del chatbot **no** llegan al CRM |
| **Entrega del código al cliente** | La licencia lo prohíbe. La instalación es suya y los datos son suyos; el programa no |
| **Búsqueda o filtrado por campos cifrados** | `nif`, `direccion`, `notas_internas` y las transcripciones son `bytea` cifrados con Fernet. Postgres no puede mirar dentro: nada de `WHERE`, `LIKE`, `ORDER BY` ni índices sobre ellos |
| **`alembic --autogenerate`** | Inservible en este repositorio: propone borrar cuatro tablas y catorce índices que están bien. Las migraciones se escriben a mano |
| **`pytest` dentro de la imagen de producción** | La imagen no lleva pytest. Las pruebas se lanzan desde `backend/` con el entorno de desarrollo |

---

## Frontera clínica

El agente atiende a pacientes de clínicas. **No opina sobre síntomas, no
diagnostica y no recomienda tratamiento**: deriva. Hoy ese guardarraíl vive en
`backend/app/services/agent_guardrails.py` y en el prompt, y está descrito en
`SECURITY.md`.

Convertirlo en casos de prueba de la categoría `limites` es trabajo de la
**Oleada 5** (arnés del agente, skill `entrenando-agentes`), no de aquí.
