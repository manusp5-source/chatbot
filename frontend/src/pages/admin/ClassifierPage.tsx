import { useProviderModels } from "@/hooks/useProviderModels";
import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import {
  getClassifierConfig,
  updateClassifierConfig,
  listLLMProviders,
  type ClassifierConfig,
  type LLMProviderOut,
} from "@/services/admin";
import { RulesCard } from "./classifier/RulesCard";

const CHANNELS = [
  { value: "instagram_dm", label: "Instagram" },
  { value: "whatsapp", label: "WhatsApp" },
  { value: "web", label: "Web" },
  { value: "email", label: "Email" },
];

/**
 * Panel del Agente clasificador. Vive como pestaña dentro de "Agentes": el
 * clasificador es, conceptualmente, un agente más — el que filtra spam antes
 * de que respondan los demás. Por eso no lleva su propio PageHeader.
 */
export function ClassifierPanel() {
  const [cfg, setCfg] = useState<ClassifierConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [providers, setProviders] = useState<LLMProviderOut[]>([]);
  const { models: providerModels, loading: loadingModels } = useProviderModels(cfg?.llm_provider_id);

  useEffect(() => {
    getClassifierConfig()
      .then(setCfg)
      .catch(() => setMsg({ ok: false, text: "No se pudo cargar la configuración." }));
    listLLMProviders().then(setProviders).catch(() => setProviders([]));
  }, []);

  function toggleChannel(v: string) {
    if (!cfg) return;
    const has = cfg.channels.includes(v);
    setCfg({ ...cfg, channels: has ? cfg.channels.filter((c) => c !== v) : [...cfg.channels, v] });
  }

  async function save() {
    if (!cfg) return;
    setSaving(true);
    setMsg(null);
    try {
      const next = await updateClassifierConfig(cfg);
      setCfg(next);
      setMsg({ ok: true, text: "Guardado." });
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setMsg({ ok: false, text: ax.response?.data?.detail || "Error al guardar." });
    } finally {
      setSaving(false);
    }
  }

  if (!cfg) {
    return <div className="p-10 text-center text-ink3 italic font-display">Cargando…</div>;
  }

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6 max-w-4xl space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <p className="text-sm text-ink3 max-w-xl">
          Filtra mensajes no deseados (spam, vendedores) <b className="text-ink2">antes</b> de que el bot
          conteste. Las conversaciones marcadas se retienen para tu revisión.
        </p>
        <button className="btn-primary shrink-0" onClick={save} disabled={saving}>
          <Save /> {saving ? "Guardando…" : "Guardar"}
        </button>
      </div>

      <div className="card p-5">
        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            checked={cfg.enabled}
            onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
            className="accent-brand w-4 h-4"
          />
          <div>
            <div className="font-semibold text-ink">Clasificador activo</div>
            <div className="text-xs text-ink3">
              Si está activo, los mensajes de los canales seleccionados se clasifican antes de llegar al bot.
            </div>
          </div>
        </label>
        {cfg.enabled && (
          <div className="mt-3 text-[11px] text-state-warn bg-state-warn/10 rounded-coro-sm p-2.5">
            ⚠️ Las conversaciones marcadas como spam se retienen y el bot no las responde. Las tienes en la
            pestaña "Descartados" del inbox (ábrelas y libéralas con "No es spam" si fuera un cliente real).
          </div>
        )}
      </div>

      <RulesCard />

      <div className="card p-5">
        <div className="eyebrow mb-2">Qué hacer en Gmail con lo descartado</div>
        <p className="text-[11px] text-ink3 mb-3">
          Además de retenerlo en "Descartados", el correo se puede sacar de Recibidos y
          etiquetar en tu buzón, como haría un filtro de Gmail. No se borra ni se marca como
          spam: se queda en la etiqueta y liberarlo aquí lo devuelve a Recibidos.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="label">Qué se archiva</label>
            <select
              className="input w-full"
              value={cfg.gmail_action}
              onChange={(e) =>
                setCfg({ ...cfg, gmail_action: e.target.value as ClassifierConfig["gmail_action"] })
              }
            >
              <option value="rules">Solo lo que cazan las reglas fijas</option>
              <option value="all">Todo lo descartado, incluido lo que decide la IA</option>
              <option value="off">Nada: no tocar Gmail</option>
            </select>
          </div>
          <div>
            <label className="label">Etiqueta</label>
            <input
              className="input w-full"
              value={cfg.gmail_label}
              onChange={(e) => setCfg({ ...cfg, gmail_label: e.target.value })}
              placeholder="Descartado por el bot"
            />
          </div>
        </div>
        {cfg.gmail_action === "all" && (
          <div className="mt-3 text-[11px] text-state-warn bg-state-warn/10 rounded-coro-sm p-2.5">
            Con esta opción, un error del clasificador saca de Recibidos un correo real. La
            etiqueta lo conserva, pero no lo verás en la bandeja de Gmail.
          </div>
        )}
      </div>

      <div className="card p-5">
        <div className="eyebrow mb-3">Canales donde aplica</div>
        <div className="space-y-2">
          {CHANNELS.map((c) => (
            <label key={c.value} className="flex items-center gap-2 cursor-pointer text-sm text-ink2">
              <input
                type="checkbox"
                checked={cfg.channels.includes(c.value)}
                onChange={() => toggleChannel(c.value)}
                className="accent-brand"
              />
              {c.label}
            </label>
          ))}
        </div>
      </div>

      <div className="card p-5">
        <div className="eyebrow mb-2">Qué considerar no deseado</div>
        <textarea
          className="input min-h-[170px]"
          value={cfg.instructions}
          onChange={(e) => setCfg({ ...cfg, instructions: e.target.value })}
        />
        <p className="text-[11px] text-ink3 mt-2">
          Sé conservador: ante la duda, que NO marque spam (mejor un falso negativo que ignorar a un cliente real).
        </p>
      </div>

      <div className="card p-5 grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="label">Proveedor LLM</label>
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
        </div>
        <div>
          <label className="label">Modelo</label>
          <input
            className="input w-full font-mono text-sm"
            list="classifier-model-suggestions"
            value={cfg.model_name}
            onChange={(e) => setCfg({ ...cfg, model_name: e.target.value })}
            placeholder="gpt-5.4-mini"
          />
          <datalist id="classifier-model-suggestions">
            {(providerModels.length > 0
              ? providerModels
              : ["gpt-5.4-mini", "gpt-5.4-nano"]
            ).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
          {loadingModels && (
            <p className="text-[11px] text-ink3 mt-1">Cargando los modelos del proveedor…</p>
          )}
        </div>
        <p className="text-[11px] text-ink3 sm:col-span-2">
          Un modelo barato (p.ej. DeepSeek) es ideal para el clasificador. Gestiona proveedores en Conexiones.
        </p>
      </div>

      {msg && <div className={msg.ok ? "text-state-ok text-sm" : "text-state-bad text-sm"}>{msg.text}</div>}
    </div>
  );
}
