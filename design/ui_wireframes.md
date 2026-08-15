# UI Wireframes — Chatbot

## Navigation Structure

**Top bar global** (siempre visible): logo Chatbot · selector tema light/dark · avatar usuario + menú (perfil, cerrar sesión).

**Side nav según rol:**

### Rol cliente
- Inbox (default al entrar)
- Contactos
- Etiquetas
- Base de Conocimiento
- Dashboard

### Rol admin
- Usuarios
- Credenciales
- Prompt del agente
- Configuración modelo
- Notificaciones
- Audit log
- Salud del sistema

Switcher de rol en top bar si el usuario tiene ambos (raro pero posible). Por defecto cada login va a su panel.

---

## Pantallas — Rol cliente

### Inbox `/inbox`

**Layout 3 columnas:**

**Columna izquierda (320px) — Lista de conversaciones:**
- Buscador arriba (por nombre, teléfono, contenido)
- Filtros tipo chips: `Todas` `Bot` `Humano` `Cerradas` `No leídas`
- Filtro etiquetas (multi-select)
- Lista de items:
  - Avatar circular (inicial nombre)
  - Nombre / teléfono
  - Último mensaje (truncado 1 línea)
  - Hora (relativa: "hace 5min")
  - Badges: contador no leídas, chip "humano" si aplica, chips etiquetas (max 2 visibles)
- Scroll infinito o paginación

**Columna central (flex 1) — Vista chat:**
- Header: nombre contacto + estado conversación (bot/humano/cerrada) + acciones (cerrar, archivar)
- Stream de mensajes:
  - Burbujas alineadas izquierda (user) y derecha (assistant/operator)
  - Color distinto entre assistant (azul claro) y operator (verde)
  - Audios con reproductor inline (waveform + play) + transcripción colapsable debajo
  - Timestamps por agrupación temporal
  - Separadores por día
- Caja respuesta abajo (solo si estado = humano):
  - Textarea (autosize)
  - Botón enviar
  - Si fuera de ventana 24h: caja desactivada con aviso "Han pasado más de 24h desde el último mensaje del usuario. Espera a que escriba o usa una plantilla (próximamente)."

**Columna derecha (320px) — Ficha contacto:**
- Avatar + nombre + teléfono
- Email, estado (badge editable inline)
- Etiquetas (chips con add/remove)
- Notas internas (textarea, autosave)
- Botones de acción: `Tomar control` / `Devolver al bot` / `Cerrar conversación`
- Historial: "X conversaciones previas" → link

**Estado vacío:** cuando no hay conversaciones, dibujo + texto "Aquí aparecerán las conversaciones de WhatsApp en cuanto entren."

### Contactos `/contacts`

- Tabla con búsqueda, filtros (estado, etiqueta, origen), paginación
- Columnas: nombre, teléfono, email, estado, etiquetas, último mensaje, acciones
- Click en fila → modal/panel lateral con ficha completa editable
- Botón "Nuevo contacto" arriba derecha
- Bulk actions: añadir etiqueta a selección, exportar CSV

### Etiquetas `/tags`

- Lista simple: nombre + color + nº contactos
- Botón crear etiqueta → modal con input nombre + color picker
- Editar / borrar inline

### Base de Conocimiento `/knowledge-base`

- Header con uploader (drag & drop o botón "Subir documento")
- Lista de documentos:
  - Icono según formato (PDF, DOCX, CSV, etc.)
  - Nombre
  - Tamaño + fecha subida
  - Status badge: `Procesando…` (spinner), `Indexado` (verde, X chunks), `Error` (rojo, click muestra detalle)
  - Acciones: borrar, re-indexar
- Panel "Probar la base de conocimiento" (collapsible):
  - Input pregunta
  - Botón "Probar"
  - Resultados: chunks devueltos con similarity score y origen (qué documento)

### Dashboard `/dashboard`

- Selector rango: 7d / 30d / 90d
- Cards arriba: conversaciones totales, leads, tasa derivación, tiempo medio respuesta
- Gráficos:
  - Línea: conversaciones por día
  - Donut: distribución de estados (bot/humano/cerrada)
  - Barras horizontales: top 10 etiquetas
- Tabla bottom: top contactos por actividad

