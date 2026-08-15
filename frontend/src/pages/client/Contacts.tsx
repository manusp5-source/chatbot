import { useEffect, useRef, useState } from "react";
import { Search, Plus, X, Trash2, ChevronLeft, ChevronRight, Download, Upload } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/PageHeader";
import { errorDetail } from "@/lib/errors";
import { ConfirmModal } from "@/components/ConfirmModal";
import { EstadoSelect, ESTADO_LABEL, ESTADO_COLOR, ESTADO_OPTIONS } from "@/components/EstadoSelect";
import {
  listContacts,
  contactStats,
  createContact,
  listTags,
  updateContact,
  deleteContact,
  addContactTag,
  exportContacts,
  importContacts,
  type ContactStats,
} from "@/services/contacts";
import type { Contact, Tag } from "@/types";
import { usePrivacy } from "@/store/privacy";
import { useAuth } from "@/store/auth";
import { maskName, maskPhone, maskEmail, maskInitials } from "@/lib/mask";

const AVATAR_PALETTE = ["#DBE09E", "#9DA362", "#6C7BFF", "#9B8AFB", "#25D366", "#E58A2F"];

function initialsOf(nombre: string | null, telefono: string): string {
  const src = (nombre || telefono || "?").trim();
  const parts = src.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return src.slice(0, 2).toUpperCase();
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? parts[0]?.[1] ?? "")).toUpperCase();
}

function colorFor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_PALETTE[h % AVATAR_PALETTE.length];
}

const ORIGEN_META: Record<string, { label: string; color: string }> = {
  whatsapp: { label: "WhatsApp", color: "var(--ch-wa)" },
  web: { label: "Web", color: "var(--ch-web)" },
  instagram: { label: "Instagram", color: "#E1306C" },
  // Email existe como origen en el backend y faltaba en el desplegable de
  // canales: los contactos que entraron por correo no se podían filtrar.
  email: { label: "Email", color: "#EA4335" },
  manual: { label: "Manual", color: "var(--ink-4)" },
};

// ── La columna "Teléfono" no siempre lleva un teléfono ──────────────────────
//
// `Contact.telefono` es la LLAVE del contacto y cada canal guarda ahí la suya:
// `web:<uuid>`, `ig:<psid>`, `email:<direccion>`, `voice:<id>`, `wa:<bsuid>`.
// Se pintaba en crudo, así que en la fila de un visitante web salía un UUID
// donde debería ir un número; y en la ficha, encima, se generaban enlaces
// `tel:` y de WhatsApp con ese valor, que no llevan a ninguna parte.
const CHANNEL_ID_LABEL: Record<string, string> = {
  "web:": "Visitante web",
  "ig:": "Instagram",
  "email:": "Email",
  "voice:": "Llamada",
  "wa:": "WhatsApp",
};

/** True si la llave del contacto es un identificador de canal, no un teléfono. */
export function isChannelIdentifier(value: string | null | undefined): boolean {
  const v = (value || "").toLowerCase();
  return Object.keys(CHANNEL_ID_LABEL).some((p) => v.startsWith(p));
}

/** Cómo se enseña la llave del contacto cuando NO es un teléfono. */
export function displayIdentifier(value: string | null | undefined): string {
  const v = value || "";
  if (!isChannelIdentifier(v)) return v;
  const prefix = Object.keys(CHANNEL_ID_LABEL).find((p) => v.toLowerCase().startsWith(p))!;
  const rest = v.slice(prefix.length);
  // El email se lee entero; los identificadores opacos (uuid, psid) se
  // recortan: enseñarlos completos no aporta nada y se come la columna.
  if (prefix === "email:") return rest;
  return `${CHANNEL_ID_LABEL[prefix]} · ${rest.slice(0, 8)}…`;
}

const SORTS = [
  { value: "recent", label: "Última actividad" },
  { value: "name", label: "Nombre" },
  { value: "created", label: "Más recientes" },
];

const PAGE_SIZE = 25;

