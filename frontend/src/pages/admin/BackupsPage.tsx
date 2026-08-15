// Copias de seguridad (nombres de UI estables):
// tarjeta de integración estilo Google Drive (proveedor S3-compatible),
// configuración de frecuencia/hora, estado SIEMPRE visible (última copia +
// próxima programada, aviso llamativo si la última falló) y acciones:
// "Hacer copia ahora" · "Descargar última copia" · "Restaurar…" (con doble
// confirmación explícita: modal de aviso + frase RESTAURAR tecleada).
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Cloud,
  CloudOff,
  CloudUpload,
  DatabaseBackup,
  Download,
  HardDrive,
  History,
  KeyRound,
  Loader2,
  PlugZap,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { ConfirmModal } from "@/components/ConfirmModal";
import { updateCredential } from "@/services/admin";
import {
  downloadBackup,
  getBackupOverview,
  listBackupCopies,
  listBackupRuns,
  restoreBackup,
  runBackupNow,
  testBackupConnection,
  updateBackupSettings,
  verifyLastBackup,
  type BackupCopy,
  type BackupDestination,
  type BackupOverview,
  type BackupRun,
} from "@/services/backups";
import { errorDetail } from "@/lib/errors";

const PROVIDER_LABEL: Record<string, string> = {
  r2: "Cloudflare R2",
  s3: "Amazon S3",
  s3_compatible: "S3 compatible",
};

const FREQ_LABEL: Record<string, string> = {
  hourly: "Cada hora",
  "6h": "Cada 6 h",
  "12h": "Cada 12 h",
  daily: "Diaria",
  weekly: "Semanal (cada 7 días)",
  monthly: "Mensual (cada 30 días)",
};

// Frecuencias "de días": corren a una hora fija (pared Madrid) → muestran
// el selector de hora.
const DAY_FREQS = ["daily", "weekly", "monthly"];

const KIND_LABEL: Record<string, string> = {
  scheduled: "Programada",
  manual: "Manual",
  restore: "Restauración",
  // Chequeos automáticos diarios (ver services/backups.py).
  verify: "Verificación",
  check: "Conexión al bucket",
};

/** "hace X días/horas" a partir de un ISO. Vacío si no hay fecha. */
function haceCuanto(iso: string | null | undefined): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "";
  const horas = Math.floor(ms / 3_600_000);
  if (horas < 1) return "hace menos de 1 hora";
  if (horas < 24) return `hace ${horas} h`;
  const dias = Math.floor(horas / 24);
  return `hace ${dias} ${dias === 1 ? "día" : "días"}`;
}

