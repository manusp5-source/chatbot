import { useEffect, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import {
  createClassifierRule,
  deleteClassifierRule,
  listClassifierRules,
  updateClassifierRule,
  type ClassifierRule,
} from "@/services/admin";
import { errorDetail } from "@/lib/errors";

const CAMPOS = [
  { value: "remitente", label: "Remitente" },
  { value: "dominio", label: "Dominio" },
  { value: "asunto", label: "Asunto" },
] as const;

const CANALES = [
  { value: "", label: "Todos los canales" },
  { value: "email", label: "Email" },
  { value: "whatsapp", label: "WhatsApp" },
  { value: "instagram_dm", label: "Instagram" },
  { value: "web", label: "Web" },
];

const AYUDA: Record<string, string> = {
  remitente: "La dirección exacta. Ej: promociones@agencia.com",
  dominio: "Todo lo que llegue de ese dominio y sus subdominios. Ej: agencia.com",
  asunto: "Si el asunto contiene ese texto. Solo email. Ej: oferta",
};

/**
 * Reglas duras: lo que en Gmail sería un filtro. A diferencia de las
 * instrucciones del clasificador, esto NO lo interpreta un modelo — se compara
 * literal, antes de gastar un solo token, y siempre da el mismo resultado.
 *
 * Cada regla se guarda al momento (no espera al botón Guardar de la página):
 * es una lista, no un formulario.
 */
export function RulesCard() {
  const [rules, setRules] = useState<ClassifierRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [campo, setCampo] = useState<string>("dominio");
  const [valor, setValor] = useState("");
  const [canal, setCanal] = useState("");
  const [nota, setNota] = useState("");

  async function refresh() {
    setLoading(true);
    try {
      setRules(await listClassifierRules());
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar las reglas."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function add() {
    if (!valor.trim()) return;
    setBusy(true);
    setError(null);
    try {
      // Una regla de asunto fuera de email no filtraría nunca nada: el backend
      // la rechaza, así que aquí se manda ya atada al canal correcto.
      const canalFinal = campo === "asunto" ? "email" : canal || null;
      const nueva = await createClassifierRule({
        campo,
        valor: valor.trim(),
        canal: canalFinal,
        nota: nota.trim() || null,
      });
      setRules([nueva, ...rules]);
      setValor("");
      setNota("");
    } catch (e) {
      setError(errorDetail(e, "No se pudo añadir la regla."));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(rule: ClassifierRule) {
    try {
      const out = await updateClassifierRule(rule.id, { enabled: !rule.enabled });
      setRules(rules.map((r) => (r.id === out.id ? out : r)));
    } catch (e) {
      setError(errorDetail(e, "No se pudo cambiar la regla."));
    }
  }

  async function remove(rule: ClassifierRule) {
    if (!window.confirm(`¿Borrar la regla "${rule.valor}"?`)) return;
    try {
      await deleteClassifierRule(rule.id);
      setRules(rules.filter((r) => r.id !== rule.id));
    } catch (e) {
      setError(errorDetail(e, "No se pudo borrar la regla."));
    }
  }

  return (
    <div className="card p-5">
      <div className="eyebrow mb-2">Reglas fijas (sin IA)</div>
      <p className="text-[11px] text-ink3 mb-3">
        Como los filtros de Gmail: lo que encaje se descarta al momento, sin pasar por el
        modelo ni generar borrador. No interpretan nada, así que aquí no hay sorpresas.
        Funcionan aunque el clasificador con IA esté desactivado.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-start">
        <select
          className="input sm:col-span-2"
          value={campo}
          onChange={(e) => setCampo(e.target.value)}
        >
          {CAMPOS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <input
          className="input sm:col-span-3"
          value={valor}
          placeholder={campo === "asunto" ? "texto del asunto" : "agencia.com"}
          onChange={(e) => setValor(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void add();
          }}
        />
        <select
          className="input sm:col-span-3"
          value={campo === "asunto" ? "email" : canal}
          disabled={campo === "asunto"}
          onChange={(e) => setCanal(e.target.value)}
        >
          {CANALES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <input
          className="input sm:col-span-3"
          value={nota}
          placeholder="nota (opcional)"
          onChange={(e) => setNota(e.target.value)}
        />
        <button
          type="button"
          className="btn-primary sm:col-span-1 justify-center"
          onClick={() => void add()}
          disabled={busy || !valor.trim()}
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
        </button>
      </div>
      <p className="text-[11px] text-ink3 mt-1.5">{AYUDA[campo]}</p>

      {error && (
        <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 mt-3">
          {error}
        </div>
      )}

      <div className="mt-4">
        {loading ? (
          <div className="flex items-center gap-2 text-ink3 text-sm py-4 justify-center">
            <Loader2 className="w-4 h-4 animate-spin" /> Cargando reglas…
          </div>
        ) : rules.length === 0 ? (
          <p className="text-xs text-ink3 py-3">
            Todavía no hay ninguna regla. El buzón funciona igual que hasta ahora.
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {rules.map((r) => (
              <li key={r.id} className="flex items-center gap-3 py-2 text-sm">
                <input
                  type="checkbox"
                  className="accent-brand"
                  checked={r.enabled}
                  onChange={() => void toggle(r)}
                  title={r.enabled ? "Activa" : "Desactivada"}
                />
                <span className="text-[10px] uppercase tracking-wide text-ink3 w-20 shrink-0">
                  {CAMPOS.find((c) => c.value === r.campo)?.label ?? r.campo}
                </span>
                {/* La regla se lee entera: lo que se recorta si falta sitio es
                    la nota, no el valor. */}
                <span
                  className={`font-mono flex-1 min-w-0 truncate ${
                    r.enabled ? "text-ink" : "text-ink3 line-through"
                  }`}
                  title={r.valor}
                >
                  {r.valor}
                </span>
                {r.nota && (
                  <span className="text-xs text-ink3 truncate max-w-[25%] hidden md:inline" title={r.nota}>
                    · {r.nota}
                  </span>
                )}
                {/* Sin `ml-auto`: ese margen se comía todo el hueco libre y
                    dejaba el valor de la regla con puntos suspensivos. */}
                <span className="text-xs text-ink3 shrink-0">
                  {CANALES.find((c) => c.value === (r.canal ?? ""))?.label}
                  {" · "}
                  {r.hits === 0 ? "sin usos" : `${r.hits} ${r.hits === 1 ? "vez" : "veces"}`}
                </span>
                <button
                  type="button"
                  className="text-ink3 hover:text-state-bad shrink-0"
                  onClick={() => void remove(r)}
                  title="Borrar"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
