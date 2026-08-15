import { useEffect, useState, useMemo } from "react";
import { RefreshCw, Pause, Play, AlertCircle, Info, AlertTriangle, EyeOff } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { getRuntimeLogs, type RuntimeLogEntry } from "@/services/admin";
import { errorDetail } from "@/lib/errors";
import { usePrivacy } from "@/store/privacy";

// El backend ya enmascara la PII al ESCRIBIR en el buffer (ver
// services/runtime_logs.py), así que aquí no llega un teléfono en claro. Pero
// el "Modo privacidad" es otra cosa: es el interruptor para compartir pantalla
// sin enseñar datos de clientes, y esta pantalla era la única que se lo saltaba
// (un id de conversación, un @usuario o un asunto de correo siguen siendo
// rastro identificable si alguien está grabando).
//
// Con el modo activo se tapa el texto libre: mensaje y campos extra. El nivel,
// el evento y la hora se quedan — son los que sirven para diagnosticar y no
// dicen nada de nadie.
const PRIVACY_MASK = "•••";

function maskLogText(value: string, enabled: boolean): string {
  if (!enabled || !value) return value;
  return PRIVACY_MASK;
}

const LEVELS = [
  { value: "", label: "Todos" },
  { value: "info", label: "Info" },
  { value: "warn", label: "Aviso" },
  { value: "error", label: "Error" },
];

const LEVEL_STYLE: Record<string, { color: string; bg: string; icon: typeof Info }> = {
  info: { color: "text-ink2", bg: "bg-paper2", icon: Info },
  warn: { color: "text-state-warn", bg: "bg-state-warn/10", icon: AlertTriangle },
  error: { color: "text-state-bad", bg: "bg-state-bad/10", icon: AlertCircle },
};

function fmtTime(ms: number): string {
  return new Date(ms).toLocaleTimeString("es-ES", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export default function SystemLogs() {
  const [logs, setLogs] = useState<RuntimeLogEntry[]>([]);
  const [level, setLevel] = useState("");
  const [autorefresh, setAutorefresh] = useState(true);
  const [loading, setLoading] = useState(false);
  // Sin esto, un fallo de la API dejaba los logs vacíos (o congelados) sin aviso.
  const [error, setError] = useState<string | null>(null);
  const privacy = usePrivacy((s) => s.enabled);

  async function refresh() {
    setLoading(true);
    try {
      const r = await getRuntimeLogs({ limit: 300, level: level || undefined });
      setLogs(r);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar los logs del sistema."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level]);

  useEffect(() => {
    if (!autorefresh) return;
    const i = setInterval(refresh, 5000);
    return () => clearInterval(i);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autorefresh, level]);

  const stats = useMemo(() => {
    const c = { info: 0, warn: 0, error: 0 };
    for (const l of logs) {
      if (l.level in c) c[l.level as keyof typeof c]++;
    }
    return c;
  }, [logs]);

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        eyebrow="Sistema"
        title={<>Logs en <span className="accent">vivo</span></>}
        description={`Últimos ${logs.length} eventos del runtime. Se actualiza cada 5s si está activo.`}
        actions={
          <>
            <button
              type="button"
              onClick={() => setAutorefresh((v) => !v)}
              className={"btn " + (autorefresh ? "btn-ghost text-state-ok" : "")}
            >
              {autorefresh ? <Pause /> : <Play />}
              <span className="hidden sm:inline">{autorefresh ? "Pausar" : "Reanudar"}</span>
            </button>
            <button type="button" onClick={() => void refresh()} className="btn" disabled={loading}>
              <RefreshCw className={loading ? "animate-spin" : undefined} />
            </button>
          </>
        }
      />
      <div className="flex-1 overflow-auto p-4 md:p-6 max-w-5xl space-y-4">
        {error && (
          <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{error}</span>
            <button type="button" onClick={() => void refresh()} className="font-semibold hover:underline shrink-0">
              Reintentar
            </button>
          </div>
        )}
        {privacy && (
          <div className="text-xs text-ink3 bg-paper2 border border-line rounded-coro-sm px-3 py-2 inline-flex items-center gap-2">
            <EyeOff className="w-3.5 h-3.5 shrink-0" />
            Modo privacidad activo: el detalle de cada evento está oculto. Se ven la hora, el
            nivel y el evento.
          </div>
        )}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="eyebrow">Nivel</span>
          <div className="inline-flex rounded-coro-sm border border-line bg-card overflow-hidden">
            {LEVELS.map((l) => (
              <button
                key={l.value}
                type="button"
                onClick={() => setLevel(l.value)}
                className={
                  "px-3 py-1.5 text-xs font-medium border-r border-line last:border-r-0 " +
                  (level === l.value ? "bg-brand text-brand-on" : "text-ink2 hover:bg-paper2")
                }
              >
                {l.label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-3 text-xs">
            <span className="inline-flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-ink3" />{stats.info} info</span>
            <span className="inline-flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-state-warn" />{stats.warn} aviso</span>
            <span className="inline-flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-state-bad" />{stats.error} error</span>
          </div>
        </div>

        <div className="card overflow-hidden">
          {logs.length === 0 && !error && (
            <div className="px-4 py-12 text-center text-ink3 italic font-display">
              Sin eventos aún.
            </div>
          )}
          <ul className="divide-y divide-line2">
            {logs.map((l, i) => {
              const meta = LEVEL_STYLE[l.level] || LEVEL_STYLE.info;
              const Icon = meta.icon;
              const extras = Object.entries(l).filter(
                ([k]) => !["ts", "level", "event", "message"].includes(k)
              );
              return (
                <li key={i} className="px-3 sm:px-4 py-2.5 flex items-start gap-3 hover:bg-paper2/40">
                  <div className={"w-6 h-6 rounded-full flex items-center justify-center shrink-0 " + meta.bg}>
                    <Icon className={"w-3.5 h-3.5 " + meta.color} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-baseline gap-2 flex-wrap">
                      <span className="font-mono text-[10px] text-ink4">{fmtTime(l.ts)}</span>
                      <span className="font-mono text-[11px] text-ink2">{l.event}</span>
                    </div>
                    {l.message && (
                      <div className="text-sm text-ink mt-0.5 break-words">
                        {maskLogText(l.message, privacy)}
                      </div>
                    )}
                    {extras.length > 0 && (
                      <div className="text-[10px] font-mono text-ink3 mt-1 flex flex-wrap gap-x-3">
                        {extras.map(([k, v]) => (
                          <span key={k} className="break-all">
                            <span className="text-ink4">{k}=</span>
                            {privacy ? PRIVACY_MASK : String(v).slice(0, 50)}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </div>
  );
}
