import { api } from "@/services/api";

/**
 * Web Push para la PWA del operador. Flujo de activación:
 *  1. Registra el service worker (/sw.js).
 *  2. Pide permiso de notificaciones (gesto del usuario obligatorio).
 *  3. Se suscribe en el PushManager con la clave pública VAPID del backend.
 *  4. Manda la suscripción (endpoint + claves) al backend.
 *
 * iOS: solo funciona si la web está AÑADIDA A INICIO (PWA instalada) y con
 * iOS 16.4+. En Safari normal, `PushManager` no está disponible.
 */

export function pushSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    "PushManager" in window &&
    "Notification" in window
  );
}

// Heurística para el caso típico de iPhone: Safari sin instalar la PWA → no
// soporta push. Sirve para mostrar la pista de "añadir a inicio".
export function isIosNeedsInstall(): boolean {
  if (pushSupported()) return false;
  const ua = navigator.userAgent || "";
  const isIos = /iphone|ipad|ipod/i.test(ua);
  const standalone =
    window.matchMedia?.("(display-mode: standalone)").matches ||
    // iOS expone navigator.standalone
    (window.navigator as unknown as { standalone?: boolean }).standalone === true;
  return isIos && !standalone;
}

function urlBase64ToArrayBuffer(base64String: string): ArrayBuffer {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const buf = new ArrayBuffer(raw.length);
  const arr = new Uint8Array(buf);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return buf;
}

async function getVapidPublicKey(): Promise<string> {
  const { data } = await api.get<{ public_key: string }>("/push/vapid-public-key");
  return data.public_key;
}

export async function isPushEnabled(): Promise<boolean> {
  if (!pushSupported()) return false;
  if (Notification.permission !== "granted") return false;
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = await reg?.pushManager.getSubscription();
  return !!sub;
}

export async function enablePush(): Promise<void> {
  if (!pushSupported()) {
    throw new Error(
      isIosNeedsInstall()
        ? "En iPhone, primero añade la web a la pantalla de inicio (Compartir → Añadir a inicio) y ábrela desde ahí."
        : "Tu navegador no soporta notificaciones push.",
    );
  }
  const reg = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Permiso de notificaciones denegado.");
  }

  const key = await getVapidPublicKey();
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToArrayBuffer(key),
    });
  }
  const json = sub.toJSON();
  if (!json.keys?.p256dh || !json.keys?.auth) {
    throw new Error("Suscripción inválida (faltan claves).");
  }
  await api.post("/push/subscribe", {
    endpoint: sub.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
  });
}

export async function disablePush(): Promise<void> {
  if (!pushSupported()) return;
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = await reg?.pushManager.getSubscription();
  if (sub) {
    await api.post("/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {});
    await sub.unsubscribe().catch(() => {});
  }
}
