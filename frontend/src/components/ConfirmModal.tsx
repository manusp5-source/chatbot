import { useEffect, type ReactNode } from "react";
import { AlertTriangle, X } from "lucide-react";

type Tone = "default" | "danger";

interface Props {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: Tone;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmLabel = "Confirmar",
  cancelLabel = "Cancelar",
  tone = "default",
  busy = false,
  onConfirm,
  onCancel,
}: Props) {
  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  // Block body scroll while open
  useEffect(() => {
    if (open) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = prev;
      };
    }
  }, [open]);

  if (!open) return null;

  const isDanger = tone === "danger";

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-ink/50 backdrop-blur-[2px] animate-[fadein_.15s_ease-out]"
        onClick={onCancel}
        aria-hidden="true"
      />
      {/* Card */}
      <div
        className="relative bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-md p-5 sm:p-6 animate-[modalin_.18s_cubic-bezier(.2,.9,.3,1.2)]"
        style={{ transformOrigin: "center" }}
      >
        <button
          type="button"
          aria-label="Cerrar"
          onClick={onCancel}
          className="absolute top-3 right-3 inline-flex items-center justify-center w-8 h-8 rounded-coro-sm hover:bg-paper2 text-ink3"
        >
          <X className="w-4 h-4" />
        </button>

        <div className="flex items-start gap-3">
          <div
            className={
              "w-10 h-10 rounded-full flex items-center justify-center shrink-0 " +
              (isDanger
                ? "bg-state-bad/15 text-state-bad"
                : "bg-brand/20 text-brand-ink")
            }
          >
            <AlertTriangle className="w-5 h-5" />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="font-display text-xl text-ink leading-tight pr-6">
              {title}
            </h2>
            {description && (
              <div className="text-sm text-ink3 mt-2 leading-relaxed">
                {description}
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 mt-6">
          <button
            type="button"
            onClick={onCancel}
            className="btn"
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={isDanger ? "btn-danger" : "btn-primary"}
          >
            {busy ? "Procesando…" : confirmLabel}
          </button>
        </div>
      </div>

      {/* Keyframes inline */}
      <style>{`
        @keyframes fadein {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        @keyframes modalin {
          from { opacity: 0; transform: scale(0.94) translateY(8px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }
      `}</style>
    </div>
  );
}
