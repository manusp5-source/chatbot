import { useEffect, useState } from "react";
import { Save, Clock } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { getAppSettings, updateAppSettings } from "@/services/admin";
import { errorDetail } from "@/lib/errors";

/**
 * Ajustes generales de la app (tabla app_settings). Ajustes transversales
 * que no pertenecen a ningún agente: hoy la zona horaria del dashboard
 * (antes vivía dentro de Agentes → Modelo; se movió aquí el 15-jul).
 */
export default function SettingsPage() {
  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Ajustes"
        description="Ajustes generales de la aplicación. Afectan a todo el panel, no a un agente concreto."
      />
      <div className="flex-1 p-4 md:p-6 max-w-2xl space-y-4 overflow-auto">
        <TimezonePanel />
      </div>
    </div>
  );
}

/**
 * Zona horaria del negocio para el dashboard/Home ("hoy/ayer", buckets por
 * hora). Es un ajuste global (tabla app_settings), no parte de la config
 * versionada del agente, por eso carga y guarda por su cuenta.
 */
function TimezonePanel() {
  const [tz, setTz] = useState<string>("");
  const [options, setOptions] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    const s = await getAppSettings();
    setTz(s.dashboard_timezone);
    setOptions(s.timezone_options);
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function save() {
    if (!tz) return;
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const s = await updateAppSettings(tz);
      setTz(s.dashboard_timezone);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      // El "Guardado" verde vive dentro del try: si el guardado fallaba, el
      // desplegable se quedaba con la zona nueva en pantalla pero el
      // dashboard seguía calculando "hoy" con la vieja, y nada lo decía.
      setError(errorDetail(e, "No se pudo guardar la zona horaria. Vuelve a intentarlo."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card p-4">
      <label className="label flex items-center gap-1.5">
        <Clock className="w-3.5 h-3.5" /> Zona horaria del dashboard
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="input flex-1 min-w-[200px]"
          value={tz}
          onChange={(e) => setTz(e.target.value)}
          disabled={options.length === 0}
        >
          {/* Si la zona guardada no está en la lista común, la mostramos igual. */}
          {tz && !options.includes(tz) && <option value={tz}>{tz}</option>}
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        <button
          onClick={save}
          className="btn-primary shrink-0"
          disabled={saving || !tz}
        >
          <Save />
          {saving ? "Guardando…" : saved ? "Guardado" : "Guardar"}
        </button>
      </div>
      {error && (
        <div className="mt-2 text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2">
          {error}
        </div>
      )}
      <p className="text-[11px] text-ink3 mt-2">
        Fija la hora local del negocio para los cálculos de "hoy/ayer" y las
        franjas horarias del Inicio. Si no la cambias, se usa Europe/Madrid.
      </p>
    </div>
  );
}
