// Widget flotante del Agente Interno. Solo se monta para usuarios admin
// desde Layout.tsx. Persiste el historial en localStorage. Consulta el
// sistema (lectura) y puede PROPONER cambios en la base de conocimiento:
// la tarjeta Aplicar/Descartar de cada propuesta la resuelve el admin.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  BookOpen,
  Check,
  MessageCircle,
  Send,
  Trash2,
  X,
  AlertCircle,
  Loader2,
  Wrench,
} from "lucide-react";
import clsx from "clsx";

import {
  applyKBProposal,
  askInternalAgent,
  discardKBProposal,
  type InternalAgentEvent,
  type InternalAgentHistoryMsg,
  type KBProposalPayload,
} from "@/services/internalAgent";
import { errorDetail } from "@/lib/errors";
import { INTERNAL_AGENT_KEY } from "@/lib/session";

// Propuesta anclada a un mensaje del chat. El status vive aquí (y en
// localStorage vía el historial); el servidor valida igualmente al aplicar,
// así que un estado desincronizado solo produce un 409 legible.
type StoredProposal = KBProposalPayload & {
  status: "pendiente" | "aplicada" | "descartada";
  error?: string;
};

type ChatMsg = {
  id: string;
  role: "user" | "assistant" | "error";
  content: string;
  // Pasos de tool intermedios (para mostrar bajo la respuesta).
  toolSteps?: Array<{ name: string; ok: boolean; summary: string }>;
  proposals?: StoredProposal[];
};

// La clave vive en `lib/session` porque este historial puede contener datos de
// clientes y TIENE que borrarse al cerrar sesión (antes sobrevivía al logout:
// en un ordenador compartido lo veía el siguiente que entrase).
const STORAGE_KEY = INTERNAL_AGENT_KEY;
const MAX_HISTORY_FOR_API = 10; // turnos previos enviados al backend

function loadHistory(): ChatMsg[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (m) => m && typeof m.content === "string" && ["user", "assistant", "error"].includes(m.role)
    );
  } catch {
    return [];
  }
}

function saveHistory(msgs: ChatMsg[]) {
  try {
    // Cap a 60 mensajes para no engordar localStorage.
    localStorage.setItem(STORAGE_KEY, JSON.stringify(msgs.slice(-60)));
  } catch {
    // ignore quota
  }
}

