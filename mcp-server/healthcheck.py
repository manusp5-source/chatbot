"""Healthcheck del servidor MCP.

Sano = el endpoint MCP contesta por HTTP. Un GET pelado a /mcp devuelve 4xx
(le falta la negociacion de Streamable HTTP) y eso YA demuestra que el servidor
esta sirviendo: lo que no vale es que la conexion ni se abra.

Se usa urllib porque la imagen slim no trae curl y no merece la pena instalarlo
solo para esto.
"""
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:{}{}".format(
    os.getenv("MCP_PORT", "8080"), os.getenv("MCP_PATH", "/mcp")
)

try:
    urllib.request.urlopen(URL, timeout=5)
except urllib.error.HTTPError:
    pass  # Contesta: esta vivo.
except Exception as e:  # noqa: BLE001 — no arranca, no resuelve, no acepta
    print("MCP no responde en {}: {}".format(URL, e), file=sys.stderr)
    sys.exit(1)
