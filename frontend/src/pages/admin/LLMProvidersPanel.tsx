import { useProviderModels } from "@/hooks/useProviderModels";
import { useEffect, useState } from "react";
import { Plus, Trash2, Loader2, CheckCircle2, XCircle, Star, LifeBuoy } from "lucide-react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { errorDetail } from "@/lib/errors";
import {
  listLLMProviders,
  createLLMProvider,
  updateLLMProvider,
  deleteLLMProvider,
  testLLMProvider,
  type LLMProviderOut,
  type LLMProviderIn,
} from "@/services/admin";

// Proveedores que ofrecemos como base_url sugerida al crear. El usuario puede
// escribir cualquier otra.
//
// La base_url NO es solo una dirección: el backend deduce de su host con qué
// PROTOCOLO hablar (backend/app/providers/llm/families.py). Anthropic tiene API
// propia (cabecera x-api-key, endpoint /messages, system aparte, otro formato
// de herramientas) y por eso su URL debe quedarse como está; Gemini va por su
// capa compatible con OpenAI, que es la que soporta tools. Cambiar estas dos
// URLs a mano hace que el backend las trate como una pasarela genérica y
// dejarán de funcionar.
const KNOWN_BASE_URLS: { label: string; base_url: string; accepts_temperature: boolean }[] = [
  { label: "OpenAI", base_url: "", accepts_temperature: false },
  // Claude: los modelos de Opus 4.7 en adelante RECHAZAN temperature con un
  // 400, así que la casilla arranca desmarcada. El backend lo comprueba además
  // por nombre de modelo, para que un despiste no tire todas las respuestas.
  { label: "Anthropic · Claude", base_url: "https://api.anthropic.com/v1", accepts_temperature: false },
  {
    label: "Google · Gemini",
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
    accepts_temperature: true,
  },
  { label: "OpenRouter", base_url: "https://openrouter.ai/api/v1", accepts_temperature: true },
  { label: "DeepSeek", base_url: "https://api.deepseek.com", accepts_temperature: true },
  { label: "Z.AI · API normal", base_url: "https://api.z.ai/api/paas/v4", accepts_temperature: true },
  // Plan de coding (suscripción): mismo protocolo OpenAI-compatible pero SIN
  // GET /models (el botón Probar valida la clave con el fallback) y con cuota
  // de prompts por ventana de ~5h — configura el LLM fallback por si se agota.
  { label: "Z.AI · Coding Plan", base_url: "https://api.z.ai/api/coding/paas/v4", accepts_temperature: true },
  { label: "Together", base_url: "https://api.together.xyz/v1", accepts_temperature: true },
];

// Aviso de transferencia internacional (RGPD Cap. V) según el host de la
// base_url. No bloquea — solo informa de la implicación legal.
function transferWarning(baseUrl: string | null | undefined): string | null {
  const u = (baseUrl || "").toLowerCase();
  if (!u) return null; // OpenAI oficial (se avisa en su tarjeta si hace falta)
  if (u.includes("deepseek.com")) {
    return "Proveedor en China: sin marco de adecuación UE. Enviar conversaciones de clientes aquí es una transferencia de alto riesgo. Alternativa: usa el modelo DeepSeek vía OpenRouter (EE.UU., con DPA).";
  }
  if (u.includes("z.ai") || u.includes("zhipu") || u.includes("bigmodel.cn")) {
    return "Proveedor en China: sin marco de adecuación UE. Transferencia de alto riesgo con datos de clientes.";
  }
  if (
    u.includes("openrouter.ai") ||
    u.includes("openai.com") ||
    u.includes("together.xyz") ||
    u.includes("anthropic.com") ||
    u.includes("googleapis.com")
  ) {
    return "Proveedor en EE.UU.: usable si firmas su DPA (acuerdo de tratamiento). Consúltalo antes de mandar datos de clientes UE.";
  }
  return "Proveedor externo: confirma dónde se procesan los datos y que tienes un DPA firmado antes de usarlo con clientes UE.";
}

/**
 * Gestión de proveedores LLM (OpenAI + gateways compatibles). Cada agente puede
 * apuntar a uno; el modelo se escribe como texto libre en la ficha del agente.
 * Se monta como sección dentro de ConnectionsPage.
 */
