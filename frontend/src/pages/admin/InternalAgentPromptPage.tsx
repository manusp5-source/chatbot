// System prompt del Agente Interno. Singleton: no hay versionado (a
// diferencia del PromptPage del bot publico). Solo guardar / restablecer.

import { useEffect, useState } from "react";
import { Save, RotateCcw, AlertCircle, Wrench } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { InternalAgentTabs } from "@/components/InternalAgentTabs";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  getInternalAgentConfig,
  getInternalAgentDefaultPrompt,
  updateInternalAgentConfig,
  type InternalAgentConfig,
} from "@/services/internalAgent";
import { errorDetail } from "@/lib/errors";

// Catalogo de las tools que el agente interno puede invocar. Lo
// hardcodeamos aqui para evitar un endpoint mas; si cambian en backend
// (tools.py), hay que actualizar esta lista.
const TOOLS: Array<{ name: string; descr: string }> = [
  { name: "get_dashboard_stats", descr: "Conteos por status / canal en hoy, 7d, 30d, 90d" },
  { name: "list_conversations", descr: "Lista filtrada por status, canal o contacto" },
  { name: "get_conversation_detail", descr: "Ultimos 20 mensajes + estado de una conversacion" },
  { name: "list_paused_channels", descr: "Estado de pausa global + por canal + tamano whitelist demo" },
  { name: "search_contacts", descr: "Busqueda LIKE en nombre, telefono o email" },
  { name: "get_agent_usage", descr: "Tokens y coste del bot publico en un rango" },
  { name: "get_handoff_summary", descr: "Conversaciones escaladas a humano + operador asignado" },
  { name: "get_message_volume", descr: "Mensajes agrupados por rol" },
];

export default function InternalAgentPromptPage() {
  const [active, setActive] = useState<InternalAgentConfig | null>(null);
  const [prompt, setPrompt] = useState("");
  const [defaultPrompt, setDefaultPrompt] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showResetConfirm, setShowResetConfirm] = useState(false);

  async function refresh() {
    try {
      const [c, d] = await Promise.all([
        getInternalAgentConfig(),
        getInternalAgentDefaultPrompt(),
      ]);
      setActive(c);
      setPrompt(c.system_prompt);
      setDefaultPrompt(d);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar la configuración del agente interno."));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function save() {
    if (!active) return;
    setSaving(true);
    setError(null);
    try {
      const next = await updateInternalAgentConfig({ system_prompt: prompt });
      setActive(next);
      setPrompt(next.system_prompt);
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 2000);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron guardar las instrucciones del agente interno."));
    } finally {
      setSaving(false);
    }
  }

  function loadDefault() {
    if (defaultPrompt) setPrompt(defaultPrompt);
    setShowResetConfirm(false);
  }

  const dirty = active && prompt !== active.system_prompt;

  if (!active) {
    return (
      <div className="p-10 text-center text-ink3 italic font-display">
        Cargando prompt...
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        eyebrow="Agente IA"
        title={
          <>
            Prompt del <span className="accent">agente interno</span>
          </>
        }
        description="System prompt del chat read-only que el equipo usa para consultar el estado del sistema. Singleton: cada guardado sobrescribe el activo (sin versionado). El audit log conserva los cambios."
      />
      <InternalAgentTabs />

      <div className="flex-1 p-4 md:p-6 grid grid-cols-1 lg:grid-cols-3 gap-4 overflow-hidden">
        {/* Editor */}
        <div className="lg:col-span-2 card p-4 md:p-5 flex flex-col">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span className="eyebrow">Editando</span>
              <span className="text-sm font-medium text-ink">
                singleton activo
              </span>
              {dirty && (
                <span className="text-[11px] px-2 py-0.5 rounded-coro-sm bg-state-warn/10 text-state-warn font-medium">
                  cambios sin guardar
                </span>
              )}
            </div>
            <div className="text-[11px] text-ink4 font-mono">
              {prompt.length} / 8000
            </div>
          </div>

          {error && (
            <div className="mb-2 p-2 rounded-coro-sm bg-state-bad/10 border border-state-bad/30 text-state-bad text-xs flex items-start gap-2">
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <textarea
            className="input flex-1 font-mono text-xs leading-relaxed resize-none min-h-[400px]"
            value={prompt}
            maxLength={8000}
            onChange={(e) => setPrompt(e.target.value)}
            spellCheck={false}
          />

          <div className="flex flex-col sm:flex-row gap-2 mt-3">
            <button
              type="button"
              onClick={save}
              disabled={saving || !dirty}
              className="btn-primary"
            >
              <Save />
              {saving ? "Guardando..." : "Guardar"}
            </button>
            <button
              type="button"
              onClick={() => setShowResetConfirm(true)}
              disabled={saving || !defaultPrompt || prompt === defaultPrompt}
              className="btn"
              title="Carga el prompt por defecto (todavia no guarda)"
            >
              <RotateCcw />
              Restablecer al default
            </button>
            {savedFlash && (
              <span className="self-center text-state-ok text-xs font-medium ml-2">
                Guardado
              </span>
            )}
          </div>

          <p className="text-[11px] text-ink4 italic font-display mt-3">
            Ultima modificacion:{" "}
            {new Date(active.updated_at).toLocaleString("es-ES")}
          </p>
        </div>

        {/* Tools que el agente puede usar */}
        <div className="card p-4 md:p-5 overflow-auto">
          <div className="flex items-center gap-2 mb-3">
            <Wrench className="w-4 h-4 text-ink2" />
            <h3 className="font-semibold text-sm text-ink">
              Tools disponibles
            </h3>
          </div>
          <p className="text-[11px] text-ink3 mb-3 leading-relaxed">
            El prompt puede mencionar estas tools por nombre. El LLM
            decide cual invocar segun la pregunta del admin.
          </p>
          <ul className="divide-y divide-line2">
            {TOOLS.map((t) => (
              <li key={t.name} className="py-2.5">
                <div className="font-mono text-[12px] text-ink font-medium">
                  {t.name}
                </div>
                <div className="text-[11px] text-ink3 mt-0.5 leading-snug">
                  {t.descr}
                </div>
              </li>
            ))}
          </ul>
          <p className="text-[10px] text-ink4 italic mt-4 font-display">
            Si anades una tool en el backend, recuerda actualizar esta
            lista (frontend/src/pages/admin/InternalAgentPromptPage.tsx).
          </p>
        </div>
      </div>

      <ConfirmModal
        open={showResetConfirm}
        title={<>Cargar prompt por defecto</>}
        description={
          <>
            Esto reemplazara el contenido del editor por el prompt de
            fabrica. <strong>NO se guarda</strong> hasta que pulses
            "Guardar". Tus cambios actuales se pierden de la pantalla.
          </>
        }
        confirmLabel="Si, cargar default"
        cancelLabel="Cancelar"
        onConfirm={loadDefault}
        onCancel={() => setShowResetConfirm(false)}
      />
    </div>
  );
}
