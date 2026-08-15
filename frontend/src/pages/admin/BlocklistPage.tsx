import { useEffect, useState } from "react";
import { RefreshCw, ShieldOff, Loader2 } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { ConfirmModal } from "@/components/ConfirmModal";
import { blockPhone, listBlockedPhones, unblockPhone, type BlockedPhone } from "@/services/admin";
import { errorDetail } from "@/lib/errors";

function formatTtl(seconds: number): string {
  if (seconds <= 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export default function BlocklistPage() {
  const [items, setItems] = useState<BlockedPhone[]>([]);
  const [loading, setLoading] = useState(false);
  const [unblocking, setUnblocking] = useState<BlockedPhone | null>(null);
  const [busy, setBusy] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    try {
      setItems(await listBlockedPhones());
      setListError(null);
    } catch (e) {
      // Sin catch, si la consulta se caía la pantalla afirmaba "No hay
      // contactos bloqueados" con la lista de bloqueos intacta por detrás.
      setListError(errorDetail(e, "No se pudo cargar la lista de contactos bloqueados."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function confirmUnblock() {
    if (!unblocking) return;
    setBusy(true);
    try {
      await unblockPhone(unblocking.phone);
      setUnblocking(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Contactos bloqueados"
        description="Números bloqueados automáticamente por exceso de mensajes o uso abusivo del agente. El bloqueo expira solo, o lo puedes quitar manualmente."
        actions={
          <button onClick={refresh} className="btn-ghost" disabled={loading}>
            {loading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <RefreshCw className="w-4 h-4" />
            )}
            Refrescar
          </button>
        }
      />
      <div className="p-4 flex-1 overflow-auto max-w-3xl space-y-4">
        <BlockForm onBlocked={refresh} />
        <div className="card overflow-x-auto">
          {loading ? (
            <div className="text-center py-10 text-ink3 inline-flex items-center justify-center gap-2 w-full">
              <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
            </div>
          ) : listError ? (
            <div className="m-3 text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
              <span className="flex-1">{listError}</span>
              <button
                type="button"
                onClick={() => void refresh()}
                className="font-semibold hover:underline shrink-0"
              >
                Reintentar
              </button>
            </div>
          ) : items.length === 0 ? (
            <div className="text-center py-10 text-ink3 italic font-display flex items-center justify-center gap-2">
              <ShieldOff className="w-4 h-4" /> No hay contactos bloqueados.
            </div>
          ) : (
            <table className="w-full text-sm min-w-[520px]">
              <thead className="bg-paper2 border-b border-line">
                <tr className="text-ink3 text-[11px] uppercase tracking-wider">
                  <th className="text-left px-4 py-2 font-medium">Teléfono</th>
                  <th className="text-left px-4 py-2 font-medium">Motivo</th>
                  <th className="text-left px-4 py-2 font-medium">Expira en</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((b) => (
                  <tr key={b.phone} className="border-t border-line2 hover:bg-paper2/40 transition">
                    <td className="px-4 py-2 font-mono text-xs text-ink2">{b.phone}</td>
                    <td className="px-4 py-2 text-ink2">{b.reason}</td>
                    <td className="px-4 py-2 text-ink3 font-mono text-xs">
                      {formatTtl(b.ttl_seconds)}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        className="btn-ghost text-state-ok text-xs"
                        onClick={() => setUnblocking(b)}
                      >
                        <ShieldOff className="w-3.5 h-3.5" /> Desbloquear
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
      <ConfirmModal
        open={!!unblocking}
        title="Desbloquear contacto"
        description={
          unblocking
            ? `¿Seguro que quieres desbloquear ${unblocking.phone}? Volverá a poder hablar con el bot.`
            : ""
        }
        confirmLabel="Desbloquear"
        cancelLabel="Cancelar"
        busy={busy}
        onConfirm={confirmUnblock}
        onCancel={() => setUnblocking(null)}
      />
    </div>
  );
}

function BlockForm({ onBlocked }: { onBlocked: () => void }) {
  const [phone, setPhone] = useState("");
  const [reason, setReason] = useState("");
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const p = phone.trim();
    if (!p) {
      setError("Introduce un teléfono.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await blockPhone(p, reason.trim() || undefined, days);
      setPhone("");
      setReason("");
      onBlocked();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "No se pudo bloquear.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card p-4 space-y-3">
      <div className="flex items-center gap-2">
        <ShieldOff className="w-4 h-4 text-state-bad" />
        <h3 className="text-sm font-semibold text-ink">Bloquear un número manualmente</h3>
      </div>
      <p className="text-[11px] text-ink3">
        El número no podrá hablar con el bot: sus mensajes se descartan en cuanto llegan, antes de procesarse.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2">
        <input
          className="input"
          placeholder="+34600000000"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
        />
        <input
          className="input"
          placeholder="Motivo (opcional)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <select className="input" value={days} onChange={(e) => setDays(parseInt(e.target.value))}>
          <option value={1}>1 día</option>
          <option value={7}>7 días</option>
          <option value={30}>30 días</option>
          <option value={365}>1 año</option>
        </select>
      </div>
      {error && <div className="text-state-bad text-sm">{error}</div>}
      <div className="flex justify-end">
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldOff className="w-4 h-4" />}
          Bloquear
        </button>
      </div>
    </form>
  );
}
