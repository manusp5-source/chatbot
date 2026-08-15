import clsx from "clsx";
import { useEffect, useState } from "react";
import {
  Bot,
  User as UserIcon,
  Headphones,
  Archive,
  RefreshCw,
  FileText,
  Paperclip,
  Info,
  Check,
  CheckCheck,
  AlertTriangle,
} from "lucide-react";
import type { Message } from "@/types";
import { apiBaseHttp } from "@/services/api";
import { MediaBubble } from "@/components/MediaBubble";
import { EmailBody } from "@/components/EmailBody";
import { recoverMessage, type RecoveredEmail } from "@/services/conversations";
import { getToken } from "@/lib/session";

// Etiqueta en castellano de cada tipo de adjunto, para cuando no tenemos el
// fichero y solo podemos decir QUÉ llegó.
const MEDIA_LABEL: Record<string, string> = {
  image: "Imagen recibida",
  sticker: "Sticker recibido",
  video: "Vídeo recibido",
  audio: "Audio recibido",
  document: "Documento recibido",
};

function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("es-ES", { day: "2-digit", month: "short", year: "numeric" });
}

/**
 * Acuse de entrega de WhatsApp, junto a la hora del mensaje.
 *
 * Que el proveedor aceptara el mensaje no es que el cliente lo tenga. Meta
 * responde 200 y minutos después avisa por webhook de que no ha podido
 * entregarlo. Antes ese aviso solo iba a los logs en vivo, así que en la
 * bandeja el mensaje aparecía enviado tan tranquilo y la conversación se daba
 * por contestada.
 *
 * El fallo es lo único que se dice con palabras y en rojo: es lo único que
 * obliga a alguien a hacer algo.
 */
function AcuseDeEntrega({ message }: { message: Message }) {
  const estado = message.delivery_status;
  if (!estado) return null;

  if (estado === "fallido") {
    const motivo = message.delivery_detail || "sin motivo";
    return (
      <span
        className="inline-flex items-center gap-1 text-state-bad font-medium"
        title={`WhatsApp no lo entregó: ${motivo}`}
      >
        <AlertTriangle className="w-3 h-3" /> No entregado
      </span>
    );
  }
  if (estado === "leido") {
    return (
      <span className="inline-flex items-center text-brand-ink" title="Leído">
        <CheckCheck className="w-3 h-3" />
      </span>
    );
  }
  if (estado === "entregado") {
    return (
      <span className="inline-flex items-center" title="Entregado en el móvil del cliente">
        <CheckCheck className="w-3 h-3" />
      </span>
    );
  }
  return (
    <span className="inline-flex items-center" title="Aceptado por WhatsApp, aún sin entregar">
      <Check className="w-3 h-3" />
    </span>
  );
}

/**
 * Sello de tiempo de la burbuja.
 *
 * Antes ponía SOLO la hora. En un hilo de meses (lo normal en email y en
 * WhatsApp) todos los mensajes decían "14:32" y no había manera de saber si algo
 * era de esta mañana o de marzo. Ahora:
 *   - hoy → "14:32"
 *   - ayer → "Ayer 14:32"
 *   - este año → "14 mar 14:32"
 *   - otro año → "14 mar 2025 14:32"
 */
export function formatStamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hora = formatTime(iso);
  const hoy = new Date();
  const mismoDia = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (mismoDia(d, hoy)) return hora;
  const ayer = new Date(hoy);
  ayer.setDate(hoy.getDate() - 1);
  if (mismoDia(d, ayer)) return `Ayer ${hora}`;
  const dia = d.toLocaleDateString("es-ES", { day: "numeric", month: "short" });
  if (d.getFullYear() === hoy.getFullYear()) return `${dia} ${hora}`;
  return `${dia} ${d.getFullYear()} ${hora}`;
}

