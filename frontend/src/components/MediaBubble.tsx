import { useEffect, useState } from "react";
import {
  FileText,
  ImageIcon,
  FileAudio,
  FileVideo,
  Sticker,
  Download,
  Loader2,
  AlertTriangle,
} from "lucide-react";
import { apiBaseHttp } from "@/services/api";
import { getToken } from "@/lib/session";
import type { MediaKind } from "@/types";

/**
 * Bubble que renderiza un adjunto (image/audio/video/document/sticker).
 *
 * Carga el blob con auth (Bearer token) porque el endpoint `/uploads/{file}`
 * está protegido. No podemos poner `<img src=...>` directo: el navegador no
 * incluiría el Authorization header. Solución: fetch → Blob URL → src.
 */
export function MediaBubble({
  mediaType,
  mediaUrl,
  mediaMime,
  mediaFilename,
  mediaSize,
  caption,
}: {
  mediaType: MediaKind;
  mediaUrl: string;
  mediaMime?: string | null;
  mediaFilename?: string | null;
  mediaSize?: number | null;
  caption?: string | null;
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let createdUrl: string | null = null;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const token = getToken();
        const fullUrl = `${apiBaseHttp()}/api/v1${mediaUrl}`;
        const res = await fetch(fullUrl, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setBlobUrl(createdUrl);
      } catch (e) {
        if (!cancelled) setError(String((e as Error).message || e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [mediaUrl]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-ink3 text-xs italic py-1">
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> Cargando adjunto…
      </div>
    );
  }
  if (error || !blobUrl) {
    return (
      <div className="flex items-center gap-2 text-state-bad text-xs">
        <AlertTriangle className="w-3.5 h-3.5" />
        No se pudo cargar el adjunto
      </div>
    );
  }

  const filename = mediaFilename || "archivo";
  const sizeLabel = mediaSize != null ? formatBytes(mediaSize) : null;

  switch (mediaType) {
    case "image":
    case "sticker":
      return (
        <div className="space-y-1.5">
          <a
            href={blobUrl}
            target="_blank"
            rel="noopener noreferrer"
            title={filename}
          >
            <img
              src={blobUrl}
              alt={filename}
              className="rounded-coro-sm max-w-full max-h-72 border border-line"
            />
          </a>
          {caption && <div className="text-sm">{caption}</div>}
        </div>
      );
    case "audio":
      return (
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-ink3 text-[11px] min-w-0">
            <FileAudio className="w-3.5 h-3.5 shrink-0" />
            <span className="truncate min-w-0">{filename}</span>
            {sizeLabel && <span className="font-mono shrink-0">{sizeLabel}</span>}
          </div>
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <audio src={blobUrl} controls className="w-full" />
        </div>
      );
    case "video":
      return (
        <div className="space-y-1.5">
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video
            src={blobUrl}
            controls
            className="rounded-coro-sm max-w-full max-h-72 border border-line"
          />
          {caption && <div className="text-sm">{caption}</div>}
        </div>
      );
    case "document":
    default:
      return (
        <a
          href={blobUrl}
          download={filename}
          className="inline-flex items-center gap-2 px-3 py-2 rounded-coro-sm border border-line bg-paper2 hover:bg-paper3 text-ink2 transition no-underline"
        >
          {iconForMime(mediaMime)}
          <span className="text-sm font-medium flex-1 min-w-0 truncate">
            {filename}
          </span>
          {sizeLabel && (
            <span className="font-mono text-[10px] text-ink3">{sizeLabel}</span>
          )}
          <Download className="w-3.5 h-3.5 text-ink3" />
        </a>
      );
  }
}

function iconForMime(mime?: string | null) {
  if (!mime) return <FileText className="w-4 h-4 text-ink3" />;
  if (mime.startsWith("image/webp"))
    return <Sticker className="w-4 h-4 text-[#6C7BFF]" />;
  if (mime.startsWith("image/"))
    return <ImageIcon className="w-4 h-4 text-[#16A085]" />;
  if (mime.startsWith("audio/"))
    return <FileAudio className="w-4 h-4 text-[#9B8AFB]" />;
  if (mime.startsWith("video/"))
    return <FileVideo className="w-4 h-4 text-[#E58A2F]" />;
  return <FileText className="w-4 h-4 text-ink3" />;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
