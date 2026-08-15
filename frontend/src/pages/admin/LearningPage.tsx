import { useEffect, useState } from "react";
import {
  RefreshCw,
  Loader2,
  GraduationCap,
  Sparkles,
  UserRound,
  SearchX,
  Trash2,
  MessageSquareWarning,
  Wand2,
  BookOpen,
  Power,
  Repeat,
  History,
  Pencil,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  listGaps,
  approveGap,
  discardGap,
  listRules,
  deactivateRule,
  listCorrections,
  promoteCorrection,
  type KnowledgeGap,
  type LearnedRule,
  type CorrectionsResponse,
  type AgentCorrection,
} from "@/services/learning";
import { errorDetail } from "@/lib/errors";

const TRIGGER_LABEL: Record<KnowledgeGap["trigger"], { text: string; icon: typeof UserRound; cls: string }> = {
  handoff: {
    text: "Derivó a humano",
    icon: UserRound,
    cls: "bg-state-warn/10 text-state-warn",
  },
  kb_miss: {
    text: "Sin respuesta en la base",
    icon: SearchX,
    cls: "bg-brand/15 text-brand-ink",
  },
  correction: {
    text: "Corrección repetida",
    icon: MessageSquareWarning,
    cls: "bg-state-warn/10 text-state-warn",
  },
  faq: {
    text: "Pregunta frecuente",
    icon: Repeat,
    cls: "bg-brand/15 text-brand-ink",
  },
};

// Si proposal_kind es "style" → al aprobar se guarda una regla de estilo (se
// inyecta en el prompt). En cualquier otro caso → Q&A a la base de conocimiento.
function isStyle(gap: KnowledgeGap): boolean {
  return gap.proposal_kind === "style";
}

/**
 * Autoaprendizaje · Fase 2 — "Aprendizajes". Lista los huecos de conocimiento
 * pendientes (el agente no supo resolver o la operadora corrigió lo mismo varias
 * veces). La operadora revisa la propuesta y la aplica (Q&A a la base, o regla
 * de estilo en el prompt), o la descarta. Nada se aprende solo. Abajo, las
 * reglas de estilo activas, que se pueden desactivar.
 */
export default function LearningPage() {
  const [items, setItems] = useState<KnowledgeGap[]>([]);
  const [rules, setRules] = useState<LearnedRule[]>([]);
  const [corrections, setCorrections] = useState<CorrectionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  // Sin esto, un fallo de la API mostraba "Nada que aprender" sin ningún aviso.
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    try {
      const [gaps, activeRules, corr] = await Promise.all([
        listGaps("pendiente"),
        listRules(),
        listCorrections(50),
      ]);
      setItems(gaps);
      setRules(activeRules);
      setCorrections(corr);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar los aprendizajes."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  // Aviso de verificación tras aprobar (p.ej. el agente aún no recupera la Q&A).
  const [verifyWarning, setVerifyWarning] = useState<string | null>(null);

  function onResolved(id: string, warning?: string | null) {
    setItems((prev) => prev.filter((g) => g.id !== id));
    setVerifyWarning(warning || null);
    // Al aprobar una regla de estilo puede aparecer una nueva regla activa:
    // refrescamos la lista de reglas sin bloquear el resto de la UI.
    void listRules().then(setRules).catch(() => {});
  }

  function onRuleDeactivated(id: string) {
    setRules((prev) => prev.filter((r) => r.id !== id));
  }

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        eyebrow="Agente IA"
        title="Aprendizajes"
        description="Cuando el agente no supo responder (derivó a una persona o no encontró nada en la base), cuando corriges lo mismo varias veces, o cuando una pregunta se repite mucho entre los clientes, lo dejamos aquí. Revisa la propuesta y aplícala: una respuesta para la base de conocimiento o una regla de estilo para el tono del agente."
        actions={
          <button onClick={refresh} className="btn-ghost" disabled={loading}>
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            Refrescar
          </button>
        }
      />
      <div className="p-4 md:p-6 flex-1 overflow-auto max-w-3xl space-y-4">
        {error && (
          <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{error}</span>
            <button type="button" onClick={() => void refresh()} className="font-semibold hover:underline shrink-0">
              Reintentar
            </button>
          </div>
        )}
        {verifyWarning && (
          <div className="text-sm text-state-warn bg-state-warn/10 border border-state-warn/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{verifyWarning}</span>
            <button
              type="button"
              onClick={() => setVerifyWarning(null)}
              className="font-semibold hover:underline shrink-0"
            >
              Cerrar
            </button>
          </div>
        )}
        {loading ? (
          <div className="card text-center py-10 text-ink3 inline-flex items-center justify-center gap-2 w-full">
            <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
          </div>
        ) : items.length === 0 && !error ? (
          // Si la carga FALLÓ no mostramos el vacío celebratorio: sería mentira.
          <div className="card text-center py-12 text-ink3 italic font-display flex flex-col items-center justify-center gap-2">
            <Sparkles className="w-6 h-6 text-brand" />
            Nada que aprender por ahora ✨
          </div>
        ) : (
          items.map((gap) => <GapCard key={gap.id} gap={gap} onResolved={onResolved} />)
        )}

        {!loading && rules.length > 0 && (
          <RulesSection rules={rules} onDeactivated={onRuleDeactivated} />
        )}

        {!loading && corrections && (
          <CorrectionsSection data={corrections} />
        )}
      </div>
    </div>
  );
}

