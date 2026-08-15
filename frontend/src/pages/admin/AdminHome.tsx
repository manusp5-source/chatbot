import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  RefreshCw,
  ArrowUpRight,
  ArrowDownRight,
  AlertTriangle,
  Workflow,
  Terminal,
  Bot,
  Sparkles,
  MessageCircle,
  ChevronRight,
  Inbox as InboxIcon,
  Rocket,
  Check,
} from "lucide-react";
import { useAuth } from "@/store/auth";
import { usePrivacy } from "@/store/privacy";
import { maskName, maskPhone } from "@/lib/mask";
import {
  dashboardSummary,
  dashboardAttention,
  getAgentPause,
  getBudgetStatus,
  adminHealth,
  onboardingStatus,
  type DashboardSummary,
  type AttentionResponse,
  type AttentionItem,
  type AgentPauseState,
  type BudgetStatus,
  type HourPoint,
  type ChannelCount,
  type OnboardingStatus,
} from "@/services/admin";

// ----------------------------- helpers -----------------------------

const CHANNEL_META: Record<string, { label: string; color: string }> = {
  whatsapp: { label: "WhatsApp", color: "var(--ch-wa)" },
  web: { label: "Web", color: "var(--ch-web)" },
  webchat: { label: "Web", color: "var(--ch-web)" },
  instagram_dm: { label: "Instagram", color: "#E1306C" },
  retell_voice: { label: "Voz", color: "var(--ch-voice)" },
};

function chanMeta(c: string) {
  return CHANNEL_META[c] ?? { label: c, color: "var(--ink-4)" };
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 6) return "Buenas noches";
  if (h < 13) return "Buenos días";
  if (h < 20) return "Buenas tardes";
  return "Buenas noches";
}

function agoLabel(mins: number): string {
  if (mins < 1) return "ahora";
  if (mins < 60) return `hace ${mins} min`;
  const h = Math.floor(mins / 60);
  if (h < 24) return `hace ${h} h`;
  return `hace ${Math.floor(h / 24)} d`;
}

function fmtInt(n: number): string {
  return n.toLocaleString("es-ES");
}

// ----------------------------- page -----------------------------

