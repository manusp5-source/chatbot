# CRM de Contactos — plan

Evolucionar **Contactos + ficha** a un **CRM operativo**,
**genérico y adaptable**: los prototipos de partida eran de un negocio de **citas**
(citas, €, LTV, profesionales), y esto no da nada de eso por hecho. Igual que en el dashboard,
producto genérico; lo específico de cada negocio se modela con **campos
personalizados** (fase D).

## Criterios
- **Genérico**: NADA de citas/ingresos/LTV/profesionales hardcodeado.
- **Campos personalizados**: para **DESPUÉS** (fase D). Primero A–C con los campos
  fijos que ya existen.

## Backend: ya está medio hecho (CRM-ready)
- `Contact`: telefono, nombre, email, **estado** (pipeline 6: contacto →
  solicitud_presupuesto → seguimiento → cliente → perdido → no_cualifica), origen,
  servicio_interes, **notas_internas (cifrado)**, in_crm, created_at/updated_at;
  conversaciones enlazadas (FK).
- **Tags** m2m con color; endpoints crear/editar(PATCH)/borrar/asignar/quitar.
- Contacts API: list (`search`, `estado`, `tag_id`, `include_outside_crm`, `page`,
  `page_size`), get, create, update(PATCH), delete, `/conversations`, add/remove
  tag, `/add-to-crm`. `ContactOut` NO expone `created_at/updated_at` (añadir en C).

## Fases
- **A ✅ Etiquetas en la ficha + menú limpio**: `components/TagPicker.tsx`
  (popover buscar / asignar-quitar / crear con color / borrar global) en la ficha;
  **eliminada la página `/tags`** (ruta + menú + archivo). Backend sin cambios.
- **B ✅ Lista de contactos CRM**: segmentos (Todos/Nuevos/Activos 30d),
  filtros (estado, canal/origen, etiqueta, orden), buscador, **alta rápida** (modal),
  paginación y tira de KPIs. Backend: filtros `origen`/`segment`/`sort` +
  `GET /contacts/stats` + `created_at`/`updated_at` en `ContactOut`.
- **C — Ficha estilo CRM**: hero (avatar, nombre con acento, chips de canal, acciones),
  tarjeta de datos, edición inline, notas, conversaciones multicanal, "contacto desde"
  (`created_at`). Genérico (sin citas/€). Arreglar `PageHeader` (title:ReactNode + eyebrow)
  de paso — hoy genera errores tsc en varias páginas.
  **Se mantienen las SECCIONES del prototipo** (tab-nav + secciones
  ancladas con scroll-spy). Secciones genéricas: **Resumen · Datos · Conversaciones ·
  Notas · Actividad** (fuera Citas/Servicios/Adjuntos, que son del negocio de citas).
  "Algunas ideas" del prototipo a confirmar al arrancar C.
- **D — Campos personalizados** (bloque grande, más adelante): modelo (secciones +
  definiciones con ~12 tipos + valores por contacto + audit), página de gestión en
  Sistema, edición inline en la ficha, y **lectura por el agente IA** (campos
  indexables). Es lo que vuelve el CRM realmente adaptable por negocio.

## Notas del prototipo (para C y D)
- **Lista**: es una **tabla** (no cards) con checkbox para acciones en bloque; columnas
  Contacto / Canales / Etiquetas / Última actividad / health-pip; KPIs arriba;
  **view-tabs** = segmentos guardados; filtros por canal con contador; paginación prev/next.
- **Ficha**: layout editorial — hero + **tab-nav con scroll-spy** + secciones ancladas
  (Resumen, Datos, Conversaciones, Notas, Actividad). Identity card con filas key/valor.
  Timeline de **actividad** (no tenemos audit feed aún; en C se puede aproximar con
  conversaciones + cambios de etiqueta). Notas multi-autor (hoy `notas_internas` único).
- **Etiquetas**: chips + "+ etiqueta" dashed → popover. Colores: esta app usa hex
  arbitrario (swatches en TagPicker); el prototipo usa variantes semánticas.
- **Campos personalizados** (D): tipos = texto corto/largo, número, dinero, fecha,
  booleano, select, multi-chip, usuario, teléfono, email, url; por sección; obligatorio;
  visibilidad por rol (todos/admin/api); `visible_in_table`; `indexable_by_ai`; borrador/activo.

Sistema visual: el de la app (`design/style_guide.md` y `frontend/src/index.css`).
