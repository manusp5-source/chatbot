import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { router } from "@/router";
import { useTheme } from "@/store/theme";
import { usePrivacy } from "@/store/privacy";
import { appName } from "@/lib/appName";
import "@/index.css";

// Título de la pestaña con el nombre configurado de la instalación (config.js
// ya está cargado: en index.html va ANTES del bundle).
document.title = appName();
// Aplica la clase `dark` en <html> antes del primer render para evitar FOUC.
useTheme.getState().init();
// Restaura el "Modo privacidad" desde localStorage (persistente por navegador).
usePrivacy.getState().init();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>
);
