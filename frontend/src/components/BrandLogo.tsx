/**
 * Marca propia del chatbot: un bocadillo de conversación (con los tres puntos
 * de "escribiendo…") dibujado en LÍNEA, sin fondo. Usa `currentColor`, así que
 * es positivo/negativo automáticamente según el color del contexto (oscuro
 * sobre claro, claro sobre oscuro). Se dibuja en línea y sin fondo a propósito:
 * así encaja sobre cualquier superficie sin recortes ni halos.
 *
 * Los iconos "maskable" para instalar como app (public/icon-192/512.svg) sí
 * llevan tile a propósito; este componente es la marca de la interfaz.
 */
import { appName } from "@/lib/appName";

export function BrandLogo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      role="img"
      aria-label={appName()}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinejoin="round"
      strokeLinecap="round"
    >
      <rect x="3.5" y="4.5" width="17" height="12" rx="3.2" />
      <path d="M8 16.5 L7 20 L12 16.5" />
      <circle cx="9" cy="10.5" r="0.9" fill="currentColor" stroke="none" />
      <circle cx="12" cy="10.5" r="0.9" fill="currentColor" stroke="none" />
      <circle cx="15" cy="10.5" r="0.9" fill="currentColor" stroke="none" />
    </svg>
  );
}
