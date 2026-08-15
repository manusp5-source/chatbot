import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/services/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post("/auth/forgot-password", { email });
    } catch {
      /* no revelamos si el email existe: mismo resultado */
    } finally {
      setBusy(false);
      setSent(true);
    }
  }

  return (
    <div className="min-h-full flex items-center justify-center bg-paper p-4">
      <div className="card w-full max-w-md p-7 sm:p-9">
        <h1 className="font-display text-2xl text-ink mb-2">Recuperar contraseña</h1>
        {sent ? (
          <div className="space-y-4">
            <p className="text-sm text-ink2">
              Si ese email tiene una cuenta, te hemos enviado un enlace para restablecer la contraseña.
              Revisa tu bandeja (y la carpeta de spam). El enlace caduca en 1 hora.
            </p>
            <Link to="/login" className="btn btn-primary w-full justify-center">Volver a entrar</Link>
          </div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-4">
            <p className="text-sm text-ink3">
              Introduce tu email y te enviaremos un enlace para crear una nueva contraseña.
            </p>
            <div>
              <label className="label">Email</label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="input w-full"
                placeholder="tu@email.com"
              />
            </div>
            <button type="submit" className="btn btn-primary w-full justify-center" disabled={busy}>
              {busy ? "Enviando…" : "Enviar enlace"}
            </button>
            <Link to="/login" className="block text-center text-xs text-ink3 hover:text-ink">
              Volver a entrar
            </Link>
          </form>
        )}
      </div>
    </div>
  );
}
