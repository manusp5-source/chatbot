"""
Agent-API MCP Server
====================

Servidor MCP (Model Context Protocol) *thin client* que expone la
"Agent API" del operador (`/api/v1/agent-api`) como herramientas MCP, para que
los asistentes operativos del operador puedan
monitorizar la app, revisar/editar la base de conocimiento (KB/RAG) y mejorar
los prompts de los agentes de forma remota.

Cada herramienta MCP realiza UNA llamada HTTP a un endpoint de la agent-api del
backend y devuelve el JSON de la respuesta. No importa nada del backend; es
totalmente autonomo.

Transporte: Streamable HTTP (mcp==1.28.1, FastMCP).
Auth: se reenvia el token de agente al backend. Se resuelve, por cada llamada,
como:
  1. La cabecera "Authorization: Bearer <token>" de la peticion MCP entrante
     (asi cada agente conectado usa su propio token y sus propios ambitos), o
  2. En su defecto, la variable de entorno AGENT_API_TOKEN (fallback).
Si no hay ninguna de las dos, se devuelve un objeto de error claro.
Los ambitos/permisos (monitor:read, interactions:read, kb:read, kb:write,
prompts:write) los aplica el backend a partir del token.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any

import httpx
from pydantic import Field

from mcp.server.fastmcp import Context, FastMCP

# ---------------------------------------------------------------------------
# Configuracion (via variables de entorno)
# ---------------------------------------------------------------------------

# Base del backend SIN el sufijo /api/v1 (p.ej. https://apichat.tudominio.com).
# El servidor anade /api/v1/agent-api.
AGENT_API_BASE_URL = os.getenv("AGENT_API_BASE_URL", "http://localhost:8000").rstrip("/")

# Token de agente de fallback si la peticion MCP no trae Authorization.
AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN", "").strip()

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")

# URL base completa de la agent-api a la que llamamos.
API_BASE = f"{AGENT_API_BASE_URL}/api/v1/agent-api"

HTTP_TIMEOUT = float(os.getenv("AGENT_API_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# Servidor FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="agent-api",
    instructions=(
        "Herramientas para operar el chatbot mediante la agent-api del "
        "operador: monitorizar metricas agregadas, revisar/editar la base de "
        "conocimiento (KB/RAG) y mejorar los prompts de los agentes. Cada agente "
        "conectado debe autenticarse enviando su token de agente como "
        "Bearer token; el backend aplica los ambitos correspondientes "
        "(monitor:read, interactions:read, kb:read, kb:write, prompts:write). "
        "Antes de editar un documento, leelo con get_kb_document: el update "
        "reemplaza el contenido ENTERO."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path=MCP_PATH,
)


# ---------------------------------------------------------------------------
# Cliente HTTP compartido (asincrono, lazy singleton ligado al event loop)
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Devuelve un httpx.AsyncClient compartido, creandolo la primera vez."""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    base_url=API_BASE,
                    timeout=HTTP_TIMEOUT,
                )
    return _client


# ---------------------------------------------------------------------------
# Resolucion del token y helper de peticion
# ---------------------------------------------------------------------------


def _resolve_token(ctx: Context) -> str | None:
    """Resuelve el token de agente para esta llamada.

    Prioridad:
      1. Cabecera 'Authorization: Bearer <token>' de la peticion MCP entrante.
      2. Variable de entorno AGENT_API_TOKEN (fallback).
    Devuelve None si no hay ninguno disponible.
    """
    # 1) Cabecera de la peticion HTTP entrante (transporte Streamable HTTP).
    #    ctx.request_context.request es un starlette.requests.Request.
    try:
        request = ctx.request_context.request
    except Exception:
        request = None
    if request is not None:
        auth = request.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                return token

    # 2) Fallback a la variable de entorno.
    return AGENT_API_TOKEN or None


