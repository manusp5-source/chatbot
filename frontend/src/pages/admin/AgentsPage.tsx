import { useProviderModels } from "@/hooks/useProviderModels";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Bot,
  Plus,
  Pencil,
  Trash2,
  Loader2,
  X,
  Save,
  Plug,
  CircleCheck,
  CircleSlash,
  History,
  RotateCcw,
  ShieldOff,
  Eye,
  ShieldCheck,
  Wand2,
  MessageCircle,
  DollarSign,
  AlertTriangle,
  Phone,
  Clock,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { errorDetail } from "@/lib/errors";
import { BusinessHoursPanel } from "./connections/BusinessHoursPanel";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  listAgents,
  createAgent,
  updateAgent,
  deleteAgent,
  getAgentPromptHistory,
  restoreAgentPrompt,
  getAgentEffectivePrompt,
  listLLMProviders,
  listRegisteredTools,
  type AgentIn,
  type AgentKind,
  type AgentOut,
  type AgentPromptHistoryOut,
  type EffectivePrompt,
  type LLMProviderOut,
  type ToolOut,
} from "@/services/admin";
import { ClassifierPanel } from "./ClassifierPage";
import { InternalAgentPanel } from "./InternalAgentSettingsPage";
import { ModelCostsPanel } from "./ConfigPage";

/**
 * F3A — Lista + CRUD de Agents.
 *
 * Cada Agent es una configuración (prompt + modelo + tools) reutilizable.
 * Un Channel apunta a UN Agent (1:1 en v1). El runtime aún lee de
 * agent_config legacy; F3B moverá la lectura a esta tabla.
 */

const MODEL_OPTIONS = [
  { value: "gpt-5.4", label: "GPT-5.4", hint: "Razonamiento alto, más caro" },
  { value: "gpt-5.4-mini", label: "GPT-5.4 mini", hint: "Equilibrado (recomendado)" },
  { value: "gpt-5.4-nano", label: "GPT-5.4 nano", hint: "Rápido y barato, ideal para voz" },
];

// Nombres bonitos para las tools conocidas. La LISTA ya no vive aquí: se pide
// a /admin/tools, que la saca del registro real del backend. Esto es solo la
// traducción; una tool nueva sale igual, con su nombre técnico y la descripción
// que trae el propio registro.
const TOOL_LABELS: Record<string, string> = {
  consultar_kb: "Buscar en base de conocimiento",
  buscar_contacto: "Buscar contacto",
  crear_actualizar_contacto: "Crear / actualizar contacto (captar lead)",
  consultar_disponibilidad: "Consultar disponibilidad",
  agendar_cita: "Agendar cita",
  derivar_humano: "Derivar a humano",
};

const KIND_OPTIONS: { value: AgentKind; label: string; hint: string }[] = [
  {
    value: "text",
    label: "Texto (chat)",
    hint: "WhatsApp, widget web, Instagram y email.",
  },
  {
    value: "voice",
    label: "Voz (llamadas)",
    hint: "Llamadas telefónicas (Retell).",
  },
];

