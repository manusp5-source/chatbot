// Config de runtime del frontend. El contenedor SOBREESCRIBE este archivo al
// arrancar (docker-entrypoint.d/40-app-config.sh) con el valor de API_BASE_URL.
// Por defecto vacío: si no se define API_BASE_URL, api.ts cae al valor de build
// (VITE_API_BASE_URL) — así el despliegue actual sigue funcionando igual.
window.__APP_CONFIG__ = { apiBaseUrl: "", mcpBaseUrl: "" };
