import { ConfirmModal } from "@/components/ConfirmModal";
import { useEffect, useMemo, useState } from "react";
import { Sparkles, RefreshCw, Pause, Play, FlaskConical, GraduationCap, ArrowRight, Mic, MicOff } from "lucide-react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/PageHeader";
import {
  channelMode,
  getAgentPause,
  getAgentUsage,
  getBudgetStatus,
  setAgentPause,
  setChannelMode,
  setChannelTranscription,
  type AgentPauseState,
  type ChannelMode,
  type ChannelPauseInfo,
  type BudgetStatus,
  type UsageSummary,
} from "@/services/admin";
import { errorDetail } from "@/lib/errors";

// Los nombres tienen que cubrir TODOS los valores de ChannelType: lo que no
// esté aquí se pinta con el identificador crudo, y en el panel se leía
// "retell_voice". "web" y "webchat" conviven porque la API ha usado los dos.
const CHANNEL_LABEL: Record<string, string> = {
  whatsapp: "WhatsApp",
  web: "Chat web",
  webchat: "Chat web",
  instagram_dm: "Instagram",
  email: "Email",
  retell_voice: "Voz",
};

// Canales que reciben notas de voz: son los únicos donde pinta el interruptor
// de transcripción.
const AUDIO_CHANNELS = ["whatsapp", "instagram_dm"];

const RANGES = [
  { key: "24h", label: "24 h" },
  { key: "7d", label: "7 días" },
  { key: "30d", label: "30 dias" },
  // Mes natural: la misma ventana que el KPI "Coste IA" de Inicio, para que
  // el total de aquí cuadre con el de la portada.
  { key: "month", label: "Este mes" },
] as const;

type RangeKey = (typeof RANGES)[number]["key"];

const SOURCE_LABEL: Record<string, string> = {
  agent: "LLM agente",
  rag: "Embeddings RAG",
  moderation: "Moderación",
  classifier: "Clasificador",
  internal_agent: "Agente interno",
};

const SOURCE_COLOR: Record<string, string> = {
  agent: "var(--brand)",
  rag: "#9B8AFB",
  moderation: "#F4B731",
  classifier: "#25D366",
  internal_agent: "#E58A2F",
};

// Paleta para los buckets "por agente" cuya key es un agent_id (no un source
// conocido). Color determinista por posición para que cada agente tenga el suyo.
const AGENT_PALETTE = ["var(--brand)", "#6C7BFF", "#9B8AFB", "#25D366", "#E58A2F", "#DC4B3C", "#2DA771"];

function bucketColor(key: string, index: number): string {
  return SOURCE_COLOR[key] || AGENT_PALETTE[index % AGENT_PALETTE.length];
}

// Numeros enteros (tokens, llamadas) en formato es-ES: PUNTO para los miles
// (1.000, 2.345.678). Los tokens son enteros, asi que no llevan decimales; el
// separador decimal (coma) solo aplica al coste, que usa fmtUsd.
const _NF_INT = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 });
function fmt(n: number): string {
  return _NF_INT.format(n || 0);
}