const EMPTY_FORM: AgentIn = {
  name: "",
  kind: "text",
  prompt_system: "",
  model_name: "gpt-5.4-mini",
  temperature: null,
  max_tokens: null,
  buffer_seconds: 8,
  response_split_max_parts: 3,
  context_window: 20,
  handoff_bridge_message: null,
  monthly_budget_usd: null,
  tools_enabled: null,
  is_active: true,
  fallback_provider_id: null,
  fallback_model: null,
};

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<AgentOut | "new" | null>(null);
  const [deleting, setDeleting] = useState<AgentOut | null>(null);
  const [historyFor, setHistoryFor] = useState<AgentOut | null>(null);
  const [effectiveFor, setEffectiveFor] = useState<AgentOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  // El clasificador, el agente interno, las tarifas/límites y el horario de
  // atención viven como pestañas aquí dentro. Permitimos enlace directo con
  // ?tab=classifier / ?tab=internal / ?tab=model / ?tab=schedule (las rutas
  // antiguas /agent/classifier, /internal-agent y /agent/config redirigen aquí,
  // y ?tab=schedule de Conexiones también; el param "model" se conserva por
  // compatibilidad aunque la pestaña se llame Tarifas y límites).
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  type Tab = "agents" | "classifier" | "internal" | "model" | "schedule";
  const tab: Tab =
    tabParam === "classifier" ||
    tabParam === "internal" ||
    tabParam === "model" ||
    tabParam === "schedule"
      ? tabParam
      : "agents";
  function setTab(t: Tab) {
    setSearchParams(t === "agents" ? {} : { tab: t }, { replace: true });
  }

  async function refresh() {
    setLoading(true);
    try {
      setAgents(await listAgents());
      setListError(null);
    } catch (e) {
      // Sin catch, un fallo de la API dejaba la lista vacía y la pantalla
      // invitaba a "crear el primero" con agentes ya creados detrás.
      setListError(errorDetail(e, "No se pudo cargar la lista de agentes."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  function startCreate() {
    setEditing("new");
  }

  function startEdit(a: AgentOut) {
    setEditing(a);
  }

  async function confirmDelete() {
    if (!deleting) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAgent(deleting.id);
      setDeleting(null);
      await refresh();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "Error borrando agente");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Agentes"
        description={
          tab === "agents"
            ? "Cada agente tiene su propio prompt y modelo, pero todos comparten la base de conocimiento. Asígnalos a canales desde Conexiones."
            : tab === "classifier"
              ? "El agente clasificador filtra mensajes no deseados (spam, vendedores) antes de que respondan los demás."
              : tab === "model"
                ? "Tarifas de los modelos (base de la estimación de coste), presupuesto mensual global y mensaje de derivación por defecto."
                : tab === "schedule"
                  ? "Cuándo atiende el negocio. Es lo que mira el agente para decir si estáis abiertos y para proponer citas: Google Calendar dice lo que está ocupado, esto dice cuándo se abre."
                  : "El agente interno es un chat read-only del panel admin para consultar el estado del sistema. Configúralo abajo."
        }
        actions={
          tab === "agents" ? (
            <button type="button" onClick={startCreate} className="btn-primary">
              <Plus className="w-4 h-4" /> Nuevo agente
            </button>
          ) : undefined
        }
      />
      {/* Pestañas: el clasificador es un agente más y vive aquí dentro.
          `overflow-x-auto` como en Conexiones: con cinco pestañas la fila no
          cabe en un móvil, y sin esto era la PÁGINA la que se ensanchaba y
          dejaba el contenido cortado por la izquierda. */}
      <div className="px-4 md:px-6 border-b border-line bg-paper2 flex gap-1 overflow-x-auto">
        <TabBtn active={tab === "agents"} onClick={() => setTab("agents")}>
          <Bot className="w-3.5 h-3.5" /> Agentes
        </TabBtn>
        <TabBtn active={tab === "classifier"} onClick={() => setTab("classifier")}>
          <ShieldOff className="w-3.5 h-3.5" /> Clasificador
        </TabBtn>
        <TabBtn active={tab === "internal"} onClick={() => setTab("internal")}>
          <MessageCircle className="w-3.5 h-3.5" /> Agente interno
        </TabBtn>
        <TabBtn active={tab === "model"} onClick={() => setTab("model")}>
          <DollarSign className="w-3.5 h-3.5" /> Tarifas y límites
        </TabBtn>
        <TabBtn active={tab === "schedule"} onClick={() => setTab("schedule")}>
          <Clock className="w-3.5 h-3.5" /> Horarios
        </TabBtn>
      </div>

      {tab === "classifier" ? (
        <ClassifierPanel />
      ) : tab === "internal" ? (
        <InternalAgentPanel />
      ) : tab === "model" ? (
        <ModelCostsPanel />
      ) : tab === "schedule" ? (
        <div className="flex-1 overflow-auto p-4 md:p-6 max-w-4xl">
          <BusinessHoursPanel />
        </div>
      ) : (
        <div className="flex-1 overflow-auto p-4 max-w-5xl">
        {loading ? (
          <div className="flex items-center justify-center py-10 text-ink3 gap-2">
            <Loader2 className="w-4 h-4 animate-spin" /> Cargando agentes…
          </div>
        ) : listError ? (
          <div className="text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{listError}</span>
            <button
              type="button"
              onClick={() => void refresh()}
              className="font-semibold hover:underline shrink-0"
            >
              Reintentar
            </button>
          </div>
        ) : agents.length === 0 ? (
          <div className="text-center text-ink3 py-10 italic font-display">
            No hay agentes todavía. Pulsa <b className="text-ink2">Nuevo agente</b> para crear el primero.
          </div>
        ) : (
          <div className="space-y-3">
            {agents.map((a) => (
              <AgentCard
                key={a.id}
                agent={a}
                onEdit={() => startEdit(a)}
                onDelete={() => setDeleting(a)}
                onHistory={() => setHistoryFor(a)}
                onEffective={() => setEffectiveFor(a)}
              />
            ))}
          </div>
        )}
        </div>
      )}

      {editing && (
        <AgentFormModal
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
          }}
        />
      )}

      {effectiveFor && (
        <EffectivePromptModal agent={effectiveFor} onClose={() => setEffectiveFor(null)} />
      )}

      <ConfirmModal
        open={!!deleting}
        title="Eliminar agente"
        description={
          deleting ? (
            <div className="space-y-2">
              <div>
                ¿Seguro que quieres eliminar <b>{deleting.name}</b>? Esta acción no se puede deshacer.
              </div>
              {error && <div className="text-state-bad text-sm">{error}</div>}
            </div>
          ) : (
            ""
          )
        }
        confirmLabel="Eliminar"
        cancelLabel="Cancelar"
        tone="danger"
        busy={busy}
        onConfirm={confirmDelete}
        onCancel={() => {
          setDeleting(null);
          setError(null);
        }}
      />

      {historyFor && (
        <PromptHistoryModal
          agent={historyFor}
          onClose={() => setHistoryFor(null)}
          onRestored={async () => {
            setHistoryFor(null);
            await refresh();
          }}
        />
      )}
    </div>
  );
}

function TabBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  // En móvil la fila de pestañas no cabe y se desplaza. Si entras por enlace
  // directo a una pestaña de la derecha (?tab=schedule), sin esto la pestaña
  // activa se queda fuera de pantalla y parece que estés en otra.
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [active]);
  return (
    <button
      ref={ref}
      type="button"
      onClick={onClick}
      className={
        "inline-flex items-center gap-1.5 px-3 py-2.5 text-sm font-medium border-b-2 -mb-px transition whitespace-nowrap " +
        (active ? "border-brand-ink text-ink" : "border-transparent text-ink3 hover:text-ink2")
      }
    >
      {children}
    </button>
  );
}

function AgentCard({
  agent,
  onEdit,
  onDelete,
  onHistory,
  onEffective,
}: {
  agent: AgentOut;
  onEdit: () => void;
  onDelete: () => void;
  onHistory: () => void;
  onEffective: () => void;
}) {
  return (
    <div className="card p-4 flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 rounded-coro-sm bg-brand-soft text-brand-ink flex items-center justify-center">
          <Bot className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 flex-wrap">
            <div className="font-medium text-ink text-base">{agent.name}</div>
            {agent.is_active ? (
              <span className="pill bg-state-ok/10 text-state-ok inline-flex items-center gap-1">
                <CircleCheck className="w-3 h-3" /> Activo
              </span>
            ) : (
              <span className="pill bg-paper3 text-ink3 inline-flex items-center gap-1">
                <CircleSlash className="w-3 h-3" /> Inactivo
              </span>
            )}
            {/* El tipo se pinta siempre: saber si un agente es de voz o de
                texto es lo que explica por qué un canal no lo coge. */}
            {(agent.kind ?? "text") === "voice" ? (
              <span className="pill bg-paper3 text-ink2 inline-flex items-center gap-1">
                <Phone className="w-3 h-3" /> Voz
              </span>
            ) : (
              <span className="pill bg-paper3 text-ink2 inline-flex items-center gap-1">
                <MessageCircle className="w-3 h-3" /> Texto
              </span>
            )}
            <span className="text-[11px] font-mono text-ink3">{agent.model_name}</span>
          </div>
          <div className="text-xs text-ink3 mt-1 line-clamp-2">
            {agent.prompt_system.slice(0, 220)}
            {agent.prompt_system.length > 220 && "…"}
          </div>
        </div>
        <div className="flex gap-1.5 flex-wrap justify-end shrink-0">
          <button
            type="button"
            onClick={onEffective}
            className="btn-ghost text-xs"
            aria-label="Ver prompt efectivo"
            title="Ver lo que recibe el modelo (seguridad + prompt + reglas)"
          >
            <Eye className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={onHistory}
            className="btn-ghost text-xs"
            aria-label="Historial de prompts"
            title="Historial de prompts"
          >
            <History className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={onEdit}
            className="btn-ghost text-xs"
            aria-label="Editar"
            title="Editar"
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="btn-ghost text-xs text-state-bad hover:text-state-bad"
            aria-label="Eliminar"
            title="Eliminar"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
      <div className="flex items-center gap-2 flex-wrap text-[11px] text-ink3 pt-1 border-t border-line2">
        {agent.channels.length === 0 ? (
          <span className="italic">Sin canales asignados</span>
        ) : (
          <span className="inline-flex items-center gap-1 text-ink2">
            <Plug className="w-3 h-3" /> Canales:
          </span>
        )}
        {agent.channels.map((c) => (
          <span
            key={c.id}
            className={`pill text-[10px] ${
              c.enabled ? "bg-brand-soft text-brand-ink" : "bg-paper3 text-ink3"
            }`}
          >
            {c.name}
          </span>
        ))}
        {agent.monthly_budget_usd != null && (
          <span className="ml-auto">
            Tope mensual: <b className="text-ink2 font-mono">{agent.monthly_budget_usd.toFixed(2)} $</b>
          </span>
        )}
      </div>
    </div>
  );
}

