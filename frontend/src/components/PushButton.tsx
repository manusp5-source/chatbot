import { useEffect, useState } from "react";
import { Bell, BellRing, BellOff, Loader2 } from "lucide-react";
import {
  disablePush,
  enablePush,
  isPushEnabled,
  pushSupported,
} from "@/services/push";

/**
 * Botón de la cabecera para activar/desactivar las notificaciones push de la
 * PWA. Estados: no soportado (campana tachada, deshabilitada con pista),
 * activadas (campana con onda) y desactivadas (campana). Un toque alterna.
 */
export function PushButton() {
  const [supported] = useState(() => pushSupported());
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (!supported) return;
    isPushEnabled()
      .then((v) => alive && setEnabled(v))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [supported]);

  async function toggle() {
    setBusy(true);
    setError(null);
    try {
      if (enabled) {
        await disablePush();
        setEnabled(false);
      } else {
        await enablePush();
        setEnabled(true);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "No se pudo cambiar las notificaciones";
      setError(msg);
      // Mensaje visible: en móvil no hay consola a mano.
      window.alert(msg);
    } finally {
      setBusy(false);
    }
  }

  if (!supported) {
    return (
      <button
        type="button"
        disabled
        title="Notificaciones no disponibles aquí. En iPhone, añade la web a inicio y ábrela desde ahí."
        aria-label="Notificaciones no disponibles"
        className="inline-flex items-center justify-center w-8 h-8 rounded-coro-sm text-ink4 shrink-0 opacity-60 cursor-not-allowed"
      >
        <BellOff className="w-4 h-4" />
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={busy}
      title={enabled ? "Notificaciones activadas (tocar para desactivar)" : "Activar notificaciones"}
      aria-label={enabled ? "Desactivar notificaciones" : "Activar notificaciones"}
      aria-pressed={enabled}
      className={
        "inline-flex items-center justify-center w-8 h-8 rounded-coro-sm hover:bg-paper3 shrink-0 transition-colors " +
        (enabled ? "text-brand-ink" : "text-ink2")
      }
    >
      {busy ? (
        <Loader2 className="w-4 h-4 animate-spin" />
      ) : enabled ? (
        <BellRing className="w-4 h-4" />
      ) : (
        <Bell className="w-4 h-4" />
      )}
      {error ? <span className="sr-only">{error}</span> : null}
    </button>
  );
}
