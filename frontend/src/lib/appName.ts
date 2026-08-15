// Nombre visible de la app (login, sidebar, título de pestaña…). Cada
// instalación pone el suyo sin recompilar: el contenedor lo inyecta en
// runtime vía config.js (env APP_NAME → window.__APP_CONFIG__.appName).
// Respaldos: el valor de build (VITE_APP_NAME) y un default neutro.
export function appName(): string {
  return (
    window.__APP_CONFIG__?.appName ||
    import.meta.env.VITE_APP_NAME ||
    "Chatbot"
  );
}

// Partes de la marca para el sidebar: primera palabra en grande (serif
// itálica) y el resto como subtítulo uppercase — el mismo patrón visual que
// tenía la marca original. Con un nombre de una sola palabra no hay subtítulo.
export function appNameParts(): { main: string; sub: string } {
  const [main, ...rest] = appName().trim().split(/\s+/);
  return { main: main || "Chatbot", sub: rest.join(" ") };
}