function EffectivePromptModal({ agent, onClose }: { agent: AgentOut; onClose: () => void }) {
  const [data, setData] = useState<EffectivePrompt | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<"sections" | "raw">("sections");

  useEffect(() => {
    let alive = true;
    getAgentEffectivePrompt(agent.id)
      .then((d) => alive && setData(d))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [agent.id]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <div className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-2xl max-h-[85vh] flex flex-col">
        <header className="px-5 py-3 border-b border-line flex items-center gap-2 flex-wrap">
          <Eye className="w-4 h-4 text-brand-ink" />
          <h2 className="font-display text-lg text-ink flex-1 min-w-[8rem]">
            Lo que recibe el modelo · <span className="text-ink2">{agent.name}</span>
          </h2>
          <div className="flex items-center gap-1 mr-2">
            <button
              className={`btn-ghost btn-sm ${view === "sections" ? "bg-paper3 text-ink" : "text-ink3"}`}
              onClick={() => setView("sections")}
            >
              Por partes
            </button>
            <button
              className={`btn-ghost btn-sm ${view === "raw" ? "bg-paper3 text-ink" : "text-ink3"}`}
              onClick={() => setView("raw")}
            >
              Texto completo
            </button>
          </div>
          <button type="button" onClick={onClose} className="text-ink3 hover:text-ink" aria-label="Cerrar">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 overflow-auto space-y-3">
          <p className="text-[12px] text-ink3">
            Solo lectura. Esto es exactamente lo que se le envía al modelo en cada respuesta de
            este agente: la capa de seguridad (fija), tu prompt y las reglas de estilo aprendidas.
          </p>
          {loading || !data ? (
            <div className="text-center py-10 text-ink3 inline-flex items-center justify-center gap-2 w-full">
              <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
            </div>
          ) : view === "raw" ? (
            <pre className="text-[12.5px] whitespace-pre-wrap font-mono bg-paper2 border border-line rounded-coro-sm p-3 text-ink2">
              {data.effective}
            </pre>
          ) : (
            <div className="space-y-3">
              <PromptSection
                icon={<ShieldCheck className="w-3.5 h-3.5" />}
                label="Capa de seguridad (fija, no editable)"
                tone="text-state-ok"
                body={data.security}
              />
              <PromptSection
                icon={<Bot className="w-3.5 h-3.5" />}
                label="Prompt del agente (lo que editas)"
                tone="text-brand-ink"
                body={data.base || "(vacío)"}
              />
              <PromptSection
                icon={<Wand2 className="w-3.5 h-3.5" />}
                label="Reglas de estilo aprendidas"
                tone="text-brand-ink"
                body={data.learned_rules?.trim() ? data.learned_rules : "(ninguna activa todavía)"}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function PromptSection({
  icon,
  label,
  tone,
  body,
}: {
  icon: React.ReactNode;
  label: string;
  tone: string;
  body: string;
}) {
  return (
    <div>
      <div className={`eyebrow mb-1 inline-flex items-center gap-1.5 ${tone}`}>
        {icon}
        {label}
      </div>
      <pre className="text-[12.5px] whitespace-pre-wrap font-mono bg-paper2 border border-line rounded-coro-sm p-3 text-ink2">
        {body}
      </pre>
    </div>
  );
}

function AgentFormModal({
  initial,
  onClose,
  onSaved,
}: {
  initial: AgentOut | null;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}) {
  const [form, setForm] = useState<AgentIn>(() =>
    initial
      ? {
          name: initial.name,
          kind: initial.kind ?? "text",
          prompt_system: initial.prompt_system,
          model_name: initial.model_name,
          temperature: initial.temperature,
          max_tokens: initial.max_tokens,
          buffer_seconds: initial.buffer_seconds,
          response_split_max_parts: initial.response_split_max_parts,
          context_window: initial.context_window,
          handoff_bridge_message: initial.handoff_bridge_message,
          monthly_budget_usd: initial.monthly_budget_usd,
          tools_enabled: initial.tools_enabled,
          is_active: initial.is_active,
          llm_provider_id: initial.llm_provider_id,
          fallback_provider_id: initial.fallback_provider_id,
          fallback_model: initial.fallback_model,
        }
      : { ...EMPTY_FORM },
  );
  // Proveedores LLM disponibles (para el selector). Se cargan una vez.
  const [providers, setProviders] = useState<LLMProviderOut[]>([]);
  useEffect(() => {
    listLLMProviders().then(setProviders).catch(() => setProviders([]));
  }, []);
  const defaultProviderName = providers.find((p) => p.is_default)?.name ?? "";
  // Modelos REALES del proveedor elegido (hook compartido con el clasificador
  // y el agente interno). [] → se cae a la lista estática de sugerencias.
  const { models: providerModels, loading: loadingModels } =
    useProviderModels(form.llm_provider_id);
  // Y los del proveedor de RESPALDO (para el campo "Modelo del respaldo").
  const { models: fallbackModels, loading: loadingFallbackModels } =
    useProviderModels(form.fallback_provider_id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Herramientas REGISTRADAS de verdad en el backend (/admin/tools). Antes esta
  // lista estaba escrita a mano aquí: registrabas una tool nueva y no se podía
  // activar desde el panel hasta que alguien se acordaba de tocar este fichero.
  const [registered, setRegistered] = useState<ToolOut[]>([]);
  useEffect(() => {
    listRegisteredTools().then(setRegistered).catch(() => setRegistered([]));
  }, []);

  // Se pinta lo registrado MÁS cualquier tool que el agente ya tenga guardada y
  // que ya no exista. Sin esto, editar un agente con una tool desconocida la
  // borraba en silencio al guardar.
  const toolRows = useMemo(() => {
    const rows = registered.map((t) => ({
      name: t.name,
      label: TOOL_LABELS[t.name] ?? t.name,
      description: t.description,
    }));
    const known = new Set(rows.map((t) => t.name));
    const extras = (initial?.tools_enabled ?? []).filter((n) => !known.has(n));
    return [
      ...rows,
      ...extras.map((name) => ({
        name,
        label: TOOL_LABELS[name] ?? name,
        description: "Ya no está registrada en el backend: no se ejecutará.",
      })),
    ];
  }, [initial, registered]);

  function toggleTool(name: string) {
    setForm((f) => {
      // `null` = sin configurar (todas). Al tocar la primera casilla partimos de
      // "todas marcadas", que es lo que el usuario está viendo.
      const current = f.tools_enabled ?? toolRows.map((t) => t.name);
      const next = current.includes(name)
        ? current.filter((t) => t !== name)
        : [...current, name];
      // OJO: `next` puede quedar en []. Es un valor VÁLIDO y distinto de null —
      // significa "ninguna herramienta", no "sin configurar". El backend ya los
      // distingue; antes una lista vacía activaba TODAS las tools.
      return { ...f, tools_enabled: next };
    });
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (initial) {
        await updateAgent(initial.id, form);
      } else {
        await createAgent(form);
      }
      await onSaved();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "Error guardando agente");
    } finally {
      setBusy(false);
    }
  }

  const toolsUnset = form.tools_enabled == null;
  const toolsEnabled = form.tools_enabled ?? toolRows.map((t) => t.name);
  const toolsNone = !toolsUnset && toolsEnabled.length === 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <div className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-2xl max-h-[90vh] flex flex-col">
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Bot className="w-4 h-4 text-brand-ink" />
          <h2 className="font-display text-xl text-ink flex-1">
            {initial ? "Editar agente" : "Nuevo agente"}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-ink3 hover:text-ink"
            aria-label="Cerrar"
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <form onSubmit={onSubmit} className="flex-1 overflow-auto p-5 space-y-4">
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Nombre
            </label>
            <input
              type="text"
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              className="input w-full"
              placeholder="Ej. Soporte WhatsApp"
            />
          </div>

          {/* Tipo de agente. No es una etiqueta: el backend NO deja que un
              agente de voz atienda un canal de texto ni al revés, así que
              equivocarse aquí deja el canal mudo. Por eso el aviso es explícito
              y cambia según lo elegido. */}
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Tipo de agente
            </label>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {KIND_OPTIONS.map((k) => {
                const selected = (form.kind ?? "text") === k.value;
                return (
                  <label
                    key={k.value}
                    className={
                      "flex items-start gap-2 p-2.5 rounded-coro-sm border cursor-pointer transition " +
                      (selected
                        ? "border-brand-ink bg-brand-soft/40"
                        : "border-line hover:bg-paper2")
                    }
                  >
                    <input
                      type="radio"
                      name="agent-kind"
                      className="mt-0.5"
                      checked={selected}
                      onChange={() => setForm({ ...form, kind: k.value })}
                    />
                    <div>
                      <div className="text-sm text-ink2 inline-flex items-center gap-1.5">
                        {k.value === "voice" ? (
                          <Phone className="w-3.5 h-3.5" />
                        ) : (
                          <MessageCircle className="w-3.5 h-3.5" />
                        )}
                        {k.label}
                      </div>
                      <div className="text-[11px] text-ink3">{k.hint}</div>
                    </div>
                  </label>
                );
              })}
            </div>
            <p className="text-[11px] text-state-bad mt-1.5 flex items-start gap-1.5">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
              <span>
                {(form.kind ?? "text") === "voice" ? (
                  <>
                    Un agente de <b>voz</b> solo puede atender <b>llamadas</b>. Si lo
                    asignas a WhatsApp, al widget web, a Instagram o a email, ese canal
                    se queda <b>sin nadie que responda</b>.
                  </>
                ) : (
                  <>
                    Un agente de <b>texto</b> solo puede atender <b>chats</b>. Si lo
                    asignas al canal de llamadas, las llamadas se quedan{" "}
                    <b>sin nadie que responda</b>.
                  </>
                )}{" "}
                Un prompt de voz (se pronuncia, sin markdown ni enlaces) y uno de chat
                no son intercambiables.
              </span>
            </p>
            {initial && (initial.kind ?? "text") !== (form.kind ?? "text") && (
              <p className="text-[11px] text-state-bad mt-1.5">
                <b>Estás cambiando el tipo de un agente que ya existe.</b> Revisa sus
                canales asignados
                {initial.channels.length > 0
                  ? ` (${initial.channels.map((c) => c.name).join(", ")})`
                  : ""}
                : los que no encajen con el tipo nuevo dejarán de estar atendidos.
              </p>
            )}
          </div>

          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Prompt del sistema
            </label>
            <textarea
              required
              rows={10}
              value={form.prompt_system}
              onChange={(e) => setForm({ ...form, prompt_system: e.target.value })}
              className="input w-full font-mono text-xs leading-relaxed"
              placeholder="Eres un asistente de…"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Proveedor LLM
              </label>
              <select
                className="input w-full"
                value={form.llm_provider_id ?? ""}
                onChange={(e) => setForm({ ...form, llm_provider_id: e.target.value || null })}
              >
                <option value="">Por defecto{defaultProviderName ? ` (${defaultProviderName})` : ""}</option>
                {providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.is_default ? " · predeterminado" : ""}
                  </option>
                ))}
              </select>
              <p className="text-[11px] text-ink3 mt-1">
                Gestiona proveedores en <b>Conexiones → Proveedores LLM</b>.
              </p>
            </div>
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Modelo
              </label>
              <input
                className="input w-full font-mono text-sm"
                list="model-suggestions"
                value={form.model_name}
                onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                placeholder="gpt-5.4-mini"
              />
              <datalist id="model-suggestions">
                {providerModels.length > 0
                  ? providerModels.map((m) => <option key={m} value={m} />)
                  : MODEL_OPTIONS.map((m) => (
                      <option key={m.value} value={m.value}>
                        {m.label}
                      </option>
                    ))}
              </datalist>
              <p className="text-[11px] text-ink3 mt-1">
                {loadingModels
                  ? "Cargando los modelos del proveedor…"
                  : providerModels.length > 0
                    ? `${providerModels.length} modelos del proveedor en las sugerencias (texto libre).`
                    : "Escribe el id del modelo del proveedor elegido (texto libre)."}
              </p>
            </div>
          </div>

          {/* Respaldo POR AGENTE (opcional): si el primario falla, la llamada
              se reintenta aquí. Vacío → aplica el respaldo global (Conexiones
              → Proveedores LLM) y, si tampoco, el legacy por credenciales. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Proveedor de respaldo (opcional)
              </label>
              <select
                className="input w-full"
                value={form.fallback_provider_id ?? ""}
                onChange={(e) =>
                  setForm({ ...form, fallback_provider_id: e.target.value || null })
                }
              >
                <option value="">Respaldo global</option>
                {providers
                  .filter((p) => p.id !== form.llm_provider_id)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </select>
            </div>
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Modelo del respaldo
              </label>
              <input
                className="input w-full font-mono text-sm"
                list="fallback-model-suggestions"
                value={form.fallback_model ?? ""}
                onChange={(e) => setForm({ ...form, fallback_model: e.target.value || null })}
                placeholder="deepseek/deepseek-v4-flash"
                disabled={!form.fallback_provider_id}
              />
              <datalist id="fallback-model-suggestions">
                {fallbackModels.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              <p className="text-[11px] text-ink3 mt-1">
                {loadingFallbackModels
                  ? "Cargando los modelos del proveedor…"
                  : fallbackModels.length > 0
                    ? `${fallbackModels.length} modelos del respaldo en las sugerencias.`
                    : "Modelo de ESE proveedor (los ids del principal no existen allí)."}
              </p>
            </div>
          </div>

          <div>
            <div className="flex items-baseline gap-2 mb-1">
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium">
                Tools disponibles
              </label>
              {!toolsUnset && (
                <button
                  type="button"
                  onClick={() => setForm({ ...form, tools_enabled: null })}
                  className="text-[11px] text-brand-ink underline"
                >
                  volver a «sin configurar»
                </button>
              )}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {toolRows.map((t) => (
                <label
                  key={t.name}
                  className="flex items-center gap-2 p-2 rounded-coro-sm border border-line hover:bg-paper2 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={toolsEnabled.includes(t.name)}
                    onChange={() => toggleTool(t.name)}
                  />
                  <div>
                    <div className="text-sm text-ink2">{t.label}</div>
                    <div className="text-[10px] font-mono text-ink3">{t.name}</div>
                    {t.description && (
                      <div className="text-[10px] text-ink3 mt-0.5 line-clamp-2">
                        {t.description}
                      </div>
                    )}
                  </div>
                </label>
              ))}
            </div>
            {registered.length === 0 && (
              <p className="text-[11px] text-ink3 mt-1.5 italic">
                Cargando las herramientas registradas en el servidor…
              </p>
            )}
            {/* "Sin configurar" y "ninguna" NO son lo mismo, y la diferencia
                cambia lo que el agente puede hacer. Se dice explícitamente. */}
            {toolsUnset && (
              <p className="text-[11px] text-ink3 mt-1.5 italic">
                Sin configurar: el agente puede usar todas las herramientas. En
                cuanto marques o desmarques una, se guarda la selección exacta.
              </p>
            )}
            {toolsNone && (
              <p className="text-[11px] text-state-bad mt-1.5">
                <b>Ninguna herramienta seleccionada.</b> Este agente solo
                conversará: no buscará en la base de conocimiento, no guardará
                contactos, no agendará citas y <b>no podrá derivar a una
                persona</b>. Si no era lo que querías, marca alguna.
              </p>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Buffer (s)
              </label>
              <input
                type="number"
                min={1}
                max={60}
                value={form.buffer_seconds}
                onChange={(e) =>
                  setForm({ ...form, buffer_seconds: Number(e.target.value) || 8 })
                }
                className="input w-full font-numbers"
              />
            </div>
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Context window (mensajes)
              </label>
              <input
                type="number"
                min={1}
                max={100}
                value={form.context_window}
                onChange={(e) =>
                  setForm({ ...form, context_window: Number(e.target.value) })
                }
                className="input w-full font-numbers"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Mensaje de derivación a humano
            </label>
            <input
              type="text"
              value={form.handoff_bridge_message ?? ""}
              onChange={(e) =>
                setForm({
                  ...form,
                  handoff_bridge_message: e.target.value || null,
                })
              }
              className="input w-full"
              placeholder="Dame un momento, paso tu mensaje al equipo…"
            />
            <p className="text-[11px] text-ink3 mt-1 italic">
              El bot lo manda justo antes de pasar la conversación a un humano. Déjalo vacío para usar el default.
            </p>
          </div>

          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Tope mensual ($)
            </label>
            <input
              type="number"
              min={0}
              step={0.01}
              value={form.monthly_budget_usd ?? ""}
              onChange={(e) =>
                setForm({
                  ...form,
                  monthly_budget_usd: e.target.value === "" ? null : Number(e.target.value),
                })
              }
              className="input w-full font-numbers max-w-xs"
              placeholder="Sin límite"
            />
            <p className="text-[11px] text-ink3 mt-1 italic">
              Tope propio de <b>este</b> agente. Si su consumo del mes lo supera,
              se <b>desactiva solo él</b> (los demás siguen atendiendo) y se avisa
              al admin. El tope de <b>toda la instalación</b>, que pausa el bot
              entero, está en <b>Tarifas y límites</b>.
            </p>
          </div>

          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={form.is_active}
              onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
            />
            <span className="text-sm text-ink2">Agente activo</span>
          </label>

          {error && <div className="text-state-bad text-sm">{error}</div>}
        </form>

        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cancelar
          </button>
          <button
            type="button"
            onClick={onSubmit}
            disabled={busy}
            className="btn-primary inline-flex items-center gap-1.5"
          >
            {busy ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {initial ? "Guardar cambios" : "Crear agente"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function PromptHistoryModal({
  agent,
  onClose,
  onRestored,
}: {
  agent: AgentOut;
  onClose: () => void;
  onRestored: () => void | Promise<void>;
}) {
  const [items, setItems] = useState<AgentPromptHistoryOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [restoring, setRestoring] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Restaurar SOBRESCRIBE el prompt vivo del agente: pedimos confirmación.
  const [confirmRestore, setConfirmRestore] = useState<string | null>(null);

  useEffect(() => {
    getAgentPromptHistory(agent.id)
      .then(setItems)
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, [agent.id]);

  async function restore(hid: string) {
    setConfirmRestore(null);
    setRestoring(hid);
    try {
      await restoreAgentPrompt(agent.id, hid);
      await onRestored();
    } finally {
      setRestoring(null);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <ConfirmModal
        open={confirmRestore !== null}
        title="¿Restaurar esta versión del prompt?"
        description="Sobrescribe el prompt actual del agente y aplica de inmediato a las respuestas. La versión actual quedará en el historial."
        confirmLabel="Restaurar"
        busy={restoring !== null}
        onConfirm={() => confirmRestore && restore(confirmRestore)}
        onCancel={() => setConfirmRestore(null)}
      />
      <div className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-2xl max-h-[90vh] flex flex-col">
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <History className="w-4 h-4 text-brand-ink" />
          <h2 className="font-display text-xl text-ink flex-1 truncate">Historial de prompts · {agent.name}</h2>
          <button type="button" onClick={onClose} className="text-ink3 hover:text-ink" aria-label="Cerrar">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="flex-1 overflow-auto p-5 space-y-3">
          <div className="rounded-coro-sm bg-paper2 p-3 text-xs">
            <div className="font-medium text-ink mb-1">Prompt actual</div>
            <pre className="whitespace-pre-wrap font-mono text-[11px] text-ink2 max-h-32 overflow-auto">{agent.prompt_system}</pre>
          </div>
          {loading ? (
            <div className="flex items-center justify-center py-8 text-ink3 gap-2">
              <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
            </div>
          ) : items.length === 0 ? (
            <div className="text-center text-ink3 py-8 italic font-display text-sm">
              Aún no hay versiones anteriores. El historial se crea cada vez que cambias el prompt.
            </div>
          ) : (
            <ul className="space-y-2">
              {items.map((h) => (
                <li key={h.id} className="border border-line rounded-coro-sm p-3">
                  <div className="flex items-center gap-2 flex-wrap text-[11px] text-ink3 mb-2">
                    <span className="font-mono">{new Date(h.created_at).toLocaleString("es-ES")}</span>
                    {h.created_by_email && <span>· {h.created_by_email}</span>}
                    {h.model_name && <span className="pill bg-paper3 text-ink3 text-[10px]">{h.model_name}</span>}
                    <div className="ml-auto flex gap-1.5">
                      <button
                        type="button"
                        className="btn-ghost text-xs"
                        onClick={() => setExpanded(expanded === h.id ? null : h.id)}
                      >
                        {expanded === h.id ? "Ocultar" : "Ver"}
                      </button>
                      <button
                        type="button"
                        className="btn-primary text-xs"
                        onClick={() => setConfirmRestore(h.id)}
                        disabled={restoring !== null}
                      >
                        {restoring === h.id ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <RotateCcw className="w-3.5 h-3.5" />
                        )}{" "}
                        Restaurar
                      </button>
                    </div>
                  </div>
                  <pre
                    className={
                      "whitespace-pre-wrap font-mono text-[11px] text-ink2 overflow-auto " +
                      (expanded === h.id ? "max-h-80" : "max-h-12")
                    }
                  >
                    {h.prompt_system}
                  </pre>
                </li>
              ))}
            </ul>
          )}
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cerrar
          </button>
        </footer>
      </div>
    </div>
  );
}
