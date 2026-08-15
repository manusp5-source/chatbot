import { FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "@/services/api";

export default function ResetPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [pass, setPass] = useState("");
  const [pass2, setPass2] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (pass.length < 8) {
      setError("La contraseña debe tener al menos 8 caracteres.");
      return;
    }
    if (pass !== pass2) {
      setError("Las contraseñas no coinciden.");
      return;
    }
    setBusy(true);
    try {
      await api.post("/auth/reset-password", { token, new_password: pass });
      setDone(true);
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "No se pudo restablecer la contraseña.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-full flex items-center justify-center bg-paper p-4">
      <div className="card w-full max-w-md p-7 sm:p-9">
        <h1 className="font-display text-2xl text-ink mb-2">Nueva contraseña</h1>
        {!token ? (
          <p className="text-sm text-state-bad">
            Enlace inválido. Solicita uno nuevo desde "Recuperar contraseña".
          </p>
        ) : done ? (
          <div className="space-y-4">
            <p className="text-sm text-state-ok">Contraseña actualizada. Ya puedes entrar.</p>
            <Link to="/login" className="btn btn-primary w-full justify-center">Entrar</Link>
          </div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-4">
            <div>
              <label className="label">Nueva contraseña</label>
              <input
                type="password"
                autoComplete="new-password"
                  minLength={10}
                required
                value={pass}
                onChange={(e) => setPass(e.target.value)}
                className="input w-full"
                placeholder="Mín. 8 caracteres"
              />
            </div>
            <div>
              <label className="label">Repite la contraseña</label>
              <input
                type="password"
                autoComplete="new-password"
                  minLength={10}
                required
                value={pass2}
                onChange={(e) => setPass2(e.target.value)}
                className="input w-full"
              />
            </div>
            {error && <div className="text-sm text-state-bad">{error}</div>}
            <button type="submit" className="btn btn-primary w-full justify-center" disabled={busy}>
              {busy ? "Guardando…" : "Guardar contraseña"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
