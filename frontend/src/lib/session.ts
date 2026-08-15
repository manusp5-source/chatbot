/**
 * Único punto donde vive la sesión del panel en el navegador.
 *
 * Antes el JWT estaba duplicado: en `localStorage["token"]` (lo leen el
 * interceptor de axios, el WebSocket del inbox, las descargas de media y el
 * agente interno) y también dentro del estado persistido de zustand
 * (`localStorage["chatbot-auth"]`). No había un sitio que limpiara los dos: el
 * interceptor de 401 borraba solo `token`, así que el JWT seguía escrito en el
 * otro y al recargar la app volvía a creerse con sesión. Y el historial del
 * Agente Interno sobrevivía al logout — en un ordenador compartido, el
 * siguiente que entra veía las conversaciones del anterior.
 *
 * Regla: el token se lee y se escribe SOLO desde aquí, y cerrar sesión es
 * `clearStoredSession()`. Nadie escribe el nombre de una clave a mano.
 */

/** JWT del panel. Fuente única de la verdad (el store NO lo persiste). */
export const TOKEN_KEY = "token";
/** Estado persistido del store de auth (zustand). Solo debería llevar `user`. */
export const AUTH_PERSIST_KEY = "chatbot-auth";
/** Historial del widget del Agente Interno: puede contener datos de clientes. */
export const INTERNAL_AGENT_KEY = "internal_agent_session_v1";

function ls(): Storage | null {
  try {
    return localStorage;
  } catch {
    // localStorage no disponible (modo privado, cookies bloqueadas…).
    return null;
  }
}

export function getToken(): string | null {
  return ls()?.getItem(TOKEN_KEY) ?? null;
}

export function setToken(token: string): void {
  ls()?.setItem(TOKEN_KEY, token);
}

/**
 * Borra TODO rastro de la sesión en el navegador. Lo llaman el logout y el
 * interceptor de 401; si aparece otra clave con datos de sesión, va aquí.
 */
export function clearStoredSession(): void {
  const s = ls();
  if (!s) return;
  for (const key of [TOKEN_KEY, AUTH_PERSIST_KEY, INTERNAL_AGENT_KEY]) {
    s.removeItem(key);
  }
}
