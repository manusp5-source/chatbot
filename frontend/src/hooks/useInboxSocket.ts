import { useEffect, useRef, useState } from "react";
import { api, apiBaseWs } from "@/services/api";
import { getToken } from "@/lib/session";

type Event = { type: string; payload: Record<string, unknown> };

/** Estado del enlace en tiempo real, para poder DECIRLO en pantalla. */
export type SocketStatus = "connecting" | "online" | "offline";

/**
 * WebSocket persistente al inbox. Reconexion con backoff exponencial
 * (max 64s) y reset al conectar OK.
 *
 * NOTA importante: el callback `onEvent` cambia entre renders (cierra sobre
 * estado del componente como `selectedId`). Lo guardamos en un ref para que
 * el handler del WebSocket SIEMPRE invoque la version mas reciente. Si no,
 * el callback queda "stale" con valores del primer render y los eventos no
 * actualizan la UI cuando cambias de conversacion.
 *
 * Lo que se arregló aquí (la bandeja se congelaba EN SILENCIO):
 *   - Al reconectar no se refrescaba nada. Todo lo que hubiera pasado durante
 *     el corte se perdía: la bandeja seguía teniendo aspecto normal, con datos
 *     viejos y sin ninguna pista de que llevaba minutos desconectada.
 *     Ahora `onReconnect` se dispara en cada conexión establecida DESPUÉS de la
 *     primera, y el inbox la usa para recargar lista y mensajes.
 *   - No había ni indicador ni red de seguridad. Ahora el hook devuelve el
 *     estado del enlace (para pintarlo) y, mientras está caído, el inbox tira
 *     de una consulta de respaldo periódica.
 */
export function useInboxSocket(
  onEvent: (ev: Event) => void,
  onReconnect?: () => void,
): SocketStatus {
  const wsRef = useRef<WebSocket | null>(null);
  const cbRef = useRef(onEvent);
  const reconnectRef = useRef(onReconnect);
  const [status, setStatus] = useState<SocketStatus>("connecting");

  // Mantener los callbacks siempre actualizados sin recrear la conexion.
  useEffect(() => {
    cbRef.current = onEvent;
    reconnectRef.current = onReconnect;
  });

  useEffect(() => {
    const token = getToken();
    if (!token) return;

    let cancelled = false;
    let retry = 0;
    // La PRIMERA conexión es una conexión normal; de la segunda en adelante es
    // una RE-conexión, y ahí es donde hay que ponerse al día.
    let hasConnectedOnce = false;

    const connect = async () => {
      if (cancelled) return;
      // Ticket efímero de un solo uso (60s) en vez del JWT en la query string
      // (el JWT completo acababa en logs de proxies). Uno fresco por intento.
      let ticket: string;
      try {
        const { data } = await api.post<{ ticket: string }>("/auth/ws-ticket");
        ticket = data.ticket;
      } catch {
        if (cancelled) return;
        setStatus("offline");
        retry = Math.min(retry + 1, 6);
        setTimeout(() => void connect(), 1000 * 2 ** retry);
        return;
      }
      if (cancelled) return;
      const url = `${apiBaseWs()}/api/v1/ws/inbox?ticket=${encodeURIComponent(ticket)}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        retry = 0; // reset backoff al conectar correctamente
        if (cancelled) return;
        setStatus("online");
        // Ponerse al día con lo que pasó mientras estábamos fuera.
        if (hasConnectedOnce) reconnectRef.current?.();
        hasConnectedOnce = true;
      };
      ws.onmessage = (e) => {
        try {
          const data: Event = JSON.parse(e.data);
          cbRef.current(data);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        if (cancelled) return;
        setStatus("offline");
        retry = Math.min(retry + 1, 6);
        setTimeout(() => void connect(), 1000 * 2 ** retry);
      };
    };

    void connect();

    // Reconectar al volver al foreground (mobile suspende WS al ir background)
    const onVisible = () => {
      if (document.visibilityState === "visible" && wsRef.current?.readyState !== WebSocket.OPEN) {
        wsRef.current?.close();
        void connect();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return status;
}
