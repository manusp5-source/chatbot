import { useCallback, useEffect, useState } from "react";
import { ScrollText, Loader2 } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { listAudit, type AuditEntry } from "@/services/admin";
import { errorDetail } from "@/lib/errors";

export default function AuditPage() {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  // Sin esto, un fallo de la API dejaba la tabla vacía sin ningún aviso.
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const p = await listAudit({ page_size: 100 });
      setItems(p.items);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar la auditoría."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Audit log"
        description="Historial de cambios sensibles (mutaciones de credenciales, agentes, canales, configuración…)."
      />
      <div className="p-4 flex-1 overflow-auto">
        {error && (
          <div className="mb-3 text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{error}</span>
            <button type="button" onClick={() => void refresh()} className="font-semibold hover:underline shrink-0">
              Reintentar
            </button>
          </div>
        )}
        <div className="card overflow-x-auto">
          {loading ? (
            <div className="text-center py-10 text-ink3 inline-flex items-center justify-center gap-2 w-full">
              <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
            </div>
          ) : items.length === 0 ? (
            // Si la carga FALLÓ, el banner de arriba ya lo explica; no
            // afirmamos que "no hay entradas" porque no lo sabemos.
            <div className="text-center py-10 text-ink3 italic font-display flex items-center justify-center gap-2">
              <ScrollText className="w-4 h-4" /> {error ? "No se pudo mostrar el historial." : "Sin entradas todavía."}
            </div>
          ) : (
            <table className="w-full text-sm min-w-[560px]">
              <thead className="bg-paper2 border-b border-line">
                <tr className="text-ink3 text-[11px] uppercase tracking-wider">
                  <th className="text-left px-3 py-2 font-medium">Fecha</th>
                  <th className="text-left px-3 py-2 font-medium">Acción</th>
                  <th className="text-left px-3 py-2 font-medium">Entidad</th>
                  <th className="text-left px-3 py-2 font-medium">ID</th>
                </tr>
              </thead>
              <tbody>
                {items.map((a) => (
                  <tr
                    key={a.id}
                    className="border-t border-line2 hover:bg-paper2/40 transition"
                  >
                    <td className="px-3 py-2 text-xs text-ink3 font-mono whitespace-nowrap">
                      {new Date(a.created_at).toLocaleString("es-ES")}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-ink2">{a.action}</td>
                    <td className="px-3 py-2 text-ink2">
                      <span className="pill bg-paper2 text-ink3">{a.entity}</span>
                    </td>
                    <td className="px-3 py-2 font-mono text-[10px] text-ink4 truncate max-w-[180px]">
                      {a.entity_id || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
