import { useEffect, useMemo, useRef, useState } from "react";
import {
  Phone,
  MessageSquare,
  Mic,
  Instagram,
  Mail,
  Bot,
  BookOpen,
  UserCheck,
  Save,
  Search,
  Sparkles,
  Activity,
  Filter,
  Send,
  FileText,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import {
  listChannels,
  listAgents,
  type AgentOut,
  type ChannelOut,
  type ChannelType,
} from "@/services/admin";
import { useInboxSocket } from "@/hooks/useInboxSocket";
import { errorDetail } from "@/lib/errors";

/**
 * F7 — Vista visual de la arquitectura del agente, estilo n8n.
 *
 * Layout en 4 columnas: Canales → Clasificador → Agentes → Salidas
 * (responder / generar borrador / tools). SVG inline con bezier curvas entre
 * nodos relacionados. Cada nodo se enciende (glow) cuando actúa, guiado por
 * los eventos `agent.step` que emite el backend por el WebSocket del inbox.
 *
 * Inspiración n8n: nodos con halo pulsante al ejecutarse, líneas con
 * dirección clara, alta densidad visual. Aquí lo mantenemos sobrio
 * (paleta Coro) para que sirva tanto de operación como de demo.
 */

const CHANNEL_ICON: Record<ChannelType, typeof Phone> = {
  whatsapp: Phone,
  webchat: MessageSquare,
  retell_voice: Mic,
  instagram_dm: Instagram,
  email: Mail,
};

const CHANNEL_COLOR: Record<ChannelType, string> = {
  whatsapp: "#25D366",
  webchat: "#6C7BFF",
  retell_voice: "#9B8AFB",
  instagram_dm: "#E4405F",
  email: "#EA4335",
};

const TOOL_META: Record<
  string,
  { label: string; Icon: typeof Bot; color: string }
> = {
  consultar_kb: { label: "Buscar KB", Icon: BookOpen, color: "#6C7BFF" },
  derivar_humano: { label: "Derivar a humano", Icon: UserCheck, color: "#E58A2F" },
  guardar_contacto: { label: "Guardar contacto", Icon: Save, color: "#16A085" },
  buscar_contacto: { label: "Buscar contacto", Icon: Search, color: "#9B8AFB" },
};

/** Pasos del pipeline que el backend emite como eventos `agent.step` por el
 * WS del inbox. `responder` y `borrador` son salidas del agente (columna de
 * la derecha); `clasificador` es su propia columna entre canales y agentes. */
const STEP_META: Record<string, { label: string; sub: string; Icon: typeof Bot; color: string }> = {
  responder: { label: "Responder", sub: "Respuesta directa al cliente", Icon: Send, color: "#2DA771" },
  borrador: { label: "Generar borrador", sub: "Pendiente de revisión", Icon: FileText, color: "#9B8AFB" },
};

interface NodeBox {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  sub?: string;
  color: string;
  Icon: typeof Bot;
}

interface Edge {
  from: string;
  to: string;
}

const NODE_W = 180;
const NODE_H = 64;
const COL_GAP = 100;
const ROW_GAP = 24;
const PADDING = 40;

export default function FlowDashboard() {
  const [channels, setChannels] = useState<ChannelOut[]>([]);
  const [agents, setAgents] = useState<AgentOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [glowing, setGlowing] = useState<Set<string>>(new Set());
  const [counters, setCounters] = useState({ in: 0, out: 0, errors: 0 });
  const glowTimers = useRef<Map<string, number>>(new Map());
  // Sin esto, un fallo de la API dejaba el diagrama vacío sin ningún aviso.
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    try {
      const [ch, ag] = await Promise.all([listChannels(), listAgents()]);
      setChannels(ch);
      setAgents(ag);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar el flujo del agente."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function pulse(nodeId: string) {
    setGlowing((prev) => new Set(prev).add(nodeId));
    const prev = glowTimers.current.get(nodeId);
    if (prev) window.clearTimeout(prev);
    const t = window.setTimeout(() => {
      setGlowing((s) => {
        const next = new Set(s);
        next.delete(nodeId);
        return next;
      });
      glowTimers.current.delete(nodeId);
    }, 1800);
    glowTimers.current.set(nodeId, t);
  }

  // WS al inbox — emite eventos de actividad del agente que mapeamos a nodos.
  useInboxSocket((ev) => {
    const p = ev.payload as Record<string, unknown>;
    if (ev.type === "message.new") {
      // Encender el canal según el rol/origin si podemos inferirlo. El
      // payload del inbox tiene `from_phone` para WA; para web e IG no
      // siempre — encendemos el primero conocido como aproximación.
      const fromPhone = String(p.from_phone || "");
      let channelType: ChannelType | null = null;
      if (fromPhone.startsWith("ig:")) channelType = "instagram_dm";
      else if (fromPhone.startsWith("web:")) channelType = "webchat";
      else if (fromPhone.startsWith("email:")) channelType = "email";
      else if (fromPhone) channelType = "whatsapp";
      if (channelType) pulse("channel:" + channelType);
      setCounters((c) => ({ ...c, in: c.in + 1 }));
    } else if (ev.type === "agent.step") {
      // Pasos del pipeline emitidos por el backend (flow_events): iluminan el
      // nodo que actúa en cada momento + el agente concreto si viene su id.
      const step = String(p.step || "");
      const agentId = p.agent_id ? String(p.agent_id) : null;
      if (agentId) pulse("agent:" + agentId);
      if (step === "clasificador") {
        pulse("step:clasificador");
      } else if (step === "responder") {
        pulse("step:responder");
        setCounters((c) => ({ ...c, out: c.out + 1 }));
      } else if (step === "borrador") {
        pulse("step:borrador");
      } else if (step === "derivar") {
        pulse("tool:derivar_humano");
      } else if (step === "tool" && p.tool) {
        pulse("tool:" + String(p.tool));
      }
    } else if (ev.type === "conversation.updated") {
      // ignorar — ya viene cubierto por message.new / agent.step
    }
  });

  // Layout deterministic — 4 columnas: Canales → Clasificador → Agentes →
  // Salidas (responder / borrador / tools). Alturas según cantidad por columna.
  const { nodes, edges, width, height } = useMemo(() => {
    const channelNodes: NodeBox[] = [];
    const classifierNodes: NodeBox[] = [];
    const agentNodes: NodeBox[] = [];
    const outputNodes: NodeBox[] = [];
    const edges: Edge[] = [];

    // Columna 1: canales (incluye placeholders gris si no existe el canal)
    const allChannelTypes: ChannelType[] = [
      "whatsapp",
      "webchat",
      "instagram_dm",
      "retell_voice",
      "email",
    ];
    const channelsByType = new Map<ChannelType, ChannelOut>();
    channels.forEach((c) => channelsByType.set(c.type, c));

    allChannelTypes.forEach((type, i) => {
      const ch = channelsByType.get(type);
      const Icon = CHANNEL_ICON[type];
      channelNodes.push({
        id: "channel:" + type,
        x: PADDING,
        y: PADDING + i * (NODE_H + ROW_GAP),
        w: NODE_W,
        h: NODE_H,
        label: ch?.name || labelForType(type),
        sub: ch ? (ch.enabled ? "Activo" : "Pausado") : "No conectado",
        color: ch?.enabled ? CHANNEL_COLOR[type] : "#A39884",
        Icon,
      });
    });

    const activeAgents = agents.filter((a) => a.is_active);
    const toolKeys = Object.keys(TOOL_META);
    // Salidas del agente: primero los pasos del pipeline, luego las tools.
    const outputKeys = ["step:responder", "step:borrador", ...toolKeys.map((k) => "tool:" + k)];

    const channelsHeight = channelNodes.length * (NODE_H + ROW_GAP);
    const agentsHeight = (activeAgents.length || 1) * (NODE_H + ROW_GAP);
    const outputsHeight = outputKeys.length * (NODE_H + ROW_GAP);
    const maxColHeight = Math.max(channelsHeight, agentsHeight, outputsHeight, NODE_H + ROW_GAP);

    // Columna 2: clasificador (un solo nodo, centrado en vertical). Todo lo
    // que entra pasa por él antes de llegar al agente (spam/cuarentena).
    const classifierX = PADDING + NODE_W + COL_GAP;
    classifierNodes.push({
      id: "step:clasificador",
      x: classifierX,
      y: PADDING + Math.max(0, (maxColHeight - (NODE_H + ROW_GAP)) / 2),
      w: NODE_W,
      h: NODE_H,
      label: "Clasificador",
      sub: "Filtra spam y cuarentena",
      color: "#9DA362",
      Icon: Filter,
    });

    // Columna 3: agentes
    const agentsX = classifierX + NODE_W + COL_GAP;
    const offsetAgents = Math.max(0, (maxColHeight - agentsHeight) / 2);
    activeAgents.forEach((a, i) => {
      agentNodes.push({
        id: "agent:" + a.id,
        x: agentsX,
        y: PADDING + offsetAgents + i * (NODE_H + ROW_GAP),
        w: NODE_W,
        h: NODE_H,
        label: a.name,
        sub: a.model_name,
        color: "#DBE09E",
        Icon: Bot,
      });
    });
    if (activeAgents.length === 0) {
      agentNodes.push({
        id: "agent:none",
        x: agentsX,
        y: PADDING + offsetAgents,
        w: NODE_W,
        h: NODE_H,
        label: "Sin agentes activos",
        sub: "Crea uno en /admin/agent/agents",
        color: "#A39884",
        Icon: Bot,
      });
    }

    // Columna 4: salidas — pasos del pipeline (responder, generar borrador) +
    // tools (catálogo fijo en v1). "Derivar a humano" es la tool derivar_humano.
    const outputsX = agentsX + NODE_W + COL_GAP;
    const offsetOutputs = Math.max(0, (maxColHeight - outputsHeight) / 2);
    outputKeys.forEach((id, i) => {
      const y = PADDING + offsetOutputs + i * (NODE_H + ROW_GAP);
      if (id.startsWith("step:")) {
        const meta = STEP_META[id.slice("step:".length)];
        outputNodes.push({
          id,
          x: outputsX,
          y,
          w: NODE_W,
          h: NODE_H,
          label: meta.label,
          sub: meta.sub,
          color: meta.color,
          Icon: meta.Icon,
        });
      } else {
        const k = id.slice("tool:".length);
        const meta = TOOL_META[k];
        outputNodes.push({
          id,
          x: outputsX,
          y,
          w: NODE_W,
          h: NODE_H,
          label: meta.label,
          sub: k,
          color: meta.color,
          Icon: meta.Icon,
        });
      }
    });

    // Edges: canal habilitado → clasificador → su agente; cada agente → sus
    // salidas (responder/borrador + tools habilitadas).
    channels.forEach((c) => {
      if (!c.enabled) return;
      edges.push({ from: "channel:" + c.type, to: "step:clasificador" });
      if (c.agent_id) {
        edges.push({ from: "step:clasificador", to: "agent:" + c.agent_id });
      }
    });
    activeAgents.forEach((a) => {
      edges.push({ from: "agent:" + a.id, to: "step:responder" });
      edges.push({ from: "agent:" + a.id, to: "step:borrador" });
      const tools =
        a.tools_enabled && a.tools_enabled.length > 0
          ? a.tools_enabled
          : toolKeys;
      tools.forEach((tk) => {
        if (TOOL_META[tk]) edges.push({ from: "agent:" + a.id, to: "tool:" + tk });
      });
    });

    const nodes = [...channelNodes, ...classifierNodes, ...agentNodes, ...outputNodes];
    const width = outputsX + NODE_W + PADDING;
    const height = PADDING * 2 + maxColHeight;
    return { nodes, edges, width, height };
  }, [channels, agents]);

  const nodeById = useMemo(() => {
    const m = new Map<string, NodeBox>();
    nodes.forEach((n) => m.set(n.id, n));
    return m;
  }, [nodes]);

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Flujo del agente"
        description="Visualización en tiempo real del pipeline: canales, clasificador, agentes y salidas. Cada nodo se ilumina cuando actúa."
      />
      <div className="px-4 py-2 border-b border-line2 bg-paper flex flex-wrap items-center gap-4 text-xs">
        <Stat label="Entrantes" value={counters.in} icon={Activity} />
        <Stat label="Respuestas" value={counters.out} icon={Sparkles} />
        <Stat label="Errores" value={counters.errors} icon={Activity} tone="bad" />
        <div className="ml-auto text-ink3 italic">
          {loading ? "Cargando flujo…" : `${channels.length} canal(es) · ${agents.length} agente(s)`}
        </div>
      </div>
      <div className="flex-1 overflow-auto bg-paper2/40 p-4">
        {error && (
          <div className="mb-3 text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{error}</span>
            <button type="button" onClick={() => void refresh()} className="font-semibold hover:underline shrink-0">
              Reintentar
            </button>
          </div>
        )}
        <div
          className="bg-card border border-line rounded-coro shadow-coro-1 overflow-auto"
          style={{ minHeight: height + 40 }}
        >
          <style>{`
            @keyframes flow-pulse {
              0%   { filter: drop-shadow(0 0 0 transparent); }
              50%  { filter: drop-shadow(0 0 14px var(--glow, #DBE09E)); }
              100% { filter: drop-shadow(0 0 0 transparent); }
            }
            .flow-glow { animation: flow-pulse 1.5s ease-out 2; }
            @keyframes flow-anim {
              from { stroke-dashoffset: 40; }
              to   { stroke-dashoffset: 0; }
            }
            .flow-edge { animation: flow-anim 1.2s linear infinite; }
          `}</style>
          <svg width={width} height={height} className="block">
            {/* Edges primero (debajo de los nodos) */}
            <g>
              {edges.map((e, i) => {
                const a = nodeById.get(e.from);
                const b = nodeById.get(e.to);
                if (!a || !b) return null;
                const x1 = a.x + a.w;
                const y1 = a.y + a.h / 2;
                const x2 = b.x;
                const y2 = b.y + b.h / 2;
                const mx = (x1 + x2) / 2;
                const path = `M ${x1} ${y1} C ${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`;
                const isActive = glowing.has(e.from) || glowing.has(e.to);
                return (
                  <path
                    key={i}
                    d={path}
                    fill="none"
                    stroke={isActive ? "var(--brand, #DBE09E)" : "rgb(var(--ink-4-rgb))"}
                    strokeWidth={isActive ? 2 : 1.4}
                    strokeDasharray={isActive ? "6 4" : ""}
                    className={isActive ? "flow-edge" : ""}
                  />
                );
              })}
            </g>
            {/* Nodos */}
            <g>
              {nodes.map((n) => {
                const isGlow = glowing.has(n.id);
                return (
                  <g
                    key={n.id}
                    transform={`translate(${n.x},${n.y})`}
                    className={isGlow ? "flow-glow" : ""}
                    style={isGlow ? ({ ["--glow" as never]: n.color } as never) : undefined}
                  >
                    {/* Tokens del tema, no colores fijos: en dark el lienzo
                        cambia y las tarjetas del diagrama deben acompañarlo. */}
                    <rect
                      width={n.w}
                      height={n.h}
                      rx={12}
                      ry={12}
                      fill="rgb(var(--card-rgb))"
                      stroke={isGlow ? n.color : "var(--line)"}
                      strokeWidth={isGlow ? 2 : 1}
                    />
                    <rect
                      x={0}
                      y={0}
                      width={6}
                      height={n.h}
                      rx={6}
                      ry={6}
                      fill={n.color}
                    />
                    <foreignObject x={14} y={6} width={n.w - 18} height={n.h - 12}>
                      <div
                        // @ts-expect-error xmlns es válido en foreignObject
                        xmlns="http://www.w3.org/1999/xhtml"
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          height: "100%",
                          fontFamily: "Geist, system-ui, sans-serif",
                        }}
                      >
                        <div
                          style={{
                            width: 28,
                            height: 28,
                            borderRadius: 8,
                            background: n.color + "22",
                            color: n.color,
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            flex: "none",
                          }}
                        >
                          <n.Icon size={14} />
                        </div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div
                            style={{
                              fontSize: 13,
                              fontWeight: 600,
                              color: "rgb(var(--ink-rgb))",
                              whiteSpace: "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {n.label}
                          </div>
                          {n.sub && (
                            <div
                              style={{
                                fontSize: 10,
                                color: "rgb(var(--ink-3-rgb))",
                                whiteSpace: "nowrap",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                marginTop: 2,
                              }}
                            >
                              {n.sub}
                            </div>
                          )}
                        </div>
                      </div>
                    </foreignObject>
                  </g>
                );
              })}
            </g>
          </svg>
        </div>
        <div className="text-center text-[11px] text-ink3 italic mt-2">
          Tip: graba un mensaje al bot y verás cómo se iluminan los nodos en cascada.
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  icon: Icon,
  tone,
}: {
  label: string;
  value: number;
  icon: typeof Activity;
  tone?: "bad";
}) {
  return (
    <div className="inline-flex items-center gap-1.5">
      <Icon
        className={`w-3.5 h-3.5 ${tone === "bad" ? "text-state-bad" : "text-ink3"}`}
      />
      <span className="text-ink3 uppercase tracking-wider text-[10px]">{label}</span>
      <span className="font-numbers font-medium text-ink2">{value}</span>
    </div>
  );
}

function labelForType(t: ChannelType): string {
  return (
    {
      whatsapp: "WhatsApp",
      webchat: "Widget Web",
      retell_voice: "Retell Voz",
      instagram_dm: "Instagram DM",
      email: "Email (Gmail)",
    }[t] || t
  );
}
