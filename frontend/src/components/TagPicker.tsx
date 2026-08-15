import { useEffect, useRef, useState } from "react";
import { Plus, X, Check, Trash2, Search } from "lucide-react";
import { errorDetail } from "@/lib/errors";
import type { Tag } from "@/types";

// Paleta Coro para etiquetas nuevas.
const SWATCHES = ["#DBE09E", "#6C7BFF", "#9B8AFB", "#25D366", "#E58A2F", "#DC4B3C", "#2DA771", "#8B7B5C"];

interface Props {
  assigned: Tag[];
  all: Tag[];
  onToggle: (tag: Tag) => Promise<void> | void;        // asignar / quitar existente
  onCreate: (nombre: string, color: string) => Promise<void> | void; // crear + asignar
  onDelete?: (tag: Tag) => Promise<void> | void;       // borrar globalmente
}

/**
 * Selector de etiquetas para la ficha de contacto. Reemplaza la antigua página
 * /tags: permite buscar, asignar/quitar, crear (con color) y borrar etiquetas
 * sin salir de la ficha.
 */
export function TagPicker({ assigned, all, onToggle, onCreate, onDelete }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [color, setColor] = useState(SWATCHES[0]);
  const [busy, setBusy] = useState(false);
  // Motivo del último fallo (p.ej. "Ya existe una etiqueta con ese nombre").
  const [error, setError] = useState<string | null>(null);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setConfirmDel(null);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const assignedIds = new Set(assigned.map((t) => t.id));
  const q = query.trim().toLowerCase();
  const filtered = all.filter((t) => t.nombre.toLowerCase().includes(q));
  const exact = all.some((t) => t.nombre.toLowerCase() === q);
  const canCreate = q.length > 0 && !exact;

  // Toda acción de etiquetas pasa por aquí. Antes el try/finally NO tenía
  // catch: el fallo se perdía por el camino (una promesa rechazada sin dueño) y
  // el desplegable se quedaba tan tranquilo. El caso que más se nota: crear una
  // etiqueta con un nombre que ya existe. El backend responde 409 y en pantalla
  // no pasaba absolutamente nada.
  async function run(fn: () => Promise<void> | void) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorDetail(e, "No se pudo completar la acción sobre la etiqueta."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative" ref={ref}>
      <div className="flex flex-wrap gap-1.5 items-center">
        {assigned.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => void run(() => onToggle(t))}
            disabled={busy}
            className="badge text-xs inline-flex items-center"
            style={{ backgroundColor: t.color + "33", color: t.color, borderColor: "transparent" }}
            title="Quitar etiqueta"
          >
            {t.nombre}
            <X className="w-2.5 h-2.5 ml-1" />
          </button>
        ))}
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="badge text-xs inline-flex items-center text-ink3 hover:text-ink"
          style={{ background: "transparent", border: "1px dashed var(--line)" }}
        >
          <Plus className="w-2.5 h-2.5 mr-0.5" /> etiqueta
        </button>
      </div>

      {open && (
        <div className="absolute left-0 z-20 mt-2 w-64 max-w-[calc(100vw-1.5rem)] card p-2 shadow-coro-2">
          <div className="flex items-center gap-2 px-2 py-1.5 mb-1 rounded-coro-sm bg-paper2">
            <Search className="w-3.5 h-3.5 text-ink4 shrink-0" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canCreate) void run(async () => {
                  await onCreate(query.trim(), color);
                  setQuery("");
                });
              }}
              placeholder="Buscar o crear…"
              className="bg-transparent text-sm flex-1 outline-none text-ink"
            />
          </div>

          {/* Motivo del fallo, junto a la acción que lo provocó. */}
          {error && (
            <div className="mt-1.5 text-[11px] text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-2 py-1.5">
              {error}
            </div>
          )}

          <div className="max-h-52 overflow-auto">
            {filtered.map((t) => {
              const on = assignedIds.has(t.id);
              const isConfirm = confirmDel === t.id;
              return (
                <div key={t.id} className="flex items-center gap-1 rounded-coro-sm hover:bg-paper2 group">
                  {isConfirm ? (
                    <div className="flex-1 flex items-center justify-between px-2 py-1.5 text-xs">
                      <span className="text-ink3">¿Borrar para todos?</span>
                      <span className="flex gap-2">
                        <button
                          className="text-state-bad font-semibold"
                          onClick={() => void run(async () => { if (onDelete) await onDelete(t); setConfirmDel(null); })}
                          disabled={busy}
                        >
                          Sí
                        </button>
                        <button className="text-ink3" onClick={() => setConfirmDel(null)}>No</button>
                      </span>
                    </div>
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={() => void run(() => onToggle(t))}
                        disabled={busy}
                        className="flex-1 flex items-center gap-2 px-2 py-1.5 text-sm text-left"
                      >
                        <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: t.color }} />
                        <span className="flex-1 text-ink2 truncate">{t.nombre}</span>
                        {on && <Check className="w-3.5 h-3.5 text-brand-700" />}
                      </button>
                      {onDelete && (
                        <button
                          type="button"
                          onClick={() => setConfirmDel(t.id)}
                          className="opacity-0 group-hover:opacity-100 px-1.5 text-ink4 hover:text-state-bad"
                          title="Borrar etiqueta"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </>
                  )}
                </div>
              );
            })}
            {filtered.length === 0 && !canCreate && (
              <div className="px-2 py-3 text-xs text-ink4 text-center">Sin etiquetas.</div>
            )}
          </div>

          {canCreate && (
            <div className="border-t border-line2 mt-1 pt-2">
              <div className="flex items-center gap-1.5 px-1 mb-2">
                {SWATCHES.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => setColor(c)}
                    className="w-4 h-4 rounded-full"
                    style={{ background: c, outline: color === c ? "2px solid var(--ink-3)" : "none", outlineOffset: 1 }}
                    title={c}
                  />
                ))}
              </div>
              <button
                type="button"
                onClick={() => void run(async () => { await onCreate(query.trim(), color); setQuery(""); })}
                disabled={busy}
                className="w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-coro-sm hover:bg-paper2"
              >
                <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: color }} />
                <span className="text-ink2 truncate">
                  Crear «<b className="text-ink">{query.trim()}</b>»
                </span>
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