export default function AdminHome() {
  const user = useAuth((s) => s.user);
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [attention, setAttention] = useState<AttentionResponse | null>(null);
  const [pause, setPause] = useState<AgentPauseState | null>(null);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [health, setHealth] = useState<Record<string, { ok: boolean; detail: string }> | null>(null);
  const [onboarding, setOnboarding] = useState<OnboardingStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  async function refresh() {
    setLoading(true);
    // allSettled: que el fallo de un endpoint no deje la Home en blanco.
    const [s, a, p, b, h, o] = await Promise.allSettled([
      dashboardSummary("30d"),
      dashboardAttention(10),
      getAgentPause(),
      getBudgetStatus(),
      adminHealth(),
      onboardingStatus(),
    ]);
    if (s.status === "fulfilled") setSummary(s.value);
    if (a.status === "fulfilled") setAttention(a.value);
    if (p.status === "fulfilled") setPause(p.value);
    if (b.status === "fulfilled") setBudget(b.value);
    if (h.status === "fulfilled") setHealth(h.value);
    if (o.status === "fulfilled") setOnboarding(o.value);
    setUpdatedAt(new Date());
    setLoading(false);
  }

  // Carga inicial + auto-refresco cada 30s ("vivo por polling", sin WebSocket).
  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 30000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const today = summary?.conversations_today ?? 0;
  const yesterday = summary?.conversations_yesterday ?? 0;
  const aiPct = Math.round((summary?.ai_resolved_rate ?? 0) * 100);
  const handoffPct = (summary?.handoff_rate ?? 0) * 100;
  const pending = attention?.handoffs_count ?? 0;
  const waiting = attention?.waiting_count ?? 0;

  const summaryLine = useMemo(() => {
    if (!summary) return "Cargando el estado del negocio…";
    if (today === 0) return "Aún no hay conversaciones hoy. Todo en orden.";
    const base = `Hoy ${fmtInt(today)} ${today === 1 ? "conversación" : "conversaciones"}, ${aiPct}% resueltas por la IA.`;
    if (pending > 0) return `${base} ${pending} ${pending === 1 ? "espera" : "esperan"} a una persona.`;
    if (waiting > 0) return `${base} ${waiting} sin respuesta del bot.`;
    return `${base} Nada pendiente ahora mismo.`;
  }, [summary, today, aiPct, pending, waiting]);

  const name = user?.nombre || user?.email?.split("@")[0] || "operador";

  return (
    <div className="h-full overflow-auto bg-paper">
      <div className="p-5 md:p-7 space-y-6 max-w-[1400px] mx-auto">
        {/* ---------------- Zona 1 · Hero ---------------- */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className="lg:col-span-2 flex flex-col justify-between gap-4">
            <div>
              <div className="flex items-center justify-between gap-3">
                <span className="eyebrow">Inicio</span>
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() => void refresh()}
                  disabled={loading}
                  title="Actualizar"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
                  <span className="hidden sm:inline">
                    {updatedAt
                      ? `Actualizado ${updatedAt.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}`
                      : "Actualizar"}
                  </span>
                </button>
              </div>
              <h1 className="h-display mt-2" style={{ fontSize: "clamp(36px, 4vw, 48px)" }}>
                {greeting()}, <span className="accent">{name}</span>.
              </h1>
              <p className="text-ink3 text-sm mt-2 max-w-xl leading-relaxed">{summaryLine}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Link to="/inbox" className="btn btn-primary btn-sm">
                <InboxIcon className="w-3.5 h-3.5" /> Ir al inbox
              </Link>
              <Link to="/admin/agent/dashboard" className="btn btn-ghost btn-sm">
                <Bot className="w-3.5 h-3.5" /> Gestionar agente
              </Link>
              <Link to="/admin/connections" className="btn btn-ghost btn-sm">
                <Workflow className="w-3.5 h-3.5" /> Conexiones
              </Link>
            </div>
          </div>

          {/* Estado actual (polling, no tiempo real) */}
          <EstadoActual pause={pause} pending={pending} today={today} />
        </div>

        {/* ---------------- Onboarding (primera ejecución) ----------------
            Checklist para el primer admin. Se calcula en el backend y se
            oculta solo cuando los 3 pasos están hechos. */}
        {onboarding && !onboarding.complete && <OnboardingCard onboarding={onboarding} />}

        {/* ---------------- Banda "necesita atención" ----------------
            Bajo el saludo y encima de las tarjetas. Misma fuente que la
            tarjeta de estado (attention.handoffs_count); si no hay nadie
            esperando a una persona, la banda no aparece. */}
        {pending > 0 && (
          <div className="flex flex-wrap items-center gap-3 rounded-coro-sm border border-state-warn/30 bg-state-warn/10 px-4 py-3">
            <span
              className="w-8 h-8 rounded-full flex items-center justify-center shrink-0"
              style={{ background: "color-mix(in srgb, var(--state-warn) 20%, transparent)", color: "var(--state-warn)" }}
            >
              <AlertTriangle className="w-4 h-4" />
            </span>
            <div className="flex-1 min-w-[200px] text-sm text-ink">
              <b>{fmtInt(pending)}</b>{" "}
              {pending === 1 ? "conversación esperando" : "conversaciones esperando"} a una persona
            </div>
            <Link to="/inbox?status=__action" className="btn btn-primary btn-sm shrink-0">
              <InboxIcon className="w-3.5 h-3.5" /> Ir al inbox
            </Link>
          </div>
        )}

        {/* ---------------- Zona 2 · KPI band ---------------- */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
          <KpiCard
            label="Conversaciones"
            sub="hoy vs ayer"
            value={fmtInt(today)}
            trend={today - yesterday}
            spark={summary?.hourly}
            accent
          />
          <KpiCard label="Resueltas por IA" sub="en el rango" value={`${aiPct}`} valueSmall="%" />
          <KpiCard label="Tasa de derivación" sub="en el rango" value={handoffPct.toFixed(1)} valueSmall="%" />
          <KpiCard label="Contactos nuevos" sub="en el rango" value={fmtInt(summary?.new_contacts ?? 0)} />
          <KpiCard
            label="Coste IA"
            sub={budget?.month ?? "este mes"}
            value={`$${(budget?.cost_usd ?? 0).toFixed(2)}`}
            footer={
              budget?.percent != null && budget?.budget_usd
                ? `${budget.percent}% de $${budget.budget_usd.toFixed(0)}`
                : undefined
            }
          />
        </div>

        {/* ---------------- Zona 3 · Actividad ---------------- */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <Panel
            className="lg:col-span-2"
            title="Conversaciones por hora"
            sub={`hoy vs media de los últimos 7 días · ${summary?.timezone ?? ""}`}
          >
            <HourlyAreaChart hourly={summary?.hourly ?? []} />
          </Panel>
          <Panel title="Canal de origen" sub={`reparto del rango (${summary?.range ?? ""})`}>
            <ChannelDonut data={summary?.by_channel ?? []} />
          </Panel>
        </div>

        {/* ---------------- Zona 4 + 5 · Atención + Salud ---------------- */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <Panel
            className="lg:col-span-2"
            title="Necesitan atención"
            sub="derivadas a humano y clientes sin respuesta del bot"
            footer={
              <Link to="/inbox" className="inline-flex items-center gap-1 hover:text-ink">
                Abrir inbox <ChevronRight className="w-3.5 h-3.5" />
              </Link>
            }
          >
            <AttentionPanel attention={attention} />
          </Panel>
          <Panel
            title="Salud del sistema"
            sub="canales y servicios externos"
            footer={
              <Link to="/admin/health" className="inline-flex items-center gap-1 hover:text-ink">
                Abrir salud <ChevronRight className="w-3.5 h-3.5" />
              </Link>
            }
          >
            <HealthPanel health={health} />
          </Panel>
        </div>
      </div>
    </div>
  );
}

// ----------------------------- Estado actual -----------------------------

function EstadoActual({
  pause,
  pending,
  today,
}: {
  pause: AgentPauseState | null;
  pending: number;
  today: number;
}) {
  const globalPaused = pause?.paused ?? false;
  return (
    <div className="card p-5 flex flex-col">
      <div className="flex items-center gap-2 mb-3">
        <span
          className="w-2 h-2 rounded-full"
          style={{ background: globalPaused ? "var(--state-bad)" : "var(--state-ok)" }}
        />
        {/* Título al mismo nivel de jerarquía que el resto de tarjetas del
            Inicio (Panel): serif display, no eyebrow de 11px. */}
        <h3 className="font-display text-ink leading-tight" style={{ fontSize: 22 }}>
          Estado actual
        </h3>
        {globalPaused && (
          <span className="pill ml-auto" style={{ color: "var(--state-bad)" }}>
            Bot en pausa global
          </span>
        )}
      </div>

      <div className="space-y-2.5">
        {(pause?.channels ?? []).map((c) => {
          const meta = chanMeta(c.canal);
          return (
            <div key={c.canal} className="flex items-center justify-between text-sm">
              <span className="flex items-center gap-2 text-ink2">
                <span className="w-2 h-2 rounded-full" style={{ background: meta.color }} />
                {meta.label}
              </span>
              <span
                className="text-xs font-semibold"
                style={{ color: c.paused ? "var(--state-bad)" : "var(--state-ok)" }}
              >
                {c.paused ? "Pausado" : "Activo"}
              </span>
            </div>
          );
        })}
        {(!pause || pause.channels.length === 0) && (
          <div className="text-sm text-ink4">Sin canales configurados.</div>
        )}
      </div>

      <div className="border-t border-line mt-4 pt-3 grid grid-cols-2 gap-3">
        <div>
          <div className="text-[22px] font-display leading-none text-ink">{fmtInt(today)}</div>
          <div className="text-[11px] text-ink3 mt-1">conversaciones hoy</div>
        </div>
        <div>
          <div
            className="text-[22px] font-display leading-none"
            style={{ color: pending > 0 ? "var(--state-warn)" : "var(--ink)" }}
          >
            {fmtInt(pending)}
          </div>
          <div className="text-[11px] text-ink3 mt-1">esperando a un humano</div>
        </div>
      </div>
    </div>
  );
}

// ----------------------------- Onboarding -----------------------------

function OnboardingCard({ onboarding }: { onboarding: OnboardingStatus }) {
  return (
    <div className="card p-5 border-2" style={{ borderColor: "var(--brand-700)" }}>
      <div className="flex items-start gap-3">
        <span
          className="w-9 h-9 rounded-full flex items-center justify-center shrink-0"
          style={{ background: "color-mix(in srgb, var(--brand) 22%, transparent)", color: "var(--brand-700)" }}
        >
          <Rocket className="w-4.5 h-4.5" />
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-display text-ink leading-tight" style={{ fontSize: 22 }}>
              Pon el chatbot en marcha
            </h3>
            <span className="pill" style={{ color: "var(--brand-700)" }}>
              {onboarding.done_count} de {onboarding.total}
            </span>
          </div>
          <p className="text-[12px] text-ink3 mt-1">
            Los pasos para dejarlo funcionando. Esta tarjeta desaparece al completarlos.
          </p>
        </div>
      </div>

      <div className="mt-4 space-y-2.5">
        {onboarding.steps.map((step, i) => (
          <div
            key={step.key}
            className="flex items-center gap-3 rounded-coro-sm border border-line p-3"
            style={step.done ? { opacity: 0.6 } : undefined}
          >
            <span
              className="w-6 h-6 rounded-full flex items-center justify-center shrink-0 text-[12px] font-semibold"
              style={
                step.done
                  ? { background: "var(--state-ok)", color: "white" }
                  : { background: "var(--paper3)", color: "var(--ink3)" }
              }
            >
              {step.done ? <Check className="w-3.5 h-3.5" /> : i + 1}
            </span>
            <div className="min-w-0 flex-1">
              <div
                className="text-sm font-medium text-ink"
                style={step.done ? { textDecoration: "line-through" } : undefined}
              >
                {step.title}
              </div>
              <div className="text-[12px] text-ink3 truncate">{step.description}</div>
            </div>
            {!step.done && (
              <Link to={step.action_path} className="btn btn-ghost btn-sm shrink-0">
                {step.action_label}
                <ChevronRight className="w-3.5 h-3.5" />
              </Link>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ----------------------------- KPI card -----------------------------

function KpiCard({
  label,
  sub,
  value,
  valueSmall,
  trend,
  footer,
  spark,
  accent,
}: {
  label: string;
  sub?: string;
  value: string;
  valueSmall?: string;
  trend?: number;
  footer?: string;
  spark?: HourPoint[];
  accent?: boolean;
}) {
  return (
    <div className={`card p-4 ${accent ? "border-2" : ""}`}>
      <div className="flex items-center justify-between">
        <span className="eyebrow">{label}</span>
        {sub && <span className="text-[10.5px] text-ink4 numbers">{sub}</span>}
      </div>
      <div className="flex items-end gap-2 mt-1.5">
        <div className="font-display text-ink leading-none" style={{ fontSize: 30 }}>
          {value}
          {valueSmall && <small className="text-ink3" style={{ fontSize: 16 }}>{valueSmall}</small>}
        </div>
        {typeof trend === "number" && trend !== 0 && (
          <span
            className="inline-flex items-center gap-0.5 text-[11px] font-semibold mb-1"
            style={{ color: trend > 0 ? "var(--state-ok)" : "var(--state-bad)" }}
          >
            {trend > 0 ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
            {Math.abs(trend)}
          </span>
        )}
      </div>
      {spark && spark.length > 0 ? (
        <Sparkline points={spark.map((p) => p.today)} />
      ) : (
        footer && <div className="text-[11px] text-ink3 mt-2 numbers">{footer}</div>
      )}
    </div>
  );
}

function Sparkline({ points }: { points: number[] }) {
  const W = 200;
  const H = 26;
  const max = Math.max(1, ...points);
  const step = points.length > 1 ? W / (points.length - 1) : W;
  const line = points
    .map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)} ${(H - (v / max) * (H - 2) - 1).toFixed(1)}`)
    .join(" ");
  const area = `${line} L ${W} ${H} L 0 ${H} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full mt-2" style={{ height: 26 }}>
      <path d={area} fill="var(--brand)" fillOpacity={0.3} />
      <path d={line} fill="none" stroke="var(--brand-700)" strokeWidth={1.6} />
    </svg>
  );
}

// ----------------------------- Panel wrapper -----------------------------

function Panel({
  title,
  sub,
  footer,
  className,
  children,
}: {
  title: string;
  sub?: string;
  footer?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`card p-5 flex flex-col ${className ?? ""}`}>
      <div className="mb-3.5">
        <h3 className="font-display text-ink leading-tight" style={{ fontSize: 22 }}>
          {title}
        </h3>
        {sub && <div className="text-[11.5px] text-ink3 mt-1">{sub}</div>}
      </div>
      <div className="flex-1">{children}</div>
      {footer && <div className="border-t border-line mt-4 pt-3 text-[12px] text-ink3">{footer}</div>}
    </div>
  );
}

// ----------------------------- Area chart -----------------------------

function HourlyAreaChart({ hourly }: { hourly: HourPoint[] }) {
  const W = 720;
  const H = 200;
  const padX = 12;
  const padTop = 14;
  const padBot = 26;
  const baseY = H - padBot;
  const innerW = W - 2 * padX;

  const hasData = hourly.some((p) => p.today > 0 || p.avg_7d > 0);
  const max = Math.max(1, ...hourly.flatMap((p) => [p.today, p.avg_7d]));
  const x = (h: number) => padX + (h / 23) * innerW;
  const y = (v: number) => baseY - (v / max) * (baseY - padTop);
  const toLine = (key: "today" | "avg_7d") =>
    hourly.map((p, i) => `${i ? "L" : "M"}${x(p.hour).toFixed(1)} ${y(p[key]).toFixed(1)}`).join(" ");

  if (!hasData) {
    return (
      <div className="flex items-center justify-center text-sm text-ink4" style={{ height: 200 }}>
        Sin actividad registrada todavía.
      </div>
    );
  }

  const todayLine = toLine("today");
  const todayArea = `${todayLine} L ${x(23).toFixed(1)} ${baseY} L ${x(0).toFixed(1)} ${baseY} Z`;
  const nowHour = new Date().getHours();

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: "clamp(160px, 24vh, 220px)" }}>
        {/* grid base */}
        <line x1={padX} y1={baseY} x2={W - padX} y2={baseY} stroke="var(--line)" strokeWidth={1} />
        {/* media 7d (dashed) */}
        <path d={toLine("avg_7d")} fill="none" stroke="var(--ink-4)" strokeWidth={1.5} strokeDasharray="4 4" />
        {/* hoy */}
        <path d={todayArea} fill="var(--brand)" fillOpacity={0.28} />
        <path d={todayLine} fill="none" stroke="var(--brand-700)" strokeWidth={2} />
        {/* marcador hora actual */}
        <line
          x1={x(nowHour)}
          y1={padTop}
          x2={x(nowHour)}
          y2={baseY}
          stroke="var(--ink-4)"
          strokeWidth={1}
          strokeDasharray="2 3"
          opacity={0.6}
        />
        {/* etiquetas de hora */}
        {[0, 6, 12, 18, 23].map((h) => (
          <text key={h} x={x(h)} y={H - 8} fontSize={10} fill="var(--ink-4)" textAnchor="middle">
            {String(h).padStart(2, "0")}h
          </text>
        ))}
      </svg>
      <div className="flex items-center gap-4 mt-1 text-[11px] text-ink3">
        <span className="inline-flex items-center gap-1.5">
          <span className="w-3 h-[3px] rounded-full" style={{ background: "var(--brand-700)" }} /> Hoy
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="w-3 border-t border-dashed" style={{ borderColor: "var(--ink-4)" }} /> Media 7d
        </span>
      </div>
    </div>
  );
}

// ----------------------------- Donut -----------------------------

function ChannelDonut({ data }: { data: ChannelCount[] }) {
  const total = data.reduce((s, c) => s + c.count, 0);
  const r = 54;
  const C = 2 * Math.PI * r;

  if (total === 0) {
    return (
      <div className="flex items-center justify-center text-sm text-ink4" style={{ height: 160 }}>
        Sin conversaciones en el rango.
      </div>
    );
  }

  let acc = 0;
  const segments = data.map((c) => {
    const frac = c.count / total;
    const seg = {
      canal: c.canal,
      count: c.count,
      frac,
      color: chanMeta(c.canal).color,
      dash: frac * C,
      offset: -acc * C,
    };
    acc += frac;
    return seg;
  });

  return (
    <div className="flex items-center gap-5">
      <div className="relative shrink-0" style={{ width: 132, height: 132 }}>
        <svg viewBox="0 0 132 132" style={{ width: 132, height: 132, transform: "rotate(-90deg)" }}>
          <circle cx={66} cy={66} r={r} fill="none" stroke="var(--line)" strokeWidth={14} />
          {segments.map((s) => (
            <circle
              key={s.canal}
              cx={66}
              cy={66}
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth={14}
              strokeDasharray={`${s.dash.toFixed(2)} ${(C - s.dash).toFixed(2)}`}
              strokeDashoffset={s.offset.toFixed(2)}
              strokeLinecap="butt"
            />
          ))}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <div className="font-display text-ink leading-none" style={{ fontSize: 24 }}>
            {fmtInt(total)}
          </div>
          <div className="text-[10px] text-ink3">total</div>
        </div>
      </div>
      <div className="flex-1 flex flex-col gap-2">
        {segments.map((s) => (
          <div key={s.canal} className="flex items-center gap-2 text-sm">
            <span className="w-2.5 h-2.5 rounded-sm" style={{ background: s.color }} />
            <span className="text-ink2 flex-1">{chanMeta(s.canal).label}</span>
            <b className="text-ink numbers">{fmtInt(s.count)}</b>
            <span className="text-ink4 numbers w-10 text-right">{Math.round(s.frac * 100)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ----------------------------- Atención -----------------------------

function AttentionPanel({ attention }: { attention: AttentionResponse | null }) {
  if (!attention) {
    return <div className="text-sm text-ink4 py-6 text-center">Cargando…</div>;
  }
  const { handoffs, waiting } = attention;
  if (handoffs.length === 0 && waiting.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-8 gap-2">
        <Sparkles className="w-6 h-6 text-brand-700" />
        <div className="text-sm text-ink2 font-medium">Nada pendiente ahora mismo</div>
        <div className="text-[12px] text-ink4">El bot está atendiendo todo solo.</div>
      </div>
    );
  }
  return (
    <div className="space-y-2.5">
      {handoffs.map((it) => (
        <AttentionRow key={it.conversation_id} item={it} kind="handoff" />
      ))}
      {waiting.map((it) => (
        <AttentionRow key={it.conversation_id} item={it} kind="waiting" />
      ))}
    </div>
  );
}

function AttentionRow({ item, kind }: { item: AttentionItem; kind: "handoff" | "waiting" }) {
  const priv = usePrivacy((s) => s.enabled);
  const meta = chanMeta(item.canal);
  const color = kind === "handoff" ? "var(--state-bad)" : "var(--state-warn)";
  const tag = kind === "handoff" ? "Derivada" : "Sin respuesta";
  return (
    <Link
      to="/inbox"
      className="flex items-center gap-3 rounded-coro-sm border border-line p-2.5 hover:bg-paper2 transition-colors"
    >
      <span
        className="w-8 h-8 rounded-full flex items-center justify-center shrink-0"
        style={{ background: `${color}22`, color }}
      >
        <AlertTriangle className="w-4 h-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-ink truncate">
            {maskName(item.contact_name, priv) || maskPhone(item.contact_phone, priv) || "Contacto"}
          </span>
          <span className="inline-flex items-center gap-1 text-[10px] text-ink3">
            <span className="w-1.5 h-1.5 rounded-full" style={{ background: meta.color }} />
            {meta.label}
          </span>
        </div>
        <div className="text-[12px] text-ink3 truncate">{item.preview || "—"}</div>
      </div>
      <div className="text-right shrink-0">
        <div className="text-[11px] font-semibold" style={{ color }}>
          {tag}
        </div>
        <div className="text-[10.5px] text-ink4 numbers">{agoLabel(item.waiting_minutes)}</div>
      </div>
    </Link>
  );
}

// ----------------------------- Salud -----------------------------

function HealthPanel({ health }: { health: Record<string, { ok: boolean; detail: string }> | null }) {
  if (!health) {
    return <div className="text-sm text-ink4 py-6 text-center">Cargando…</div>;
  }
  const entries = Object.entries(health);
  if (entries.length === 0) {
    return <div className="text-sm text-ink4 py-6 text-center">Sin datos de salud.</div>;
  }
  return (
    <div className="space-y-2">
      {entries.map(([name, st]) => (
        <div key={name} className="flex items-center gap-2 text-sm min-w-0">
          <span
            className="w-2 h-2 rounded-full shrink-0"
            style={{ background: st.ok ? "var(--state-ok)" : "var(--state-bad)" }}
          />
          <span className="text-ink2 capitalize truncate shrink-0">{name}</span>
          <span className="text-[11px] text-ink4 truncate ml-auto" title={st.detail}>
            {st.detail}
          </span>
        </div>
      ))}
      <div className="flex flex-wrap gap-2 pt-2">
        <Link to="/admin/agent/flow" className="pill hover:bg-paper3">
          <Workflow className="w-3 h-3" /> Flujo en vivo
        </Link>
        <Link to="/admin/system/logs" className="pill hover:bg-paper3">
          <Terminal className="w-3 h-3" /> Logs
        </Link>
        <Link to="/admin/internal-agent" className="pill hover:bg-paper3">
          <MessageCircle className="w-3 h-3" /> Agente interno
        </Link>
      </div>
    </div>
  );
}
