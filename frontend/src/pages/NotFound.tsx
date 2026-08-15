import { Link } from "react-router-dom";
import { BrandLogo } from "@/components/BrandLogo";

/**
 * 404 propia: antes el comodín del router redirigía en silencio a "/" y una
 * URL mal escrita aterrizaba en el inbox sin explicación.
 */
export default function NotFound() {
  return (
    <div className="min-h-full flex items-center justify-center bg-paper p-6">
      <div className="text-center">
        <BrandLogo className="inline-block w-12 h-12 mb-5" />
        <div className="font-numbers text-5xl text-ink mb-2">404</div>
        <h1 className="font-display text-2xl text-ink mb-2">Página no encontrada</h1>
        <p className="text-sm text-ink3 mb-6 max-w-sm">
          La dirección no existe o ha cambiado. Revisa la URL o vuelve al panel.
        </p>
        <Link to="/" className="btn-primary inline-flex">
          Ir al inicio
        </Link>
      </div>
    </div>
  );
}