const CHANNEL_LABEL: Record<string, string> = {
  whatsapp: "WhatsApp",
  instagram_dm: "Instagram",
  web: "Web",
  email: "Email",
  retell_voice: "Voz",
};

function CorrectionsSection({ data }: { data: CorrectionsResponse }) {
  return (
    <div className="card p-5 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <History className="w-4 h-4 text-brand" />
        <h3 className="font-display text-ink">Correcciones recientes</h3>
        <span className="text-[11px] text-ink3 ml-auto">
          {data.total} registradas · {data.pending} por analizar
        </span>
      </div>
      <p className="text-[12px] text-ink3">
        Cada vez que corriges al agente (con una indicación o editando el borrador a mano)
        se guarda aquí. Cuando algo parecido se corrige <b>2 o más veces</b>, el sistema
        propone arriba una regla o respuesta para que la apruebes. Una corrección suelta no
        crea una regla por sí sola.
      </p>
      {data.items.length === 0 ? (
        <div className="text-sm text-ink3 italic">Aún no hay correcciones registradas.</div>
      ) : (
        <ul className="space-y-2">
          {data.items.map((c) => (
            <CorrectionRow key={c.id} c={c} />
          ))}
        </ul>
      )}
    </div>
  );
}

function CorrectionRow({ c }: { c: AgentCorrection }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"style" | "content">("style");
  // Prefill: la indicación si la hay; si fue edición a mano, el texto resultante.
  const [text, setText] = useState(c.instruction && !c.manual ? c.instruction : c.resulting_preview || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [promoted, setPromoted] = useState<"style" | "content" | null>(c.promoted_to);

  async function promote() {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await promoteCorrection(c.id, kind, text.trim());
      setPromoted(updated.promoted_to);
      setOpen(false);
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "No se pudo convertir.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="rounded-lg bg-paper2 px-3 py-2 space-y-1.5">
      <div className="flex items-center gap-2 flex-wrap text-[11px]">
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ${
            c.manual ? "bg-state-warn/15 text-state-warn" : "bg-brand/15 text-brand-ink"
          }`}
        >
          {c.manual ? <Pencil className="w-3 h-3" /> : <Wand2 className="w-3 h-3" />}
          {c.manual ? "Edición manual" : "Indicación"}
        </span>
        {c.canal && <span className="text-ink3">{CHANNEL_LABEL[c.canal] || c.canal}</span>}
        {promoted && (
          <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold bg-state-ok/15 text-state-ok">
            {promoted === "style" ? <Wand2 className="w-3 h-3" /> : <BookOpen className="w-3 h-3" />}
            {promoted === "style" ? "→ Prompt (regla)" : "→ Base de conocimiento"}
          </span>
        )}
        <span className="text-ink3 ml-auto">{new Date(c.created_at).toLocaleString("es-ES")}</span>
      </div>
      {!c.manual && c.instruction && <p className="text-sm text-ink">“{c.instruction}”</p>}
      {c.resulting_preview && (
        <p className="text-[12px] text-ink2">
          <span className="text-ink4">Resultado: </span>
          {c.resulting_preview}
        </p>
      )}

      {!promoted && !open && (
        <button
          className="btn-ghost btn-sm text-brand-ink"
          onClick={() => setOpen(true)}
          title="Convertir esta corrección en aprendizaje sin esperar a que se repita"
        >
          <GraduationCap className="w-3.5 h-3.5" /> Convertir en aprendizaje
        </button>
      )}

      {!promoted && open && (
        <div className="rounded-lg border border-line bg-card p-3 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-[12px]">
            <span className="text-ink3">¿Qué es?</span>
            <label className="inline-flex items-center gap-1 cursor-pointer">
              <input type="radio" checked={kind === "style"} onChange={() => setKind("style")} />
              <span>Tono → prompt</span>
            </label>
            <label className="inline-flex items-center gap-1 cursor-pointer">
              <input type="radio" checked={kind === "content"} onChange={() => setKind("content")} />
              <span>Dato → base de conocimiento</span>
            </label>
          </div>
          <textarea
            className="input min-h-[70px] text-sm"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={
              kind === "style"
                ? "Regla de tono, en imperativo. Ej.: Usa un tono cálido; no ofrezcas descuentos."
                : "El dato correcto. Ej.: El horario de atención es de 9:00 a 18:00."
            }
          />
          {error && <div className="text-state-bad text-xs">{error}</div>}
          <div className="flex justify-end gap-2">
            <button className="btn-ghost btn-sm" onClick={() => setOpen(false)} disabled={busy}>
              Cancelar
            </button>
            <button className="btn-primary btn-sm" onClick={promote} disabled={busy || !text.trim()}>
              {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <GraduationCap className="w-3.5 h-3.5" />}
              Aplicar aprendizaje
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

function GapCard({
  gap,
  onResolved,
}: {
  gap: KnowledgeGap;
  onResolved: (id: string, warning?: string | null) => void;
}) {
  const [answer, setAnswer] = useState(gap.suggested_answer || "");
  const [busy, setBusy] = useState<"approve" | "discard" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const trigger = TRIGGER_LABEL[gap.trigger];
  const TriggerIcon = trigger?.icon ?? GraduationCap;
  const style = isStyle(gap);

  async function approve() {
    if (!answer.trim()) return;
    setBusy("approve");
    setError(null);
    try {
      const result = await approveGap(gap.id, answer.trim());
      onResolved(gap.id, result.verification_warning);
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "No se pudo aplicar la propuesta.");
      setBusy(null);
    }
  }

  async function discard() {
    setConfirmDiscard(false);
    setBusy("discard");
    setError(null);
    try {
      await discardGap(gap.id);
      onResolved(gap.id);
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "No se pudo descartar.");
      setBusy(null);
    }
  }

  return (
    <div className="card p-5 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold ${
            trigger?.cls ?? "bg-paper3 text-ink2"
          }`}
        >
          <TriggerIcon className="w-3.5 h-3.5" />
          {trigger?.text ?? gap.trigger}
        </span>
        {/* Indica qué hace "Aprobar": regla de estilo (prompt) vs Q&A (base). */}
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold ${
            style ? "bg-brand/15 text-brand-ink" : "bg-paper3 text-ink2"
          }`}
          title={
            style
              ? "Al aprobar se guarda como regla de estilo y se aplica al tono del agente."
              : "Al aprobar se añade como pregunta/respuesta a la base de conocimiento."
          }
        >
          {style ? <Wand2 className="w-3.5 h-3.5" /> : <BookOpen className="w-3.5 h-3.5" />}
          {style ? "Regla de estilo" : "Q&A · Base de conocimiento"}
        </span>
        <span className="text-[11px] text-ink3 ml-auto">
          {new Date(gap.created_at).toLocaleString("es-ES")}
        </span>
      </div>

      <div>
        <div className="eyebrow mb-1">{style ? "Qué se corrige" : "Pregunta sin responder"}</div>
        <p className="text-sm text-ink whitespace-pre-wrap">{gap.question}</p>
      </div>

      <div>
        <label className="label">
          {style ? "Regla de estilo para el agente" : "Respuesta para la base de conocimiento"}
        </label>
        <textarea
          className="input min-h-[100px]"
          placeholder={
            style
              ? "Ej.: Usa un tono cálido y cercano. No ofrezcas descuentos."
              : "Escribe la respuesta correcta…"
          }
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
        />
      </div>

      {error && <div className="text-state-bad text-sm">{error}</div>}

      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-end gap-2">
        <ConfirmModal
          open={confirmDiscard}
          title="¿Descartar esta propuesta?"
          description="La propuesta desaparecerá de Aprendizajes y el agente no la incorporará."
          confirmLabel="Descartar"
          tone="danger"
          busy={busy === "discard"}
          onConfirm={discard}
          onCancel={() => setConfirmDiscard(false)}
        />
        <button
          className="btn-ghost text-state-bad"
          onClick={() => setConfirmDiscard(true)}
          disabled={busy !== null}
        >
          {busy === "discard" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
          Descartar
        </button>
        <button className="btn-primary" onClick={approve} disabled={busy !== null || !answer.trim()}>
          {busy === "approve" ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : style ? (
            <Wand2 className="w-4 h-4" />
          ) : (
            <GraduationCap className="w-4 h-4" />
          )}
          {style ? "Guardar regla de estilo" : "Añadir a la base de conocimiento"}
        </button>
      </div>
    </div>
  );
}

function RulesSection({
  rules,
  onDeactivated,
}: {
  rules: LearnedRule[];
  onDeactivated: (id: string) => void;
}) {
  return (
    <div className="card p-5 space-y-3">
      <div className="flex items-center gap-2">
        <Wand2 className="w-4 h-4 text-brand" />
        <h3 className="font-display text-ink">Reglas de estilo activas</h3>
        <span className="text-[11px] text-ink3 ml-auto">
          Se aplican al tono del agente en cada respuesta.
        </span>
      </div>
      <ul className="space-y-2">
        {rules.map((rule) => (
          <RuleRow key={rule.id} rule={rule} onDeactivated={onDeactivated} />
        ))}
      </ul>
    </div>
  );
}

function RuleRow({
  rule,
  onDeactivated,
}: {
  rule: LearnedRule;
  onDeactivated: (id: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  async function deactivate() {
    setConfirmOpen(false);
    setBusy(true);
    try {
      await deactivateRule(rule.id);
      onDeactivated(rule.id);
    } catch {
      setBusy(false);
    }
  }

  return (
    <li className="flex items-start gap-3 rounded-lg bg-paper2 px-3 py-2">
      <ConfirmModal
        open={confirmOpen}
        title="¿Desactivar esta regla?"
        description="El agente dejará de aplicarla inmediatamente."
        confirmLabel="Desactivar"
        tone="danger"
        busy={busy}
        onConfirm={deactivate}
        onCancel={() => setConfirmOpen(false)}
      />
      <p className="text-sm text-ink whitespace-pre-wrap flex-1">{rule.text}</p>
      <button
        className="btn-ghost text-state-bad shrink-0"
        onClick={() => setConfirmOpen(true)}
        disabled={busy}
        title="Desactivar: deja de aplicarse al agente"
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Power className="w-4 h-4" />}
        Desactivar
      </button>
    </li>
  );
}
