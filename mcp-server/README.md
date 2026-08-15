# Agent-API MCP Server

Servidor **MCP (Model Context Protocol)** *thin client* que expone la **agent-api**
del operador (`/api/v1/agent-api`) como herramientas MCP. Permite que los
asistentes operativos del operador monitoricen la app, revisen/editen la base
de conocimiento (KB/RAG) y mejoren los prompts de los agentes **de forma
remota**.

Es un cliente HTTP fino: **cada herramienta MCP hace UNA llamada a un endpoint de
la agent-api del backend y devuelve su JSON**. No contiene lógica de negocio ni
importa nada del backend; los ámbitos/permisos los aplica el backend a partir del
token de agente.

- Transporte: **Streamable HTTP** (`mcp==1.28.1`, FastMCP).
- Endpoint MCP: `https://<mcp-domain>/mcp`.

## Cómo se conecta un agente

1. Crea un token de agente en el panel: **Conexiones → Tokens de agente** (elige
   nombre y ámbitos). El token (`agt_…`) se muestra **una sola vez**.
2. Configura el cliente MCP para que apunte al endpoint Streamable HTTP:

   ```
   URL:  https://<mcp-domain>/mcp
   Auth: Authorization: Bearer agt_XXXXXXXX
   ```

El servidor reenvía esa cabecera `Authorization` tal cual a la agent-api, así que
**cada agente conectado usa su propio token y sus propios ámbitos**. Si la petición
MCP no trae `Authorization`, se usa el token de la variable de entorno
`AGENT_API_TOKEN` como fallback; si no hay ninguno, la herramienta devuelve un
objeto de error claro.

Los **ámbitos** los enforcea el backend a partir del token:

| Ámbito | Herramientas |
|---|---|
| `monitor:read` | `monitor_overview`, `get_llm_costs`, `get_channels_status`, `get_health` |
| `interactions:read` | `list_interactions`, `get_interaction_metadata` |
| `kb:read` | `list_kb_documents`, `get_kb_document`, `search_kb` |
| `kb:write` | `create_kb_document`, `update_kb_document` |
| `prompts:write` | `list_agents`, `update_agent_prompt` |

## Herramientas expuestas

| Herramienta | Endpoint agent-api | Ámbito |
|---|---|---|
| `monitor_overview(range="7d")` | `GET /monitor/overview?range=` | `monitor:read` |
| `get_llm_costs(range="7d")` | `GET /monitor/llm-costs?range=` | `monitor:read` |
| `get_channels_status()` | `GET /monitor/channels` | `monitor:read` |
| `get_health()` | `GET /monitor/health` | `monitor:read` |
| `list_interactions(status?, canal?, limit=20)` | `GET /interactions` | `interactions:read` |
| `get_interaction_metadata(conversation_id)` | `GET /interactions/{id}/metadata` | `interactions:read` |
| `list_kb_documents()` | `GET /kb/documents` | `kb:read` |
| `get_kb_document(document_id)` | `GET /kb/documents/{id}` | `kb:read` |
| `search_kb(query, top_k=5)` | `POST /kb/search` | `kb:read` |
| `create_kb_document(titulo, contenido)` | `POST /kb/documents` | `kb:write` |
| `update_kb_document(document_id, contenido)` | `PUT /kb/documents/{id}` | `kb:write` |
| `list_agents()` | `GET /agents` | `prompts:write` |
| `update_agent_prompt(agent_id, prompt_system)` | `PUT /agents/{id}/prompt` | `prompts:write` |

Todas devuelven el JSON de la API como string. En caso de error (token ausente,
red o respuesta no-2xx) devuelven `{"error": true, "status"?, "detail"}`.

Notas de uso (las aplica el backend, respétalas):
- Antes de editar un documento, **léelo** con `get_kb_document`: `update_kb_document`
  reemplaza el contenido **entero**, no hace parches. Los formatos binarios
  (pdf/docx/xlsx) no son editables (HTTP 409).
- `monitor_overview`, `get_llm_costs`, `get_channels_status` y `get_health`
  devuelven **solo agregados** (conteos, coste, salud), nunca contenido de
  mensajes ni datos personales.
- `list_interactions` y `get_interaction_metadata` devuelven **solo metadata**
  de conversaciones (canal, estado, tiempos): sin contenido de mensajes y con el
  contacto únicamente como UUID.
- `update_agent_prompt` afecta al bot que habla con clientes **al instante**: haz
  cambios incrementales. Las reglas de seguridad del sistema no forman parte del
  prompt y no se pueden alterar por esta vía.

## Variables de entorno

| Variable | Por defecto | Descripción |
|---|---|---|
| `AGENT_API_BASE_URL` | `http://localhost:8000` | Base del backend **sin** `/api/v1`. El servidor añade `/api/v1/agent-api`. |
| `AGENT_API_TOKEN` | *(vacío)* | Token `agt_…` de fallback si la petición MCP no trae `Authorization`. |
| `AGENT_API_TIMEOUT` | `60` | Timeout HTTP (segundos) hacia la agent-api. |
| `MCP_HOST` | `0.0.0.0` | Host de escucha del servidor MCP. |
| `MCP_PORT` | `8080` | Puerto de escucha del servidor MCP. |
| `MCP_PATH` | `/mcp` | Ruta del endpoint Streamable HTTP. |

## Ejecución local

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
AGENT_API_BASE_URL="https://<tu-backend>" ./venv/bin/python server.py
# MCP escuchando en http://0.0.0.0:8080/mcp
```

## Docker

Va como un servicio más de la pila (`mcp`) en `docker-compose.yml` y en
`docker-compose.easypanel.yml`, así que normalmente no hay que lanzarlo a mano:

```bash
docker compose up -d mcp     # local: queda en http://localhost:8080/mcp
```

Suelto, si hace falta:

```bash
docker build -t mcp-agent-api .
docker run --rm -p 8080:8080 \
  -e AGENT_API_BASE_URL="https://<tu-backend>" \
  mcp-agent-api
```

Imagen `python:3.12-slim`, usuario no-root (uid `10001`), `EXPOSE 8080`,
`CMD python server.py` y healthcheck propio (`healthcheck.py`: un 4xx en `/mcp`
cuenta como sano, porque demuestra que el servidor contesta).

**No lleva ninguna clave de la aplicación, y no debe llevarla**: lo único que
necesita es la URL del backend. Los permisos los aplica el backend a partir del
token de agente de cada cliente MCP.

En producción necesita su propio dominio apuntando al puerto 8080 — los agentes
se conectan desde fuera. Pasos en `DEPLOY_EASYPANEL.md` §2.7. Acuérdate de poner
`MCP_BASE_URL` en el servicio `frontend`: es la URL que el panel enseña en
**Conexiones → API / MCP**.

## Referencia

Contrato completo de la API que envuelve este servidor (endpoints, ámbitos,
cuerpos, errores): [`docs/agent_api.md`](../docs/agent_api.md).
