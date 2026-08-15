/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_MCP_BASE_URL: string;
  readonly VITE_APP_NAME: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Config inyectada en RUNTIME por el contenedor (public/config.js sobreescrito al
// arrancar desde API_BASE_URL). Permite que el MISMO build apunte a distintos
// backends sin recompilar. Respaldo: el valor de build VITE_API_BASE_URL.
interface Window {
  __APP_CONFIG__?: { apiBaseUrl?: string; mcpBaseUrl?: string; appName?: string };
}
