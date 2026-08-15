import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Search, Check, Loader2, CheckCheck, X, Users } from "lucide-react";
import { listContacts, listTags } from "@/services/contacts";
import type { Tag } from "@/types";
import { usePrivacy } from "@/store/privacy";
import { maskName, maskPhone } from "@/lib/mask";

/** Contacto seleccionado para un envío masivo. Solo lo que necesita el envío. */
export interface PickedContact {
  id: string;
  telefono: string;
  nombre: string | null;
}

// Enums del backend (app/models/contact.py). Mantener en sync.
const ESTADO_OPTIONS = [
  { value: "", label: "Todos los estados" },
  { value: "contacto", label: "Contacto" },
  { value: "solicitud_presupuesto", label: "Solicitud presupuesto" },
  { value: "seguimiento", label: "Seguimiento" },
  { value: "cliente", label: "Cliente" },
  { value: "perdido", label: "Perdido" },
  { value: "no_cualifica", label: "No cualifica" },
];
const ORIGEN_OPTIONS = [
  { value: "", label: "Todos los orígenes" },
  { value: "whatsapp", label: "WhatsApp" },
  { value: "web", label: "Web" },
  { value: "instagram", label: "Instagram" },
  { value: "email", label: "Email" },
  { value: "manual", label: "Manual" },
];
const SEGMENT_OPTIONS = [
  { value: "", label: "Todos" },
  { value: "nuevos", label: "Nuevos (30d)" },
  { value: "activos", label: "Activos (30d)" },
];

const PAGE_SIZE = 20;
// Tope al "seleccionar todos del filtro" — coincide con el cap del job en backend.
const SELECT_ALL_CAP = 1000;
// Estados a los que normalmente NO se manda una difusión. El filtro por defecto
// («Todos los estados») los incluía sin decir nada: acababas mandándole una
// promoción a quien ya dijiste que no cualificaba.
const ESTADOS_EXCLUIDOS_POR_DEFECTO = new Set(["perdido", "no_cualifica"]);

interface PickerRow {
  id: string;
  telefono: string;
  nombre: string | null;
  estado: string;
}

/** Resumen honesto de lo que hizo "seleccionar todos del filtro". */
interface SelectAllSummary {
  enFiltro: number;
  anadidos: number;
  sinWhatsapp: number;
  porEstado: number;
  topeAlcanzado: boolean;
}

interface Props {
  selected: PickedContact[];
  onChange: (next: PickedContact[]) => void;
}

/**
 * Selector de contactos del CRM para el envío masivo. Reutiliza `listContacts`
 * con sus filtros (búsqueda, estado, origen, etiqueta, segmento) y permite
 * "seleccionar todos los del filtro actual" recorriendo las páginas.
 *
 * El teléfono de WhatsApp solo está en contactos cuyo `telefono` es un número
 * E.164 real (los de otros canales llevan prefijo email:/ig:/web:). Filtramos
 * esos al seleccionar para no intentar enviarles una plantilla de WhatsApp.
 */