/** Fecha completa, para el tooltip del sello. */
function formatFullStamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("es-ES", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * ¿Es un mensaje del SISTEMA y no de una persona?
 *
 * El puente del handoff, el aviso de cierre y demás avisos automáticos se
 * pintaban con el avatar de auriculares y la etiqueta "Equipo", igual que si los
 * hubiera escrito alguien del equipo. En una grabación eso es contar una
 * mentira: parece que hay una persona atendiendo cuando no la hay.
 */
function isSystemMessage(message: Message): boolean {
  if (message.rol === "system") return true;
  const meta = message.metadata as Record<string, unknown> | undefined;
  return !!(meta?.system || meta?.is_system || meta?.handoff_bridge);
}

/**
 * Retención email — Stub de un correo ARCHIVADO (purgado a los 6 meses).
 *
 * El contenido se vació de la BD (Gmail es el archivo). Mostramos un recuadro
 * discreto y atenuado con el asunto + fecha y un botón para recuperarlo de
 * Gmail bajo demanda. Al recuperar, el contenido se muestra INLINE con el mismo
 * EmailBody saneado (estado local; no se persiste nada).
 */
function ArchivedEmailStub({ message }: { message: Message }) {
  const [recovered, setRecovered] = useState<RecoveredEmail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // El asunto se conserva en la metadata serializada (extra.subject) tras la
  // purga; lo usamos como título del stub. Si no está, no mostramos asunto.
  const subject =
    (message.metadata && (message.metadata.subject as string | undefined)) || null;

  async function handleRecover() {
    setLoading(true);
    setError(null);
    try {
      const data = await recoverMessage(message.conversation_id, message.id);
      setRecovered(data);
    } catch {
      setError("No se pudo recuperar de Gmail.");
    } finally {
      setLoading(false);
    }
  }

  // Una vez recuperado en esta sesión, mostramos el cuerpo con el render seguro
  // (HTML saneado con DOMPurify o texto autolinkificado), igual que un correo
  // normal. No se vuelve a guardar en la BD.
  if (recovered) {
    return <EmailBody html={recovered.html_body} text={recovered.contenido || ""} />;
  }

  return (
    <div className="rounded-coro-sm border border-dashed border-line bg-paper2/60 px-3 py-2.5 text-ink3">
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-ink2">
        <Archive className="w-3.5 h-3.5" />
        {subject ? <span className="truncate">{subject}</span> : <span>Correo archivado</span>}
      </div>
      <div className="mt-0.5 text-[11px] italic">
        Contenido archivado tras 6 meses · {formatDate(message.created_at)}
      </div>
      {error && <div className="mt-1.5 text-[11px] text-state-warn">{error}</div>}
      <button
        type="button"
        onClick={handleRecover}
        disabled={loading}
        className="mt-2 inline-flex items-center gap-1.5 rounded-coro-sm border border-line bg-card px-2.5 py-1 text-[11px] text-ink2 hover:bg-paper3 transition-colors disabled:opacity-60"
        title="Volver a traer el contenido desde Gmail (no se guarda en la base de datos)"
      >
        <RefreshCw className={clsx("w-3 h-3", loading && "animate-spin")} />
        {loading ? "Recuperando…" : "Recuperar de Gmail"}
      </button>
    </div>
  );
}

function useAuthenticatedBlobUrl(url: string | null | undefined): string | null {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!url) return;
    // URLs externas (https://…) se reproducen tal cual.
    if (!url.startsWith("/")) {
      setBlobUrl(url);
      return;
    }
    const token = getToken();
    if (!token) return;
    let cancelled = false;
    let createdBlob: string | null = null;
    (async () => {
      try {
        const res = await fetch(`${apiBaseHttp()}${url}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) return;
        const blob = await res.blob();
        if (cancelled) return;
        createdBlob = URL.createObjectURL(blob);
        setBlobUrl(createdBlob);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
      if (createdBlob) URL.revokeObjectURL(createdBlob);
    };
  }, [url]);
  return blobUrl;
}

export function MessageBubble({
  message,
  isEmail = false,
}: {
  message: Message;
  // 5d — En conversaciones Email renderizamos el cuerpo con EmailBody (HTML
  // saneado + autolink), no con el div whitespace-pre-wrap plano.
  isEmail?: boolean;
}) {
  const isSystem = isSystemMessage(message);
  const isUser = message.rol === "user" && !isSystem;
  const isAssistant = message.rol === "assistant" && !isSystem;
  const isOperator = message.rol === "operator" && !isSystem;
  const audioSrc = useAuthenticatedBlobUrl(message.audio_url);

  // Aviso del sistema: ni burbuja ni avatar ni "Equipo". Va centrado, en gris y
  // con su icono, para que se lea como lo que es: una nota de la máquina.
  if (isSystem) {
    return (
      <div className="flex justify-center mb-3">
        <div className="max-w-[85%] rounded-coro-sm bg-paper2 border border-line2 px-3 py-1.5 text-center">
          <div className="flex items-center justify-center gap-1.5 text-[11px] text-ink3">
            <Info className="w-3 h-3 shrink-0" />
            <span className="whitespace-pre-wrap break-words">
              {message.contenido || "Aviso del sistema"}
            </span>
          </div>
          <div className="text-[10px] text-ink4 mt-0.5" title={formatFullStamp(message.created_at)}>
            {formatStamp(message.created_at)}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={clsx("flex gap-2 mb-3", isUser ? "justify-start" : "justify-end")}>
      {isUser && (
        <div className="w-7 h-7 rounded-full bg-paper3 flex items-center justify-center shrink-0">
          <UserIcon className="w-3.5 h-3.5 text-ink3" />
        </div>
      )}
      <div
        className={clsx(
          "rounded-2xl px-3 py-2 text-sm shadow-sm",
          // Email: el correo formateado necesita más ancho para leerse bien.
          isEmail ? "max-w-[85%]" : "max-w-[70%]",
          isUser && "bg-card border border-line text-ink",
          isAssistant && "bg-brand/15 border border-brand/30 text-ink",
          isOperator && "bg-state-ok/12 border border-state-ok/30 text-ink"
        )}
      >
        {message.audio_url && audioSrc && (
          <audio controls className="w-full mb-2" src={audioSrc} />
        )}
        {message.audio_transcript && (
          <div className="mb-1.5 rounded-lg bg-ink/[0.06] border border-line px-2.5 py-1.5">
            <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-ink3 mb-0.5">
              <FileText className="w-3 h-3" /> Transcripción
            </div>
            <div className="text-[13px] italic text-ink2 leading-snug whitespace-pre-wrap break-words">
              “{message.audio_transcript}”
            </div>
          </div>
        )}
        {/* Adjunto que llegó pero del que no tenemos el fichero: o falló la
            descarga, o la retención ya lo borró. Sin esto la burbuja se veía
            COMPLETAMENTE en blanco y no había forma de saber que el cliente
            había mandado una foto. */}
        {message.media_type && !message.media_url && (
          <div className="mb-1.5 flex items-center gap-1.5 text-[13px] italic text-ink3">
            <Paperclip className="w-3.5 h-3.5 shrink-0" />
            {MEDIA_LABEL[message.media_type] || "Adjunto recibido"}
            {message.purged ? " (archivado)" : " (no se pudo guardar el archivo)"}
          </div>
        )}
        {message.media_type && message.media_url && (
          <div className="mb-2">
            <MediaBubble
              mediaType={message.media_type}
              mediaUrl={message.media_url}
              mediaMime={message.media_mime}
              mediaFilename={message.media_filename}
              mediaSize={message.media_size}
              caption={message.contenido}
            />
          </div>
        )}
        {/* Email purgado (retención a 6 meses): stub archivado con opción de
            recuperar el cuerpo desde Gmail. Tiene prioridad sobre el render
            normal (el contenido ya no está en BD). */}
        {isEmail && !message.media_type && message.purged && (
          <ArchivedEmailStub message={message} />
        )}
        {/* Email: render seguro del cuerpo (HTML saneado o texto autolinkificado).
            Resto de canales: texto plano con saltos respetados. */}
        {isEmail && !message.media_type && !message.purged && (message.contenido || message.html_body) && (
          <EmailBody html={message.html_body} text={message.contenido || ""} />
        )}
        {!isEmail && message.contenido && !message.media_type && (
          <div className="whitespace-pre-wrap break-words">{message.contenido}</div>
        )}
        <div
          className="text-[10px] text-ink4 mt-1 flex items-center gap-1 justify-end"
          title={formatFullStamp(message.created_at)}
        >
          {isAssistant && (
            <>
              <Bot className="w-3 h-3" /> Bot ·{" "}
            </>
          )}
          {isOperator && (
            <>
              <Headphones className="w-3 h-3" /> Equipo ·{" "}
            </>
          )}
          {/* Fecha + hora, no solo la hora: ver formatStamp. */}
          {formatStamp(message.created_at)}
          <AcuseDeEntrega message={message} />
        </div>
      </div>
      {!isUser && (
        <div
          className={clsx(
            "w-7 h-7 rounded-full flex items-center justify-center shrink-0",
            isAssistant ? "bg-brand/20" : "bg-state-ok/20"
          )}
        >
          {isAssistant ? (
            <Bot className="w-3.5 h-3.5 text-brand-ink" />
          ) : (
            <Headphones className="w-3.5 h-3.5 text-state-ok" />
          )}
        </div>
      )}
    </div>
  );
}