export function LLMProvidersPanel() {
  const [items, setItems] = useState<LLMProviderOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<LLMProviderOut | "new" | null>(null);
  const [toDelete, setToDelete] = useState<LLMProviderOut | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [testing, setTesting] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      setItems(await listLLMProviders());
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar los proveedores."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function onTest(id: string) {
    setTesting(id);
    try {
      const r = await testLLMProvider(id);
      setTestResult((s) => ({ ...s, [id]: { ok: r.ok, message: r.message } }));
    } catch (e) {
      setTestResult((s) => ({ ...s, [id]: { ok: false, message: errorDetail(e, "Fallo al probar.") } }));
    } finally {
      setTesting(null);
    }
  }

  async function onDelete() {
    if (!toDelete) return;
    setDeleting(true);
    try {
      await deleteLLMProvider(toDelete.id);
      setToDelete(null);
      await refresh();
    } catch (e) {
      setError(errorDetail(e, "No se pudo borrar el proveedor."));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="font-display text-lg text-ink">Proveedores LLM</h3>
          <p className="text-xs text-ink3">
            OpenAI, Anthropic (Claude), Google (Gemini) y gateways compatibles (OpenRouter,
            DeepSeek, Z.AI…). Cada agente elige el suyo.
          </p>
        </div>
        <button className="btn-primary btn-sm" onClick={() => setEditing("new")}>
          <Plus className="w-4 h-4" /> Nuevo
        </button>
      </div>

      {error && (
        <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-ink3 text-sm inline-flex items-center gap-2 py-4">
          <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
        </div>
      ) : items.length === 0 ? (
        <div className="text-ink3 text-sm italic py-4">Aún no hay proveedores.</div>
      ) : (
        <ul className="space-y-2">
          {items.map((p) => (
            <li key={p.id} className="card p-3">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium text-sm text-ink">{p.name}</span>
                {p.is_default && (
                  <span className="pill bg-brand/15 text-brand-ink text-[10px] inline-flex items-center gap-1">
                    <Star className="w-3 h-3" /> Predeterminado
                  </span>
                )}
                {p.is_fallback && (
                  <span className="pill bg-state-warn/15 text-state-warn text-[10px] inline-flex items-center gap-1">
                    <LifeBuoy className="w-3 h-3" /> Respaldo{p.fallback_model ? ` · ${p.fallback_model}` : ""}
                  </span>
                )}
                {!p.accepts_temperature && (
                  <span className="pill bg-paper3 text-ink3 text-[10px]">reasoning (sin temperature)</span>
                )}
                <span className="text-[11px] text-ink3 font-mono ml-auto truncate max-w-[45%]">
                  {p.base_url || "api.openai.com"}
                </span>
              </div>
              <div className="flex items-center gap-2 flex-wrap mt-2 text-[11px]">
                <span className="text-ink3">
                  Clave: {p.has_key ? p.api_key_masked : <i>sin configurar</i>}
                </span>
                {!p.base_url && (
                  <span className="pill bg-paper3 text-ink3 text-[10px]">
                    clave compartida con embeddings, transcripción y moderación
                  </span>
                )}
                <div className="ml-auto flex items-center gap-1.5">
                  <button className="btn-ghost btn-sm" onClick={() => onTest(p.id)} disabled={testing === p.id}>
                    {testing === p.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "Probar"}
                  </button>
                  <button className="btn-ghost btn-sm" onClick={() => setEditing(p)}>
                    Editar
                  </button>
                  <button
                    className="btn-ghost btn-sm text-state-bad"
                    onClick={() => setToDelete(p)}
                    disabled={p.is_default}
                    title={p.is_default ? "Marca otro como predeterminado antes de borrar" : "Borrar"}
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
              {testResult[p.id] && (
                <div
                  className={`mt-2 text-[11px] inline-flex items-center gap-1 ${
                    testResult[p.id].ok ? "text-state-ok" : "text-state-bad"
                  }`}
                >
                  {testResult[p.id].ok ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                  {testResult[p.id].message}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {editing && (
        <ProviderForm
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
          }}
        />
      )}

      <ConfirmModal
        open={toDelete !== null}
        title={`¿Borrar el proveedor "${toDelete?.name ?? ""}"?`}
        description="Los agentes que lo usaban volverán al proveedor por defecto."
        confirmLabel="Borrar"
        tone="danger"
        busy={deleting}
        onConfirm={onDelete}
        onCancel={() => setToDelete(null)}
      />
    </div>
  );
}

function ProviderForm({
  initial,
  onClose,
  onSaved,
}: {
  initial: LLMProviderOut | null;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}) {
  const [form, setForm] = useState<LLMProviderIn>(
    initial
      ? {
          name: initial.name,
          base_url: initial.base_url,
          api_key: null, // null = conservar la existente
          accepts_temperature: initial.accepts_temperature,
          is_default: initial.is_default,
          is_fallback: initial.is_fallback,
          fallback_model: initial.fallback_model,
        }
      : {
          name: "",
          base_url: "",
          api_key: "",
          accepts_temperature: true,
          is_default: false,
          is_fallback: false,
          fallback_model: null,
        },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Modelos del PROPIO proveedor en edición, para sugerir el "modelo de
  // respaldo". Al crear uno nuevo aún no hay id → sin sugerencias hasta guardar.
  const { models: ownModels, loading: loadingOwnModels } = useProviderModels(initial?.id);

  function applyPreset(label: string) {
    const preset = KNOWN_BASE_URLS.find((k) => k.label === label);
    if (!preset) return;
    setForm((f) => ({
      ...f,
      name: f.name || preset.label,
      base_url: preset.base_url,
      accepts_temperature: preset.accepts_temperature,
    }));
  }

  async function onSubmit() {
    setBusy(true);
    setError(null);
    try {
      if (initial) await updateLLMProvider(initial.id, form);
      else await createLLMProvider(form);
      await onSaved();
    } catch (e) {
      setError(errorDetail(e, "No se pudo guardar el proveedor."));
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <div className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-lg max-h-[90vh] overflow-auto p-5 space-y-3">
        <h3 className="font-display text-lg text-ink">
          {initial ? "Editar proveedor" : "Nuevo proveedor LLM"}
        </h3>

        <div>
          <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
            Servicio
          </label>
          {/* Desplegable sincronizado con la Base URL: elegir un servicio la
              rellena; editar la URL a mano lo pone en "Personalizada". Útil
              p. ej. para alternar entre Z.AI API normal y su Coding Plan. */}
          <select
            className="input w-full"
            value={
              KNOWN_BASE_URLS.find((k) => (k.base_url || "") === (form.base_url || ""))?.label ??
              "__custom__"
            }
            onChange={(e) => {
              if (e.target.value !== "__custom__") applyPreset(e.target.value);
            }}
          >
            {KNOWN_BASE_URLS.map((k) => (
              <option key={k.label} value={k.label}>
                {k.label}
              </option>
            ))}
            <option value="__custom__">Personalizada (edita la URL abajo)</option>
          </select>
        </div>

        <div>
          <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
            Nombre
          </label>
          <input
            className="input w-full"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="p.ej. DeepSeek"
          />
        </div>

        <div>
          <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
            Base URL <span className="text-ink4 normal-case">(vacío = OpenAI)</span>
          </label>
          <input
            className="input w-full font-mono text-sm"
            value={form.base_url ?? ""}
            onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            placeholder="https://api.deepseek.com"
          />
          {transferWarning(form.base_url) && (
            <div className="mt-2 text-[11px] text-state-warn bg-state-warn/10 border border-state-warn/30 rounded-coro-sm px-2.5 py-1.5">
              {transferWarning(form.base_url)}
            </div>
          )}
        </div>

        <div>
          <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
            API key
          </label>
          <input
            type="password"
            autoComplete="new-password"
            className="input w-full font-mono text-sm"
            value={form.api_key ?? ""}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            placeholder={
              initial
                ? initial.has_key
                  ? "•••• (deja vacío para conservar la actual)"
                  : "Sin clave — usa openai_api_key"
                : "sk-…"
            }
          />
        </div>

        <label className="flex items-center gap-2 text-sm text-ink cursor-pointer">
          <input
            type="checkbox"
            checked={form.accepts_temperature}
            onChange={(e) => setForm({ ...form, accepts_temperature: e.target.checked })}
          />
          El modelo acepta <code className="text-xs">temperature</code>{" "}
          <span className="text-ink3 text-[11px]">
            (desmárcalo para los reasoning de OpenAI —gpt-5, o1, o3…— y para Claude de Opus 4.7 en
            adelante)
          </span>
        </label>

        <label className="flex items-center gap-2 text-sm text-ink cursor-pointer">
          <input
            type="checkbox"
            checked={form.is_default}
            onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
          />
          Proveedor por defecto <span className="text-ink3 text-[11px]">(para agentes sin proveedor asignado)</span>
        </label>

        <label className="flex items-center gap-2 text-sm text-ink cursor-pointer">
          <input
            type="checkbox"
            checked={form.is_fallback}
            onChange={(e) => setForm({ ...form, is_fallback: e.target.checked })}
          />
          Respaldo global{" "}
          <span className="text-ink3 text-[11px]">
            (si el proveedor principal de cualquier agente falla, se reintenta aquí)
          </span>
        </label>
        {form.is_fallback && (
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Modelo cuando actúa de respaldo
            </label>
            <input
              className="input w-full font-mono text-sm"
              list="provider-fallback-model-suggestions"
              value={form.fallback_model ?? ""}
              onChange={(e) => setForm({ ...form, fallback_model: e.target.value || null })}
              placeholder="deepseek/deepseek-v4-flash"
            />
            <datalist id="provider-fallback-model-suggestions">
              {ownModels.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
            <p className="text-[11px] text-ink3 mt-1">
              {loadingOwnModels
                ? "Cargando los modelos del proveedor…"
                : ownModels.length > 0
                  ? `${ownModels.length} modelos de este proveedor en las sugerencias.`
                  : initial
                    ? "Los ids del proveedor principal no existen aquí: indica el modelo de ESTE proveedor."
                    : "Guarda el proveedor y vuelve a editarlo para ver las sugerencias de modelos."}
            </p>
          </div>
        )}

        {error && <div className="text-xs text-state-bad">{error}</div>}

        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancelar
          </button>
          <button className="btn-primary" onClick={onSubmit} disabled={busy || !form.name.trim()}>
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : null} Guardar
          </button>
        </div>
      </div>
    </div>
  );
}
