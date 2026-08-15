/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Coro — papeles y tintas. Vinculados a variables CSS de canal RGB
        // (definidas en index.css) para que cambien con la clase `.dark` y a
        // la vez admitan modificadores de opacidad (p. ej. bg-ink/40).
        paper: "rgb(var(--paper-rgb) / <alpha-value>)",
        paper2: "rgb(var(--paper-2-rgb) / <alpha-value>)",
        paper3: "rgb(var(--paper-3-rgb) / <alpha-value>)",
        card: "rgb(var(--card-rgb) / <alpha-value>)",
        ink: "rgb(var(--ink-rgb) / <alpha-value>)",
        ink2: "rgb(var(--ink-2-rgb) / <alpha-value>)",
        ink3: "rgb(var(--ink-3-rgb) / <alpha-value>)",
        ink4: "rgb(var(--ink-4-rgb) / <alpha-value>)",
        // Las líneas ya llevan alpha incorporado y no usan modificadores.
        line: "var(--line)",
        line2: "var(--line-2)",

        // Coro — pistacho (brand). Igual en claro y oscuro salvo `ink`.
        brand: {
          50: "#F7F8E8",
          100: "#EEF2D0",
          500: "#DBE09E",
          600: "#C6CC85",
          700: "#9DA362",
          DEFAULT: "#DBE09E",
          ink: "rgb(var(--brand-ink-rgb) / <alpha-value>)",
          on: "#1A1612",
          // Fondo suave de marca por tema (var en index.css). Antes esta clave
          // no existía y `bg-brand-soft` (Users/Agents/Outbound) no generaba
          // CSS: los estados activos quedaban sin fondo.
          soft: "var(--brand-soft)",
        },

        // Canales y estado
        ch: {
          wa: "#25D366",
          web: "#6C7BFF",
          voice: "#9B8AFB",
        },
        state: {
          ok: "#2DA771",
          warn: "#E58A2F",
          bad: "#DC4B3C",
        },
      },
      fontFamily: {
        sans: ["Geist", "Inter", "system-ui", "sans-serif"],
        display: ["Instrument Serif", "Georgia", "serif"],
        numbers: ["Space Grotesk", "Geist", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      borderRadius: {
        coro: "18px",
        "coro-sm": "12px",
      },
      // Sombras por tema (vars en index.css): en claro tinta cálida diluida,
      // en dark negro más denso — la tinta al 6-14% desaparecía sobre gris.
      boxShadow: {
        "coro-1": "var(--shadow-coro-1)",
        "coro-2": "var(--shadow-coro-2)",
        "coro-3": "var(--shadow-coro-3)",
      },
    },
  },
  plugins: [],
};
