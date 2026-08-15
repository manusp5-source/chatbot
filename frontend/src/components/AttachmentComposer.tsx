import { useCallback, useEffect, useRef, useState } from "react";
import {
  Paperclip,
  Mic,
  X,
  Send,
  Loader2,
  Square,
  AlertTriangle,
} from "lucide-react";
import { sendAttachment } from "@/services/conversations";
import type { Message } from "@/types";

/**
 * Composer de adjuntos para el inbox en modo humano.
 *
 *  - Botón clip: abre selector de archivo nativo. Acepta los MIME que admite
 *    WhatsApp Business API (imágenes, audios, vídeos, docs, stickers).
 *  - Botón micro: mantener pulsado para grabar (estilo WhatsApp). Al soltar
 *    se ofrece preview con opción de enviar / cancelar.
 *
 * Tras un envío exitoso se llama a `onSent(message)` para que el padre
 * inserte la nueva fila en el chat.
 */
const ACCEPT_MIMES = [
  // imágenes
  "image/jpeg",
  "image/png",
  // audios
  "audio/aac",
  "audio/mp4",
  "audio/mpeg",
  "audio/amr",
  "audio/ogg",
  "audio/opus",
  "audio/webm",
  // vídeos
  "video/mp4",
  "video/3gpp",
  // documentos
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "text/plain",
  "text/csv",
  // stickers
  "image/webp",
].join(",");

