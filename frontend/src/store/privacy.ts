import { create } from "zustand";

// "Modo privacidad" — toggle PERSONAL (por navegador) para compartir pantalla
// sin exponer datos reales de los clientes (nombres, @usuarios, teléfonos,
// emails). Es puramente visual en el frontend: NO altera los datos del backend
// ni los que viajan por la red; solo cómo se PINTAN en pantalla. Al apagarlo,
// todo vuelve a verse normal. Pensado para directos/comunidad.

interface PrivacyState {
  enabled: boolean;
  setEnabled: (v: boolean) => void;
  toggle: () => void;
  init: () => void;
}

const STORAGE_KEY = "chatbot-privacy";

function resolveInitial(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // localStorage no disponible (modo privado del navegador, etc.)
    return false;
  }
}

export const usePrivacy = create<PrivacyState>((set, get) => ({
  // Por defecto OFF: nunca enmascaramos el trabajo normal sin que se pida.
  enabled: false,
  setEnabled: (v) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, v ? "1" : "0");
    } catch {
      /* noop */
    }
    set({ enabled: v });
  },
  toggle: () => get().setEnabled(!get().enabled),
  init: () => set({ enabled: resolveInitial() }),
}));
