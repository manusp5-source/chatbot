import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  FileText,
  Phone,
  PhoneOff,
  Clock,
  AudioLines,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";
import clsx from "clsx";
import { getMessages, listConversations } from "@/services/conversations";
import { api } from "@/services/api";
import { errorDetail } from "@/lib/errors";
import { useInboxSocket } from "@/hooks/useInboxSocket";
import { MessageBubble } from "@/components/MessageBubble";
import { PageHeader } from "@/components/PageHeader";
import type { Conversation, Message } from "@/types";
import { usePrivacy } from "@/store/privacy";
import { maskName, maskPhone, maskInitials } from "@/lib/mask";

// Color de marca del canal de voz (mismo que usa el inbox para "Voz").
const VOICE_COLOR = "#9B8AFB";

const AVATAR_PALETTE = ["#DBE09E", "#9DA362", "#6C7BFF", "#9B8AFB", "#25D366", "#E58A2F"];

function initialsOf(nombre: string | null | undefined, telefono: string | null | undefined): string {
  const src = (nombre || telefono || "?").trim();
  const parts = src.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return src.slice(0, 2).toUpperCase();
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? parts[0]?.[1] ?? "")).toUpperCase();
}

function colorFor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_PALETTE[h % AVATAR_PALETTE.length];
}