export default function Contacts() {
  const navigate = useNavigate();
  const priv = usePrivacy((s) => s.enabled);
  const [items, setItems] = useState<(Contact & { tags: Tag[] })[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<ContactStats | null>(null);
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);

  // Filtros
  const [search, setSearch] = useState("");
  // Buscador con espera: el de Contactos lanzaba UNA PETICIÓN POR TECLA y sin
  // control de orden, así que la respuesta lenta de "ma" podía llegar después de
  // la de "maria" y pintar los resultados equivocados. Se copia el patrón del
  // buscador de la bandeja, que ya lo hacía bien.
  const [debouncedSearch, setDebouncedSearch] = useState("");
  // "Gana la última": descarta las respuestas de peticiones ya superadas.
  const listSeq = useRef(0);
  // Error de las acciones en bloque / de la carga de la lista.
  const [listError, setListError] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(t);
  }, [search]);
  const [segment, setSegment] = useState(""); // "" | "nuevos" | "activos"
  const [estado, setEstado] = useState("");
  const [origen, setOrigen] = useState("");
  const [tagId, setTagId] = useState("");
  const [sort, setSort] = useState("recent");
  const [page, setPage] = useState(1);

  // El borrado de contactos es solo para admin (RGPD): el rol decide si se
  // pinta el botón de borrado en bloque. Importar/exportar CSV también son
  // admin-only en el backend (`require_admin` en /contacts/export e /import):
  // sin esta comprobación una operadora veía dos botones que solo dan 403.
  const role = useAuth((s) => s.user?.role);
  const isAdmin = role === "admin";
  const [showAdd, setShowAdd] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [ioBusy, setIoBusy] = useState(false);
  const [ioMsg, setIoMsg] = useState<string | null>(null);
  // Motivo de cada fila rechazada por la importación (fila N: por qué).
  const [ioErrors, setIoErrors] = useState<string[]>([]);

  async function onExport() {
    setIoBusy(true);
    setIoMsg(null);
    try {
      await exportContacts();
    } catch {
      setIoMsg("No se pudo exportar.");
    } finally {
      setIoBusy(false);
    }
  }

  async function onImportFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // permite re-subir el mismo archivo
    if (!file) return;
    setIoBusy(true);
    setIoMsg(null);
    try {
      const r = await importContacts(file);
      // Los errores por fila que devuelve el backend se ignoraban: las filas
      // rechazadas salían como "omitidos" a secas y no había forma de saber
      // por qué se había quedado fuera media agenda.
      setIoMsg(`Importado: ${r.created} nuevos, ${r.updated} actualizados, ${r.skipped} omitidos.`);
      setIoErrors(r.errors || []);
      loadStats();
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setIoMsg(errorDetail(err, "No se pudo importar el archivo."));
      setIoErrors([]);
    } finally {
      setIoBusy(false);
    }
  }

  function loadStats() {
    contactStats().then(setStats).catch(() => {});
  }

  function onEstadoChanged(id: string, next: string) {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, estado: next } : it)));
    loadStats();
  }

  function toggleOne(id: string) {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }
  function toggleAllOnPage() {
    setSelected((s) => {
      const all = items.length > 0 && items.every((c) => s.has(c.id));
      const n = new Set(s);
      items.forEach((c) => (all ? n.delete(c.id) : n.add(c.id)));
      return n;
    });
  }
  // ── Acciones en bloque: contar los fallos y DECIRLOS ────────────────────
  //
  // Antes iban con `Promise.all` sin `catch`: si fallaba una, la promesa entera
  // se rechazaba, `finally` desactivaba el "ocupado" y ahí acababa la historia.
  // Un fallo parcial dejaba unos borrados y otros no, sin decir cuántos ni
  // cuáles, y la lista se quedaba sin recargar mostrando lo que ya no existía.
  //
  // `allSettled` ejecuta todas, cuenta las que fallaron y siempre recarga.
  async function runBulk(
    ids: string[],
    action: (id: string) => Promise<unknown>,
    verbo: string,
  ): Promise<number> {
    const results = await Promise.allSettled(ids.map(action));
    const fallos = results.filter((r) => r.status === "rejected");
    if (fallos.length > 0) {
      const motivo = errorDetail(
        (fallos[0] as PromiseRejectedResult).reason,
        "el servidor lo rechazó",
      );
      setListError(
        fallos.length === ids.length
          ? `No se pudo ${verbo} ninguno de los ${ids.length} contactos: ${motivo}`
          : `Se ${verbo === "borrar" ? "borraron" : "actualizaron"} ${
              ids.length - fallos.length
            } de ${ids.length}. ${fallos.length} fallaron: ${motivo}`,
      );
    } else {
      setListError(null);
    }
    return fallos.length;
  }

  async function bulkEstado(next: string) {
    const ids = [...selected];
    setBulkBusy(true);
    try {
      await runBulk(ids, (id) => updateContact(id, { estado: next } as Partial<Contact>), "cambiar");
      setReloadKey((k) => k + 1);
      loadStats();
    } finally {
      setBulkBusy(false);
    }
  }
  async function bulkTag(tag: string) {
    const ids = [...selected];
    setBulkBusy(true);
    try {
      await runBulk(ids, (id) => addContactTag(id, tag), "etiquetar");
      setReloadKey((k) => k + 1);
    } finally {
      setBulkBusy(false);
    }
  }
  async function bulkDelete() {
    const ids = [...selected];
    setBulkBusy(true);
    try {
      await runBulk(ids, (id) => deleteContact(id), "borrar");
      setConfirmBulkDelete(false);
      setReloadKey((k) => k + 1);
      loadStats();
    } finally {
      setBulkBusy(false);
    }
  }

  useEffect(() => {
    loadStats();
    listTags().then(setAllTags).catch(() => {});
  }, []);

  useEffect(() => {
    setLoading(true);
    const seq = ++listSeq.current;
    listContacts({
      search: debouncedSearch || undefined,
      segment: segment || undefined,
      estado: estado || undefined,
      origen: origen || undefined,
      tag_id: tagId || undefined,
      sort,
      page,
      page_size: PAGE_SIZE,
    })
      .then((p) => {
        // Ignora la respuesta si ya se pidió otra por detrás.
        if (seq !== listSeq.current) return;
        setItems(p.items);
        setTotal(p.total);
        setSelected(new Set());
        setListError(null);
      })
      .catch((e) => {
        if (seq !== listSeq.current) return;
        // Sin catch, un fallo de la API dejaba la tabla vacía con el mismo
        // aspecto que "no hay contactos con estos filtros".
        setListError(errorDetail(e, "No se pudo cargar la lista de contactos."));
      })
      .finally(() => {
        if (seq === listSeq.current) setLoading(false);
      });
  }, [debouncedSearch, segment, estado, origen, tagId, sort, page, reloadKey]);

  // Cualquier cambio de filtro vuelve a la página 1.
  function reset<T>(setter: (v: T) => void) {
    return (v: T) => {
      setter(v);
      setPage(1);
    };
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const to = (page - 1) * PAGE_SIZE + items.length;

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        eyebrow="Operativa · CRM"
        title={<>Contactos</>}
        description="Personas que han contactado o que añadiste manualmente."
        actions={
          <div className="flex items-center gap-2">
            {/* Importar/Exportar CSV: solo admin (el backend responde 403 al
                resto). Mismo criterio que el borrado en bloque de abajo. */}
            {isAdmin && (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".csv,text/csv"
                  className="hidden"
                  onChange={onImportFile}
                />
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={ioBusy}
                  title="Importar contactos desde CSV (dedup por teléfono)"
                >
                  <Upload className="w-4 h-4" /> <span className="hidden sm:inline">Importar</span>
                </button>
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={onExport}
                  disabled={ioBusy}
                  title="Exportar todos los contactos a CSV"
                >
                  <Download className="w-4 h-4" /> <span className="hidden sm:inline">Exportar</span>
                </button>
              </>
            )}
            <button type="button" className="btn-primary" onClick={() => setShowAdd(true)}>
              <Plus /> <span className="hidden sm:inline">Nuevo contacto</span>
            </button>
          </div>
        }
      />
      {ioMsg && (
        <div className="mx-4 md:mx-6 mt-3 text-sm text-ink2 bg-paper2 border border-line rounded-coro-sm px-3 py-2">
          <div className="flex items-center justify-between gap-3">
            <span>{ioMsg}</span>
            <button
              type="button"
              className="text-ink3 hover:text-ink shrink-0"
              onClick={() => {
                setIoMsg(null);
                setIoErrors([]);
              }}
              aria-label="Cerrar"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
          {/* Por qué se quedó fuera cada fila. Antes salían como "omitidos" y
              ahí se acababa la explicación. */}
          {ioErrors.length > 0 && (
            <ul className="mt-2 pt-2 border-t border-line space-y-0.5 text-[12px] text-state-warn max-h-40 overflow-auto">
              {ioErrors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {listError && (
        <div className="mx-4 md:mx-6 mt-3 text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-center justify-between gap-3">
          <span>{listError}</span>
          <button
            type="button"
            className="text-ink3 hover:text-ink shrink-0"
            onClick={() => setListError(null)}
            aria-label="Cerrar"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}
      <div className="flex-1 overflow-auto p-4 md:p-6 space-y-4">
        {/* KPIs */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi label="Total" value={stats?.total} />
          <Kpi label="Nuevos · 30d" value={stats?.nuevos_30d} />
          <Kpi label="Activos · 30d" value={stats?.activos_30d} />
          <Kpi label="Clientes" value={stats?.by_estado?.cliente ?? 0} />
        </div>

        {/* Segmentos */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <Seg label="Todos" count={stats?.total} active={segment === ""} onClick={() => reset(setSegment)("")} />
          <Seg label="Nuevos" count={stats?.nuevos_30d} active={segment === "nuevos"} onClick={() => reset(setSegment)("nuevos")} />
          <Seg label="Activos" count={stats?.activos_30d} active={segment === "activos"} onClick={() => reset(setSegment)("activos")} />
        </div>

        {/* Filtros */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink3" />
            <input
              placeholder="Buscar nombre, teléfono, email…"
              value={search}
              onChange={(e) => reset(setSearch)(e.target.value)}
              className="input pl-9 w-full"
            />
          </div>
          <select className="input w-full sm:w-auto" value={estado} onChange={(e) => reset(setEstado)(e.target.value)}>
            <option value="">Todos los estados</option>
            {Object.entries(ESTADO_LABEL).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
          <select className="input w-full sm:w-auto" value={origen} onChange={(e) => reset(setOrigen)(e.target.value)}>
            <option value="">Todos los canales</option>
            {Object.entries(ORIGEN_META).map(([v, m]) => (
              <option key={v} value={v}>{m.label}</option>
            ))}
          </select>
          <select className="input w-full sm:w-auto" value={tagId} onChange={(e) => reset(setTagId)(e.target.value)}>
            <option value="">Todas las etiquetas</option>
            {allTags.map((t) => (
              <option key={t.id} value={t.id}>{t.nombre}</option>
            ))}
          </select>
          <select className="input w-full sm:w-auto" value={sort} onChange={(e) => setSort(e.target.value)}>
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>Orden: {s.label}</option>
            ))}
          </select>
        </div>

        {/* Barra de acciones en bloque (desktop) */}
        {selected.size > 0 && (
          <div className="card p-2.5 hidden md:flex items-center gap-3 flex-wrap bg-paper2">
            <span className="text-sm text-ink2 font-medium">
              {selected.size} seleccionado{selected.size > 1 ? "s" : ""}
            </span>
            <select
              className="input w-auto text-xs"
              value=""
              onChange={(e) => { if (e.target.value) void bulkEstado(e.target.value); }}
              disabled={bulkBusy}
            >
              <option value="">Cambiar estado…</option>
              {ESTADO_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            <select
              className="input w-auto text-xs"
              value=""
              onChange={(e) => { if (e.target.value) void bulkTag(e.target.value); }}
              disabled={bulkBusy || allTags.length === 0}
            >
              <option value="">Añadir etiqueta…</option>
              {allTags.map((t) => (
                <option key={t.id} value={t.id}>{t.nombre}</option>
              ))}
            </select>
            {/* Borrado RGPD: solo admin (el backend responde 403 al resto). */}
            {isAdmin && (
              <button type="button" className="btn-danger btn-sm" onClick={() => setConfirmBulkDelete(true)} disabled={bulkBusy}>
                <Trash2 className="w-3.5 h-3.5" /> Borrar
              </button>
            )}
            <button type="button" className="btn-ghost btn-sm" onClick={() => setSelected(new Set())} disabled={bulkBusy}>
              Deseleccionar
            </button>
          </div>
        )}

        {/* Desktop: tabla */}
        <div className="card hidden md:block overflow-x-auto">
          <table className="w-full text-sm min-w-[940px]">
            <thead className="text-[11px] uppercase tracking-wider text-ink3 bg-paper2">
              <tr>
                <th className="px-3 py-2.5 w-8">
                  <input
                    type="checkbox"
                    checked={items.length > 0 && items.every((c) => selected.has(c.id))}
                    onChange={toggleAllOnPage}
                    className="cursor-pointer align-middle"
                    aria-label="Seleccionar todos"
                  />
                </th>
                <th className="text-left px-4 py-2.5 font-medium">Contacto</th>
                <th className="text-left px-4 py-2.5 font-medium">Estado</th>
                <th className="text-left px-4 py-2.5 font-medium">Servicio</th>
                <th className="text-left px-4 py-2.5 font-medium">Email</th>
                <th className="text-left px-4 py-2.5 font-medium">Canal</th>
                <th className="text-left px-4 py-2.5 font-medium">Etiquetas</th>
                <th className="text-left px-4 py-2.5 font-medium">Alta</th>
                <th className="text-left px-4 py-2.5 font-medium">Última actividad</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={9} className="text-center text-ink3 py-10 italic font-display">Cargando…</td></tr>
              )}
              {!loading && items.length === 0 && (
                <tr><td colSpan={9} className="text-center text-ink3 py-10 italic font-display">Sin contactos con estos filtros.</td></tr>
              )}
              {items.map((c) => (
                <tr
                  key={c.id}
                  onClick={() => navigate(`/contacts/${c.id}`)}
                  className="border-t border-line2 hover:bg-paper2 cursor-pointer"
                >
                  <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selected.has(c.id)}
                      onChange={() => toggleOne(c.id)}
                      className="cursor-pointer align-middle"
                      aria-label="Seleccionar"
                    />
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-2.5">
                      <div
                        className="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold shrink-0"
                        style={{ background: colorFor(c.telefono || c.id), color: "var(--brand-on)" }}
                      >
                        {maskInitials(initialsOf(c.nombre, c.telefono), priv)}
                      </div>
                      <div className="min-w-0">
                        <div className="text-ink truncate">
                          {maskName(c.nombre, priv) || <span className="text-ink3 italic">Sin nombre</span>}
                        </div>
                        {/* Los contactos de web, email, Instagram y voz no
                            tienen teléfono: llevan la llave de su canal. Se
                            enseñaba en crudo (un UUID donde debía ir un
                            número). */}
                        <div className="font-mono text-[11px] text-ink3 truncate">
                          {isChannelIdentifier(c.telefono)
                            ? displayIdentifier(c.telefono)
                            : maskPhone(c.telefono, priv)}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-2.5">
                    <EstadoSelect contactId={c.id} value={c.estado} onChanged={(next) => onEstadoChanged(c.id, next)} />
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="max-w-[160px] truncate text-ink2">
                      {c.servicio_interes || <span className="text-ink4">—</span>}
                    </div>
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="max-w-[220px] truncate text-ink2">
                      {maskEmail(c.email, priv) || <span className="text-ink4">—</span>}
                    </div>
                  </td>
                  <td className="px-4 py-2.5">
                    <OrigenBadge origen={c.origen} />
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex flex-wrap gap-1">
                      {c.tags.slice(0, 3).map((t) => (
                        <span key={t.id} className="badge text-[10px] border-transparent" style={{ backgroundColor: t.color + "26", color: t.color }}>
                          {t.nombre}
                        </span>
                      ))}
                      {c.tags.length > 3 && <span className="text-[10px] text-ink3">+{c.tags.length - 3}</span>}
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-[11px] text-ink3 font-mono whitespace-nowrap">
                    {c.created_at ? new Date(c.created_at).toLocaleDateString("es-ES") : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-[11px] text-ink3 font-mono whitespace-nowrap">
                    {c.ultimo_mensaje_at ? new Date(c.ultimo_mensaje_at).toLocaleString("es-ES") : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Mobile: cards */}
        <div className="md:hidden space-y-2">
          {loading && <div className="text-center text-ink3 py-10 italic font-display">Cargando…</div>}
          {!loading && items.length === 0 && (
            <div className="text-center text-ink3 py-10 italic font-display">Sin contactos con estos filtros.</div>
          )}
          {items.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => navigate(`/contacts/${c.id}`)}
              className="card p-4 w-full text-left flex items-center gap-3 hover:bg-paper2"
            >
              <div
                className="w-10 h-10 rounded-full flex items-center justify-center text-sm font-bold shrink-0"
                style={{ background: colorFor(c.telefono || c.id), color: "var(--brand-on)" }}
              >
                {maskInitials(initialsOf(c.nombre, c.telefono), priv)}
              </div>
              <div className="flex-1 min-w-0">
                <div className="font-medium text-ink truncate">
                  {maskName(c.nombre, priv) || <span className="text-ink3 italic">Sin nombre</span>}
                </div>
                <div className="text-[11px] text-ink3 font-mono truncate">
                  {isChannelIdentifier(c.telefono)
                    ? displayIdentifier(c.telefono)
                    : maskPhone(c.telefono, priv)}
                </div>
                <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                  <OrigenBadge origen={c.origen} />
                  <span className={"badge text-[10px] border-transparent " + (ESTADO_COLOR[c.estado] || "bg-paper2 text-ink3")}>
                    {ESTADO_LABEL[c.estado] || c.estado}
                  </span>
                  {c.tags.slice(0, 2).map((t) => (
                    <span key={t.id} className="badge text-[10px] border-transparent" style={{ backgroundColor: t.color + "26", color: t.color }}>
                      {t.nombre}
                    </span>
                  ))}
                </div>
              </div>
            </button>
          ))}
        </div>

        {/* Paginación */}
        {total > 0 && (
          <div className="flex items-center justify-between text-[12px] text-ink3">
            <span>Mostrando {from}–{to} de <b className="text-ink2 numbers">{total}</b></span>
            <div className="flex items-center gap-2">
              <button type="button" className="btn-ghost btn-sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                <ChevronLeft className="w-4 h-4" />
              </button>
              <span className="numbers text-xs">{page} / {totalPages}</span>
              <button type="button" className="btn-ghost btn-sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>

      {showAdd && (
        <QuickAdd
          onClose={() => setShowAdd(false)}
          onCreated={(id) => {
            setShowAdd(false);
            loadStats();
            navigate(`/contacts/${id}`);
          }}
        />
      )}

      <ConfirmModal
        open={confirmBulkDelete}
        tone="danger"
        title="Borrar contactos"
        description={`Vas a borrar ${selected.size} contacto(s) y sus conversaciones asociadas. Esta acción no se puede deshacer.`}
        confirmLabel="Sí, borrar"
        busy={bulkBusy}
        onConfirm={bulkDelete}
        onCancel={() => setConfirmBulkDelete(false)}
      />
    </div>
  );
}

function Kpi({ label, value }: { label: string; value?: number }) {
  return (
    <div className="card p-3">
      <div className="eyebrow">{label}</div>
      <div className="font-display text-ink mt-1 numbers" style={{ fontSize: 24 }}>
        {value ?? "—"}
      </div>
    </div>
  );
}

function Seg({ label, count, active, onClick }: { label: string; count?: number; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={"badge cursor-pointer " + (active ? "bg-brand text-brand-on" : "bg-paper2 text-ink3 hover:bg-paper3")}
    >
      {label}
      {typeof count === "number" && <span className="ml-1.5 numbers opacity-70">{count}</span>}
    </button>
  );
}

function OrigenBadge({ origen }: { origen: string }) {
  const m = ORIGEN_META[origen] ?? { label: origen, color: "var(--ink-4)" };
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink2">
      <span className="w-2 h-2 rounded-full" style={{ background: m.color }} />
      {m.label}
    </span>
  );
}

function QuickAdd({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const [telefono, setTelefono] = useState("");
  // Email en el alta manual: el modal solo pedía teléfono, así que un cliente
  // que solo existe por correo no se podía dar de alta desde aquí.
  const [email, setEmail] = useState("");
  const [nombre, setNombre] = useState("");
  const [estado, setEstado] = useState("contacto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const tel = telefono.trim();
    const mail = email.trim().toLowerCase();
    // Uno de los dos, no los dos: con teléfono manda el teléfono; solo con
    // email, el backend abre la ficha con la llave `email:<direccion>`, que es
    // la misma que usa el canal de correo.
    if (!tel && !mail) {
      setError("Pon un teléfono o un email.");
      return;
    }
    if (mail && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(mail)) {
      setError("Ese email no tiene buena pinta.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const c = await createContact({
        telefono: tel || null,
        email: mail || null,
        nombre: nombre.trim() || null,
        estado,
      } as Partial<Contact>);
      onCreated(c.id);
    } catch (e) {
      // El backend explica el motivo (409 por duplicado, 422 por datos que no
      // valen). Mostrarlo es mejor que "No se pudo crear el contacto".
      setError(errorDetail(e, "No se pudo crear el contacto."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-ink/30" onClick={onClose}>
      <div className="card p-5 w-full max-w-sm max-h-[90vh] overflow-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-display text-ink" style={{ fontSize: 20 }}>Nuevo contacto</h3>
          <button type="button" className="btn-icon" onClick={onClose}><X className="w-4 h-4" /></button>
        </div>
        <div className="space-y-3">
          <p className="text-[12px] text-ink3">
            Con el teléfono o con el email basta. Los dos, mejor.
          </p>
          <div>
            <label className="label">Teléfono</label>
            <input
              className="input"
              value={telefono}
              onChange={(e) => setTelefono(e.target.value)}
              placeholder="+34600000000"
              autoFocus
            />
            <p className="text-[11px] text-ink4 mt-1">
              Se guarda en formato internacional; los espacios y guiones se
              quitan solos.
            </p>
          </div>
          <div>
            <label className="label">Email</label>
            <input
              className="input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="cliente@empresa.com"
            />
          </div>
          <div>
            <label className="label">Nombre</label>
            <input className="input" value={nombre} onChange={(e) => setNombre(e.target.value)} placeholder="Opcional" />
          </div>
          <div>
            <label className="label">Estado</label>
            <select className="input" value={estado} onChange={(e) => setEstado(e.target.value)}>
              {Object.entries(ESTADO_LABEL).map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </div>
          {error && <div className="text-xs text-state-bad">{error}</div>}
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>Cancelar</button>
          <button type="button" className="btn-primary" onClick={submit} disabled={busy}>{busy ? "Creando…" : "Crear"}</button>
        </div>
      </div>
    </div>
  );
}