function fmtBytes(n: number | null | undefined): string {
  if (n == null || n < 0) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} KB`;
  return `${n} B`;
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("es-ES", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// ── Destino de cada copia (claridad de "dónde quedó") ──
// server: el dump quedó en el disco del servidor.
// remote: subida al bucket → ok | failed (configurado pero falló) | none.
type Destino = { server: boolean; remote: "ok" | "failed" | "none" };

function destinoDe(r: BackupRun): Destino {
  if (r.kind === "restore") return { server: false, remote: "none" };
  if (r.upload_status != null) {
    return {
      server: r.local_file != null,
      remote:
        r.upload_status === "uploaded" ? "ok" : r.upload_status === "failed" ? "failed" : "none",
    };
  }
  // Filas anteriores al campo upload_status: se deduce de status + s3_key
  // (un "failed" CON s3_key era una subida fallida con dump local conservado).
  if (r.status === "ok") return { server: true, remote: r.s3_key ? "ok" : "none" };
  return r.s3_key ? { server: true, remote: "failed" } : { server: false, remote: "none" };
}

function DestinoBadges({ run, providerLabel }: { run: BackupRun; providerLabel: string }) {
  if (run.kind === "restore") return <span className="text-xs text-ink3">—</span>;
  const d = destinoDe(run);
  if (!d.server && d.remote === "none")
    return <span className="text-xs text-ink3">Sin copia (el dump falló)</span>;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {d.server && (
        <span
          className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-coro-sm border border-line bg-paper2 text-ink2"
          title={run.local_file ? `Fichero: ${run.local_file}` : undefined}
        >
          <HardDrive className="w-3 h-3" /> Servidor
        </span>
      )}
      {d.remote === "ok" && (
        <span className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-coro-sm border border-state-ok/30 bg-state-ok/10 text-state-ok">
          <Cloud className="w-3 h-3" /> {providerLabel}
        </span>
      )}
      {d.remote === "failed" && (
        <span
          className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-coro-sm border border-state-bad/30 bg-state-bad/10 text-state-bad"
          title={run.error || "La subida al bucket falló"}
        >
          <CloudOff className="w-3 h-3" /> {providerLabel}: fallo
        </span>
      )}
      {d.remote === "none" && d.server && (
        <span className="text-[11px] text-ink3">solo servidor</span>
      )}
    </div>
  );
}

export default function BackupsPage() {
  const [overview, setOverview] = useState<BackupOverview | null>(null);
  const [runs, setRuns] = useState<BackupRun[]>([]);
  const [copies, setCopies] = useState<BackupCopy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Tarjeta de integración (credenciales del proveedor).
  const [provider, setProvider] = useState("r2");
  const [endpoint, setEndpoint] = useState("");
  const [bucket, setBucket] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [savingCreds, setSavingCreds] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [testing, setTesting] = useState(false);

  // Configuración (destino / frecuencia / hora).
  const [destination, setDestination] = useState<BackupDestination>("server");
  const [frequency, setFrequency] = useState("daily");
  const [dailyHour, setDailyHour] = useState(4);
  const [savingConfig, setSavingConfig] = useState(false);

  // Acciones.
  const [runningNow, setRunningNow] = useState(false);
  const [downloading, setDownloading] = useState(false);
  // Verificación manual: descarga la última copia, la descifra y comprueba que
  // es un dump válido. Se reutiliza la misma tarjeta de resultado del test de
  // conexión (mismo formato {ok, message}).
  const [verifying, setVerifying] = useState(false);
  const [verifyResult, setVerifyResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [showRestoreList, setShowRestoreList] = useState(false);
  const [restoreKey, setRestoreKey] = useState<string | null>(null); // paso 1 (ConfirmModal)
  const [restorePhraseFor, setRestorePhraseFor] = useState<string | null>(null); // paso 2 (frase)
  const [restorePhrase, setRestorePhrase] = useState("");
  const [restoring, setRestoring] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  async function refresh(withCopies = false) {
    try {
      const [ov, rs] = await Promise.all([getBackupOverview(), listBackupRuns()]);
      setOverview(ov);
      setRuns(rs);
      setProvider(ov.provider);
      setEndpoint(ov.endpoint);
      setBucket(ov.bucket);
      // Preselección del selector: lo elegido; si aún no eligió, lo
      // recomendado (bucket si está configurado, servidor si no).
      setDestination(ov.destination ?? (ov.s3_configured ? "cloud" : "server"));
      setFrequency(ov.frequency);
      setDailyHour(ov.daily_hour);
      setError(null);
      if (withCopies && ov.s3_configured) {
        // El `.catch(() => [])` de antes se tragaba el fallo del listado y la
        // pantalla afirmaba "No hay copias en el bucket": en copias de
        // seguridad, dar por perdido lo que sí existe es lo más caro que
        // puede pasar. Ahora el fallo cae en el catch de abajo y se ve.
        setCopies(await listBackupCopies());
      }
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar el estado de las copias."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh(true);
  }, []);

  const lastFailed = overview?.last_run?.status === "failed";
  // Fallo de SUBIDA (el dump local sí se hizo): mensaje distinto al fallo total.
  const lastUploadFailed =
    lastFailed &&
    (overview?.last_run?.upload_status === "failed" ||
      (overview?.last_run?.upload_status == null && !!overview?.last_run?.s3_key));
  const latestCopy = copies[0] ?? null;
  const providerLabel = PROVIDER_LABEL[overview?.provider || "r2"] || "Cloudflare R2";
  // UNA frase que dice dónde acaba cada copia según el destino EFECTIVO,
  // siempre visible en la cabecera.
  const destinoFrase: Record<string, string> = {
    cloud: `Cada copia se genera y se sube cifrada a ${providerLabel} sobre la marcha, sin dejar ningún fichero en el servidor.`,
    server: "Cada copia se guarda como fichero en el disco del servidor; no se sube a ningún bucket.",
    both: `Cada copia se guarda en el disco del servidor y además se sube cifrada a ${providerLabel}.`,
  };
  const headerDestino = !overview
    ? "Copia cifrada de la base de datos, con retención automática y restauración."
    : !overview.s3_configured
      ? "Las copias se hacen SOLO en el disco del servidor — configura Cloudflare R2 (u otro bucket S3) para tener copia externa."
      : destinoFrase[overview.effective_destination];

  async function onSaveCredentials() {
    setSavingCreds(true);
    setTestResult(null);
    try {
      // Cada campo con contenido se guarda como credencial normal del panel
      // (cifrada + auditada). Los vacíos no se tocan (no pisar lo guardado).
      if (endpoint.trim()) await updateCredential("backup_s3_endpoint", endpoint.trim());
      if (bucket.trim()) await updateCredential("backup_s3_bucket", bucket.trim());
      if (accessKey.trim()) await updateCredential("backup_s3_access_key_id", accessKey.trim());
      if (secretKey.trim())
        await updateCredential("backup_s3_secret_access_key", secretKey.trim());
      await updateBackupSettings({ provider });
      setAccessKey("");
      setSecretKey("");
      setNotice("Credenciales guardadas.");
      await refresh(true);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron guardar las credenciales."));
    } finally {
      setSavingCreds(false);
    }
  }

  async function onTestConnection() {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testBackupConnection());
    } catch (e) {
      setTestResult({ ok: false, message: errorDetail(e, "Error al probar la conexión.") });
    } finally {
      setTesting(false);
    }
  }

  async function onSaveConfig() {
    setSavingConfig(true);
    try {
      const ov = await updateBackupSettings({
        frequency,
        daily_hour: dailyHour,
        destination,
      });
      setOverview(ov);
      setNotice("Configuración guardada.");
    } catch (e) {
      setError(errorDetail(e, "No se pudo guardar la configuración."));
    } finally {
      setSavingConfig(false);
    }
  }

  async function onToggleEnabled() {
    if (!overview) return;
    try {
      const ov = await updateBackupSettings({ enabled: !overview.enabled });
      setOverview(ov);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cambiar el estado."));
    }
  }

  async function onRunNow() {
    setRunningNow(true);
    setNotice(null);
    try {
      await runBackupNow();
      setNotice("Copia encolada: en unos segundos aparecerá en el registro.");
      // La copia corre en el worker; refrescamos con un pequeño margen.
      window.setTimeout(() => void refresh(true), 8000);
    } catch (e) {
      setError(errorDetail(e, "No se pudo lanzar la copia."));
    } finally {
      setRunningNow(false);
    }
  }

  async function onDownloadLatest() {
    if (!latestCopy) return;
    setDownloading(true);
    try {
      await downloadBackup(latestCopy.key);
    } catch (e) {
      setError(errorDetail(e, "No se pudo descargar la copia."));
    } finally {
      setDownloading(false);
    }
  }

  async function onVerifyNow() {
    setVerifying(true);
    setVerifyResult(null);
    try {
      setVerifyResult(await verifyLastBackup());
    } catch (e) {
      setVerifyResult({ ok: false, message: errorDetail(e, "No se pudo verificar la copia.") });
    } finally {
      setVerifying(false);
      // La verificación queda registrada en el histórico (kind=verify).
      void refresh(true);
    }
  }

  async function onConfirmRestore() {
    if (!restorePhraseFor || restorePhrase !== "RESTAURAR") return;
    setRestoring(true);
    try {
      await restoreBackup(restorePhraseFor, restorePhrase);
      setRestorePhraseFor(null);
      setRestorePhrase("");
      setShowRestoreList(false);
      setNotice(
        "Restauración encolada. La base de datos se sobrescribirá en breve; el resultado quedará en el registro."
      );
      window.setTimeout(() => void refresh(true), 10000);
    } catch (e) {
      setError(errorDetail(e, "No se pudo lanzar la restauración."));
    } finally {
      setRestoring(false);
    }
  }

  const restoreCopyLabel = useMemo(() => {
    const key = restoreKey ?? restorePhraseFor;
    if (!key) return "";
    const c = copies.find((x) => x.key === key);
    return `${key.replace("chatbot/", "")}${c ? ` · ${fmtBytes(c.size)}` : ""}`;
  }, [restoreKey, restorePhraseFor, copies]);

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Copias de seguridad"
        description={headerDestino}
        actions={
          <button onClick={() => void refresh(true)} className="btn-ghost" disabled={loading}>
            {loading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <RefreshCw className="w-4 h-4" />
            )}
            Refrescar
          </button>
        }
      />

      {/* Paso 1 de la restauración: aviso de que PISA los datos. */}
      <ConfirmModal
        open={restoreKey !== null}
        title="¿Restaurar esta copia?"
        description={
          <>
            Vas a restaurar <span className="font-mono text-xs">{restoreCopyLabel}</span>.
            <br />
            Esto <b>SOBRESCRIBE los datos actuales</b> de la base de datos (conversaciones,
            contactos, configuración). Todo lo posterior a esa copia se pierde.
          </>
        }
        confirmLabel="Continuar"
        tone="danger"
        onConfirm={() => {
          setRestorePhraseFor(restoreKey);
          setRestoreKey(null);
          setRestorePhrase("");
        }}
        onCancel={() => setRestoreKey(null)}
      />

      {/* Paso 2: frase tecleada (doble confirmación explícita de la spec). */}
      {restorePhraseFor !== null && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4" role="dialog" aria-modal="true">
          <div
            className="absolute inset-0 bg-ink/50 backdrop-blur-[2px]"
            onClick={() => !restoring && setRestorePhraseFor(null)}
            aria-hidden="true"
          />
          <div className="relative bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-md p-5 sm:p-6">
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-full flex items-center justify-center shrink-0 bg-state-bad/15 text-state-bad">
                <AlertTriangle className="w-5 h-5" />
              </div>
              <div className="flex-1 min-w-0">
                <h2 className="font-display text-xl text-ink leading-tight">Última confirmación</h2>
                <div className="text-sm text-ink3 mt-2 leading-relaxed">
                  Escribe <span className="font-mono font-semibold text-ink">RESTAURAR</span> para
                  sobrescribir la base de datos con{" "}
                  <span className="font-mono text-xs">{restoreCopyLabel}</span>.
                </div>
                <input
                  className="input w-full mt-3 font-mono"
                  value={restorePhrase}
                  onChange={(e) => setRestorePhrase(e.target.value)}
                  placeholder="RESTAURAR"
                  autoFocus
                  disabled={restoring}
                />
              </div>
            </div>
            <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 mt-6">
              <button
                type="button"
                className="btn"
                onClick={() => setRestorePhraseFor(null)}
                disabled={restoring}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="btn-danger"
                onClick={() => void onConfirmRestore()}
                disabled={restoring || restorePhrase !== "RESTAURAR"}
              >
                {restoring ? <Loader2 className="w-4 h-4 animate-spin" /> : <RotateCcw className="w-4 h-4" />}
                Restaurar ahora
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="p-4 md:p-6 flex-1 overflow-auto">
        <div className="max-w-3xl space-y-4">
          {error && (
            <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
              <span className="flex-1">{error}</span>
              <button type="button" onClick={() => void refresh(true)} className="font-semibold hover:underline shrink-0">
                Reintentar
              </button>
            </div>
          )}
          {notice && (
            <div className="text-xs text-ink2 bg-brand/10 border border-brand/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
              <span className="flex-1">{notice}</span>
              <button type="button" onClick={() => setNotice(null)} className="font-semibold hover:underline shrink-0">
                Cerrar
              </button>
            </div>
          )}

          {/* Aviso PERMANENTE: sin la clave de cifrado la copia del bucket no
              sirve de nada. Va aquí, y no solo en la documentación, porque
              esto lo tiene que leer quien monta el sistema sin ser técnico. */}
          <div className="rounded-coro border border-state-warn/40 bg-state-warn/10 px-4 py-3 flex items-start gap-3">
            <KeyRound className="w-5 h-5 text-state-warn shrink-0 mt-0.5" />
            <div className="text-sm">
              <div className="font-semibold text-ink">
                Guarda la clave de cifrado FUERA de este servidor
              </div>
              <div className="text-ink2 mt-0.5">
                Las copias se cifran con la variable <span className="font-mono text-xs">ENCRYPTION_KEY</span>{" "}
                del servidor. Si pierdes el servidor —justo el desastre del que te protege la copia
                del bucket— pierdes también la clave, y el fichero cifrado no se puede recuperar de
                ninguna forma. Cópiala ahora a un gestor de contraseñas. Si la cambias, las copias
                anteriores dejan de poder restaurarse.
              </div>
            </div>
          </div>

          {/* Aviso llamativo si la última copia falló (spec). */}
          {lastFailed && overview?.last_run && (
            <div className="rounded-coro border border-state-bad/40 bg-state-bad/10 px-4 py-3 flex items-start gap-3">
              <XCircle className="w-5 h-5 text-state-bad shrink-0 mt-0.5" />
              <div className="text-sm">
                <div className="font-semibold text-state-bad">
                  {lastUploadFailed
                    ? `La última copia se hizo en el servidor pero la subida a ${providerLabel} FALLÓ`
                    : "La última copia FALLÓ"}
                </div>
                <div className="text-ink2 mt-0.5">
                  {fmtDate(overview.last_run.created_at)} —{" "}
                  {overview.last_run.error || "sin detalle"}
                </div>
              </div>
            </div>
          )}

          {/* Estado siempre visible */}
          <div className="card p-4">
            <div className="flex items-center justify-between gap-2 mb-3">
              <h2 className="font-semibold text-ink flex items-center gap-2">
                <Clock className="w-4 h-4 text-ink3" /> Estado
              </h2>
              {overview && (
                <button
                  type="button"
                  onClick={() => void onToggleEnabled()}
                  className={overview.enabled ? "btn-ghost btn-sm" : "btn-primary btn-sm"}
                >
                  {overview.enabled ? "Desactivar copias" : "Activar copias"}
                </button>
              )}
            </div>
            {loading && !overview ? (
              <div className="text-sm text-ink3 flex items-center gap-2">
                <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
              </div>
            ) : (
              <div className="grid sm:grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-xs text-ink3 mb-0.5">Última copia</div>
                  {overview?.last_run ? (
                    <div className="flex items-start gap-1.5">
                      {overview.last_run.status === "ok" ? (
                        <CheckCircle2 className="w-4 h-4 text-state-ok shrink-0 mt-0.5" />
                      ) : (
                        <XCircle className="w-4 h-4 text-state-bad shrink-0 mt-0.5" />
                      )}
                      <div>
                        <div className="text-ink">
                          {fmtDate(overview.last_run.created_at)} ·{" "}
                          {fmtBytes(overview.last_run.size_bytes)}
                        </div>
                        <div className="text-xs text-ink3">
                          {KIND_LABEL[overview.last_run.kind] || overview.last_run.kind}
                          {overview.last_run.status === "failed" &&
                            ` — ${overview.last_run.error || "fallo sin detalle"}`}
                        </div>
                        {/* Dónde quedó la copia: Servidor y/o bucket. */}
                        <div className="mt-1">
                          <DestinoBadges run={overview.last_run} providerLabel={providerLabel} />
                        </div>
                        {/* Copia correcta pero con matiz (p. ej. degradada:
                            se subió al bucket sin dejar fichero local). */}
                        {overview.last_run.status === "ok" && overview.last_run.error && (
                          <div className="text-xs text-state-warn mt-1">
                            {overview.last_run.error}
                          </div>
                        )}
                        {/* Si la última falló, lo que importa es cuánto hace
                            de la última BUENA. */}
                        {overview.last_run.status === "failed" && (
                          <div className="text-xs text-ink3 mt-1">
                            {overview.last_successful_at
                              ? `Última copia correcta: ${fmtDate(overview.last_successful_at)} (${haceCuanto(overview.last_successful_at)})`
                              : "No hay ninguna copia correcta registrada."}
                          </div>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className="text-ink3">Todavía no hay copias registradas.</div>
                  )}
                </div>
                <div>
                  <div className="text-xs text-ink3 mb-0.5">Próxima programada</div>
                  <div className="text-ink">
                    {overview?.enabled ? fmtDate(overview?.next_run_at) : "Copias desactivadas"}
                  </div>
                  <div className="text-xs text-ink3">
                    {FREQ_LABEL[overview?.frequency || "daily"]}
                    {overview &&
                      DAY_FREQS.includes(overview.frequency) &&
                      ` a las ${String(overview.daily_hour).padStart(2, "0")}:00 (Madrid)`}
                  </div>
                </div>
              </div>
            )}
            {overview && !overview.s3_configured && (
              <div className="mt-3 text-xs text-state-warn bg-state-warn/10 border border-state-warn/30 rounded-coro-sm px-3 py-2">
                Bucket sin configurar: las copias se quedan SOLO en el disco del servidor.
                Conecta un proveedor abajo para tenerlas cifradas fuera.
              </div>
            )}
            <div className="flex flex-wrap gap-2 mt-4">
              <button className="btn-primary" onClick={() => void onRunNow()} disabled={runningNow}>
                {runningNow ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <DatabaseBackup className="w-4 h-4" />
                )}
                Hacer copia ahora
              </button>
              <button
                className="btn"
                onClick={() => void onDownloadLatest()}
                disabled={downloading || !latestCopy}
                title={!latestCopy ? "No hay copias en el bucket" : undefined}
              >
                {downloading ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Download className="w-4 h-4" />
                )}
                Descargar última copia
              </button>
              {/* Verificación REAL: que el objeto exista y pese algo no
                  garantiza que se pueda restaurar. Esto lo descarga, lo
                  descifra y comprueba que es un dump de PostgreSQL. */}
              <button
                className="btn"
                onClick={() => void onVerifyNow()}
                disabled={verifying || !overview?.s3_configured}
                title={
                  !overview?.s3_configured
                    ? "Sin bucket configurado no hay nada que verificar"
                    : "Descarga la última copia del bucket, la descifra y comprueba que es un dump válido"
                }
              >
                {verifying ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <ShieldCheck className="w-4 h-4" />
                )}
                Verificar ahora
              </button>
              <button
                className="btn"
                onClick={() => setShowRestoreList((v) => !v)}
                disabled={!overview?.s3_configured}
              >
                <RotateCcw className="w-4 h-4" />
                Restaurar…
              </button>
            </div>
            {verifyResult && (
              <div
                className={
                  "mt-3 text-xs rounded-coro-sm px-3 py-2 flex items-start gap-2 border " +
                  (verifyResult.ok
                    ? "text-state-ok bg-state-ok/10 border-state-ok/30"
                    : "text-state-bad bg-state-bad/10 border-state-bad/30")
                }
              >
                {verifyResult.ok ? (
                  <CheckCircle2 className="w-3.5 h-3.5 shrink-0 mt-px" />
                ) : (
                  <XCircle className="w-3.5 h-3.5 shrink-0 mt-px" />
                )}
                <span className="flex-1">{verifyResult.message}</span>
                <button
                  type="button"
                  onClick={() => setVerifyResult(null)}
                  className="font-semibold hover:underline shrink-0"
                  aria-label="Cerrar el resultado de la verificación"
                >
                  Cerrar
                </button>
              </div>
            )}
            {showRestoreList && (
              <div className="mt-3 border border-line rounded-coro-sm divide-y divide-line max-h-64 overflow-auto">
                {/* Si la carga falló no se puede afirmar que el bucket esté
                    vacío: no se ha llegado a mirar. El aviso de error de
                    arriba es lo que hay que leer. */}
                {copies.length === 0 && (
                  <div className="px-3 py-2 text-sm text-ink3">
                    {error
                      ? "No se ha podido consultar el bucket, así que no se puede decir qué copias hay. Reintenta arriba."
                      : "No hay copias en el bucket."}
                  </div>
                )}
                {copies.map((c) => (
                  <div key={c.key} className="px-3 py-2 flex items-center gap-2 text-sm">
                    <span className="font-mono text-xs flex-1 truncate">
                      {c.key.replace("chatbot/", "")}
                    </span>
                    <span className="text-xs text-ink3 shrink-0">{fmtBytes(c.size)}</span>
                    <button
                      className="btn-ghost btn-sm shrink-0"
                      onClick={() => void downloadBackup(c.key)}
                    >
                      <Download className="w-3.5 h-3.5" />
                    </button>
                    <button
                      className="btn-danger btn-sm shrink-0"
                      onClick={() => setRestoreKey(c.key)}
                    >
                      Restaurar
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Tarjeta de integración (estilo Google Drive) */}
          <div className="card p-4">
            <h2 className="font-semibold text-ink flex items-center gap-2 mb-1">
              <CloudUpload className="w-4 h-4 text-ink3" /> Proveedor de almacenamiento
            </h2>
            <p className="text-xs text-ink3 mb-3">
              Bucket S3-compatible donde se suben las copias cifradas (AES-256-GCM). El secret se
              guarda cifrado como el resto de credenciales.
              {overview?.s3_configured && (
                <span className="text-state-ok font-medium"> Conectado.</span>
              )}
            </p>
            <div className="grid sm:grid-cols-2 gap-3">
              <label className="text-xs text-ink3">
                Proveedor
                <select
                  className="input w-full mt-1"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                >
                  {Object.entries(PROVIDER_LABEL).map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-xs text-ink3">
                Endpoint URL
                <input
                  className="input w-full mt-1 font-mono text-xs"
                  value={endpoint}
                  onChange={(e) => setEndpoint(e.target.value)}
                  placeholder="https://<accountid>.r2.cloudflarestorage.com"
                />
              </label>
              <label className="text-xs text-ink3">
                Access Key ID
                <input
                  className="input w-full mt-1 font-mono text-xs"
                  value={accessKey}
                  onChange={(e) => setAccessKey(e.target.value)}
                  placeholder={overview?.access_key_masked || "AKIA…"}
                />
              </label>
              <label className="text-xs text-ink3">
                Secret Access Key
                <input
                  type="password"
                  className="input w-full mt-1 font-mono text-xs"
                  value={secretKey}
                  onChange={(e) => setSecretKey(e.target.value)}
                  placeholder={overview?.secret_set ? "•••••••• (guardada)" : "Secret"}
                />
              </label>
              <label className="text-xs text-ink3 sm:col-span-2">
                Bucket
                <input
                  className="input w-full mt-1 font-mono text-xs"
                  value={bucket}
                  onChange={(e) => setBucket(e.target.value)}
                  placeholder="mis-backups"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-2 mt-3">
              <button className="btn-primary" onClick={() => void onSaveCredentials()} disabled={savingCreds}>
                {savingCreds ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                Guardar
              </button>
              <button className="btn" onClick={() => void onTestConnection()} disabled={testing}>
                {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <PlugZap className="w-4 h-4" />}
                Probar conexión
              </button>
              {testResult && (
                <span
                  className={
                    "text-xs " + (testResult.ok ? "text-state-ok" : "text-state-bad")
                  }
                >
                  {testResult.message}
                </span>
              )}
            </div>
          </div>

          {/* Configuración */}
          <div className="card p-4">
            <h2 className="font-semibold text-ink mb-1">Configuración</h2>
            <p className="text-xs text-ink3 mb-3">
              Retención automática en el bucket: diaria → 7 diarias + 4 semanales; con más
              frecuencia se conservan además las últimas 24-48 copias; semanal y mensual →
              las últimas 7 copias.
            </p>

            {/* Destino: dónde acaba cada copia. Selector explícito (9602). */}
            <div className="mb-4">
              <div className="text-xs text-ink3 mb-1.5">Dónde se guarda cada copia</div>
              {overview && overview.destination == null && overview.s3_configured && (
                <div className="text-xs text-ink2 bg-brand/10 border border-brand/30 rounded-coro-sm px-3 py-2 mb-2">
                  Todavía no has elegido destino: mientras tanto cada copia se guarda en el
                  servidor y además se sube a {providerLabel} (comportamiento anterior). Elige
                  una opción y pulsa «Guardar configuración».
                </div>
              )}
              <div className="border border-line rounded-coro-sm divide-y divide-line">
                {(
                  [
                    {
                      value: "cloud" as BackupDestination,
                      icon: <Cloud className="w-4 h-4" />,
                      title: `${providerLabel} (recomendado)`,
                      desc: `La copia se genera y se sube cifrada a ${providerLabel} sobre la marcha, sin escribir nada en el disco del servidor (así se hace aunque el disco esté lleno). Si la subida falla, se avisa y se intenta dejar la copia en el servidor.`,
                      needsBucket: true,
                    },
                    {
                      value: "server" as BackupDestination,
                      icon: <HardDrive className="w-4 h-4" />,
                      title: "Servidor",
                      desc: "La copia se queda como fichero en el disco del servidor. No se sube nada al bucket.",
                      needsBucket: false,
                    },
                    {
                      value: "both" as BackupDestination,
                      icon: <CloudUpload className="w-4 h-4" />,
                      title: `Ambos (servidor + ${providerLabel})`,
                      desc: `La copia se guarda en el disco del servidor y además se sube cifrada a ${providerLabel}. Si no queda espacio en el disco, se sube igual al bucket y se marca como copia degradada.`,
                      needsBucket: true,
                    },
                  ] as const
                ).map((opt) => {
                  const disabled = opt.needsBucket && !overview?.s3_configured;
                  return (
                    <label
                      key={opt.value}
                      className={
                        "flex items-start gap-3 px-3 py-2.5 " +
                        (disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer")
                      }
                    >
                      <input
                        type="radio"
                        name="backup-destination"
                        className="mt-1 accent-current shrink-0"
                        checked={destination === opt.value}
                        disabled={disabled}
                        onChange={() => setDestination(opt.value)}
                      />
                      <div className="min-w-0">
                        <div className="text-sm text-ink flex items-center gap-1.5">
                          <span className="text-ink3">{opt.icon}</span> {opt.title}
                        </div>
                        <div className="text-xs text-ink3 mt-0.5">
                          {disabled ? "Configura el bucket primero (tarjeta «Proveedor de almacenamiento»)." : opt.desc}
                        </div>
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="grid sm:grid-cols-2 gap-3">
              <label className="text-xs text-ink3">
                Frecuencia
                <select
                  className="input w-full mt-1"
                  value={frequency}
                  onChange={(e) => setFrequency(e.target.value)}
                >
                  {Object.entries(FREQ_LABEL).map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </select>
              </label>
              {DAY_FREQS.includes(frequency) && (
                <label className="text-xs text-ink3">
                  Hora de la copia (Madrid)
                  <select
                    className="input w-full mt-1"
                    value={dailyHour}
                    onChange={(e) => setDailyHour(Number(e.target.value))}
                  >
                    {Array.from({ length: 24 }, (_, h) => (
                      <option key={h} value={h}>
                        {String(h).padStart(2, "0")}:00
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <div className="mt-3">
              <button className="btn-primary" onClick={() => void onSaveConfig()} disabled={savingConfig}>
                {savingConfig ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                Guardar configuración
              </button>
            </div>
          </div>

          {/* Registro de intentos */}
          <div className="card p-4">
            <h2 className="font-semibold text-ink flex items-center gap-2 mb-3">
              <History className="w-4 h-4 text-ink3" /> Registro de intentos
            </h2>
            {runs.length === 0 ? (
              <div className="text-sm text-ink3">Sin intentos registrados todavía.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-ink3 border-b border-line">
                      <th className="py-1.5 pr-3 font-medium">Fecha</th>
                      <th className="py-1.5 pr-3 font-medium">Tipo</th>
                      <th className="py-1.5 pr-3 font-medium">Resultado</th>
                      <th className="py-1.5 pr-3 font-medium">Destino</th>
                      <th className="py-1.5 pr-3 font-medium">Tamaño</th>
                      <th className="py-1.5 pr-3 font-medium">Duración</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {runs.map((r) => (
                      <tr key={r.id || r.created_at}>
                        <td className="py-1.5 pr-3 whitespace-nowrap">{fmtDate(r.created_at)}</td>
                        <td className="py-1.5 pr-3">{KIND_LABEL[r.kind] || r.kind}</td>
                        <td className="py-1.5 pr-3">
                          {r.status === "ok" ? (
                            <span className="inline-flex items-center gap-1 text-state-ok">
                              <CheckCircle2 className="w-3.5 h-3.5" /> OK
                            </span>
                          ) : (
                            <span
                              className="inline-flex items-center gap-1 text-state-bad"
                              title={r.error || undefined}
                            >
                              <XCircle className="w-3.5 h-3.5" /> Fallo
                            </span>
                          )}
                          {r.error && (
                            // En una copia correcta el texto es una NOTA (p. ej.
                            // "copia degradada: sin fichero local"), no un fallo.
                            <div
                              className={
                                "text-xs max-w-xs truncate " +
                                (r.status === "ok" ? "text-state-warn" : "text-ink3")
                              }
                              title={r.error}
                            >
                              {r.error}
                            </div>
                          )}
                        </td>
                        <td className="py-1.5 pr-3">
                          <DestinoBadges run={r} providerLabel={providerLabel} />
                        </td>
                        <td className="py-1.5 pr-3 whitespace-nowrap">{fmtBytes(r.size_bytes)}</td>
                        <td className="py-1.5 pr-3 whitespace-nowrap">
                          {r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)} s` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
