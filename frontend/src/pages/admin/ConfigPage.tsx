import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { ModelPricesPanel } from "@/pages/admin/ModelPricesPanel";
import { errorDetail } from "@/lib/errors";
import {
  createAgentConfig,
  getActiveAgentConfig,
  type AgentConfig,
} from "@/services/admin";

/**
 * Pestaña "Tarifas y límites" dentro de Agentes (antes "Modelo").
 *
 * La antigua "Configuración de modelo" (agent_config legacy) quedó casi toda
 * duplicada cuando cada agente pasó a tener su propio modelo/buffer/contexto
 * en la pestaña Agentes; esos campos se eliminaron de aquí (15-jul). Quedan
 * SOLO las piezas con efecto real y único en el backend:
 *
 *   - Tarifas de modelos (ModelPricesPanel): base de la estimación de coste
 *     del dashboard, auto-actualizadas desde OpenRouter.
 *   - Presupuesto mensual GLOBAL: services/budget.py lo lee de agent_config
 *     y pausa el agente al superarlo. El tope por-agente aún no se aplica.
 *   - Mensaje al derivar a humano (por defecto): human_handoff.py lo lee de
 *     agent_config como default global del mensaje puente.
 *
 * El resto (modelo LLM, buffer, partes, contexto) se configura por agente; el
 * botón de pausa global vive en Dashboard. La zona horaria se movió a
 * Admin → Ajustes.
 */
export function ModelCostsPanel() {
  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setCfg(await getActiveAgentConfig());
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function save() {
    if (!cfg) return;
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      // Se reenvía la config completa (los campos legacy no mostrados
      // conservan su valor) y se activa como nueva versión.
      await createAgentConfig({ ...cfg, activate: true });
      await refresh();
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      // El "Guardado" verde vive dentro del try, así que cuando el guardado
      // fallaba la única pista era que no aparecía nada: el presupuesto
      // seguía sin tope y nadie se enteraba.
      setError(errorDetail(e, "No se pudieron guardar los cambios. Vuelve a intentarlo."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex-1 p-4 md:p-6 max-w-2xl space-y-4 overflow-auto">
      {/* Tarifas de modelos (estimación de coste del dashboard) */}
      <ModelPricesPanel />

      {/* Ajustes globales con efecto real en backend (agent_config) */}
      {!cfg ? (
        <div className="p-6 text-center text-ink3 italic font-display">
          Cargando límites globales…
        </div>
      ) : (
        <>
          <Row
            label="Presupuesto mensual global (USD)"
            hint="Tope de gasto LLM de TODO el sistema (todos los agentes). Si el coste acumulado del mes lo supera, el agente se pausa automáticamente y recibirás una alerta. Dejar vacío para sin tope."
          >
            <input
              className="input"
              type="number"
              min={0}
              step={1}
              placeholder="Ej: 50"
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

          <Row
            label="Mensaje al derivar a humano (por defecto)"
            hint="Default global: se envía al cliente justo antes de que el equipo tome el relevo, cuando el agente no genera uno propio. Máximo 280 caracteres. No menciones nombres propios."
          >
            <textarea
              className="input min-h-[70px]"
              rows={2}
              maxLength={280}
              placeholder="Te paso con el equipo. Te contestaran por aqui en cuanto puedan."
              value={cfg.handoff_bridge_message || ""}
              onChange={(e) =>
                setCfg({ ...cfg, handoff_bridge_message: e.target.value })
              }
            />
          </Row>

          {error && (
            <div className="text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2">
              {error}
            </div>
          )}

          <button
            onClick={save}
            className="btn-primary w-full sm:w-auto justify-center"
            disabled={saving}
          >
            <Save />
            {saving ? "Guardando…" : saved ? "Guardado" : "Guardar"}
          </button>
        </>
      )}
    </div>
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
