import { ConfirmModal } from "@/components/ConfirmModal";
import { useEffect, useRef, useState } from "react";
import { Pencil, Send, Trash2, Wand2 } from "lucide-react";
import type { Message } from "@/types";
import { discardDraft, refineDraft, sendDraft } from "@/services/conversations";

/**
 * Tarjeta "Borrador / sugerencia del agente". Cubre DOS casos con la misma UI:
 *
 *   - **Email (F5c):** el agente nunca envía un correo; genera un borrador real
 *     en Gmail para revisión humana.
 *   - **Entrenamiento (sombra):** en un canal NO email puesto en Entrenamiento,
 *     el agente genera una sugerencia (no envía). `message.training` la marca.
 *
 * En ambos, la tarjeta muestra el texto en SOLO LECTURA y ofrece tres acciones:
 *   - Descartar: la elimina (en email, también el borrador de Gmail).
 *   - Editar:    activa la edición en un textarea (toggle).
 *   - Enviar:    manda la respuesta con el texto final (editado o no). El backend
 *                enruta por Gmail (email) o por el canal (WhatsApp/IG/Web).
 * Visualmente se distingue como un borrador NO enviado (ámbar + badge).
 */
export function DraftCard({
  message,
  onSent,
  onDiscarded,
}: {
  message: Message;
  onSent: () => void;
  onDiscarded: () => void;
}) {
  const [text, setText] = useState(message.contenido || "");
  const [editing, setEditing] = useState(false);
  // El texto del borrador que vino del servidor la última vez. Sirve para
  // detectar cuándo ha cambiado DE VERDAD ahí fuera.
  const serverTextRef = useRef(message.contenido || "");
  // Aviso: el borrador cambió en el servidor mientras lo estabas editando aquí.
  const [conflict, setConflict] = useState<string | null>(null);

  // ── Dos pestañas abiertas ────────────────────────────────────────────────
  //
  // `useState(message.contenido)` solo se usa en el PRIMER render: el texto se
  // quedaba congelado y no se volvía a sincronizar nunca. Corregías el borrador
  // en una pestaña, enviabas desde la otra, y salía disparado el texto ANTIGUO
  // pisando la corrección — sin que nadie se enterara hasta leer lo que le
  // había llegado al cliente.
  //
  // Ahora, cuando llega una versión distinta del servidor (el websocket refresca
  // los mensajes del hilo):
  //   - si no estabas editando, se adopta sin más;
  //   - si SÍ estabas editando, no se te pisa lo escrito: se avisa de que hay
  //     una versión más nueva y se ofrece cargarla.
  useEffect(() => {
    const incoming = message.contenido || "";
    if (incoming === serverTextRef.current) return;
    serverTextRef.current = incoming;
    const dirty = editing && text !== incoming;
    if (dirty) {
      setConflict(incoming);
    } else {
      setText(incoming);
      setConflict(null);
    }
    // `text` y `editing` a propósito fuera de las dependencias: solo queremos
    // reaccionar a los cambios que vienen del servidor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [message.id, message.contenido]);
  const [correcting, setCorrecting] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  // Copy adaptada: una sugerencia de Entrenamiento no es un "correo".
  const isTraining = !!message.training;
  const noun = isTraining ? "sugerencia" : "borrador";
  const Noun = isTraining ? "Sugerencia" : "Borrador";

  // Editar a mano y Corregir (re-redactar) son excluyentes: abrir uno cierra
  // el otro, para no perder el foco ni mezclar los dos flujos.
  function openEditing() {
    setCorrecting(false);
    setEditing((v) => !v);
  }
  function openCorrecting() {
    setEditing(false);
    setCorrecting((v) => !v);
  }

  // Autoaprendizaje Fase 1: le decimos en lenguaje natural qué cambiar y el
  // agente re-redacta. Sigue siendo borrador (no se envía).
  async function handleRefine() {
    const ins = instruction.trim();
    if (!ins) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await refineDraft(message.conversation_id, message.id, ins);
      setText(updated.contenido || "");
      // Esta versión ES la del servidor: si no lo apuntamos, el efecto de
      // sincronización la tomaría por un cambio ajeno y avisaría de un
      // conflicto que no existe.
      serverTextRef.current = updated.contenido || "";
      setConflict(null);
      setInstruction("");
      setCorrecting(false);
    } catch {
      setError(`No se pudo re-redactar la ${noun}. Inténtalo de nuevo.`);
    } finally {
      setBusy(false);
    }
  }

  async function handleSend() {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await sendDraft(message.conversation_id, message.id, text.trim());
      onSent();
    } catch {
      setError(`No se pudo enviar la ${noun}. Inténtalo de nuevo.`);
    } finally {
      setBusy(false);
    }
  }

  async function handleDiscard() {
    setConfirmDiscard(false);
    setBusy(true);
    setError(null);
    try {
      await discardDraft(message.conversation_id, message.id);
      onDiscarded();
    } catch {
      setError(`No se pudo descartar la ${noun}.`);
      setBusy(false);
    }
  }

  return (
    <div className="flex justify-end mb-3">
      <div className="max-w-[85%] w-full rounded-2xl border border-state-warn/40 bg-state-warn/10 shadow-sm overflow-hidden">
        <div className="flex items-center gap-1.5 px-3 py-2 border-b border-state-warn/30 bg-state-warn/15 text-state-warn text-xs font-semibold">
          <Pencil className="w-3.5 h-3.5" />
          {Noun} del agente
          {isTraining && (
            <span className="pill bg-state-warn/20 text-state-warn text-[10px]">
              Entrenamiento
            </span>
          )}
          <span className="ml-auto pill bg-state-warn/20 text-state-warn text-[10px]">
            sin enviar
          </span>
        </div>
        <div className="p-3">
          {/* Alguien (u otra pestaña tuya) cambió este borrador mientras lo
              editabas aquí. No te pisamos lo escrito: decides tú. */}
          {conflict !== null && (
            <div className="mb-2 rounded-coro-sm border border-state-warn/40 bg-card px-2.5 py-2 text-[11px] text-ink2">
              <div className="font-semibold text-state-warn">
                Hay una versión más nueva de {isTraining ? "esta sugerencia" : "este borrador"}
              </div>
              <div className="mt-0.5">
                Se ha editado en otro sitio. Si envías ahora, se manda lo que tienes en
                pantalla.
              </div>
              <div className="mt-1.5 flex gap-2">
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  onClick={() => {
                    setText(conflict);
                    setConflict(null);
                  }}
                >
                  Cargar la versión nueva
                </button>
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  onClick={() => setConflict(null)}
                >
                  Quedarme con la mía
                </button>
              </div>
            </div>
          )}
          {editing ? (
            <textarea
              rows={5}
              value={text}
              disabled={busy}
              autoFocus
              onChange={(e) => setText(e.target.value)}
              className="input resize-y w-full text-sm whitespace-pre-wrap"
              placeholder={isTraining ? "Texto de la respuesta…" : "Texto del correo…"}
            />
          ) : (
            <div className="text-sm whitespace-pre-wrap break-words text-ink">
              {text || <span className="text-ink3 italic">({noun} vacía)</span>}
            </div>
          )}
          {correcting && (
            <div className="mt-2 rounded-lg border border-state-warn/30 bg-state-warn/10 p-2">
              <label className="block text-[11px] font-semibold text-state-warn mb-1">
                Dile cómo mejorarla y el agente la reescribe:
              </label>
              <textarea
                rows={2}
                value={instruction}
                disabled={busy}
                autoFocus
                onChange={(e) => setInstruction(e.target.value)}
                className="input resize-y w-full text-sm"
                placeholder={'Ej: "No ofrezcas descuentos, di que la promoción ha terminado y sé más cálida."'}
              />
              <div className="mt-1.5 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setCorrecting(false);
                    setInstruction("");
                  }}
                  disabled={busy}
                  className="btn-ghost btn-sm text-ink3"
                >
                  Cancelar
                </button>
                <button
                  type="button"
                  onClick={handleRefine}
                  disabled={busy || !instruction.trim()}
                  className="btn-primary btn-sm"
                  title="El agente reescribe siguiendo tu indicación"
                >
                  <Wand2 className="w-4 h-4" /> {busy ? "Re-redactando…" : "Re-redactar"}
                </button>
              </div>
            </div>
          )}
          {error && (
            <div className="mt-2 text-[12px] text-state-warn">{error}</div>
          )}
          <div className="mt-2 flex items-center justify-end gap-2">
            <ConfirmModal
              open={confirmDiscard}
              title={isTraining ? "¿Descartar esta sugerencia?" : "¿Descartar este borrador?"}
              description={
                isTraining
                  ? "La sugerencia del agente se elimina; el cliente sigue sin respuesta."
                  : "Se eliminará también el borrador de Gmail."
              }
              confirmLabel="Descartar"
              tone="danger"
              busy={busy}
              onConfirm={handleDiscard}
              onCancel={() => setConfirmDiscard(false)}
            />
            <button
              type="button"
              onClick={() => setConfirmDiscard(true)}
              disabled={busy}
              className="btn-ghost btn-sm text-ink3"
              title={`Descartar la ${noun}`}
            >
              <Trash2 className="w-4 h-4" /> Descartar
            </button>
            <button
              type="button"
              onClick={openCorrecting}
              disabled={busy}
              aria-pressed={correcting}
              className={`btn-ghost btn-sm ${correcting ? "bg-state-warn/20 text-state-warn" : "text-ink2"}`}
              title="Corregir al agente con una indicación y que reescriba"
            >
              <Wand2 className="w-4 h-4" /> Corregir
            </button>
            <button
              type="button"
              onClick={openEditing}
              disabled={busy}
              aria-pressed={editing}
              className={`btn-ghost btn-sm ${editing ? "bg-state-warn/20 text-state-warn" : "text-ink2"}`}
              title={editing ? "Terminar de editar" : `Editar la ${noun} a mano`}
            >
              <Pencil className="w-4 h-4" /> Editar
            </button>
            <button
              type="button"
              onClick={handleSend}
              disabled={busy || !text.trim()}
              className="btn-primary btn-sm"
              title={isTraining ? "Enviar la respuesta" : "Enviar el correo"}
            >
              <Send className="w-4 h-4" /> Enviar
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