export function ContactPicker({ selected, onChange }: Props) {
  const priv = usePrivacy((s) => s.enabled);
  const [search, setSearch] = useState("");
  const [estado, setEstado] = useState("");
  const [origen, setOrigen] = useState("");
  const [tagId, setTagId] = useState("");
  const [segment, setSegment] = useState("");
  const [page, setPage] = useState(1);

  const [items, setItems] = useState<PickerRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectingAll, setSelectingAll] = useState(false);
  const [tags, setTags] = useState<Tag[]>([]);
  // Por defecto fuera: es lo que casi nadie quiere en una difusión, y antes
  // entraban en silencio.
  const [excluirDescartados, setExcluirDescartados] = useState(true);
  const [summary, setSummary] = useState<SelectAllSummary | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const selectedIds = useMemo(() => new Set(selected.map((c) => c.id)), [selected]);

  const filterParams = useMemo(
    () => ({
      search: search.trim() || undefined,
      estado: estado || undefined,
      origen: origen || undefined,
      tag_id: tagId || undefined,
      segment: segment || undefined,
    }),
    [search, estado, origen, tagId, segment],
  );

  useEffect(() => {
    listTags().then(setTags).catch(() => setTags([]));
  }, []);

  // Cualquier cambio de filtro vuelve a la página 1.
  useEffect(() => {
    setPage(1);
  }, [filterParams]);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      let cancelled = false;
      setLoading(true);
      setError(null);
      listContacts({ ...filterParams, page, page_size: PAGE_SIZE, sort: "recent" })
        .then((res) => {
          if (cancelled) return;
          setItems(
            res.items.map((c) => ({
              id: c.id,
              telefono: c.telefono,
              nombre: c.nombre,
              estado: c.estado,
            })),
          );
          setTotal(res.total);
        })
        .catch((e) => {
          if (cancelled) return;
          const ax = e as { response?: { data?: { detail?: string } }; message?: string };
          setError(ax.response?.data?.detail || ax.message || "No se pudieron cargar contactos");
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
      return () => {
        cancelled = true;
      };
    }, 250);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [filterParams, page]);

  // ¿Este contacto entra en la difusión con los ajustes actuales?
  const excluded = useCallback(
    (c: PickerRow) =>
      excluirDescartados && ESTADOS_EXCLUIDOS_POR_DEFECTO.has(c.estado),
    [excluirDescartados],
  );

  function toggle(c: PickerRow) {
    if (!isSendablePhone(c.telefono) || excluded(c)) return;
    if (selectedIds.has(c.id)) {
      onChange(selected.filter((s) => s.id !== c.id));
    } else {
      onChange([...selected, { id: c.id, telefono: c.telefono, nombre: c.nombre }]);
    }
  }

  // Recorre todas las páginas del filtro actual y añade los contactos que se
  // pueden enviar, hasta SELECT_ALL_CAP. Al terminar deja un RESUMEN de lo que
  // pasó: antes el botón prometía 247 y acababas con 120 sin explicación, y el
  // tope de 1000 se aplicaba en silencio.
  async function selectAllInFilter() {
    setSelectingAll(true);
    setError(null);
    setSummary(null);
    try {
      const collected = new Map(selected.map((c) => [c.id, c]));
      const yaEstaban = collected.size;
      let p = 1;
      let enFiltro = 0;
      let sinWhatsapp = 0;
      let porEstado = 0;
      let topeAlcanzado = false;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const res = await listContacts({ ...filterParams, page: p, page_size: 100, sort: "recent" });
        enFiltro = res.total;
        for (const c of res.items) {
          if (collected.size >= SELECT_ALL_CAP) {
            topeAlcanzado = true;
            break;
          }
          if (!isSendablePhone(c.telefono)) {
            sinWhatsapp += 1;
            continue;
          }
          if (excluirDescartados && ESTADOS_EXCLUIDOS_POR_DEFECTO.has(c.estado)) {
            porEstado += 1;
            continue;
          }
          if (!collected.has(c.id)) {
            collected.set(c.id, { id: c.id, telefono: c.telefono, nombre: c.nombre });
          }
        }
        if (topeAlcanzado) break;
        if (res.page * res.page_size >= res.total) break;
        p += 1;
      }
      onChange(Array.from(collected.values()));
      setSummary({
        enFiltro,
        anadidos: collected.size - yaEstaban,
        sinWhatsapp,
        porEstado,
        topeAlcanzado,
      });
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(ax.response?.data?.detail || ax.message || "No se pudieron seleccionar todos");
    } finally {
      setSelectingAll(false);
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const ocultosEnPagina = items.filter(excluded).length;

  return (
    <div className="space-y-2">
      {/* Filtros */}
      <div className="flex flex-wrap gap-1.5 items-center">
        <div className="flex items-center gap-1.5 px-2 py-1 rounded-coro-sm bg-paper2 border border-line flex-1 min-w-[10rem]">
          <Search className="w-3.5 h-3.5 text-ink4 shrink-0" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Buscar nombre, email o teléfono…"
            className="bg-transparent text-xs flex-1 outline-none text-ink"
          />
        </div>
        <select value={estado} onChange={(e) => setEstado(e.target.value)} className="input text-xs w-full sm:w-auto">
          {ESTADO_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select value={origen} onChange={(e) => setOrigen(e.target.value)} className="input text-xs w-full sm:w-auto">
          {ORIGEN_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select value={tagId} onChange={(e) => setTagId(e.target.value)} className="input text-xs w-full sm:w-auto">
          <option value="">Todas las etiquetas</option>
          {tags.map((t) => (
            <option key={t.id} value={t.id}>{t.nombre}</option>
          ))}
        </select>
        <select value={segment} onChange={(e) => setSegment(e.target.value)} className="input text-xs w-full sm:w-auto">
          {SEGMENT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>

      {/* Acciones de selección */}
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <button
          type="button"
          onClick={selectAllInFilter}
          disabled={selectingAll || loading || total === 0}
          className="btn-ghost text-xs inline-flex items-center gap-1"
          title={`El filtro tiene ${total} contacto(s); se añadirán solo los que se puedan enviar por WhatsApp`}
        >
          {selectingAll ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCheck className="w-3.5 h-3.5" />}
          Seleccionar todos los enviables del filtro
        </button>
        <span className="text-ink3">{total} en el filtro</span>
        <span className="text-ink3 inline-flex items-center gap-1">
          <Users className="w-3.5 h-3.5" /> {selected.length} seleccionado(s)
        </span>
        {selected.length > 0 && (
          <button
            type="button"
            onClick={() => onChange([])}
            className="btn-ghost text-xs inline-flex items-center gap-1 text-state-bad"
          >
            <X className="w-3.5 h-3.5" /> Limpiar
          </button>
        )}
      </div>

      <label className="flex items-center gap-1.5 text-[11px] text-ink3 cursor-pointer">
        <input
          type="checkbox"
          checked={excluirDescartados}
          onChange={(e) => setExcluirDescartados(e.target.checked)}
          className="accent-brand-ink"
        />
        Dejar fuera los contactos en estado «Perdido» y «No cualifica»
        {ocultosEnPagina > 0 && (
          <span className="text-state-warn">
            ({ocultosEnPagina} de esta página quedan fuera)
          </span>
        )}
      </label>

      {summary && (
        <div className="text-[11px] text-ink2 bg-paper2 border border-line rounded-coro-sm px-2.5 py-1.5">
          Añadidos <b>{summary.anadidos}</b> de {summary.enFiltro} del filtro.
          {summary.sinWhatsapp > 0 && ` ${summary.sinWhatsapp} sin WhatsApp.`}
          {summary.porEstado > 0 && ` ${summary.porEstado} descartados por estado.`}
          {summary.topeAlcanzado && (
            <span className="text-state-warn">
              {" "}
              Se alcanzó el tope de {SELECT_ALL_CAP} destinatarios por envío: el
              resto no se ha añadido.
            </span>
          )}
        </div>
      )}

      {error && <div className="text-state-bad text-xs">{error}</div>}

      {/* Lista paginada */}
      <div className="border border-line rounded-coro-sm divide-y divide-line max-h-72 overflow-auto">
        {loading ? (
          <div className="p-3 text-ink3 text-xs inline-flex items-center gap-1.5">
            <Loader2 className="w-3.5 h-3.5 animate-spin" /> Cargando…
          </div>
        ) : items.length === 0 ? (
          <div className="p-3 text-ink3 text-xs italic">Sin contactos para este filtro.</div>
        ) : (
          items.map((c) => {
            const on = selectedIds.has(c.id);
            const fueraPorEstado = excluded(c);
            const sendable = isSendablePhone(c.telefono) && !fueraPorEstado;
            const motivo = fueraPorEstado
              ? "Fuera por su estado. Desmarca la casilla de arriba para incluirlo."
              : reasonNotSendable(c.telefono);
            return (
              <button
                key={c.id}
                type="button"
                onClick={() => toggle(c)}
                disabled={!sendable}
                className={`w-full flex items-center gap-2 px-2.5 py-1.5 text-left text-xs ${
                  sendable ? "hover:bg-paper2" : "opacity-50 cursor-not-allowed"
                }`}
                title={sendable ? undefined : motivo}
              >
                <span
                  className={`w-4 h-4 rounded border flex items-center justify-center shrink-0 ${
                    on ? "bg-brand-ink border-brand-ink" : "border-line"
                  }`}
                >
                  {on && <Check className="w-3 h-3 text-paper" />}
                </span>
                <span className="flex-1 truncate text-ink2">{maskName(c.nombre, priv) || "(sin nombre)"}</span>
                <span className="font-mono text-ink3 truncate max-w-[9rem]">{maskPhone(c.telefono, priv)}</span>
              </button>
            );
          })
        )}
      </div>

      {/* Paginación */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-xs text-ink3">
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1 || loading}
            className="btn-ghost text-xs disabled:opacity-40"
          >
            ← Anterior
          </button>
          <span>Página {page} de {totalPages}</span>
          <button
            type="button"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages || loading}
            className="btn-ghost text-xs disabled:opacity-40"
          >
            Siguiente →
          </button>
        </div>
      )}
    </div>
  );
}

