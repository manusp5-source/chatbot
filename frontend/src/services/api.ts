import axios, { AxiosInstance } from "axios";

import { clearStoredSession, getToken } from "@/lib/session";

// La URL del backend se resuelve en RUNTIME primero: el contenedor genera
// /config.js (window.__APP_CONFIG__) desde la variable API_BASE_URL. Respaldos:
// el valor de build (VITE_API_BASE_URL) y localhost. Así el MISMO build sirve en
// varios servidores — cada uno pone su API_BASE_URL sin recompilar — y si no se
// define, se cae al default de build (compatibilidad con el despliegue actual).
export function apiBaseHttp(): string {
  return (
    window.__APP_CONFIG__?.apiBaseUrl ||
    import.meta.env.VITE_API_BASE_URL ||
    "http://localhost:8000"
  );
}

export function apiBaseWs(): string {
  return apiBaseHttp().replace(/^http/, "ws");
}

// Endpoint del servidor MCP (mcp-server, transporte Streamable HTTP, ruta /mcp).
// Es un despliegue APARTE con su propio dominio, por eso se configura en runtime
// con MCP_BASE_URL. Si no está definido, damos un fallback derivado del backend
// (mismo host) para no dejar el panel vacío — el operador debe ajustarlo si su
// MCP vive en otro dominio.
export function mcpEndpoint(): string {
  const base = window.__APP_CONFIG__?.mcpBaseUrl || import.meta.env.VITE_MCP_BASE_URL;
  if (base) return base.replace(/\/+$/, "") + "/mcp";
  return apiBaseHttp().replace(/\/+$/, "") + "/mcp";
}

const baseURL = apiBaseHttp() + "/api/v1";

export const api: AxiosInstance = axios.create({
  baseURL,
  headers: { "Content-Type": "application/json" },
});

api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (error) => {
    if (error.response?.status === 401) {
      // Limpieza COMPLETA. Antes solo se borraba la clave `token` y el JWT se
      // quedaba escrito dentro del estado persistido del store: al recargar,
      // la app se rehidrataba con una sesión muerta. Y el historial del
      // Agente Interno seguía ahí para el siguiente usuario del equipo.
      clearStoredSession();
      window.dispatchEvent(new Event("app:session-cleared"));
      if (!window.location.pathname.endsWith("/login")) {
        // Conserva dónde estabas: tras volver a entrar, Login te devuelve ahí.
        const from = window.location.pathname + window.location.search;
        window.location.href = "/login?from=" + encodeURIComponent(from);
      }
    }
    return Promise.reject(error);
  }
);
