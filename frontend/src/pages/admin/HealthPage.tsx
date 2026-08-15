import { useEffect, useState } from "react";
import { CheckCircle2, XCircle, RefreshCw, Loader2, HardDrive } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { adminHealth, adminStorageReport, type StorageReport } from "@/services/admin";
import { errorDetail } from "@/lib/errors";

function fmtBytes(n: number | null | undefined): string {
  if (n == null || n < 0) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} KB`;
  return `${n} B`;
}

// Etiquetas legibles de la política de retención (claves del backend).
const RETENTION_LABEL: Record<string, string> = {
  email_meses: "Contenido de emails (meses)",
  audios_dias: "Notas de voz en disco (días)",
  trazas_dias: "Trazas del agente (días)",
  uso_llm_dias: "Registro de uso LLM (días)",
  auditoria_dias: "Auditoría (días)",
  envios_masivos_dias: "Envíos masivos terminados (días)",
  aprendizajes_resueltos_dias: "Aprendizajes resueltos (días)",
  backups_dias: "Backups de la BD (días)",
};

export default function HealthPage() {
  const [data, setData] = useState<Record<string, { ok: boolean; detail: string }> | null>(null);
  const [storage, setStorage] = useState<StorageReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [d, s] = await Promise.all([
        adminHealth(),
        adminStorageReport().catch(() => null),
      ]);
      setData(d);
      setStorage(s);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar el estado del sistema."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const allOk = data ? Object.values(data).every((v) => v.ok) : null;
  const diskPct =
    storage?.disk_total_bytes && storage.disk_free_bytes != null
      ? (storage.disk_total_bytes - storage.disk_free_bytes) / storage.disk_total_bytes
      : null;

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Salud del sistema"
        description="Estado de las dependencias críticas y del almacenamiento (BD, disco, retención)."
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
      <div className="p-4 max-w-2xl flex-1 overflow-auto">
        {error && (
          <div className="mb-3 px-3 py-2 rounded-coro-sm border text-sm bg-state-bad/10 border-state-bad/30 text-state-bad">
            {error}
          </div>
        )}
        {allOk != null && (
          <div
            className={`mb-3 px-3 py-2 rounded-coro-sm border text-sm ${
              allOk
                ? "bg-state-ok/10 border-state-ok/30 text-state-ok"
                : "bg-state-bad/10 border-state-bad/30 text-state-bad"
            }`}
          >
            {allOk
              ? "Todo correcto. Sistema operativo."
              : "Hay servicios con problemas. Revisa abajo."}
          </div>
        )}
        <div className="space-y-2">
          {data &&
            Object.entries(data).map(([k, v]) => (
              <div
                key={k}
                className="card p-3 flex items-center justify-between gap-3"
              >
                <div className="min-w-0">
                  <div className="font-medium text-sm text-ink uppercase tracking-wide">
                    {k}
                  </div>
                  <div className="text-xs text-ink3 mt-0.5 break-words">
                    {v.detail}
                  </div>
                </div>
                {v.ok ? (
                  <CheckCircle2 className="w-5 h-5 text-state-ok flex-none" />
                ) : (
                  <XCircle className="w-5 h-5 text-state-bad flex-none" />
                )}
              </div>
            ))}
        </div>

        {storage && (
          <div className="mt-6 space-y-3">
            <div className="flex items-center gap-2">
              <HardDrive className="w-4 h-4 text-brand-ink" />
              <h3 className="font-display text-lg text-ink">Almacenamiento</h3>
            </div>

            {/* Disco: barra de uso con aviso al 85% */}
            {diskPct != null && (
              <div className="card p-4">
                <div className="flex items-center justify-between text-sm mb-1.5">
                  <span className="text-ink">Disco del volumen de datos</span>
                  <span
                    className={
                      diskPct >= 0.85 ? "text-state-bad font-semibold" : "text-ink3"
                    }
                  >
                    {Math.round(diskPct * 100)}% usado · {fmtBytes(storage.disk_free_bytes)} libres
                  </span>
                </div>
                <div className="h-2 rounded-full bg-paper3 overflow-hidden">
                  <div
                    className={`h-full ${diskPct >= 0.85 ? "bg-state-bad" : diskPct >= 0.7 ? "bg-state-warn" : "bg-state-ok"}`}
                    style={{ width: `${Math.min(100, Math.round(diskPct * 100))}%` }}
                  />
                </div>
                {diskPct >= 0.85 && (
                  <p className="text-[11px] text-state-bad mt-1.5">
                    Disco casi lleno: revisa backups antiguos y considera acortar las retenciones.
                  </p>
                )}
              </div>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="card p-4">
                <div className="text-xs text-ink3 uppercase tracking-wide">Base de datos</div>
                <div className="font-numbers text-2xl text-ink mt-1">
                  {fmtBytes(storage.db_total_bytes)}
                </div>
              </div>
              <div className="card p-4">
                <div className="text-xs text-ink3 uppercase tracking-wide">Audios y ficheros</div>
                <div className="font-numbers text-2xl text-ink mt-1">
                  {fmtBytes(storage.audio_dir_bytes)}
                </div>
                <div className="text-[11px] text-ink3 mt-0.5">
                  incluye subidas, backups y documentos de la KB
                </div>
              </div>
            </div>

            <div className="card p-4">
              <div className="text-xs text-ink3 uppercase tracking-wide mb-2">
                Tablas que más ocupan
              </div>
              <ul className="space-y-1">
                {storage.tables.slice(0, 8).map((t) => (
                  <li key={t.name} className="flex items-center justify-between text-sm">
                    <span className="font-mono text-[12px] text-ink2 truncate">{t.name}</span>
                    <span className="text-ink3 text-[12px] shrink-0 ml-2">
                      {fmtBytes(t.total_bytes)}
                      {/* reltuples=-1 = tabla aún sin analizar por Postgres */}
                      {t.rows >= 0 ? ` · ~${t.rows.toLocaleString("es-ES")} filas` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="card p-4">
              <div className="text-xs text-ink3 uppercase tracking-wide mb-2">
                Política de retención (limpieza automática diaria)
              </div>
              <ul className="space-y-1">
                {Object.entries(storage.retention).map(([k, v]) => (
                  <li key={k} className="flex items-center justify-between text-sm">
                    <span className="text-ink2">{RETENTION_LABEL[k] ?? k}</span>
                    <span className="text-ink3">{v}</span>
                  </li>
                ))}
              </ul>
              <p className="text-[11px] text-ink3 mt-2">
                Configurable por variables de entorno (AUDIO_RETENTION_DAYS, TRACE_RETENTION_DAYS…).
                Los mensajes de texto y contactos no caducan (decisión de negocio).
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
