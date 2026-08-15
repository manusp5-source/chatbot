/* Service worker del panel — solo Web Push (no cachea la app).
 *
 * Recibe el push del backend (firmado con VAPID) y muestra la notificación.
 * Al tocarla, enfoca el panel ya abierto o abre la conversación.
 */

self.addEventListener("install", () => {
  // Activa la nueva versión del SW sin esperar a que se cierren las pestañas.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { body: event.data ? event.data.text() : "" };
  }
  // Fallback neutro: el backend manda siempre su propio título en el payload.
  const title = data.title || "Chatbot";
  const url = data.url || "/";
  const options = {
    body: data.body || "",
    icon: "/icon-192.svg",
    // tag por destino → varias notis de la misma conversación se colapsan.
    tag: url,
    renotify: true,
    data: { url },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of all) {
        // Reutiliza una pestaña/PWA abierta: la enfoca y navega al destino.
        if ("focus" in client) {
          try {
            await client.focus();
            if ("navigate" in client) await client.navigate(url);
          } catch (e) {
            /* noop */
          }
          return;
        }
      }
      if (self.clients.openWindow) await self.clients.openWindow(url);
    })(),
  );
});
