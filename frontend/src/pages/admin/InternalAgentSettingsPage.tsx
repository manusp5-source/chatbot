// Configuracion del Agente Interno (chat read-only para admin).
// Editable: modelo, temperature, max_tokens, presupuesto mensual, cap diario
// por usuario, system prompt. Visible solo a admins.

import { useProviderModels } from "@/hooks/useProviderModels";
import { useEffect, useState } from "react";
import { Save, AlertCircle, Activity } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { InternalAgentTabs } from "@/components/InternalAgentTabs";
import {
  getInternalAgentConfig,
  getInternalAgentStatus,
  updateInternalAgentConfig,
  type InternalAgentConfig,
  type InternalAgentStatus,
} from "@/services/internalAgent";
import { listLLMProviders, type LLMProviderOut } from "@/services/admin";
import { errorDetail } from "@/lib/errors";

const MODEL_HINTS: Record<string, string> = {
  "gpt-5.4-mini": "Recomendado · equilibrio coste/calidad",
  "gpt-5.4-nano": "Mas barato, util para consultas muy cortas",
  "gpt-5.4": "Mas caro, mejor razonamiento si las queries son complejas",
};

const MODELS = ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4"];

export default function InternalAgentSettingsPage() {
  return (
    <div className="h-full flex flex-col">
      <PageHeader
        eyebrow="Agente IA"
        title={
          <>
            Agente <span className="accent">interno</span>
          </>
        }
        description="Chat read-only del panel admin para preguntar por el estado del sistema. Configurable abajo."
      />
      <InternalAgentPanel />
    </div>
  );
}

/**
 * Panel de ajustes del Agente interno. Vive como pestaña dentro de "Agentes"
 * (junto al clasificador), por eso no lleva su propio PageHeader — igual que
 * ClassifierPanel. Mantiene las sub-pestañas Ajustes/Prompt para no perder el
 * enlace al prompt del agente interno.
 */
