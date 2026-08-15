# Rediseño del Dashboard → Home principal

Enfoque: **dashboard genérico del producto, adaptable
por negocio**. Implementación en **sesión dedicada** (esta fue de planificación).

El diseño de referencia es el que implementa el propio panel; la guía de estilo está en `design/style_guide.md`.

> **Sin tiempo real (WebSocket).** Para un operador único
> con un bot que responde solo, el "wallboard en vivo" (colas, visitas web ahora,
> latencia, traza siempre corriendo) es vanidad y sobre-ingeniería. La Home será
> **"viva por polling" (~30s)**, indistinguible para este caso y mucho más simple.
> La señal que importa ("¿hay alguien esperando que el bot no atendió?") ya la da
> `/dashboard/attention`. La traza en vivo se queda **on-demand** donde ya vive:
> traza por conversación + `/admin/system/logs`. **Se elimina la Fase 3 (WebSocket).**

## Objetivo

Convertir el dashboard en la **pantalla de inicio** del operador: visión "de un
vistazo" del estado del negocio + accesos rápidos a la operación. **Adaptable**:
las zonas específicas (citas/agenda) solo aparecen si el negocio usa esa
capacidad (calendario conectado).

## Layout (6 zonas del prototipo)

1. **Hero** — saludo + resumen en lenguaje natural + tarjeta **"Estado actual"** (bot activo/pausado por canal, handoffs pendientes ahora, conversaciones hoy). Refrescada por **polling**, no WebSocket.
2. **KPI band (4)** — conversaciones hoy (vs ayer), % resueltas por IA, [métrica de negocio configurable], coste/uso LLM. Con tendencia.
3. **Actividad** — gráfico de área "conversaciones por hora" (hoy vs media 7d) + donut "canal de origen".
4. **Necesitan atención** — handoffs pendientes + conversaciones sin respuesta > N min.
5. **Salud del sistema** + acceso **on-demand** a la traza (por conversación) y a `/admin/system/logs`. Sin widget de traza en vivo.
6. **Ticker** de novedades — opcional, baja prioridad.

## Adaptable por negocio (clave de la decisión)

- **Zonas universales** (1, 2 parcial, 3, 4, 5, 6): para cualquier cliente.
- **Citas/Agenda e "ingreso por citas"**: SOLO si el negocio tiene calendario
  conectado. Para tu proyecto (sin citas): ocultarlas o sustituir por
  métricas de comunidad (registros nuevos, conversiones de gratis a pago, miembros).
- El **4.º KPI** y la **métrica de negocio** deben ser condicionales/configurables.

## Datos: qué hay vs. qué falta

**Ya disponible (endpoints):** uso de tokens/coste, estado de pausa, salud de
canales, traza (`agent_trace_event`), conversaciones (lista + filtros por canal).

**Creado (Fase 1, ✅):**
- `GET /admin/dashboard/summary` — KPIs agregados (hoy/ayer, % resueltas por IA,
  derivadas), serie por hora (hoy vs media 7d), mix por canal.
- `GET /admin/dashboard/attention` — handoffs pendientes + sin respuesta > N min.

**Estado actual (Hero):** se compone con datos **ya existentes y baratos** vía
polling — `/admin/agent/pause` (bot por canal), `/dashboard/attention` (handoffs
ahora) y `/dashboard/summary` (hoy). Sin endpoints de "en vivo" nuevos.

## Routing

- Hacer esta Home la **ruta de inicio** del operador (`/` o `/admin` → home).
- El dashboard de agente actual (`AgentDashboard.tsx`: pausa global/canal, budget,
  demo whitelist) **no se tira**: se integra como sección o se enlaza.

## Fases

1. ✅ **Backend**: endpoints de agregados (`summary` enriquecido + `attention`) + `services/dashboard.py` + setting `DASHBOARD_TIMEZONE`. Tests 39/39.
2. ✅ **Frontend**: `AdminHome.tsx` con layout Coro (6 zonas), datos reales de los endpoints, **refresco por polling (~30s)**. Gráficos SVG a mano (área + donut).
3. **Adaptabilidad** (parcial): ya es genérico (sin citas/ingreso hardcodeado; usa métricas disponibles). Falta que la métrica de negocio del 4.º KPI sea configurable por negocio.
4. ✅ **Routing**: Home como inicio del admin (operador → inbox) + entrada "Inicio" en el menú lateral.
5. **Verificación E2E** + seed con datos realistas para que todas las zonas rendericen.

> ~~Fase WebSocket "en vivo"~~ — **eliminada** (decisión 2026-06-02, ver arriba).

## Notas

- Respetar el sistema **Coro** (`design/style_guide.md`). El prototipo ya lo usa.
- El prototipo dice "GPT-4o"; el sistema real usa **GPT-5.4 mini** → ajustar.
- Los prototipos de partida traían datos de ejemplo de un negocio con agenda (citas, profesionales): son solo de
  ejemplo del caso piloto; el diseño real es genérico.