---

## Pantallas — Rol admin

### Usuarios `/admin/users`

- Tabla: email, rol, nombre, activo, último login, acciones
- Crear usuario (modal)
- Editar (modal): cambiar rol, activar/desactivar
- Resetear contraseña (envía email o muestra temporal)

### Credenciales `/admin/credentials`

- Lista de claves agrupadas por proveedor:
  - YCloud: api_key, webhook_secret, phone_number
  - OpenAI: api_key
  - Resend: api_key, from_email
  - Telegram: bot_token, chat_id
  - Slack: webhook_url
- Cada item: nombre, valor enmascarado (`sk-...***...abc`), botón editar, botón "Probar conexión"
- Edit: input password con toggle visibilidad + guardar
- Probar conexión: spinner → resultado ok/error

### Prompt del agente `/admin/agent/prompt`

- Editor texto grande (monospaced, syntax highlight markdown si aplica)
- Sidebar con historial de versiones (fecha, autor, nº caracteres) + botón "Restaurar"
- Botón "Guardar como nueva versión" (no activa por defecto)
- Botón "Activar esta versión"
- Preview del comportamiento esperado (collapsible)

### Configuración modelo `/admin/agent/config`

- Form:
  - Modelo (dropdown: GPT-5.4 mini, GPT-5 mini, GPT-5 nano, custom)
  - Temperature (slider 0-1)
  - Max tokens (number)
  - Buffer mensajes (segundos)
  - Context window (mensajes)
  - Max partes de respuesta
- Botón guardar (crea nueva versión de `agent_config`)

### Notificaciones `/admin/notifications`

- Toggle Telegram on/off + chat_id
- Toggle Slack on/off + webhook
- Test "Enviar notificación de prueba"

### Audit log `/admin/audit`

- Tabla con filtros: usuario, acción, entidad, rango fechas
- Columnas: fecha, usuario, acción, entidad, IP, ver detalle (modal con before/after JSON)

### Salud del sistema `/admin/health`

- Cards: DB (latencia ms, conexiones activas), Redis (memoria, keys), Celery workers (cantidad, tareas en cola, failed)
- Lista últimos errores (top 20) con stack trace expandible
- Botón "Reintentar tareas fallidas"

---

## User Flows clave

### Conversación nueva entrante (visión cliente)

1. Cliente final envía WhatsApp → llega al webhook
2. Inbox del operador: nueva conversación aparece arriba con badge "no leída", suena ding suave (si tab tiene foco)
3. Operador hace click → ve el chat, ve respuesta del bot ya enviada (si bot pudo responder)
4. Si el bot ha derivado: la conversación viene con estado "humano" y notificación adicional en Telegram

### Derivación manual desde el panel

1. Operador en una conversación en estado `bot`
2. Click "Tomar control" → estado pasa a `humano`, el bot deja de responder
3. Operador escribe en la caja respuesta y envía → mensaje llega al cliente WhatsApp
4. Cuando termina: click "Devolver al bot" o "Cerrar conversación"

### Subir documento a la KB

1. Cliente sube PDF en `/knowledge-base`
2. Item aparece con status "Procesando…"
3. Tarea Celery chunkea + embedde
4. Status cambia a "Indexado" + nº chunks
5. Cliente prueba con "Probar la base de conocimiento" → ve qué chunks devuelve

### Cambiar prompt del agente

1. Admin entra a `/admin/agent/prompt`
2. Edita el texto
3. Guarda como nueva versión (no activa)
4. Revisa en historial → cuando esté seguro, activa
5. Próxima conversación que entre usa la nueva versión

---

## Responsive

- Desktop (1024+): layout 3 columnas inbox
- Tablet (768-1023): 2 columnas (lista + chat); ficha contacto en drawer
- Mobile (<768): 1 columna con navegación entre lista / chat / ficha contacto (Fase 1 mobile-friendly pero el inbox real está pensado para desktop)

## Accesibilidad

- Contraste mínimo AA en todo el panel
- Foco visible en todos los interactivos
- Atajos de teclado: `/` enfocar buscador, `↑↓` navegar conversaciones, `Enter` abrir, `Esc` cerrar drawers
- Live region para nuevos mensajes (lectores de pantalla)
