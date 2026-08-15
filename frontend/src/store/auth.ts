import { create } from "zustand";
import { persist } from "zustand/middleware";
import { api } from "@/services/api";
import {
  AUTH_PERSIST_KEY,
  clearStoredSession,
  getToken,
  setToken,
} from "@/lib/session";
import type { LoginResponse, User } from "@/types";

interface AuthState {
  token: string | null;
  user: User | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  loginWithToken: (token: string) => Promise<void>;
  logout: () => Promise<void>;
  fetchMe: () => Promise<void>;
  updateProfile: (body: {
    nombre?: string | null;
    email?: string;
    current_password?: string;
    new_password?: string;
  }) => Promise<void>;
}

export const useAuth = create<AuthState>()(
  persist(
    (set, get) => ({
      // El token se lee de su ÚNICA clave (`lib/session`), no del estado
      // persistido: ver `partialize` abajo.
      token: getToken(),
      user: null,
      isLoading: false,
      login: async (email, password) => {
        set({ isLoading: true });
        try {
          const { data } = await api.post<LoginResponse>("/auth/login", { email, password });
          setToken(data.access_token);
          set({ token: data.access_token, user: data.user, isLoading: false });
        } catch (e) {
          set({ isLoading: false });
          throw e;
        }
      },
      // Entrada vía "Iniciar sesión con Google": el backend nos devuelve un JWT
      // ya emitido (en el fragmento de la URL). Lo guardamos y cargamos el perfil.
      loginWithToken: async (token) => {
        setToken(token);
        set({ token });
        await get().fetchMe();
      },
      logout: async () => {
        // Revoca el JWT en el backend (blocklist en Redis + audita el evento)
        // antes de limpiar el estado local. Best-effort: si la llamada falla
        // (token ya caducado, red caída) cerramos sesión igualmente en local.
        try {
          await api.post("/auth/logout");
        } catch {
          // ignoramos: la sesión local se cierra de todas formas
        }
        // Limpieza COMPLETA: token, estado persistido y el historial del
        // Agente Interno (que puede llevar datos de clientes y antes seguía
        // ahí para el siguiente que entrase en el mismo ordenador).
        clearStoredSession();
        set({ token: null, user: null });
      },
      fetchMe: async () => {
        try {
          const { data } = await api.get<User>("/auth/me");
          set({ user: data });
        } catch {
          clearStoredSession();
          set({ token: null, user: null });
        }
      },
      updateProfile: async (body) => {
        const { data } = await api.patch<User>("/auth/me", body);
        set({ user: data });
      },
    }),
    {
      name: AUTH_PERSIST_KEY,
      // El token NO se persiste aquí: vivía duplicado (en `token` y dentro de
      // este blob) y una limpieza parcial dejaba el JWT escrito en el que no se
      // borraba. Aquí solo el perfil, para pintar el nombre sin parpadeo.
      partialize: (s) => ({ user: s.user }) as unknown as AuthState,
    }
  )
);

// El interceptor de 401 (services/api.ts) limpia el almacenamiento y avisa por
// evento: así el estado en memoria se cae también cuando NO hay recarga (p. ej.
// un 401 estando ya en /login). Sin ciclo de imports entre api.ts y este store.
if (typeof window !== "undefined") {
  window.addEventListener("app:session-cleared", () => {
    useAuth.setState({ token: null, user: null });
  });
}
