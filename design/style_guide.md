# Guía de estilo del panel

Sistema de diseño **real** implementado en `frontend/tailwind.config.js` y
`frontend/src/index.css`. Esa es la fuente de verdad; este documento la resume.

## Concepto

Cálido, editorial, calmado. Papel + tinta + acento
pistacho. **No** es SaaS azul corporativo ni glassmorphism.

## Color

### Modo claro
- **Fondos (papel):** `--paper #F7F2E9`, `--paper-2 #EFE8DA`, `--paper-3 #E7DFCD`
- **Tarjeta:** `--card #FFFCF6`
- **Tinta (texto):** `--ink #1A1612`, `--ink-2 #3A332A`, `--ink-3 #6A604F`, `--ink-4 #A39884`
- **Líneas:** `--line rgba(26,22,18,.10)`, `--line-2 rgba(26,22,18,.06)`

### Modo oscuro (`.dark`)
- **Papel:** `#14110D / #1C1814 / #24201A` · **Tarjeta:** `#1F1B15`
- **Tinta:** `#F3ECDC / #D8D0BD / #9A9180 / #6A614F`

### Marca (pistacho)
- `brand #DBE09E` (default) · `brand-600 #C6CC85` · `brand-700 #9DA362`
- Texto sobre marca: `brand-on #1A1612` · tinta de marca: `brand-ink #3F4525` (claro) / `#F7F8E8` (oscuro)
- **Usos:** botón primario, estado activo de nav, selección, badges de marca

### Color por canal
- WhatsApp `#25D366` · Web `#6C7BFF` · Voz `#9B8AFB`

### Estado
- ok `#2DA771` · warn `#E58A2F` · bad `#DC4B3C`

## Tipografía
- **Sans (UI):** Geist → Inter → system-ui
- **Display (títulos):** Instrument Serif → Georgia. Grande, con tratamiento `.accent` (cursiva + subrayado ondulado pistacho)
- **Números/métricas:** Space Grotesk (`tabular-nums`)
- **Mono (logs/IDs):** JetBrains Mono
- **Escala display:** h1 3rem · h2 2rem · h3 1.4rem (móvil: 2.25 / 1.6 / 1.2rem)

## Radios y sombras
- **Radios:** `coro 18px` (tarjetas) · `coro-sm 12px` (controles)
- **Sombras** (cálidas, baja opacidad): `coro-1` sutil · `coro-2` media · `coro-3` elevada
- **Scrollbar** fina 6px

## Componentes (clases en `index.css`)
- **Botones:** `.btn` (neutro) · `.btn-primary` (pistacho) · `.btn-ghost` · `.btn-danger`; modificadores `.btn-sm`, `.btn-icon`. Min-height 40px (32 en `sm`)
- **Inputs:** `.input`, `.textarea`, `.label` (uppercase + tracking)
- **Superficies:** `.card`
- **Badges/pills:** `.badge`, `.badge-brand`, `.pill` (+ `.dot`)
- **Tipográficos:** `.eyebrow`, `.h-display`, `.numbers`, `.accent`

## Layout
- **Sidebar** 220px (desktop) / drawer `<lg`. Secciones: **Operativa / Admin / Agente IA / Sistema**
- **Avatares:** iniciales sobre color de la paleta de marca (hash del email)

## Responsive
- Breakpoint clave: `lg` (sidebar estática vs. drawer). Componentes mobile-first.

## Accesibilidad
- Contraste objetivo **AA (4.5:1)**
- Focus visible: `ring-2 ring-brand/30`
- Navegación por teclado · `aria-label` en botones de icono