async def _api_request(
    ctx: Context,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> str:
    """Realiza la llamada HTTP a la agent-api y devuelve el JSON como str.

    En caso de error (falta de token, error de red o respuesta no-2xx) devuelve
    un objeto JSON con {"error": true, "status"?, "detail"} y el mensaje de
    error de la API.
    """
    token = _resolve_token(ctx)
    if not token:
        return json.dumps(
            {
                "error": True,
                "detail": (
                    "No hay token de agente disponible. Envia la cabecera "
                    "'Authorization: Bearer <token>' en la peticion MCP o "
                    "configura la variable de entorno AGENT_API_TOKEN."
                ),
            },
            ensure_ascii=False,
        )

    # Limpia params/body de valores None.
    clean_params = (
        {k: v for k, v in params.items() if v is not None} if params else None
    )

    client = await _get_client()
    try:
        resp = await client.request(
            method,
            path,
            params=clean_params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        return json.dumps(
            {
                "error": True,
                "detail": f"Error de red al llamar a la agent-api: {exc}",
            },
            ensure_ascii=False,
        )

    if not (200 <= resp.status_code < 300):
        # Devuelve el mensaje de error de la API tal cual.
        detail: Any
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("message") or body
            else:
                detail = body
        except Exception:
            detail = resp.text
        return json.dumps(
            {"error": True, "status": resp.status_code, "detail": detail},
            ensure_ascii=False,
        )

    # Exito: devuelve el JSON de la API. Si no es JSON, devuelve el texto.
    try:
        return json.dumps(resp.json(), ensure_ascii=False)
    except Exception:
        return resp.text


# ===========================================================================
# Grupo: MONITOR (monitor:read) — metricas AGREGADAS (sin PII ni mensajes)
# ===========================================================================


@mcp.tool()
async def monitor_overview(ctx: Context, range: str = "7d") -> str:
    """Vision agregada del bot para un rango temporal.

    Devuelve conversaciones por estado/canal, handoffs, volumen de mensajes por
    rol, coste/uso LLM del rango y canales pausados. Sin contenido de mensajes
    ni datos personales de clientes. Requiere el ambito 'monitor:read'.

    GET /monitor/overview?range=... (valores: today | 7d | 30d | 90d).
    """
    return await _api_request(ctx, "GET", "/monitor/overview", params={"range": range})


@mcp.tool()
async def get_llm_costs(ctx: Context, range: str = "7d") -> str:
    """Coste y uso LLM del rango: por agente, por modelo y serie por dia.

    Misma query que el dashboard admin de uso de tokens (los numeros cuadran
    con el panel). Solo agregados: llamadas, tokens y coste estimado en USD.
    Requiere el ambito 'monitor:read'.
    GET /monitor/llm-costs?range=... (valores: 24h | 7d | 30d | month).
    """
    return await _api_request(ctx, "GET", "/monitor/llm-costs", params={"range": range})


@mcp.tool()
async def get_channels_status(ctx: Context) -> str:
    """Estado operativo de cada canal del bot.

    Por canal: pausado o activo, si esta configurado/habilitado, si tiene
    agente asignado y la ultima entrada de cliente recibida (proxy del ultimo
    webhook). Incluye los errores recientes del runtime (saneados: solo
    ts/level/event/canal). Requiere el ambito 'monitor:read'.
    GET /monitor/channels.
    """
    return await _api_request(ctx, "GET", "/monitor/channels")


@mcp.tool()
async def get_health(ctx: Context) -> str:
    """Salud operativa del backend: BD, Redis y backlog de colas.

    Devuelve {status, db, redis, queues:{celery_backlog,
    outbound_jobs_pending}}. Requiere el ambito 'monitor:read'.
    GET /monitor/health.
    """
    return await _api_request(ctx, "GET", "/monitor/health")


# ===========================================================================
# Grupo: INTERACTIONS (interactions:read) — metadata de conversaciones
# (sin contenido de mensajes ni PII: el contacto viaja solo como UUID)
# ===========================================================================


@mcp.tool()
async def list_interactions(
    ctx: Context,
    status: str | None = None,
    canal: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> str:
    """Lista conversaciones con SOLO metadata operativa (sin contenido ni PII).

    Por conversacion: id, canal, estado, created_at, last_activity_at,
    segundos en el estado actual, si esta asignada a un operador y el contacto
    como UUID. Filtros opcionales: status (bot | humano | cerrada) y canal
    (whatsapp | web | instagram_dm | email | retell_voice). Ordenadas por
    ultima actividad descendente. Requiere el ambito 'interactions:read'.
    GET /interactions?status=&canal=&limit=.
    """
    return await _api_request(
        ctx,
        "GET",
        "/interactions",
        params={"status": status, "canal": canal, "limit": limit},
    )


@mcp.tool()
async def get_interaction_metadata(conversation_id: str, ctx: Context) -> str:
    """Metadata de UNA conversacion (sin contenido de mensajes ni PII).

    Igual de limpia que el listado, y ademas: ended_at, derivada_a_humano_at,
    archived/quarantined y conteo de mensajes por rol. Nunca devuelve texto de
    mensajes ni datos del contacto (solo su UUID). Requiere el ambito
    'interactions:read'. GET /interactions/{conversation_id}/metadata.
    """
    return await _api_request(ctx, "GET", f"/interactions/{conversation_id}/metadata")


# ===========================================================================
# Grupo: KB — Base de Conocimiento / RAG
# ===========================================================================


@mcp.tool()
async def list_kb_documents(ctx: Context) -> str:
    """Lista los documentos de la base de conocimiento.

    Devuelve id, nombre, formato y si es editable, entre otros. Requiere el
    ambito 'kb:read'. GET /kb/documents.
    """
    return await _api_request(ctx, "GET", "/kb/documents")


@mcp.tool()
async def get_kb_document(document_id: str, ctx: Context) -> str:
    """Obtiene el contenido completo (texto) de un documento de la KB.

    Devuelve {document_id, nombre, formato, editable, contenido}. Leelo ANTES de
    editar, porque update_kb_document reemplaza el contenido entero. Requiere el
    ambito 'kb:read'. GET /kb/documents/{document_id}.
    """
    return await _api_request(ctx, "GET", f"/kb/documents/{document_id}")


@mcp.tool()
async def search_kb(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    ctx: Context,
    top_k: Annotated[int, Field(ge=1, le=8)] = 5,
) -> str:
    """Busqueda semantica (RAG) en la base de conocimiento.

    Devuelve los fragmentos mas relevantes para la consulta. top_k entre 1 y 8
    (por defecto 5). Requiere el ambito 'kb:read'.
    POST /kb/search con body {query, top_k}.
    """
    return await _api_request(
        ctx,
        "POST",
        "/kb/search",
        json_body={"query": query, "top_k": top_k},
    )


@mcp.tool()
async def create_kb_document(
    titulo: Annotated[str, Field(min_length=1, max_length=200)],
    contenido: Annotated[str, Field(min_length=1, max_length=200_000)],
    ctx: Context,
) -> str:
    """Crea un documento de texto (.txt) en la base de conocimiento.

    Se indexa automaticamente (chunks + embeddings) y queda auditado con el
    token que lo creo. Requiere el ambito 'kb:write'.
    POST /kb/documents con body {titulo, contenido}.
    """
    return await _api_request(
        ctx,
        "POST",
        "/kb/documents",
        json_body={"titulo": titulo, "contenido": contenido},
    )


@mcp.tool()
async def update_kb_document(
    document_id: str,
    contenido: Annotated[str, Field(min_length=1, max_length=200_000)],
    ctx: Context,
) -> str:
    """Edita un documento de texto (txt/md) de la base de conocimiento.

    Reemplaza el contenido ENTERO (no hace parches): leelo antes con
    get_kb_document. La version anterior queda en el historial (restaurable
    desde el panel) y se reindexa al momento. Los formatos binarios
    (pdf/docx/xlsx) no son editables (HTTP 409). Requiere el ambito 'kb:write'.
    PUT /kb/documents/{document_id} con body {contenido}.
    """
    return await _api_request(
        ctx,
        "PUT",
        f"/kb/documents/{document_id}",
        json_body={"contenido": contenido},
    )


# ===========================================================================
# Grupo: PROMPTS (prompts:write) — leer y mejorar prompts de los agentes
# ===========================================================================


@mcp.tool()
async def list_agents(ctx: Context) -> str:
    """Lista los agentes con su prompt_system actual.

    Devuelve {agent_id, name, is_active, model_name, prompt_system} por agente.
    Requiere el ambito 'prompts:write'. GET /agents.
    """
    return await _api_request(ctx, "GET", "/agents")


@mcp.tool()
async def update_agent_prompt(
    agent_id: str,
    prompt_system: Annotated[str, Field(min_length=1, max_length=50_000)],
    ctx: Context,
) -> str:
    """Reemplaza el prompt_system de un agente.

    El prompt anterior queda en el historial (restaurable desde el panel de
    Agentes). Las reglas de seguridad del sistema (anti-inyeccion, transparencia
    IA) NO forman parte del prompt y no se pueden alterar por aqui. El cambio
    afecta al bot que habla con clientes AL INSTANTE: haz cambios incrementales.
    Requiere el ambito 'prompts:write'.
    PUT /agents/{agent_id}/prompt con body {prompt_system}.
    """
    return await _api_request(
        ctx,
        "PUT",
        f"/agents/{agent_id}/prompt",
        json_body={"prompt_system": prompt_system},
    )


# ---------------------------------------------------------------------------
# Punto de entrada: transporte Streamable HTTP
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecuta el servidor MCP con transporte Streamable HTTP.
    # host / port / streamable_http_path se han configurado en el constructor
    # de FastMCP a partir de las variables de entorno.
    mcp.run(transport="streamable-http")
