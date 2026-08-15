#!/bin/sh
# Genera /config.js en RUNTIME desde la variable de entorno API_BASE_URL, para
# que la URL del backend NO tenga que estar horneada en el build. Así el MISMO
# build/imagen sirve en varios servidores (cada instalación define su
# API_BASE_URL) y cambiar la URL solo requiere reiniciar, no recompilar.
#
# Si API_BASE_URL está vacía, config.js queda con apiBaseUrl:"" y api.ts cae al
# valor de build (VITE_API_BASE_URL) → el despliegue actual no cambia.
#
# La imagen oficial de nginx ejecuta los scripts de /docker-entrypoint.d/*.sh
# antes de levantar el servidor, así que config.js queda listo antes de servir.
set -e

# MCP_BASE_URL: base pública del servidor MCP (mcp-server, endpoint /mcp). Es un
# despliegue aparte con su propio dominio, así que no se deriva del backend; el
# panel de tokens la muestra para que el operador copie el comando de conexión.
# Vacía = el panel deriva un fallback razonable del backend.
#
# APP_NAME: nombre visible de la instalación (login, sidebar, título de la
# pestaña). Vacío = la app cae al valor de build (VITE_APP_NAME) o a "Chatbot".
cat > /usr/share/nginx/html/config.js <<EOF
window.__APP_CONFIG__ = { apiBaseUrl: "${API_BASE_URL:-}", mcpBaseUrl: "${MCP_BASE_URL:-}", appName: "${APP_NAME:-}" };
EOF

echo "[app-config] config.js -> apiBaseUrl='${API_BASE_URL:-}' mcpBaseUrl='${MCP_BASE_URL:-}' appName='${APP_NAME:-}'"
