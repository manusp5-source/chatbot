# Agent-API MCP Server
# Thin-client MCP server (Streamable HTTP) que expone /api/v1/agent-api.
FROM python:3.12-slim

# No escribir .pyc, salida sin buffer (logs inmediatos).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Configuracion por defecto del transporte MCP (sobreescribible en runtime).
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8080 \
    MCP_PATH=/mcp

WORKDIR /app

# Instala dependencias primero (mejor cacheo de capas).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Codigo de la aplicacion.
COPY server.py healthcheck.py ./

# Usuario no-root (uid fijo 10001).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER 10001

EXPOSE 8080

# Ver healthcheck.py: 4xx cuenta como sano (el servidor contesta), solo falla si
# la conexion no se abre.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "healthcheck.py"]

CMD ["python", "server.py"]