// Prefijo del identificador de contacto de WhatsApp cuando NO hay teléfono y
// solo tenemos el BSUID (espejo de WA_USER_PREFIX en providers/whatsapp/base.py).
const WA_USER_PREFIX = "wa:";

/**
 * ¿Se le puede mandar una plantilla de WhatsApp a este contacto?
 *
 * Sí en dos casos:
 *   - teléfono E.164 real;
 *   - identificador `wa:<bsuid>` — el cliente con NOMBRE DE USUARIO de
 *     WhatsApp, del que Meta ya no manda el teléfono. El backend sabe
 *     enviarles desde hace tiempo (usa `recipient` en vez de `to`), pero aquí
 *     se descartaban por llevar dos puntos —el filtro era para email:/ig:/web:—
 *     y quedaban fuera de TODA difusión, con un tooltip que además mentía.
 */
export function isSendablePhone(telefono: string): boolean {
  const t = (telefono || "").trim();
  if (!t) return false;
  if (t.startsWith(WA_USER_PREFIX)) return t.length > WA_USER_PREFIX.length;
  if (t.includes(":")) return false; // email:, ig:, web:
  return /^\+?\d[\d\s-]*$/.test(t);
}

/** Por qué este contacto no entra en la difusión. Solo para el tooltip. */
function reasonNotSendable(telefono: string): string {
  const t = (telefono || "").trim();
  if (!t) return "Este contacto no tiene teléfono";
  if (t.startsWith("email:")) return "Contacto de email: no se le puede enviar por WhatsApp";
  if (t.startsWith("ig:")) return "Contacto de Instagram: no se le puede enviar por WhatsApp";
  if (t.startsWith("web:")) return "Contacto del chat web: no se le puede enviar por WhatsApp";
  return "El identificador no es un teléfono de WhatsApp válido";
}
