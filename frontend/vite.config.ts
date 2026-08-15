import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    host: "0.0.0.0",
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // En un servidor modesto (pocos GB de RAM y compartido con otros
    // servicios), un único bundle de ~630 KB hacía que el minificado consumiera
    // mucha memoria y el build muriese por falta de memoria. Partirlo en chunks
    // de vendor reduce ese pico y, de paso, mejora la caché del navegador entre
    // despliegues.
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          // recharts + d3 (lo más pesado) y los iconos, cada uno aparte. El
          // resto (incl. React/react-dom/react-router) va en "vendor": así las
          // dependencias entre chunks van en un solo sentido (charts/icons →
          // vendor) y se evita el aviso de "circular chunk".
          if (id.includes("recharts") || id.includes("d3")) return "charts";
          if (id.includes("lucide-react")) return "icons";
          return "vendor";
        },
      },
    },
  },
});