export function InternalAgentPanel() {
  const [cfg, setCfg] = useState<InternalAgentConfig | null>(null);
  const [status, setStatus] = useState<InternalAgentStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);
  const [providers, setProviders] = useState<LLMProviderOut[]>([]);
  const { models: providerModels, loading: loadingModels } = useProviderModels(cfg?.llm_provider_id);

  async function refresh() {
    try {
      const [c, s] = await Promise.all([
        getInternalAgentConfig(),
        getInternalAgentStatus(),
      ]);
      setCfg(c);
      setStatus(s);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar la configuración del agente interno."));
    }
  }

  useEffect(() => {
    void refresh();
    listLLMProviders().then(setProviders).catch(() => setProviders([]));
  }, []);

  async function save() {
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      const patch: Partial<InternalAgentConfig> & {
        clear_monthly_budget?: boolean;
        clear_llm_provider?: boolean;
      } = {
        model_name: cfg.model_name,
        temperature: cfg.temperature,
        max_tokens: cfg.max_tokens,
        daily_query_limit: cfg.daily_query_limit,
      };
      if (cfg.monthly_budget_usd === null) {
        patch.clear_monthly_budget = true;
      } else {
        patch.monthly_budget_usd = cfg.monthly_budget_usd;
      }
      // null = volver al proveedor por defecto → flag explícito (el backend
      // ignora llm_provider_id ausente; clear_llm_provider lo pone a NULL).
      if (cfg.llm_provider_id === null) {
        patch.clear_llm_provider = true;
      } else {
        patch.llm_provider_id = cfg.llm_provider_id;
      }
      const next = await updateInternalAgentConfig(patch);
      setCfg(next);
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 2000);
    } catch (e) {
      // Solo aquí (el guardado en sí) es un error real que mostrar.
      setError(errorDetail(e, "No se pudieron guardar los cambios del agente interno."));
      setSaving(false);
      return;
    }
    // El refresco del estado es best-effort: si una lectura posterior se cae
    // (red, estado, etc.) NO debe convertir un guardado CORRECTO en un error en
    // pantalla — el cambio ya está persistido en la BD.
    try {
      await refresh();
    } catch {
      /* noop: el guardado ya fue OK */
    }
    setSaving(false);
  }

  if (!cfg) {
    return (
      <div className="p-10 text-center text-ink3 italic font-display">
        Cargando configuracion...
      </div>
    );
  }

  return (
    <>
      <InternalAgentTabs />

      <div className="flex-1 p-4 md:p-6 max-w-2xl space-y-4 overflow-auto">
        {error && (
          <div className="card p-3 border-state-bad/40 bg-state-bad/5 text-state-bad text-sm flex items-start gap-2">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {status && (
          <div className="card p-4 border-2 border-line">
            <div className="flex items-center gap-2 mb-2">
              <Activity className="w-4 h-4 text-ink2" />
              <span className="text-sm font-semibold text-ink">Uso actual</span>
            </div>
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <div className="text-ink3">Tu uso hoy</div>
                <div className="font-semibold text-ink">
                  {status.daily_used} / {status.daily_limit}
                </div>
              </div>
              <div>
                <div className="text-ink3">Coste mes (interno)</div>
                <div className="font-semibold text-ink">
                  ${status.monthly_cost_usd.toFixed(4)}
                  {status.monthly_budget_usd !== null && (
                    <span className="text-ink3 font-normal">
                      {" "}
                      / ${status.monthly_budget_usd.toFixed(2)}
                    </span>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        <Row label="Proveedor LLM" hint="Por defecto usa el proveedor predeterminado.">
          <select
            className="input w-full"
            value={cfg.llm_provider_id ?? ""}
            onChange={(e) => setCfg({ ...cfg, llm_provider_id: e.target.value || null })}
          >
            <option value="">Por defecto</option>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.is_default ? " · predeterminado" : ""}
              </option>
            ))}
          </select>
        </Row>

        <Row label="Modelo" hint="Escribe el id del modelo del proveedor elegido.">
          <input
            className="input w-full font-mono text-sm"
            list="internal-model-suggestions"
            value={cfg.model_name}
            onChange={(e) => setCfg({ ...cfg, model_name: e.target.value })}
            placeholder="gpt-5.4-mini"
          />
          <datalist id="internal-model-suggestions">
            {providerModels.length > 0
              ? providerModels.map((m) => <option key={m} value={m} />)
              : MODELS.map((m) => (
                  <option key={m} value={m}>
                    {MODEL_HINTS[m] || ""}
                  </option>
                ))}
          </datalist>
          {loadingModels && (
            <p className="text-[11px] text-ink3 mt-1">Cargando los modelos del proveedor…</p>
          )}
        </Row>

        <Row
          label="Temperature"
          hint="0 = más factual y determinista. Recomendado 0.2 para consultas internas."
        >
          <input
            className="input"
            type="number"
            step={0.1}
            min={0}
            max={1.5}
            value={cfg.temperature}
            onChange={(e) =>
              setCfg({ ...cfg, temperature: parseFloat(e.target.value) || 0 })
            }
          />
        </Row>

        <Row
          label="Max tokens por respuesta"
          hint="Tope de tokens en la respuesta final del agente. 1500 cubre la mayoría de consultas."
        >
          <input
            className="input"
            type="number"
            min={64}
            max={8000}
            value={cfg.max_tokens}
            onChange={(e) =>
              setCfg({ ...cfg, max_tokens: parseInt(e.target.value) || 64 })
            }
          />
        </Row>

        <Row
          label="Tope diario por usuario admin"
          hint="Numero maximo de preguntas que cada admin puede hacer al dia. Rate limit en Redis con TTL 24h."
        >
          <input
            className="input"
            type="number"
            min={1}
            max={1000}
            value={cfg.daily_query_limit}
            onChange={(e) =>
              setCfg({
                ...cfg,
                daily_query_limit: parseInt(e.target.value) || 1,
              })
            }
          />
        </Row>

        <Row
          label="Presupuesto mensual (USD)"
          hint="Si el coste acumulado del agente interno supera este tope, las queries devuelven 429 hasta el siguiente mes. Dejar vacio para sin tope."
        >
          <input
            className="input"
            type="number"
            min={0}
            step={1}
            placeholder="Ej: 20"
            value={cfg.monthly_budget_usd ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              setCfg({
                ...cfg,
                monthly_budget_usd: v === "" ? null : parseFloat(v),
              });
            }}
          />
        </Row>

        <div className="flex items-center gap-3">
          <button
            onClick={save}
            className="btn-primary w-full sm:w-auto justify-center"
            disabled={saving}
          >
            <Save />
            {saving ? "Guardando..." : "Guardar"}
          </button>
          {savedFlash && (
            <span className="text-state-ok text-xs font-medium">Guardado</span>
          )}
        </div>

        <p className="text-[11px] text-ink4 italic font-display">
          Cada query del agente interno queda registrada en el audit log
          (action: <code>internal_agent.query</code>). Solo lectura: no
          puede pausar, asignar ni modificar nada.
        </p>
      </div>
    </>
  );
}

function Row({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="card p-4">
      <label className="label">{label}</label>
      {children}
      {hint && <p className="text-[11px] text-ink3 mt-2">{hint}</p>}
    </div>
  );
}
