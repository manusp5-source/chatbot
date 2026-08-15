// Gestión de tarifas de modelos (USD por 1M de tokens) para la estimación de
// coste del dashboard. Se auto-actualizan desde OpenRouter (botón + tarea
// diaria) y se pueden editar a mano. Vive en Agentes → "Tarifas y límites".
import { useEffect, useState } from "react";
import { RefreshCw, Save, DollarSign } from "lucide-react";
import {
  getModelPrices,
  refreshModelPrices,
  updateModelPrice,
  type ModelPrice,
} from "@/services/admin";
import { errorDetail } from "@/lib/errors";

const SOURCE_LABEL: Record<string, string> = {
  openrouter: "OpenRouter",
  manual: "Manual",
  seed: "Inicial",
};

const SOURCE_STYLE: Record<string, string> = {
  openrouter: "bg-brand/15 text-brand-ink",
  manual: "bg-state-warn/15 text-state-warn",
  seed: "bg-paper2 text-ink3",
};

export function ModelPricesPanel() {
  const [prices, setPrices] = useState<ModelPrice[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  // Edición en línea: model → {input, output} como strings (para el input).
  const [edit, setEdit] = useState<Record<string, { input: string; output: string }>>({});
  const [savingModel, setSavingModel] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      setPrices(await getModelPrices());
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar los precios."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function onRefresh() {
    setRefreshing(true);
    setMsg(null);
    setError(null);
    try {
      const r = await refreshModelPrices();
      if (r.error) {
        setError(`OpenRouter: ${r.error}`);
      } else {
        setMsg(
          `Actualizados ${r.updated.length} modelo(s) desde OpenRouter` +
            (r.unmatched.length ? ` · ${r.unmatched.length} sin coincidencia (edítalos a mano)` : "")
        );
      }
      await load();
    } catch (e) {
      setError(errorDetail(e, "No se pudo actualizar desde OpenRouter."));
    } finally {
      setRefreshing(false);
    }
  }

  async function onSave(model: string) {
    const e = edit[model];
    if (!e) return;
    const input = parseFloat(e.input);
    const output = parseFloat(e.output);
    if (Number.isNaN(input) || Number.isNaN(output) || input < 0 || output < 0) {
      setError(`Precio inválido para ${model}.`);
      return;
    }
    setSavingModel(model);
    setError(null);
    try {
      await updateModelPrice(model, { input_per_1m: input, output_per_1m: output });
      setEdit((prev) => {
        const next = { ...prev };
        delete next[model];
        return next;
      });
      setMsg(`Precio de ${model} guardado.`);
      await load();
    } catch (err) {
      setError(errorDetail(err, `No se pudo guardar ${model}.`));
    } finally {
      setSavingModel(null);
    }
  }

  return (
    <div className="card overflow-hidden">
      <div className="px-4 py-3 border-b border-line bg-paper2 flex items-center gap-2">
        <DollarSign className="w-4 h-4 text-brand-ink" />
        <h3 className="font-semibold text-ink text-sm">Tarifas de modelos</h3>
        <span className="text-[11px] text-ink3 hidden sm:inline">USD por 1M de tokens</span>
        <button
          type="button"
          onClick={() => void onRefresh()}
          disabled={refreshing}
          className="btn btn-ghost btn-sm ml-auto"
          title="Trae los precios actuales desde OpenRouter"
        >
          <RefreshCw className={"w-4 h-4 " + (refreshing ? "animate-spin" : "")} />
          <span className="hidden sm:inline">{refreshing ? "Actualizando…" : "Actualizar desde OpenRouter"}</span>
        </button>
      </div>

      {error && (
        <div className="px-4 py-2 text-xs text-state-bad bg-state-bad/10 border-b border-state-bad/30">
          {error}
        </div>
      )}
      {msg && !error && (
        <div className="px-4 py-2 text-xs text-state-ok bg-state-ok/10 border-b border-state-ok/20">{msg}</div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm min-w-[560px]">
          <thead className="text-[11px] uppercase tracking-wider text-ink3">
            <tr className="border-b border-line">
              <th className="text-left px-4 py-2 font-medium">Modelo</th>
              <th className="text-right px-4 py-2 font-medium">Entrada /1M</th>
              <th className="text-right px-4 py-2 font-medium">Salida /1M</th>
              <th className="text-left px-4 py-2 font-medium">Origen</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {loading && !prices ? (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-ink3 italic font-display">
                  Cargando…
                </td>
              </tr>
            ) : (prices ?? []).length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-ink3 italic font-display">
                  Sin precios. Pulsa "Actualizar desde OpenRouter".
                </td>
              </tr>
            ) : (
              (prices ?? []).map((p) => {
                const e = edit[p.model];
                const editing = e !== undefined;
                return (
                  <tr key={p.model} className="border-b border-line2 last:border-b-0">
                    <td className="px-4 py-2 font-mono text-xs">
                      {p.model}
                      {p.openrouter_id && (
                        <div className="text-[10px] text-ink4">{p.openrouter_id}</div>
                      )}
                    </td>
                    <td className="text-right px-4 py-2">
                      <input
                        className="input h-8 w-24 text-right font-mono text-xs"
                        type="number"
                        step="0.01"
                        min={0}
                        value={editing ? e.input : p.input_per_1m}
                        onChange={(ev) =>
                          setEdit((prev) => ({
                            ...prev,
                            [p.model]: {
                              input: ev.target.value,
                              output: (prev[p.model]?.output ?? String(p.output_per_1m)),
                            },
                          }))
                        }
                      />
                    </td>
                    <td className="text-right px-4 py-2">
                      <input
                        className="input h-8 w-24 text-right font-mono text-xs"
                        type="number"
                        step="0.01"
                        min={0}
                        value={editing ? e.output : p.output_per_1m}
                        onChange={(ev) =>
                          setEdit((prev) => ({
                            ...prev,
                            [p.model]: {
                              input: (prev[p.model]?.input ?? String(p.input_per_1m)),
                              output: ev.target.value,
                            },
                          }))
                        }
                      />
                    </td>
                    <td className="px-4 py-2">
                      <span
                        className={
                          "inline-block px-2 py-0.5 rounded-full text-[10px] font-medium " +
                          (SOURCE_STYLE[p.source] || "bg-paper2 text-ink3")
                        }
                      >
                        {SOURCE_LABEL[p.source] || p.source}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right">
                      {editing && (
                        <button
                          type="button"
                          onClick={() => void onSave(p.model)}
                          disabled={savingModel === p.model}
                          className="btn btn-primary btn-sm"
                        >
                          <Save className="w-3.5 h-3.5" />
                          {savingModel === p.model ? "…" : "Guardar"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <p className="px-4 py-2.5 text-[11px] text-ink4 italic font-display border-t border-line2">
        Se auto-actualizan a diario desde OpenRouter. Edita a mano un modelo que OpenRouter no tenga (quedará como "Manual" y no se sobrescribirá).
      </p>
    </div>
  );
}