// Formato es-ES con punto miles, coma decimales. Decimales adaptativos:
// montos pequenios (<1$) muestran 4 decimales; >1$ muestran 2.
const _NF_USD_BIG = new Intl.NumberFormat("es-ES", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const _NF_USD_SMALL = new Intl.NumberFormat("es-ES", {
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
});
function fmtUsd(n: number): string {
  const v = n || 0;
  const formatted = v < 1 && v > 0 ? _NF_USD_SMALL.format(v) : _NF_USD_BIG.format(v);
  return formatted + " $";
}

export default function AgentDashboard() {
  const [range, setRange] = useState<RangeKey>("7d");
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [pause, setPause] = useState<AgentPauseState | null>(null);
  const [togglingPause, setTogglingPause] = useState(false);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  // Sin esto, un fallo de la API dejaba el dashboard vacío (o con datos
  // desactualizados) sin ningún aviso.
  const [error, setError] = useState<string | null>(null);
  // Error de las acciones (pausar, reanudar, transcripción). Va aparte del de
  // carga porque aquí "Reintentar" sería recargar el panel, no repetir la
  // acción que falló.
  const [actionError, setActionError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    try {
      const [u, p, b] = await Promise.all([getAgentUsage(range), getAgentPause(), getBudgetStatus()]);
      setUsage(u);
      setPause(p);
      setBudget(b);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar el dashboard del agente."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range]);

  // Confirmación pendiente de una pausa (global o de un canal concreto).
  const [confirmPause, setConfirmPause] = useState<
    { kind: "global" } | { kind: "channel"; ch: ChannelPauseInfo } | null
  >(null);

  async function togglePause() {
    if (!pause) return;
    const next = !pause.paused;
    // Pausar TODO pide confirmación; reanudar no.
    if (next && !confirmPause) {
      setConfirmPause({ kind: "global" });
      return;
    }
    setConfirmPause(null);
    setTogglingPause(true);
    setActionError(null);
    try {
      setPause(await setAgentPause(next));
    } catch (e) {
      // Sin catch, el interruptor volvía solo a su sitio y parecía que no se
      // había pulsado bien: mientras tanto el bot seguía como estaba,
      // contestando a clientes cuando se creía pausado.
      setActionError(
        errorDetail(
          e,
          next
            ? "No se pudo pausar el agente. Sigue respondiendo."
            : "No se pudo reanudar el agente. Sigue pausado.",
        ),
      );
    } finally {
      setTogglingPause(false);
    }
  }

  async function changeChannelMode(ch: ChannelPauseInfo, mode: ChannelMode, confirmed = false) {
    if (channelMode(ch) === mode) return; // ya está en ese estado
    if (mode === "paused" && !confirmed) {
      setConfirmPause({ kind: "channel", ch });
      return;
    }
    setConfirmPause(null);
    setTogglingPause(true);
    setActionError(null);
    try {
      setPause(await setChannelMode(ch.canal, mode));
    } catch (e) {
      // Igual que arriba: el canal se quedaba como estaba, sin aviso, y quien
      // lo pausó daba por hecho que el bot ya no contestaba ahí.
      setActionError(
        errorDetail(e, "No se pudo cambiar el estado del agente en ese canal. Sigue como estaba."),
      );
    } finally {
      setTogglingPause(false);
    }
  }

  const [togglingAudio, setTogglingAudio] = useState<string | null>(null);

  async function toggleTranscription(ch: ChannelPauseInfo) {
    setTogglingAudio(ch.canal);
    setActionError(null);
    try {
      setPause(await setChannelTranscription(ch.canal, !ch.transcribe_audio));
    } catch (e) {
      // El interruptor volvía a su sitio y no se decía nada: los audios se
      // seguían tratando como antes sin que nadie lo supiera.
      setActionError(
        errorDetail(e, "No se pudo cambiar la transcripción de audios en ese canal."),
      );
    } finally {
      setTogglingAudio(null);
    }
  }

  // Serie por dia del COSTE ($) sumando sources, RELLENANDO todos los dias del
  // rango con 0 para que el eje X sea una linea temporal continua.
  const dailyTotals = useMemo(() => {
    const m = new Map<string, { day: string; total: number; bySource: Record<string, number> }>();
    for (const p of usage?.series ?? []) {
      const slot = m.get(p.day) || { day: p.day, total: 0, bySource: {} };
      slot.total += p.cost_usd;
      slot.bySource[p.source] = (slot.bySource[p.source] || 0) + p.cost_usd;
      m.set(p.day, slot);
    }
    // Genera los ultimos N dias (en UTC, que es como agrupa el backend) y
    // rellena los que falten con 0.
    const now = new Date();
    const n =
      range === "24h" ? 1
      : range === "7d" ? 7
      : range === "month" ? now.getUTCDate() // días transcurridos del mes (UTC)
      : 30;
    for (let i = n - 1; i >= 0; i--) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
      const key = d.toISOString().slice(0, 10);
      if (!m.has(key)) m.set(key, { day: key, total: 0, bySource: {} });
    }
    return Array.from(m.values()).sort((a, b) => (a.day < b.day ? -1 : 1));
  }, [usage, range]);

  const maxDaily = useMemo(
    () => Math.max(0, ...dailyTotals.map((d) => d.total)),
    [dailyTotals]
  );
  // El grafico es de coste: hay algo que pintar si hay coste estimado.
  const hasConsumo = (usage?.total_cost_usd ?? 0) > 0;

  const isPaused = pause?.paused ?? false;
  const demoCount = pause?.demo_conversations.length ?? 0;

  return (
    <div className="h-full flex flex-col">
      <ConfirmModal
        open={confirmPause !== null}
        title={
          confirmPause?.kind === "channel"
            ? `¿Pausar el agente en ${CHANNEL_LABEL[confirmPause.ch.canal] || confirmPause.ch.canal}?`
            : "¿Pausar el agente en TODOS los canales?"
        }
        description="Dejará de responder (salvo las conversaciones marcadas como demo) hasta que lo reanudes."
        confirmLabel="Pausar"
        tone="danger"
        busy={togglingPause}
        onConfirm={() => {
          if (confirmPause?.kind === "channel") void changeChannelMode(confirmPause.ch, "paused", true);
          else void togglePause();
        }}
        onCancel={() => setConfirmPause(null)}
      />
      <PageHeader
        eyebrow="Estado y consumo"
        title={<>Dashboard del <span className="accent">agente</span></>}
        description="Toggle del agente, conversaciones en modo demo y consumo de tokens del LLM"
        actions={
          <button
            type="button"
            onClick={() => void refresh()}
            className="btn btn-ghost"
            disabled={loading}
          >
            <RefreshCw className={"w-4 h-4 " + (loading ? "animate-spin" : "")} />
            <span className="hidden sm:inline">Actualizar</span>
          </button>
        }
      />

      <div className="flex-1 overflow-auto p-4 md:p-6 space-y-6 max-w-6xl">
        {error && (
          <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{error}</span>
            <button type="button" onClick={() => void refresh()} className="font-semibold hover:underline shrink-0">
              Reintentar
            </button>
          </div>
        )}
        {actionError && (
          <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{actionError}</span>
            <button
              type="button"
              onClick={() => setActionError(null)}
              className="font-semibold hover:underline shrink-0"
              aria-label="Cerrar el aviso de error"
            >
              Cerrar
            </button>
          </div>
        )}
        {/* Atajo global — pausa o reactiva todos los canales a la vez */}
        <div
          className={
            "card p-5 md:p-6 border-2 " +
            (isPaused ? "border-state-warn/40 bg-state-warn/5" : "border-line")
          }
        >
          <div className="flex flex-col md:flex-row md:items-center gap-4">
            <div className="flex items-start gap-3 flex-1 min-w-0">
              <div
                className={
                  "w-11 h-11 rounded-full flex items-center justify-center shrink-0 " +
                  (isPaused
                    ? "bg-state-warn/15 text-state-warn"
                    : "bg-state-ok/15 text-state-ok")
                }
              >
                {isPaused ? <Pause className="w-5 h-5" /> : <Play className="w-5 h-5" />}
              </div>
              <div className="min-w-0">
                <div className="text-[10px] uppercase tracking-wider text-ink3 font-medium">
                  Atajo global
                </div>
                <div className="font-display text-2xl text-ink leading-tight mt-0.5">
                  {isPaused ? <>Todos los canales pausados</> : <>Todos los canales activos</>}
                </div>
                <p className="text-sm text-ink3 mt-1.5">
                  {isPaused
                    ? demoCount > 0
                      ? <>El agente NO responde a nadie excepto a las <span className="font-semibold text-ink2">{demoCount}</span> conversacion{demoCount === 1 ? "" : "es"} en modo demo.</>
                      : <>El agente NO responde a ninguna conversacion. Marca alguna como demo desde el Inbox para activarlo solo ahí.</>
                    : <>Atajo para pausar o reactivar todos los canales a la vez. Para pausa independiente por canal, usa las tarjetas de abajo.</>}
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => void togglePause()}
              disabled={togglingPause || !pause}
              className={
                isPaused
                  ? "btn btn-primary shrink-0 justify-center min-w-[120px]"
                  : "btn shrink-0 justify-center min-w-[120px] border-state-warn/40 text-state-warn hover:bg-state-warn/10"
              }
            >
              {togglingPause ? "Cambiando…" : isPaused ? "Reactivar todo" : "Pausar todo"}
            </button>
          </div>
          {isPaused && (
            <div className="mt-4 pt-4 border-t border-line2 flex flex-col sm:flex-row sm:items-center gap-2 text-xs text-ink3">
              <FlaskConical className="w-4 h-4 text-brand-ink" />
              <span className="flex-1">
                Para activar el agente solo en una conversacion concreta, entra en ella y pulsa <span className="font-semibold text-ink2">"Activar agente aqui"</span>.
              </span>
              <Link
                to="/inbox"
                className="btn btn-ghost btn-sm self-start sm:self-auto"
              >
                Ir al Inbox <ArrowRight className="w-3.5 h-3.5" />
              </Link>
            </div>
          )}
        </div>

        {/* Estado por canal — 3 vías: Activo / Entrenamiento / Pausado */}
        {pause?.channels && pause.channels.length > 0 && (
          <div className="space-y-3">
            <div className="text-[10px] uppercase tracking-wider text-ink3 font-medium">
              Estado por canal
            </div>
            <p className="text-xs text-ink3 -mt-1">
              <span className="font-medium text-ink2">Activo</span>: el agente responde solo. <span className="font-medium text-ink2">Entrenamiento</span>: el agente redacta una sugerencia que revisas y envías a mano desde el Inbox. <span className="font-medium text-ink2">Pausado</span>: el agente no hace nada.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {pause.channels.map((ch) => {
                const label = CHANNEL_LABEL[ch.canal] || ch.canal;
                const mode = channelMode(ch);
                const channelPaused = mode === "paused";
                const channelTraining = mode === "training";
                // Pausado por el atajo global (no por su propia pausa): no se
                // puede cambiar el modo del canal hasta reactivar el global.
                const overriddenByGlobal = channelPaused && !ch.self_paused;
                const accentBorder = channelPaused
                  ? "border-state-warn/40 bg-state-warn/5"
                  : channelTraining
                  ? "border-brand/40 bg-brand/5"
                  : "border-line";
                const iconWrap = channelPaused
                  ? "bg-state-warn/15 text-state-warn"
                  : channelTraining
                  ? "bg-brand/15 text-brand-ink"
                  : "bg-state-ok/15 text-state-ok";
                return (
                  <div key={ch.canal} className={"card p-4 md:p-5 border-2 " + accentBorder}>
                    <div className="flex items-start gap-3">
                      <div className={"w-9 h-9 rounded-full flex items-center justify-center shrink-0 " + iconWrap}>
                        {channelPaused ? (
                          <Pause className="w-4 h-4" />
                        ) : channelTraining ? (
                          <GraduationCap className="w-4 h-4" />
                        ) : (
                          <Play className="w-4 h-4" />
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="text-[10px] uppercase tracking-wider text-ink3 font-medium">
                          {label}
                        </div>
                        <div className="font-display text-lg text-ink leading-tight mt-0.5">
                          {channelPaused ? "Pausado" : channelTraining ? "Entrenamiento" : "Activo"}
                        </div>
                        <p className="text-xs text-ink3 mt-1.5">
                          {channelPaused
                            ? ch.demo_count > 0
                              ? <>El agente NO responde en {label} excepto a las <span className="font-semibold text-ink2">{ch.demo_count}</span> conversación{ch.demo_count === 1 ? "" : "es"} en modo demo.</>
                              : <>El agente NO responde a ninguna conversación de {label}. Marca alguna como demo desde el Inbox para activarlo solo ahí.</>
                            : channelTraining
                            ? <>El agente redacta una <span className="font-semibold text-ink2">sugerencia</span> por cada mensaje en {label}, pero NO la envía: la revisas y la envías a mano desde el Inbox.</>
                            : <>El agente responde automáticamente en {label}.</>}
                        </p>
                        {overriddenByGlobal && (
                          <p className="text-[11px] text-state-warn mt-1.5">
                            Pausado por el atajo global. Reactiva el global para cambiar el modo de este canal.
                          </p>
                        )}
                      </div>
                    </div>
                    {/* Control segmentado de 3 estados */}
                    <div className="mt-3 inline-flex rounded-coro-sm border border-line bg-card overflow-hidden w-full">
                      {(["active", "training", "paused"] as ChannelMode[]).map((m) => {
                        const selected = mode === m;
                        const onColor =
                          m === "paused"
                            ? "bg-state-warn text-brand-on"
                            : m === "training"
                            ? "bg-brand text-brand-on"
                            : "bg-state-ok text-white";
                        return (
                          <button
                            key={m}
                            type="button"
                            onClick={() => void changeChannelMode(ch, m)}
                            disabled={togglingPause || overriddenByGlobal}
                            aria-pressed={selected}
                            title={overriddenByGlobal ? "Reactiva el atajo global primero" : undefined}
                            className={
                              "flex-1 px-2 py-1.5 text-xs font-medium border-r border-line last:border-r-0 transition disabled:opacity-50 disabled:cursor-not-allowed " +
                              (selected ? onColor : "text-ink2 hover:bg-paper2")
                            }
                          >
                            {m === "active" ? "Activo" : m === "training" ? "Entrenamiento" : "Pausado"}
                          </button>
                        );
                      })}
                    </div>
                    {/* Notas de voz. Solo en los canales que las reciben. Va
                        aparte del modo a propósito: se transcribe siempre, aunque
                        el canal esté pausado o la lleve una persona. */}
                    {AUDIO_CHANNELS.includes(ch.canal) && (
                      <div className="mt-3 pt-3 border-t border-line2 flex items-start gap-2">
                        <div className="flex-1 min-w-0">
                          <div className="text-xs font-medium text-ink2 flex items-center gap-1.5">
                            {ch.transcribe_audio ? (
                              <Mic className="w-3.5 h-3.5 text-state-ok" />
                            ) : (
                              <MicOff className="w-3.5 h-3.5 text-ink3" />
                            )}
                            Transcribir notas de voz
                          </div>
                          <p className="text-[11px] text-ink3 mt-1">
                            {ch.transcribe_audio
                              ? <>Las notas de voz de {label} se pasan a texto siempre, aunque el bot esté pausado o la conversación la lleves tú.</>
                              : <>Las notas de voz de {label} NO se transcriben: llegan al Inbox como audio, para escucharlas a mano.</>}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => void toggleTranscription(ch)}
                          disabled={togglingAudio === ch.canal}
                          aria-pressed={ch.transcribe_audio}
                          className={
                            "pill shrink-0 transition disabled:opacity-50 disabled:cursor-not-allowed " +
                            (ch.transcribe_audio
                              ? "bg-state-ok/10 text-state-ok hover:bg-state-ok/20"
                              : "bg-paper3 text-ink3 hover:bg-paper2")
                          }
                        >
                          {ch.transcribe_audio ? "Encendido" : "Apagado"}
                        </button>
                      </div>
                    )}
                    {channelPaused && ch.demo_count === 0 && (
                      <div className="mt-3 pt-3 border-t border-line2 text-[11px] text-ink3 flex items-center gap-1.5">
                        <FlaskConical className="w-3.5 h-3.5 text-brand-ink" />
                        <Link to="/inbox" className="underline hover:text-ink2">
                          Ir al Inbox para activar el agente en una conversacion
                        </Link>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Presupuesto mensual */}
        {budget?.budget_usd != null && (
          <div className={"card p-4 md:p-5 " + (budget.exceeded ? "border-state-bad/40 border-2 bg-state-bad/5" : "")}>
            <div className="flex items-center justify-between gap-3 mb-3">
              <div>
                <div className="eyebrow">Presupuesto · {budget.month}</div>
                <div className="font-display text-xl text-ink mt-0.5">
                  {fmtUsd(budget.cost_usd)} <span className="text-ink3 text-base">/ {fmtUsd(budget.budget_usd)}</span>
                </div>
              </div>
              <div className={"text-2xl font-numbers tabular-nums font-semibold " + (budget.exceeded ? "text-state-bad" : (budget.percent && budget.percent > 80) ? "text-state-warn" : "text-ink2")}>
                {budget.percent != null ? `${budget.percent}%` : "—"}
              </div>
            </div>
            <div className="h-2.5 bg-paper2 rounded-full overflow-hidden">
              <div
                className={"h-full transition-all " + (budget.exceeded ? "bg-state-bad" : (budget.percent && budget.percent > 80) ? "bg-state-warn" : "bg-brand")}
                style={{ width: Math.min(100, budget.percent ?? 0) + "%" }}
              />
            </div>
            {budget.exceeded && (
              <p className="text-xs text-state-bad mt-2 font-medium">
                Tope superado. El agente está pausado automáticamente. Sube el tope en Agentes → Tarifas y límites para reactivar.
              </p>
            )}
          </div>
        )}

        {/* Selector de rango */}
        <div className="flex items-center gap-2">
          <span className="eyebrow">Rango</span>
          <div className="inline-flex rounded-coro-sm border border-line bg-card overflow-hidden">
            {RANGES.map((r) => (
              <button
                key={r.key}
                type="button"
                onClick={() => setRange(r.key)}
                className={
                  "px-3 py-1.5 text-xs font-medium border-r border-line last:border-r-0 " +
                  (range === r.key
                    ? "bg-brand text-brand-on"
                    : "text-ink2 hover:bg-paper2")
                }
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>

        {/* KPIs */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Kpi label="Llamadas LLM" value={usage?.total_calls ?? 0} />
          <Kpi label="Total tokens" value={usage?.total_tokens ?? 0} />
          <Kpi
            label="Entrada / Salida"
            value={0}
            display={`${fmt(usage?.total_prompt_tokens ?? 0)} / ${fmt(usage?.total_completion_tokens ?? 0)}`}
          />
          <Kpi
            label="Coste estimado"
            value={usage?.total_cost_usd ?? 0}
            display={fmtUsd(usage?.total_cost_usd ?? 0)}
            accent
          />
        </div>

        {/* Grafico de barras por dia */}
        <div className="card p-4 md:p-5">
          <div className="flex items-center gap-2 mb-4">
            <Sparkles className="w-4 h-4 text-brand-ink" />
            <h3 className="font-semibold text-ink text-sm">Coste del LLM por día ($)</h3>
            <span className="eyebrow ml-auto">Apilado por fuente</span>
          </div>
          {/* Alturas en PX (no en %): un % de altura no resuelve contra un
              padre de altura automatica y colapsaba a ~0 → el grafico salia
              "vacio". Con px sobre una altura fija se ve siempre. */}
          <div className="relative">
            <div className="flex items-end gap-2 overflow-x-auto pb-2" style={{ height: 176 }}>
              {dailyTotals.map((d) => {
                const sources = Object.keys(d.bySource);
                const barH = d.total > 0 && maxDaily > 0 ? Math.max(3, (d.total / maxDaily) * 150) : 0;
                return (
                  <div
                    key={d.day}
                    className="flex flex-col items-center justify-end gap-1.5 shrink-0 h-full"
                    style={{ minWidth: 34 }}
                  >
                    <div
                      className="w-7 sm:w-9 rounded-t-md overflow-hidden flex flex-col-reverse"
                      style={{ height: barH, background: barH > 0 ? "var(--paper-2)" : "transparent" }}
                      title={`${d.day} — ${fmtUsd(d.total)}`}
                    >
                      {d.total > 0 &&
                        sources.map((s) => {
                          const v = d.bySource[s];
                          if (!v) return null;
                          return (
                            <div
                              key={s}
                              style={{
                                height: (v / d.total) * barH,
                                background: SOURCE_COLOR[s] || "var(--ink-3)",
                              }}
                            />
                          );
                        })}
                    </div>
                    <div className="text-[10px] font-mono text-ink3">{d.day.slice(5)}</div>
                  </div>
                );
              })}
            </div>
            {!hasConsumo && (
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                <span className="text-ink3 text-sm italic font-display bg-card/70 px-3 py-1 rounded">
                  Sin coste estimado en este rango
                </span>
              </div>
            )}
          </div>
          {/* Leyenda */}
          {Object.keys(usage?.by_source ?? {}).length > 0 && (
            <div className="flex flex-wrap items-center gap-3 mt-3 text-[11px] text-ink3">
              {Object.entries(usage?.by_source ?? {}).map(([s]) => (
                <span key={s} className="inline-flex items-center gap-1.5">
                  <span
                    className="w-2.5 h-2.5 rounded-sm"
                    style={{ background: SOURCE_COLOR[s] || "var(--ink-3)" }}
                  />
                  {SOURCE_LABEL[s] || s}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Desglose por agente (incluye clasificador y agente interno) */}
        <div className="card overflow-x-auto">
          <div className="px-4 py-3 border-b border-line bg-paper2 flex items-center">
            <h3 className="font-semibold text-ink text-sm">Desglose por agente</h3>
            <span className="eyebrow ml-auto">quién consume más</span>
          </div>
          <table className="w-full text-sm min-w-[480px]">
            <thead className="text-[11px] uppercase tracking-wider text-ink3">
              <tr className="border-b border-line">
                <th className="text-left px-4 py-2 font-medium">Agente</th>
                <th className="text-right px-4 py-2 font-medium">Llamadas</th>
                <th className="text-right px-4 py-2 font-medium hidden sm:table-cell">Tokens entrada</th>
                <th className="text-right px-4 py-2 font-medium hidden sm:table-cell">Tokens salida</th>
                <th className="text-right px-4 py-2 font-medium">Total</th>
                <th className="text-right px-4 py-2 font-medium">Coste</th>
              </tr>
            </thead>
            <tbody>
              {(usage?.by_agent ?? []).length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-ink3 text-sm italic font-display">
                    Sin datos
                  </td>
                </tr>
              ) : (
                (usage?.by_agent ?? []).map((b, i) => (
                  <tr key={b.key} className="border-b border-line2 last:border-b-0">
                    <td className="px-4 py-2.5">
                      <span className="inline-flex items-center gap-2">
                        <span
                          className="w-2 h-2 rounded-full"
                          style={{ background: bucketColor(b.key, i) }}
                        />
                        {b.label}
                      </span>
                    </td>
                    <td className="text-right px-4 py-2.5 font-mono text-xs">{fmt(b.calls)}</td>
                    <td className="text-right px-4 py-2.5 font-mono text-xs hidden sm:table-cell">{fmt(b.prompt_tokens)}</td>
                    <td className="text-right px-4 py-2.5 font-mono text-xs hidden sm:table-cell">{fmt(b.completion_tokens)}</td>
                    <td className="text-right px-4 py-2.5 font-numbers font-medium">{fmt(b.total_tokens)}</td>
                    <td className="text-right px-4 py-2.5 font-numbers font-medium">{fmtUsd(b.cost_usd)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Desglose por modelo (para controlar qué modelo se está usando) */}
        <div className="card overflow-x-auto">
          <div className="px-4 py-3 border-b border-line bg-paper2 flex items-center">
            <h3 className="font-semibold text-ink text-sm">Desglose por modelo</h3>
            <span className="eyebrow ml-auto">qué modelo se usa</span>
          </div>
          <table className="w-full text-sm min-w-[480px]">
            <thead className="text-[11px] uppercase tracking-wider text-ink3">
              <tr className="border-b border-line">
                <th className="text-left px-4 py-2 font-medium">Modelo</th>
                <th className="text-right px-4 py-2 font-medium">Llamadas</th>
                <th className="text-right px-4 py-2 font-medium hidden sm:table-cell">Tokens entrada</th>
                <th className="text-right px-4 py-2 font-medium hidden sm:table-cell">Tokens salida</th>
                <th className="text-right px-4 py-2 font-medium">Total</th>
                <th className="text-right px-4 py-2 font-medium">Coste</th>
              </tr>
            </thead>
            <tbody>
              {(usage?.by_model ?? []).length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-ink3 text-sm italic font-display">
                    Sin datos
                  </td>
                </tr>
              ) : (
                (usage?.by_model ?? []).map((b) => (
                  <tr key={b.model} className="border-b border-line2 last:border-b-0">
                    <td className="px-4 py-2.5 font-mono text-xs">{b.model || "—"}</td>
                    <td className="text-right px-4 py-2.5 font-mono text-xs">{fmt(b.calls)}</td>
                    <td className="text-right px-4 py-2.5 font-mono text-xs hidden sm:table-cell">{fmt(b.prompt_tokens)}</td>
                    <td className="text-right px-4 py-2.5 font-mono text-xs hidden sm:table-cell">{fmt(b.completion_tokens)}</td>
                    <td className="text-right px-4 py-2.5 font-numbers font-medium">{fmt(b.total_tokens)}</td>
                    <td className="text-right px-4 py-2.5 font-numbers font-medium">{fmtUsd(b.cost_usd)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <p className="text-[11px] text-ink4 text-center italic font-display">
          El coste es una estimación basada en precios por modelo (auto-actualizados desde OpenRouter). La factura real puede variar ligeramente. Las tarifas se gestionan en <Link to="/admin/agent/agents?tab=model" className="underline hover:text-ink2">Agentes → Tarifas y límites</Link>.
        </p>
      </div>
    </div>
  );
}

function Kpi({
  label,
  value,
  display,
  accent = false,
}: {
  label: string;
  value: number;
  display?: string;
  accent?: boolean;
}) {
  return (
    <div
      className={
        "rounded-coro p-4 md:p-5 border " +
        (accent
          ? "bg-brand border-transparent text-brand-on"
          : "bg-card border-line shadow-coro-1")
      }
    >
      <div
        className={"text-[10px] uppercase tracking-wider font-medium " + (accent ? "opacity-80" : "text-ink3")}
      >
        {label}
      </div>
      <div className="font-numbers font-medium tabular-nums text-[28px] sm:text-[34px] leading-none tracking-[-0.02em] mt-2">
        {display ?? fmt(value)}
      </div>
    </div>
  );
}