export function AttachmentComposer({
  conversationId,
  disabled,
  onSent,
}: {
  conversationId: string;
  disabled?: boolean;
  onSent: (msg: Message) => void;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);

  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingBlob, setPendingBlob] = useState<Blob | null>(null);
  const [pendingPreviewUrl, setPendingPreviewUrl] = useState<string | null>(null);
  const [caption, setCaption] = useState("");

  const [recording, setRecording] = useState(false);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const recordingTimer = useRef<number | null>(null);

  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const stopMediaStream = useCallback(() => {
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
  }, []);

  // Limpieza al desmontar — evita micros activos olvidados y Blob URLs huérfanos.
  useEffect(() => {
    return () => {
      stopMediaStream();
      if (recordingTimer.current) window.clearInterval(recordingTimer.current);
      if (pendingPreviewUrl) URL.revokeObjectURL(pendingPreviewUrl);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function clearPending() {
    if (pendingPreviewUrl) URL.revokeObjectURL(pendingPreviewUrl);
    setPendingFile(null);
    setPendingBlob(null);
    setPendingPreviewUrl(null);
    setCaption("");
    setError(null);
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    // Topes de WhatsApp por tipo (los mismos que valida el backend en
    // media.py): rechazar aquí evita subir el archivo entero para un 400.
    const kind = f.type.startsWith("image/")
      ? "imagen"
      : f.type.startsWith("audio/")
        ? "audio"
        : f.type.startsWith("video/")
          ? "vídeo"
          : "documento";
    const maxBytes =
      kind === "imagen"
        ? 5 * 1024 * 1024
        : kind === "documento"
          ? 100 * 1024 * 1024
          : 16 * 1024 * 1024;
    if (f.size > maxBytes) {
      setError(
        `El ${kind} supera el máximo de ${Math.round(maxBytes / 1024 / 1024)} MB de WhatsApp.`
      );
      e.target.value = "";
      return;
    }
    if (pendingPreviewUrl) URL.revokeObjectURL(pendingPreviewUrl);
    setPendingFile(f);
    setPendingBlob(null);
    setPendingPreviewUrl(URL.createObjectURL(f));
    setError(null);
    // Reset input para poder reseleccionar el mismo archivo si quieres.
    e.target.value = "";
  }

  async function startRecording() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaStreamRef.current = stream;
      // Preferimos webm/opus (universal en browsers modernos) y dejamos que
      // YCloud lo transcodifique. Fallback al default si no soporta.
      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/webm")
          ? "audio/webm"
          : "";
      const mr = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      audioChunksRef.current = [];
      mr.ondataavailable = (ev) => {
        if (ev.data.size > 0) audioChunksRef.current.push(ev.data);
      };
      mr.onstop = () => {
        const finalMime = mr.mimeType || "audio/webm";
        const blob = new Blob(audioChunksRef.current, { type: finalMime });
        audioChunksRef.current = [];
        if (pendingPreviewUrl) URL.revokeObjectURL(pendingPreviewUrl);
        setPendingBlob(blob);
        setPendingFile(null);
        setPendingPreviewUrl(URL.createObjectURL(blob));
        stopMediaStream();
      };
      mediaRecorderRef.current = mr;
      mr.start();
      setRecording(true);
      setRecordingSeconds(0);
      recordingTimer.current = window.setInterval(() => {
        setRecordingSeconds((s) => s + 1);
      }, 1000);
    } catch (e) {
      setError(
        "No se pudo acceder al micrófono. Comprueba permisos del navegador.",
      );
    }
  }

  function stopRecording(commit: boolean) {
    if (recordingTimer.current) {
      window.clearInterval(recordingTimer.current);
      recordingTimer.current = null;
    }
    const mr = mediaRecorderRef.current;
    if (!mr) {
      setRecording(false);
      stopMediaStream();
      return;
    }
    if (commit) {
      mr.stop(); // dispara onstop → setPendingBlob
    } else {
      audioChunksRef.current = [];
      mr.stop();
      stopMediaStream();
    }
    setRecording(false);
  }

  async function send() {
    setError(null);
    setSending(true);
    try {
      const blob = pendingBlob || pendingFile;
      if (!blob) return;
      const filename =
        pendingFile?.name ||
        `voice-${new Date().toISOString().replace(/[:.]/g, "-")}.webm`;
      const msg = await sendAttachment(conversationId, blob, {
        caption: caption.trim() || undefined,
        filename,
      });
      onSent(msg);
      clearPending();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(
        ax.response?.data?.detail || ax.message || "Error enviando adjunto",
      );
    } finally {
      setSending(false);
    }
  }

  // Vista de preview cuando hay algo pendiente de enviar.
  if (pendingFile || pendingBlob) {
    const isAudio =
      pendingBlob != null ||
      (pendingFile?.type.startsWith("audio/") ?? false);
    const isImage = pendingFile?.type.startsWith("image/") ?? false;
    const isVideo = pendingFile?.type.startsWith("video/") ?? false;
    return (
      <div className="border-t border-line bg-paper2 p-3 space-y-2">
        <div className="flex items-start gap-2">
          <div className="flex-1 min-w-0">
            {isImage && pendingPreviewUrl && (
              <img
                src={pendingPreviewUrl}
                alt="preview"
                className="max-h-32 max-w-full rounded-coro-sm border border-line"
              />
            )}
            {isAudio && pendingPreviewUrl && (
              // eslint-disable-next-line jsx-a11y/media-has-caption
              <audio src={pendingPreviewUrl} controls className="w-full" />
            )}
            {isVideo && pendingPreviewUrl && (
              // eslint-disable-next-line jsx-a11y/media-has-caption
              <video
                src={pendingPreviewUrl}
                controls
                className="max-h-32 max-w-full rounded-coro-sm border border-line"
              />
            )}
            {!isImage && !isAudio && !isVideo && pendingFile && (
              <div className="text-sm text-ink2 truncate">
                {pendingFile.name}{" "}
                <span className="text-ink3 text-xs">
                  ({Math.round(pendingFile.size / 1024)} KB)
                </span>
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={clearPending}
            className="text-ink3 hover:text-state-bad"
            aria-label="Descartar"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        {(isImage || isVideo || pendingFile?.type.startsWith("application/")) && (
          <input
            type="text"
            placeholder="Caption opcional…"
            value={caption}
            onChange={(e) => setCaption(e.target.value)}
            className="w-full px-2 py-1 text-sm rounded-coro-sm border border-line bg-card focus:outline-none focus:ring-1 focus:ring-brand"
          />
        )}
        {error && (
          <div className="text-xs text-state-bad flex items-center gap-1">
            <AlertTriangle className="w-3 h-3" /> {error}
          </div>
        )}
        <div className="flex justify-end">
          <button
            type="button"
            disabled={sending}
            onClick={send}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-coro-sm bg-brand text-brand-on text-sm font-medium hover:bg-brand-600 disabled:opacity-50"
          >
            {sending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
            Enviar
          </button>
        </div>
      </div>
    );
  }

  // Vista compacta de botones (lo que se renderiza junto al input de texto del Inbox).
  return (
    <div className="flex items-center gap-1">
      <input
        ref={fileInputRef}
        type="file"
        accept={ACCEPT_MIMES}
        className="hidden"
        onChange={handleFileChange}
      />
      <button
        type="button"
        disabled={disabled || recording}
        onClick={() => fileInputRef.current?.click()}
        className="p-2 rounded-coro-sm text-ink3 hover:text-ink hover:bg-paper3 disabled:opacity-40"
        aria-label="Adjuntar archivo"
        title="Adjuntar archivo"
      >
        <Paperclip className="w-4 h-4" />
      </button>
      {recording ? (
        <button
          type="button"
          onClick={() => stopRecording(true)}
          className="p-2 rounded-coro-sm bg-state-bad text-white hover:opacity-90 inline-flex items-center gap-1 text-xs font-mono"
          aria-label="Parar y enviar"
          title="Parar grabación"
        >
          <Square className="w-3 h-3" /> {fmtSec(recordingSeconds)}
        </button>
      ) : (
        <button
          type="button"
          disabled={disabled}
          onClick={startRecording}
          className="p-2 rounded-coro-sm text-ink3 hover:text-ink hover:bg-paper3 disabled:opacity-40"
          aria-label="Grabar audio"
          title="Grabar audio"
        >
          <Mic className="w-4 h-4" />
        </button>
      )}
      {recording && (
        <button
          type="button"
          onClick={() => stopRecording(false)}
          className="p-2 rounded-coro-sm text-ink3 hover:text-state-bad"
          aria-label="Cancelar grabación"
          title="Cancelar grabación"
        >
          <X className="w-4 h-4" />
        </button>
      )}
      {error && !recording && (
        <span className="text-[10px] text-state-bad ml-1">{error}</span>
      )}
    </div>
  );
}

function fmtSec(s: number): string {
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}
