# Panel (frontend)

Panel web del chatbot: React 18 + TypeScript + Vite + Tailwind. `src/pages/client/`
es el día a día (inbox, contactos, base de conocimiento) y `src/pages/admin/` la
configuración.

Con Docker ya se sirve compilado en el puerto 5173. Para trabajar aquí con
recarga en caliente: `npm install && npm run dev`. Antes de dar un cambio por
bueno: `npx tsc --noEmit -p tsconfig.json && npm run build`.