function newId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export function InternalAgentWidget() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>(() => loadHistory());
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [statusLabel, setStatusLabel] = useState<string | null>(null);
  const [lastUsage, setLastUsage] = useState<{ in: number; out: number } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Persistir cada cambio.
  useEffect(() => {
    saveHistory(messages);
  }, [messages]);

  // Autoscroll al final.
  useEffect(() => {
    if (open && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [open, messages, statusLabel]);

  function clear() {
    if (sending) return;
    if (messages.length === 0) return;
    if (!confirm("Borrar el historial del agente interno?")) return;
    setMessages([]);
    setLastUsage(null);
    setStatusLabel(null);
  }

  function close() {
    abortRef.current?.abort();
    setOpen(false);
  }

  async function send() {
    const question = input.trim();
    if (!question || sending) return;
    setInput("");

    const userMsg: ChatMsg = { id: newId(), role: "user", content: question };
    // Placeholder mensaje del asistente (se ira rellenando).
    const assistantId = newId();
    const assistantMsg: ChatMsg = {
      id: assistantId,
      role: "assistant",
      content: "",
      toolSteps: [],
    };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setSending(true);
    setStatusLabel("Pensando...");

    const history: InternalAgentHistoryMsg[] = messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .filter((m) => m.content.trim())
      .slice(-MAX_HISTORY_FOR_API * 2)
      .map((m) => ({
        role: m.role as "user" | "assistant",
        content: m.content,
      }));

    const ac = new AbortController();
    abortRef.current = ac;

    try {
      await askInternalAgent({
        question,
        history,
        signal: ac.signal,
        onEvent: (ev: InternalAgentEvent) => handleEvent(ev, assistantId),
      });
    } catch (err) {
      const msg = (err as Error).message || "Error desconocido";
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? { ...m, role: "error", content: msg }
            : m
        )
      );
    } finally {
      setSending(false);
      setStatusLabel(null);
      abortRef.current = null;
    }
  }

  function handleEvent(ev: InternalAgentEvent, assistantId: string) {
    if (ev.event === "status") {
      setStatusLabel(ev.data.message);
      return;
    }
    if (ev.event === "tool_call") {
      setStatusLabel(`Consultando ${ev.data.name}...`);
      return;
    }
    if (ev.event === "tool_result") {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                toolSteps: [
                  ...(m.toolSteps || []),
                  { name: ev.data.name, ok: ev.data.ok, summary: ev.data.summary },
                ],
              }
            : m
        )
      );
      return;
    }
    if (ev.event === "proposal") {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                proposals: [
                  ...(m.proposals || []),
                  { ...ev.data, status: "pendiente" as const },
                ],
              }
            : m
        )
      );
      return;
    }
    if (ev.event === "message") {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, content: ev.data.content } : m
        )
      );
      return;
    }
    if (ev.event === "done") {
      setLastUsage({
        in: ev.data.usage.prompt_tokens || 0,
        out: ev.data.usage.completion_tokens || 0,
      });
      setStatusLabel(null);
      return;
    }
    if (ev.event === "error") {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? { ...m, role: "error", content: ev.data.message }
            : m
        )
      );
      setStatusLabel(null);
      return;
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  function patchProposal(proposalId: string, patch: Partial<StoredProposal>) {
    setMessages((prev) =>
      prev.map((m) =>
        m.proposals?.some((p) => p.proposal_id === proposalId)
          ? {
              ...m,
              proposals: m.proposals!.map((p) =>
                p.proposal_id === proposalId ? { ...p, ...patch } : p
              ),
            }
          : m
      )
    );
  }

  async function resolveProposal(proposalId: string, action: "apply" | "discard") {
    patchProposal(proposalId, { error: undefined });
    try {
      const out =
        action === "apply"
          ? await applyKBProposal(proposalId)
          : await discardKBProposal(proposalId);
      patchProposal(proposalId, { status: out.status });
    } catch (e) {
      patchProposal(proposalId, {
        error: errorDetail(e, "No se pudo resolver la propuesta."),
      });
    }
  }

  const totalTokens = useMemo(
    () => (lastUsage ? lastUsage.in + lastUsage.out : 0),
    [lastUsage]
  );

  return (
    <>
      {/* FAB */}
      {!open && (
        <button
          type="button"
          aria-label="Abrir agente interno"
          onClick={() => setOpen(true)}
          className="fixed bottom-6 right-6 z-40 inline-flex items-center justify-center w-12 h-12 rounded-full shadow-coro-2 transition-transform hover:scale-105"
          style={{ background: "var(--brand)", color: "var(--brand-on)" }}
        >
          <MessageCircle className="w-5 h-5" />
        </button>
      )}

      {/* Panel */}
      {open && (
        <div
          className="fixed bottom-6 right-6 z-50 flex flex-col bg-paper border border-line rounded-coro-md shadow-coro-2"
          style={{ width: "min(calc(100vw - 1.5rem), 380px)", height: "min(85vh, 600px)" }}
        >
          {/* Header */}
          <div className="flex items-center gap-2 px-3 py-2.5 border-b border-line">
            <div
              className="w-7 h-7 rounded-full flex items-center justify-center"
              style={{ background: "var(--brand)", color: "var(--brand-on)" }}
            >
              <Bot className="w-4 h-4" />
            </div>
            <div className="flex-1 min-w-0 leading-tight">
              <div className="text-sm font-semibold text-ink">Agente interno</div>
              <div className="text-[11px] text-ink3 truncate">
                Consulta el sistema · propone cambios de KB que tú apruebas
              </div>
            </div>
            <button
              type="button"
              aria-label="Borrar historial"
              onClick={clear}
              disabled={sending || messages.length === 0}
              className="inline-flex items-center justify-center w-8 h-8 rounded-coro-sm text-ink3 hover:bg-paper3 disabled:opacity-40"
            >
              <Trash2 className="w-4 h-4" />
            </button>
            <button
              type="button"
              aria-label="Cerrar"
              onClick={close}
              className="inline-flex items-center justify-center w-8 h-8 rounded-coro-sm text-ink2 hover:bg-paper3"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Mensajes */}
          <div
            ref={scrollRef}
            className="flex-1 overflow-auto px-3 py-3 bg-paper2 space-y-3"
          >
            {messages.length === 0 && (
              <div className="text-center text-ink3 text-xs py-8 px-2 font-display italic">
                Pregunta por conversaciones, contactos, canales pausados, uso
                del bot... o pídele que revise la base de conocimiento.
                <br />
                <br />
                <span className="not-italic font-normal text-ink4">
                  Ej: "¿cuantas conversaciones tuvimos hoy?", "revisa si hay
                  contradicciones en los documentos de envíos", "actualiza el
                  horario en la KB a 9-18h"
                </span>
              </div>
            )}
            {messages.map((m) => (
              <Bubble key={m.id} msg={m} onResolveProposal={resolveProposal} />
            ))}
            {statusLabel && sending && (
              <div className="flex items-center gap-2 text-[12px] text-ink3 italic">
                <Loader2 className="w-3 h-3 animate-spin" />
                {statusLabel}
              </div>
            )}
          </div>

          {/* Pie con uso */}
          {lastUsage && (
            <div className="px-3 py-1.5 border-t border-line text-[10px] text-ink4 flex items-center justify-between">
              <span>
                Tokens ultima: <strong>{totalTokens}</strong>
                <span className="ml-1 text-ink3">
                  ({lastUsage.in} in · {lastUsage.out} out)
                </span>
              </span>
              <span className="text-ink3">propone · no aplica</span>
            </div>
          )}

          {/* Input */}
          <div className="border-t border-line p-2 flex items-end gap-2">
            <textarea
              className="input flex-1 resize-none text-sm"
              rows={2}
              maxLength={2000}
              placeholder="Pregunta al agente interno..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={sending}
            />
            <button
              type="button"
              aria-label="Enviar"
              onClick={() => void send()}
              disabled={sending || !input.trim()}
              className="btn-primary shrink-0 disabled:opacity-50"
              style={{ padding: "0.5rem 0.75rem" }}
            >
              {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function ProposalCard({
  proposal,
  onResolve,
}: {
  proposal: StoredProposal;
  onResolve: (id: string, action: "apply" | "discard") => Promise<void>;
}) {
  const [busy, setBusy] = useState<"apply" | "discard" | null>(null);

  async function act(action: "apply" | "discard") {
    setBusy(action);
    try {
      await onResolve(proposal.proposal_id, action);
    } finally {
      setBusy(null);
    }
  }

  const headline =
    proposal.kind === "edit"
      ? `Editar: ${proposal.document_nombre || "documento"}`
      : `Nuevo documento: ${proposal.titulo || "(sin título)"}`;

  return (
    <div className="mt-2 border border-brand/40 rounded-coro-sm bg-paper overflow-hidden">
      <div className="px-2.5 py-1.5 bg-brand/10 flex items-center gap-1.5 text-[11px] font-medium text-ink">
        <BookOpen className="w-3.5 h-3.5 text-brand-ink shrink-0" />
        <span className="truncate flex-1">{headline}</span>
        {proposal.status === "aplicada" && (
          <span className="text-state-ok inline-flex items-center gap-1 shrink-0">
            <Check className="w-3 h-3" /> Aplicada
          </span>
        )}
        {proposal.status === "descartada" && (
          <span className="text-ink3 shrink-0">Descartada</span>
        )}
      </div>
      {proposal.motivo && (
        <div className="px-2.5 pt-1.5 text-[11px] text-ink3 italic">{proposal.motivo}</div>
      )}
      <div className="px-2.5 py-1.5 text-[11px] text-ink2 whitespace-pre-wrap break-words max-h-36 overflow-auto font-mono">
        {proposal.contenido}
      </div>
      {proposal.error && (
        <div className="px-2.5 pb-1.5 text-[11px] text-state-bad">{proposal.error}</div>
      )}
      {proposal.status === "pendiente" && (
        <div className="px-2.5 py-1.5 border-t border-line2 flex justify-end gap-2">
          <button
            type="button"
            className="btn-ghost text-xs"
            disabled={busy !== null}
            onClick={() => void act("discard")}
          >
            {busy === "discard" ? "Descartando…" : "Descartar"}
          </button>
          <button
            type="button"
            className="btn-primary text-xs"
            disabled={busy !== null}
            onClick={() => void act("apply")}
          >
            {busy === "apply" ? "Aplicando…" : "Aplicar"}
          </button>
        </div>
      )}
    </div>
  );
}

function Bubble({
  msg,
  onResolveProposal,
}: {
  msg: ChatMsg;
  onResolveProposal: (id: string, action: "apply" | "discard") => Promise<void>;
}) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div
          className="max-w-[85%] rounded-2xl px-3 py-2 text-sm shadow-sm"
          style={{ background: "var(--brand)", color: "var(--brand-on)" }}
        >
          <div className="whitespace-pre-wrap break-words">{msg.content}</div>
        </div>
      </div>
    );
  }
  if (msg.role === "error") {
    return (
      <div className="flex justify-start">
        <div className="max-w-[90%] rounded-2xl px-3 py-2 text-sm bg-state-bad/10 border border-state-bad/30 text-state-bad flex items-start gap-2">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <div className="whitespace-pre-wrap break-words">{msg.content}</div>
        </div>
      </div>
    );
  }
  // assistant
  return (
    <div className="flex justify-start">
      <div className="max-w-[90%] rounded-2xl px-3 py-2 text-sm bg-card border border-line text-ink shadow-sm">
        {(msg.toolSteps?.length ?? 0) > 0 && (
          <ul className="mb-2 text-[11px] text-ink3 space-y-0.5">
            {msg.toolSteps!.map((s, i) => (
              <li
                key={i}
                className={clsx("flex items-center gap-1.5", !s.ok && "text-state-warn")}
              >
                <Wrench className="w-3 h-3" />
                <span className="font-mono">{s.name}</span>
                <span className="opacity-70">· {s.summary}</span>
              </li>
            ))}
          </ul>
        )}
        {msg.content ? (
          <div className="whitespace-pre-wrap break-words">{msg.content}</div>
        ) : (
          <div className="text-ink3 italic text-xs">
            <Loader2 className="inline w-3 h-3 animate-spin mr-1" />
            generando respuesta...
          </div>
        )}
        {(msg.proposals || []).map((p) => (
          <ProposalCard key={p.proposal_id} proposal={p} onResolve={onResolveProposal} />
        ))}
      </div>
    </div>
  );
}