// Duración en mm:ss (o h:mm:ss si supera la hora). Null/0 → "—".
function formatDuration(seconds?: number | null): string {
  if (seconds == null || seconds < 0) return "—";
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const h = Math.floor(seconds / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function formatDateTime(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("es-ES", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Motivos de fin de Retell → etiqueta legible en español. Si llega uno
// desconocido, lo mostramos "humanizado" (snake_case → palabras) como fallback.
const ENDED_REASON_LABEL: Record<string, string> = {
  user_hangup: "Colgó el usuario",
  agent_hangup: "Colgó el agente",
  call_transfer: "Transferida",
  voicemail_reached: "Buzón de voz",
  inactivity: "Inactividad",
  machine_detected: "Contestador",
  max_duration_reached: "Duración máxima",
  concurrency_limit_reached: "Límite de concurrencia",
  no_valid_payment: "Pago no válido",
  scam_detected: "Posible fraude",
  dial_busy: "Comunicando",
  dial_failed: "Fallo al marcar",
  dial_no_answer: "Sin respuesta",
  error_llm_websocket_open: "Error de conexión (LLM)",
  error_llm_websocket_lost_connection: "Conexión perdida (LLM)",
  error_frontend_corrupted_payload: "Error de datos",
  error_no_audio_received: "Sin audio",
  error_asr: "Error de transcripción",
  error_retell: "Error de Retell",
  error_unknown: "Error desconocido",
  registered_call_timeout: "Tiempo de registro agotado",
};

function endedReasonLabel(reason?: string | null): string {
  if (!reason) return "";
  return ENDED_REASON_LABEL[reason] || reason.replace(/_/g, " ");
}

// Los motivos de fin "buenos" (cuelgue normal) van en verde; los de error/fallo
// en rojo; el resto en gris neutro.
function endedReasonTone(reason?: string | null): string {
  if (!reason) return "bg-paper3 text-ink3";
  if (reason.startsWith("error") || reason.startsWith("dial_") || reason === "scam_detected") {
    return "bg-state-bad/15 text-state-bad";
  }
  if (reason === "user_hangup" || reason === "agent_hangup" || reason === "call_transfer") {
    return "bg-state-ok/15 text-state-ok";
  }
  return "bg-paper3 text-ink3";
}

/**
 * Estado del canal de voz (GET /voice/status). Sirve para explicar POR QUÉ la
 * página no tiene datos en vez de enseñar huecos: sin Retell conectado, o con
 * el webhook del agente sin pegar en Retell (que es lo que trae duración,
 * grabación, resumen y motivo de fin).
 */
interface VoiceStatus {
  connected: boolean;
  llm_webhook_url: string;
  call_webhook_url: string;
  call_webhook_configured: boolean;
  total_calls: number;
  calls_with_metadata: number;
  paused: boolean;
  training: boolean;
  price_per_minute_usd: number;
}

// Coste estimado de telefonía de una llamada (minutos de Retell × tarifa).
// Es una ESTIMACIÓN: esos minutos se facturan en Retell, no aquí, y no entran
// en el tope mensual de gasto del panel (que solo mide tokens del modelo).
function formatCallCost(seconds: number | null | undefined, pricePerMinute: number): string | null {
  if (!pricePerMinute || pricePerMinute <= 0) return null;
  if (seconds == null || seconds <= 0) return null;
  const usd = (seconds / 60) * pricePerMinute;
  return `~${usd.toFixed(2)} $`;
}

function dedupeById<T extends { id: string }>(list: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of list) {
    if (!seen.has(item.id)) {
      seen.add(item.id);
      out.push(item);
    }
  }
  return out;
}

export default function Calls() {
  const priv = usePrivacy((s) => s.enabled);
  const [calls, setCalls] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  // Errores visibles de carga: sin esto, un fallo de la API dejaba la página
  // vacía (o desactualizada) sin ningún aviso.
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  // "Gana la última": evita que una respuesta lenta machaque la lista actual.
  const reqSeq = useRef(0);
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus | null>(null);

  const refreshList = useCallback(async () => {
    const seq = ++reqSeq.current;
    try {
      const data = await listConversations({
        canal: "retell_voice",
        // El webhook de fin de llamada ARCHIVA la conversación (sale de
        // "Activas" del inbox). Esta página es el registro de TODAS las
        // llamadas, así que pedimos también las archivadas.
        archived: "all",
        page_size: 50,
      });
      if (seq === reqSeq.current) {
        setCalls(data.items);
        setListError(null);
        setLoading(false);
      }
    } catch (e) {
      // Antes el fallo era silencioso: la lista quedaba vacía o desactualizada.
      if (seq === reqSeq.current) {
        setListError(errorDetail(e, "No se pudieron cargar las llamadas."));
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  // Estado del canal: si falla, la página sigue funcionando (solo se pierde el
  // aviso explicativo), así que el error no se enseña.
  const refreshStatus = useCallback(async () => {
    try {
      const { data } = await api.get<VoiceStatus>("/voice/status");
      setVoiceStatus(data);
    } catch {
      setVoiceStatus(null);
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  // Deep-link opcional ?call=<id> (p. ej. desde la ficha de un contacto).
  useEffect(() => {
    const wanted = searchParams.get("call");
    if (!wanted) return;
    setSelectedId(wanted);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("call");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = useMemo(
    () => calls.find((c) => c.id === selectedId) || null,
    [calls, selectedId],
  );

  // Transcripción de la llamada seleccionada (solo lectura). "Gana la última"
  // como en la lista: al cambiar rápido de llamada, una respuesta lenta no
  // machaca la transcripción de la llamada abierta.
  const msgSeq = useRef(0);
  const loadMessages = useCallback(async (convId: string) => {
    const seq = ++msgSeq.current;
    try {
      const msgs = await getMessages(convId, 200);
      if (seq !== msgSeq.current) return;
      setMessages(dedupeById(msgs));
      setDetailError(null);
    } catch (e) {
      // Antes el fallo era silencioso: el detalle se quedaba sin transcripción
      // (o con la de la llamada anterior) sin ningún aviso.
      if (seq !== msgSeq.current) return;
      setDetailError(errorDetail(e, "No se pudo cargar la transcripción de la llamada."));
    }
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setMessages([]);
      setDetailError(null);
      return;
    }
    void loadMessages(selectedId);
  }, [selectedId, loadMessages]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [selectedId]);

  // Refresco en vivo: cuando entra una llamada nueva o se cierra (el webhook
  // rellena metadatos), el inbox emite eventos por el mismo WS. Refrescamos la
  // lista y, si es la abierta, su transcripción.
  useInboxSocket((ev) => {
    if (
      ev.type === "message.new" ||
      ev.type === "conversation.updated" ||
      ev.type === "message.updated"
    ) {
      void refreshList();
      const convId = ev.payload.conversation_id as string | undefined;
      if (selectedId && convId === selectedId) {
        void loadMessages(selectedId);
      }
    }
  });

  return (
    <div className="h-full flex flex-col bg-paper">
      <PageHeader
        eyebrow="Canal de voz"
        title="Llamadas"
        description="Registro de llamadas del agente de voz. Solo lectura."
      />

      <div className="flex-1 flex min-h-0">
        {/* Lista de llamadas */}
        <aside
          className={clsx(
            "flex flex-col bg-card border-r border-line",
            selected ? "hidden md:flex" : "flex w-full md:w-auto",
            "md:w-80 md:shrink-0",
          )}
        >
          <div className="flex-1 overflow-auto">
            {listError && (
              <div className="mx-3 mt-3 text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
                <span className="flex-1">{listError}</span>
                <button
                  type="button"
                  onClick={() => void refreshList()}
                  className="font-semibold hover:underline shrink-0"
                >
                  Reintentar
                </button>
              </div>
            )}
            {loading && (
              <div className="text-center text-ink3 text-sm py-12 italic font-display">
                Cargando llamadas…
              </div>
            )}
            {/* El canal de voz no está conectado: decirlo, en vez de dejar que
                parezca que "aún no ha llamado nadie". */}
            {!loading && voiceStatus && !voiceStatus.connected && (
              <div className="mx-3 mt-3 text-xs text-ink2 bg-paper2 border border-line rounded-coro-sm px-3 py-2.5 leading-relaxed">
                <div className="font-semibold text-ink mb-1">Retell no está conectado</div>
                El canal de voz necesita una cuenta de Retell conectada desde{" "}
                <b>Conexiones</b> (API key, agente, voz y número). Hasta entonces
                no entra ninguna llamada.
              </div>
            )}
            {/* Hay llamadas pero ninguna trae metadatos: falta la segunda URL
                (el webhook del agente) en el panel de Retell. */}
            {!loading &&
              voiceStatus &&
              voiceStatus.connected &&
              !voiceStatus.call_webhook_configured && (
                <div className="mx-3 mt-3 text-xs text-ink2 bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2.5 leading-relaxed">
                  <div className="font-semibold text-ink mb-1">
                    Falta el webhook del agente en Retell
                  </div>
                  Las llamadas entran y se contestan, pero llegan sin{" "}
                  <b>duración</b>, sin <b>grabación</b>, sin <b>resumen</b> y sin{" "}
                  <b>motivo de fin</b>: esos datos los manda Retell a una segunda
                  URL que hay que pegar en su panel (Agent → Webhook Settings).
                  La tienes en <b>Conexiones → Retell</b>, al volver a guardar las
                  credenciales.
                </div>
              )}
            {!loading && voiceStatus?.paused && (
              <div className="mx-3 mt-3 text-xs text-ink2 bg-paper2 border border-line rounded-coro-sm px-3 py-2.5 leading-relaxed">
                <b>Canal de voz pausado.</b> Las llamadas entran, pero el agente
                no responde: se disculpa y cuelga.
              </div>
            )}
            {!loading && calls.length === 0 && !listError && (
              <div className="text-center text-ink3 text-sm py-12 px-6 italic font-display">
                {voiceStatus && !voiceStatus.connected
                  ? "No has conectado Retell todavía."
                  : "Aún no hay llamadas registradas."}
              </div>
            )}
            {calls.map((c) => {
              const init = maskInitials(initialsOf(c.contact?.nombre, c.contact?.telefono), priv);
              const color = colorFor(c.contact?.telefono || c.id);
              const isActive = selectedId === c.id;
              // La voz intermedia Retell: el "teléfono" es voice:{call_id}, así
              // que mostramos el nombre del contacto si existe; si no, el id.
              const title =
                maskName(c.contact?.nombre, priv) ||
                (c.contact?.telefono?.startsWith("voice:")
                  ? "Llamada de voz"
                  : maskPhone(c.contact?.telefono, priv)) ||
                "Llamada de voz";
              return (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => setSelectedId(c.id)}
                  className={clsx(
                    "w-full text-left px-3 py-2.5 border-b border-line2 hover:bg-paper2 transition-colors flex items-start gap-3",
                    isActive && "bg-brand/10",
                  )}
                >
                  <div className="relative shrink-0">
                    <div
                      className="w-9 h-9 rounded-full flex items-center justify-center text-[11px] font-bold"
                      style={{ background: color, color: "var(--brand-on)" }}
                    >
                      {init}
                    </div>
                    <span
                      className="absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full ring-2 ring-card flex items-center justify-center"
                      style={{ background: VOICE_COLOR }}
                      title="Voz"
                    >
                      <Phone className="w-2 h-2 text-white" />
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-sm truncate font-medium text-ink">{title}</div>
                      <div className="text-[10px] text-ink4 font-mono shrink-0">
                        {formatDateTime(c.started_at)}
                      </div>
                    </div>
                    <div className="mt-1 flex items-center gap-3 text-[12px] text-ink3">
                      <span className="inline-flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {formatDuration(c.call_duration_seconds)}
                      </span>
                      {c.call_recording_url && (
                        <span className="inline-flex items-center gap-1" title="Grabación disponible">
                          <AudioLines className="w-3 h-3" />
                          audio
                        </span>
                      )}
                    </div>
                    {c.call_ended_reason && (
                      <div className="mt-1.5">
                        <span
                          className={
                            "badge text-[10px] border-transparent " +
                            endedReasonTone(c.call_ended_reason)
                          }
                        >
                          {endedReasonLabel(c.call_ended_reason)}
                        </span>
                      </div>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </aside>

        {/* Detalle de la llamada */}
        <main className={clsx("flex-1 flex flex-col bg-paper min-w-0", !selected && "hidden md:flex")}>
          {!selected && (
            <div className="flex-1 flex flex-col items-center justify-center text-ink3 text-sm gap-2 px-6 text-center">
              <Phone className="w-12 h-12 text-ink4" />
              <div className="font-display text-xl italic text-ink2">Selecciona una llamada</div>
              <div className="text-xs max-w-xs">
                Aquí verás la grabación, la transcripción y el resumen de cada llamada del agente
                de voz.
              </div>
            </div>
          )}
          {selected && (
            <>
              <header className="px-4 py-3 bg-card border-b border-line flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setSelectedId(null)}
                  className="md:hidden inline-flex items-center justify-center w-9 h-9 rounded-coro-sm hover:bg-paper2 text-ink2 shrink-0"
                  aria-label="Volver"
                >
                  <ArrowLeft className="w-4 h-4" />
                </button>
                <div
                  className="w-9 h-9 rounded-full flex items-center justify-center text-[11px] font-bold shrink-0"
                  style={{ background: colorFor(selected.contact?.telefono || selected.id), color: "var(--brand-on)" }}
                >
                  {maskInitials(initialsOf(selected.contact?.nombre, selected.contact?.telefono), priv)}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold text-ink truncate">
                    {maskName(selected.contact?.nombre, priv) || "Llamada de voz"}
                  </div>
                  <div className="text-[11px] text-ink3 truncate font-mono">
                    {formatDateTime(selected.started_at)}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-wrap justify-end sm:shrink-0">
                  <span className="badge text-[11px] border-transparent bg-paper2 text-ink2 inline-flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {formatDuration(selected.call_duration_seconds)}
                  </span>
                  {/* Coste de telefonía estimado (minutos × tarifa de Retell).
                      Solo aparece si hay tarifa configurada. */}
                  {voiceStatus &&
                    formatCallCost(
                      selected.call_duration_seconds,
                      voiceStatus.price_per_minute_usd,
                    ) && (
                      <span
                        className="badge text-[11px] border-transparent bg-paper2 text-ink2"
                        title="Coste estimado de los minutos de Retell (no incluye el modelo, y se factura en Retell)"
                      >
                        {formatCallCost(
                          selected.call_duration_seconds,
                          voiceStatus.price_per_minute_usd,
                        )}
                      </span>
                    )}
                  {selected.call_ended_reason && (
                    <span
                      className={
                        "badge text-[11px] border-transparent inline-flex items-center gap-1 " +
                        endedReasonTone(selected.call_ended_reason)
                      }
                    >
                      <PhoneOff className="w-3 h-3" />
                      {endedReasonLabel(selected.call_ended_reason)}
                    </span>
                  )}
                </div>
              </header>

              <div ref={scrollRef} className="flex-1 overflow-auto px-4 py-4 space-y-4">
                {detailError && (
                  <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
                    <span className="flex-1">{detailError}</span>
                    <button
                      type="button"
                      onClick={() => void loadMessages(selected.id)}
                      className="font-semibold hover:underline shrink-0"
                    >
                      Reintentar
                    </button>
                  </div>
                )}
                {/* Reproductor de la grabación (si Retell la mandó). NO se
                    descarga al servidor: se reproduce el enlace de Retell tal
                    cual. El enlace puede caducar (es de Retell). */}
                {selected.call_recording_url ? (
                  <div className="card" style={{ padding: 12 }}>
                    <div className="flex items-center gap-2 mb-2 text-[12px] font-semibold text-ink2">
                      <AudioLines className="w-3.5 h-3.5" />
                      Grabación
                    </div>
                    <audio controls preload="none" className="w-full" src={selected.call_recording_url}>
                      Tu navegador no puede reproducir este audio.
                    </audio>
                  </div>
                ) : (
                  <div className="text-[12px] text-ink3 italic">
                    {voiceStatus && !voiceStatus.call_webhook_configured
                      ? "No hay grabación porque falta el webhook del agente en Retell: la grabación (y la duración, el resumen y el motivo de fin) los manda Retell a esa URL al colgar."
                      : "No hay grabación disponible para esta llamada."}
                  </div>
                )}

                {/* Resumen (si Retell lo envió en call_analyzed). */}
                {selected.resumen && (
                  <div className="card" style={{ padding: 12 }}>
                    <div className="flex items-center gap-2 mb-1.5 text-[12px] font-semibold text-ink2">
                      <FileText className="w-3.5 h-3.5" />
                      Resumen
                    </div>
                    <div className="text-sm text-ink whitespace-pre-wrap break-words">
                      {selected.resumen}
                    </div>
                  </div>
                )}

                {/* Transcripción (reutiliza MessageBubble del inbox). */}
                <div>
                  <div className="text-[12px] font-semibold text-ink2 mb-2">Transcripción</div>
                  {messages.length === 0 ? (
                    <div className="text-center text-ink3 text-sm italic font-display py-6">
                      Sin transcripción.
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {messages.map((m) => (
                        <MessageBubble key={m.id} message={m} />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
