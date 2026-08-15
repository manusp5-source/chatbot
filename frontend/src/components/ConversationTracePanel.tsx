import { useEffect, useMemo, useState } from "react";
import {
  X,
  ArrowDown,
  ArrowUp,
  Sparkles,
  ScrollText,
  Loader2,
  ChevronDown,
  ChevronRight,
  MessageSquare,
  Send,
  Brain,
  Wrench,
  BookOpen,
  GitBranch,
  AlertTriangle,
  Activity,
} from "lucide-react";
import {
  getConversationTrace,
  type TraceLevel,
  type TraceStep,
  type TraceStepKind,
} from "@/services/admin";

const _NF_USD_SMALL = new Intl.NumberFormat("es-ES", {
  minimumFractionDigits: 4,
  maximumFractionDigits: 6,
});
function fmtUsd(n: number): string {
  return _NF_USD_SMALL.format(n) + " $";
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("es-ES", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtLatency(ms?: number | null): string | null {
  if (ms == null) return null;
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

// Estilo + icono por tipo de step. Centralizado para que añadir un tipo nuevo
// sea trivial.
const KIND_META: Record<
  TraceStepKind,
  { label: string; Icon: typeof MessageSquare; color: string; dotColor: string }
> = {
  message_in: {
    label: "Cliente",
    Icon: MessageSquare,
    color: "text-state-warn",
    dotColor: "#E58A2F",
  },
  message_out: {
    label: "Respuesta",
    Icon: Send,
    color: "text-brand-ink",
    dotColor: "var(--brand, #DBE09E)",
  },
  llm_call: {
    label: "LLM",
    Icon: Brain,
    color: "text-[#9B8AFB]",
    dotColor: "#9B8AFB",
  },
  tool: {
    label: "Tool",
    Icon: Wrench,
    color: "text-[#16A085]",
    dotColor: "#16A085",
  },
  kb: {
    label: "KB",
    Icon: BookOpen,
    color: "text-[#6C7BFF]",
    dotColor: "#6C7BFF",
  },
  router: {
    label: "Router",
    Icon: GitBranch,
    color: "text-ink2",
    dotColor: "var(--ink-3, #6A604F)",
  },
  error: {
    label: "Error",
    Icon: AlertTriangle,
    color: "text-state-bad",
    dotColor: "#DC4B3C",
  },
  audit: {
    label: "Audit",
    Icon: Activity,
    color: "text-ink2",
    dotColor: "var(--ink-3, #6A604F)",
  },
};

const LEVEL_FILTERS: { value: TraceLevel | "all"; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "info", label: "Info" },
  { value: "warn", label: "Avisos" },
  { value: "error", label: "Errores" },
];

const KIND_FILTERS: { value: TraceStepKind | "all"; label: string }[] = [
  { value: "all", label: "Todo" },
  { value: "message_in", label: "Cliente" },
  { value: "message_out", label: "Respuesta" },
  { value: "llm_call", label: "LLM" },
  { value: "tool", label: "Tools" },
  { value: "kb", label: "KB" },
  { value: "router", label: "Router" },
  { value: "error", label: "Errores" },
];

function levelBg(level: TraceLevel): string {
  // Tinte de color de estado sobre la fila. La opacidad al 6% se pierde sobre el
  // papel oscuro del tema dark, así que se sube en `.dark` (los tokens de estado
  // son iguales en ambos temas; solo cambia la opacidad del tinte).
  if (level === "error") return "bg-state-bad/[0.06] dark:bg-state-bad/[0.16] border-state-bad/30";
  if (level === "warn") return "bg-state-warn/[0.06] dark:bg-state-warn/[0.16] border-state-warn/30";
  return "bg-paper2 border-line";
}

export function ConversationTracePanel({
  conversationId,
  onClose,
}: {
  conversationId: string;
  onClose: () => void;
}) {
  const [steps, setSteps] = useState<TraceStep[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [levelFilter, setLevelFilter] = useState<TraceLevel | "all">("all");
  const [kindFilter, setKindFilter] = useState<TraceStepKind | "all">("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getConversationTrace(conversationId)
      .then((data) => {
        if (!cancelled) setSteps(data);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e?.message || e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  // Totales sobre el conjunto completo (no filtrado) para que el header sea
  // estable aunque cambies los filtros.
  const totals = useMemo(() => {
    let tokens = 0;
    let cost = 0;
    let latency = 0;
    let llmCalls = 0;
    let errors = 0;
    for (const s of steps) {
      tokens += (s.tokens_in || 0) + (s.tokens_out || 0);
      cost += s.cost_usd || 0;
      if (s.kind === "llm_call") {
        llmCalls += 1;
        latency += s.latency_ms || 0;
      }
      if (s.level === "error") errors += 1;
    }
    const avgLatency = llmCalls > 0 ? Math.round(latency / llmCalls) : 0;
    return { tokens, cost, avgLatency, llmCalls, errors };
  }, [steps]);

  const visible = useMemo(() => {
    return steps.filter((s) => {
      if (levelFilter !== "all" && s.level !== levelFilter) return false;
      if (kindFilter !== "all" && s.kind !== kindFilter) return false;
      return true;
    });
  }, [steps, levelFilter, kindFilter]);

  function toggleExpand(i: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        className="absolute inset-0 bg-ink/40 backdrop-blur-[1px]"
        onClick={onClose}
      />
      <aside className="relative bg-card border-l border-line w-full max-w-lg h-full flex flex-col shadow-coro-3 animate-[slideleft_.18s_ease-out]">
        <style>{`
          @keyframes slideleft {
            from { transform: translateX(100%); }
            to { transform: translateX(0); }
          }
        `}</style>
        <header className="px-4 py-3 border-b border-line bg-paper2 flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-brand-ink" />
          <div className="flex-1 min-w-0">
            <div className="eyebrow">Actividad del agente</div>
            <h2 className="font-display text-lg text-ink leading-tight">
              Ejecución del agente
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center justify-center w-8 h-8 rounded-coro-sm hover:bg-paper3 text-ink2"
            aria-label="Cerrar"
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="px-4 py-3 border-b border-line2 bg-paper grid grid-cols-2 sm:grid-cols-4 gap-2 text-center">
          <div>
            <div className="text-[10px] uppercase text-ink3 font-medium tracking-wider">
              Pasos
            </div>
            <div className="font-numbers font-medium text-lg mt-0.5">
              {steps.length}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-ink3 font-medium tracking-wider">
              Tokens
            </div>
            <div className="font-numbers font-medium text-lg mt-0.5">
              {totals.tokens.toLocaleString("es-ES")}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-ink3 font-medium tracking-wider">
              Coste
            </div>
            <div className="font-numbers font-medium text-lg mt-0.5">
              {fmtUsd(totals.cost)}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-ink3 font-medium tracking-wider">
              Latencia
            </div>
            <div
              className="font-numbers font-medium text-lg mt-0.5"
              title={`Promedio de ${totals.llmCalls} llamada(s) LLM`}
            >
              {totals.avgLatency > 0
                ? `${(totals.avgLatency / 1000).toFixed(1)}s`
                : "—"}
            </div>
          </div>
        </div>

        {totals.errors > 0 && (
          <div className="px-4 py-2 bg-state-bad/[0.08] dark:bg-state-bad/[0.18] border-b border-state-bad/30 text-xs text-state-bad flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" />
            {totals.errors} evento(s) con error en esta conversación
          </div>
        )}

        {/* Filtros */}
        <div className="px-3 py-2 border-b border-line2 bg-card flex flex-wrap gap-1.5">
          {LEVEL_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              onClick={() => setLevelFilter(f.value)}
              className={`text-[11px] px-2 py-0.5 rounded-coro-sm border ${
                levelFilter === f.value
                  ? "bg-ink text-paper border-ink"
                  : "bg-paper2 text-ink2 border-line hover:bg-paper3"
              }`}
            >
              {f.label}
            </button>
          ))}
          <span className="border-l border-line mx-1" />
          {KIND_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              onClick={() => setKindFilter(f.value)}
              className={`text-[11px] px-2 py-0.5 rounded-coro-sm border ${
                kindFilter === f.value
                  ? "bg-brand-ink text-paper border-brand-ink"
                  : "bg-paper2 text-ink2 border-line hover:bg-paper3"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-auto p-3">
          {loading ? (
            <div className="flex items-center justify-center py-10 text-ink3 gap-2">
              <Loader2 className="w-4 h-4 animate-spin" /> Cargando trace…
            </div>
          ) : error ? (
            <div className="text-center text-state-bad py-10">
              No se pudo cargar el trace: {error}
            </div>
          ) : visible.length === 0 ? (
            <div className="text-center text-ink3 italic font-display py-10">
              {steps.length === 0
                ? "Sin actividad registrada todavía."
                : "Ningún evento coincide con los filtros."}
            </div>
          ) : (
            <ol className="relative space-y-2">
              {visible.map((s, i) => {
                const meta = KIND_META[s.kind] ?? KIND_META.audit;
                const Icon = meta.Icon;
                const isExpanded = expanded.has(i);
                const hasPayload =
                  s.payload && Object.keys(s.payload).length > 0;
                return (
                  <li
                    key={i}
                    className={`relative pl-9 pr-2 py-2 rounded-coro-sm border ${levelBg(s.level)}`}
                  >
                    <span
                      className="absolute left-3 top-2.5 w-4 h-4 rounded-full flex items-center justify-center"
                      style={{ background: meta.dotColor }}
                    >
                      <Icon className="w-2.5 h-2.5 text-paper" />
                    </span>
                    <div className="flex items-baseline gap-2 flex-wrap">
                      <span className="text-[10px] font-mono text-ink4">
                        {fmtTime(s.ts)}
                      </span>
                      <span className={`text-[10px] uppercase font-medium tracking-wider ${meta.color}`}>
                        {meta.label}
                      </span>
                      <span className="font-medium text-sm text-ink2 flex-1 min-w-0 break-words">
                        {s.label}
                      </span>
                      {hasPayload && (
                        <button
                          type="button"
                          onClick={() => toggleExpand(i)}
                          className="text-ink3 hover:text-ink2 ml-auto"
                          aria-label={isExpanded ? "Contraer" : "Expandir"}
                        >
                          {isExpanded ? (
                            <ChevronDown className="w-3.5 h-3.5" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5" />
                          )}
                        </button>
                      )}
                    </div>
                    {s.detail && (
                      <div className="text-xs text-ink3 mt-1 line-clamp-3">
                        {s.detail}
                      </div>
                    )}
                    {s.kind === "llm_call" && (
                      <div className="flex items-center gap-3 mt-1.5 text-[11px] font-mono text-ink3 flex-wrap">
                        {s.model && (
                          <span className="text-ink2 break-all">{s.model}</span>
                        )}
                        <span className="inline-flex items-center gap-0.5" title="Tokens prompt">
                          <ArrowDown className="w-3 h-3" /> {s.tokens_in ?? 0}
                        </span>
                        <span className="inline-flex items-center gap-0.5" title="Tokens completion">
                          <ArrowUp className="w-3 h-3" /> {s.tokens_out ?? 0}
                        </span>
                        {s.cost_usd != null && s.cost_usd > 0 && (
                          <span className="text-brand-ink">
                            {fmtUsd(s.cost_usd)}
                          </span>
                        )}
                        {s.latency_ms != null && (
                          <span title="Latencia LLM">
                            {fmtLatency(s.latency_ms)}
                          </span>
                        )}
                      </div>
                    )}
                    {(s.kind === "tool" || s.kind === "kb") && s.latency_ms != null && (
                      <div className="text-[11px] font-mono text-ink3 mt-1">
                        {fmtLatency(s.latency_ms)}
                      </div>
                    )}
                    {isExpanded && hasPayload && (
                      <pre className="mt-2 text-[10.5px] leading-relaxed font-mono text-ink2 bg-card border border-line2 rounded-coro-sm p-2 overflow-auto max-h-60 whitespace-pre-wrap break-words">
                        {JSON.stringify(s.payload, null, 2)}
                      </pre>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>

        <footer className="px-4 py-2 border-t border-line bg-paper2 flex items-center gap-2 text-[10px] text-ink3 font-mono">
          <ScrollText className="w-3 h-3" />
          Mensajes · LLM · Tools · KB · Router · Errores · Auditoría
        </footer>
      </aside>
    </div>
  );
}
