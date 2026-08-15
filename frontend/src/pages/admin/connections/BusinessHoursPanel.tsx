import { useEffect, useState } from "react";
import { Check, Loader2, Save } from "lucide-react";
import {
  getCalendarSettings,
  updateCalendarSettings,
  type CalendarSettings,
} from "@/services/admin";
import { errorDetail } from "@/lib/errors";

const DIAS = [
  { iso: 1, label: "Lunes" },
  { iso: 2, label: "Martes" },
  { iso: 3, label: "Miércoles" },
  { iso: 4, label: "Jueves" },
  { iso: 5, label: "Viernes" },
  { iso: 6, label: "Sábado" },
  { iso: 7, label: "Domingo" },
] as const;

/**
 * Horario de atención: zona horaria, días laborables, tramos, festivos y las
 * dos reglas de las citas (cada cuánto se ofrece hueco y con cuánta antelación
 * mínima). Es lo que el agente mira para decir si estáis abiertos y para
 * proponer citas.
 *
 * Se enseña en Agentes → Horarios. Estaba en Conexiones, donde parecía duplicar
 * lo de Google Calendar, y no es lo mismo: Calendar dice qué está OCUPADO y
 * esto dice cuándo se abre (sin ello el agente ofrecería cita a las tres de la
 * mañana, porque para Calendar esa hora está libre). El fichero se queda en
 * `connections/` por historia; no cambia nada de su comportamiento.
 */
export function BusinessHoursPanel() {
  const [data, setData] = useState<CalendarSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Los tramos y los festivos se editan como texto (una línea cada uno): es la
  // forma más directa de meter jornada partida sin inventar un editor entero.
  const [horas, setHoras] = useState("");
  const [festivos, setFestivos] = useState("");

  async function refresh() {
    setLoading(true);
    try {
      const d = await getCalendarSettings();
      setData(d);
      setHoras(d.work_hours.join("\n"));
      setFestivos(d.holidays.join("\n"));
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar el horario."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  function toggleDia(iso: number) {
    setData((d) =>
      d
        ? {
            ...d,
            work_days: d.work_days.includes(iso)
              ? d.work_days.filter((x) => x !== iso)
              : [...d.work_days, iso].sort((a, b) => a - b),
          }
        : d,
    );
  }

  async function save() {
    if (!data) return;
    setBusy(true);
    setError(null);
    try {
      const out = await updateCalendarSettings({
        timezone: data.timezone,
        work_days: data.work_days,
        work_hours: horas.split("\n").map((s) => s.trim()).filter(Boolean),
        holidays: festivos.split("\n").map((s) => s.trim()).filter(Boolean),
        slot_step_min: data.slot_step_min,
        min_notice_min: data.min_notice_min,
      });
      setData(out);
      setHoras(out.work_hours.join("\n"));
      setFestivos(out.holidays.join("\n"));
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      setError(errorDetail(e, "No se pudo guardar el horario."));
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-ink3 py-10 justify-center">
        <Loader2 className="w-4 h-4 animate-spin" /> Cargando horario…
      </div>
    );
  }
  if (!data) {
    return (
      <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2 max-w-4xl">
        <span className="flex-1">{error || "No se pudo cargar el horario."}</span>
        <button
          type="button"
          onClick={() => void refresh()}
          className="font-semibold hover:underline shrink-0"
        >
          Reintentar
        </button>
      </div>
    );
  }

  return (
    <div className="max-w-4xl space-y-4">
      <div>
        <h3 className="font-display text-lg text-ink mb-1">Horario de atención</h3>
        <p className="text-xs text-ink3">
          Lo que el agente usa para saber si estáis abiertos y para proponer citas en el calendario.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="border border-line rounded-coro bg-card p-4 space-y-3">
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">
              Zona horaria
            </label>
            <select
              value={data.timezone}
              onChange={(e) => setData({ ...data, timezone: e.target.value })}
              className="input w-full text-xs"
            >
              {data.timezone_options.map((tz) => (
                <option key={tz} value={tz}>
                  {tz}
                </option>
              ))}
            </select>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1.5">
              Días laborables
            </div>
            <div className="flex flex-wrap gap-1.5">
              {DIAS.map((d) => {
                const on = data.work_days.includes(d.iso);
                return (
                  <button
                    key={d.iso}
                    type="button"
                    onClick={() => toggleDia(d.iso)}
                    aria-pressed={on}
                    className={`pill transition ${
                      on
                        ? "bg-state-ok/10 text-state-ok hover:bg-state-ok/20"
                        : "bg-paper3 text-ink3 hover:bg-paper2"
                    }`}
                  >
                    {d.label}
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">
              Tramos horarios
            </label>
            <textarea
              rows={3}
              value={horas}
              onChange={(e) => setHoras(e.target.value)}
              className="input w-full font-mono text-xs"
              placeholder={"09:00-14:00\n16:00-19:00"}
            />
            <p className="text-[10px] text-ink3 mt-1">
              Uno por línea, en formato HH:MM-HH:MM. Dos líneas = jornada partida.
            </p>
          </div>
        </div>

        <div className="border border-line rounded-coro bg-card p-4 space-y-3">
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">
              Festivos
            </label>
            <textarea
              rows={5}
              value={festivos}
              onChange={(e) => setFestivos(e.target.value)}
              className="input w-full font-mono text-xs"
              placeholder={"2026-12-25\n2027-01-01"}
            />
            <p className="text-[10px] text-ink3 mt-1">
              Uno por línea, en formato AAAA-MM-DD. Cerrado aunque caiga en día laborable.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">
                Hueco cada (min)
              </label>
              <input
                type="number"
                min={5}
                max={240}
                value={data.slot_step_min}
                onChange={(e) => setData({ ...data, slot_step_min: Number(e.target.value) })}
                className="input w-full text-xs numbers"
              />
            </div>
            <div>
              <label className="block text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">
                Antelación mínima (min)
              </label>
              <input
                type="number"
                min={0}
                max={10080}
                value={data.min_notice_min}
                onChange={(e) => setData({ ...data, min_notice_min: Number(e.target.value) })}
                className="input w-full text-xs numbers"
              />
            </div>
          </div>
          <p className="text-[10px] text-ink3">
            La antelación mínima evita que alguien coja una cita para dentro de cinco minutos.
          </p>
        </div>
      </div>

      {error && <div className="text-state-bad text-xs">{error}</div>}
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="btn-primary inline-flex items-center gap-1.5"
        >
          {busy ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : saved ? (
            <Check className="w-4 h-4" />
          ) : (
            <Save className="w-4 h-4" />
          )}
          {saved ? "Guardado" : "Guardar horario"}
        </button>
      </div>
    </div>
  );
}
