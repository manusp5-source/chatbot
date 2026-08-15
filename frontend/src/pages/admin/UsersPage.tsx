import { useEffect, useState } from "react";
import { KeyRound, Pencil, Plus, Trash2, Loader2, X, Save, Users as UsersIcon } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { ConfirmModal } from "@/components/ConfirmModal";
import { createUser, deleteUser, listUsers, resetUserPassword, updateUser } from "@/services/admin";
import { errorDetail } from "@/lib/errors";
import type { User, UserRole } from "@/types";

// "Última conexión" en lenguaje humano. NULL/sin valor → "Nunca".
function fmtLastLogin(iso?: string | null): string {
  if (!iso) return "Nunca";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "Nunca";
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "Ahora mismo";
  if (mins < 60) return `hace ${mins} min`;
  const h = Math.floor(mins / 60);
  if (h < 24) return `hace ${h} h`;
  const d = Math.floor(h / 24);
  if (d < 30) return `hace ${d} d`;
  return new Date(iso).toLocaleDateString("es-ES");
}

export default function UsersPage() {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [resetTarget, setResetTarget] = useState<User | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<User | null>(null);
  const [editTarget, setEditTarget] = useState<User | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    email: "",
    password: "",
    nombre: "",
    role: "cliente" as UserRole,
  });
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  // Error de las acciones sobre la tabla (borrar). El `error` de arriba es el
  // del formulario de alta y solo se pinta con el formulario abierto.
  const [actionError, setActionError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    try {
      setUsers(await listUsers());
      setListError(null);
    } catch (e) {
      // Sin catch, un fallo de la API dejaba la tabla vacía diciendo "Sin
      // usuarios": justo lo contrario de lo que pasa (hay usuarios, no se han
      // podido leer).
      setListError(errorDetail(e, "No se pudo cargar la lista de usuarios."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function onCreate() {
    setError(null);
    try {
      await createUser(form);
      setShowForm(false);
      setForm({ email: "", password: "", nombre: "", role: "cliente" });
      await refresh();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "Error creando usuario");
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setBusy(true);
    setActionError(null);
    try {
      await deleteUser(deleteTarget.id);
      setDeleteTarget(null);
      await refresh();
    } catch (e) {
      // Sin catch, el borrado que fallaba (el último admin, por ejemplo)
      // cerraba el diálogo sin decir nada y el usuario seguía en la lista.
      setDeleteTarget(null);
      setActionError(errorDetail(e, "No se pudo borrar el usuario."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Usuarios"
        description="Cuentas de acceso al panel. Dos roles: admin y cliente (operador de bandeja)."
        actions={
          <button onClick={() => setShowForm((v) => !v)} className="btn-primary">
            <Plus className="w-4 h-4" /> Nuevo usuario
          </button>
        }
      />
      <div className="p-4 max-w-4xl flex-1 overflow-auto space-y-4">
        {actionError && (
          <div className="text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
            <span className="flex-1">{actionError}</span>
            <button
              type="button"
              onClick={() => setActionError(null)}
              className="font-semibold hover:underline shrink-0"
              aria-label="Cerrar el aviso de error"
            >
              Cerrar
            </button>
          </div>
        )}
        {/* Qué puede hacer cada rol, dicho tal cual lo aplica el backend
            (app/api/deps.py). La descripción anterior — "cliente (inbox +
            contactos)" — daba a entender un rol acotado que no existía: el
            operador podía exportar toda la base de contactos, borrar etiquetas
            de todo el mundo y archivar en lote. Ya no. */}
        <div className="card p-4 text-sm text-ink2 space-y-2">
          <div>
            <span className="font-semibold text-ink">Admin</span> — todo lo anterior y, además,
            lo que no tiene vuelta atrás, lo masivo y lo que cuesta dinero: borrar contactos y
            etiquetas, exportar e importar la base de contactos, archivar en lote, sacar del
            antispam, la base de conocimiento y la configuración del sistema.
          </div>
          <div>
            <span className="font-semibold text-ink">Cliente</span> — operador de bandeja: leer y
            responder conversaciones, adjuntar, tomar el control y devolverlo al bot, archivar y
            cerrar una conversación, afinar borradores, y ver y editar fichas de contacto, sus
            notas y sus etiquetas.
          </div>
        </div>
        {showForm && (
          <div className="card p-4 space-y-3">
            <h3 className="font-display text-lg text-ink inline-flex items-center gap-2">
              <Plus className="w-4 h-4 text-brand-ink" /> Crear usuario
            </h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                  Email
                </label>
                <input
                  className="input w-full"
                  type="email"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                />
              </div>
              <div>
                <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                  Contraseña
                </label>
                <input
                  className="input w-full"
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />
              </div>
              <div>
                <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                  Nombre
                </label>
                <input
                  className="input w-full"
                  value={form.nombre}
                  onChange={(e) => setForm({ ...form, nombre: e.target.value })}
                />
              </div>
              <div>
                <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                  Rol
                </label>
                <select
                  className="input w-full"
                  value={form.role}
                  onChange={(e) => setForm({ ...form, role: e.target.value as UserRole })}
                >
                  <option value="cliente">Cliente</option>
                  <option value="admin">Admin</option>
                </select>
              </div>
            </div>
            {error && <div className="text-state-bad text-sm">{error}</div>}
            <div className="flex justify-end gap-2">
              <button className="btn-ghost" onClick={() => setShowForm(false)}>
                Cancelar
              </button>
              <button className="btn-primary" onClick={onCreate}>
                <Save className="w-4 h-4" /> Crear
              </button>
            </div>
          </div>
        )}

        <div className="card overflow-x-auto">
          {loading ? (
            <div className="text-center py-10 text-ink3 inline-flex items-center justify-center gap-2 w-full">
              <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
            </div>
          ) : listError ? (
            <div className="m-3 text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
              <span className="flex-1">{listError}</span>
              <button
                type="button"
                onClick={() => void refresh()}
                className="font-semibold hover:underline shrink-0"
              >
                Reintentar
              </button>
            </div>
          ) : users.length === 0 ? (
            <div className="text-center py-10 text-ink3 italic font-display flex items-center justify-center gap-2">
              <UsersIcon className="w-4 h-4" /> Sin usuarios.
            </div>
          ) : (
            <table className="w-full text-sm min-w-[640px]">
              <thead className="bg-paper2 border-b border-line">
                <tr className="text-ink3 text-[11px] uppercase tracking-wider">
                  <th className="text-left px-4 py-2 font-medium">Email</th>
                  <th className="text-left px-4 py-2 font-medium">Nombre</th>
                  <th className="text-left px-4 py-2 font-medium">Rol</th>
                  <th className="text-left px-4 py-2 font-medium">Activo</th>
                  <th className="text-left px-4 py-2 font-medium">Última conexión</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id} className="border-t border-line2 hover:bg-paper2/40 transition">
                    <td className="px-4 py-2 text-ink2 font-mono text-xs">{u.email}</td>
                    <td className="px-4 py-2 text-ink2">{u.nombre || "—"}</td>
                    <td className="px-4 py-2">
                      <span
                        className={`pill text-[10px] ${
                          u.role === "admin"
                            ? "bg-brand-soft text-brand-ink"
                            : "bg-paper3 text-ink3"
                        }`}
                      >
                        {u.role}
                      </span>
                    </td>
                    <td className="px-4 py-2">
                      <input
                        type="checkbox"
                        checked={u.activo}
                        onChange={(e) => updateUser(u.id, { activo: e.target.checked }).then(refresh)}
                        className="cursor-pointer"
                      />
                    </td>
                    <td className="px-4 py-2 text-ink3 text-xs whitespace-nowrap">
                      {fmtLastLogin(u.last_login)}
                    </td>
                    <td className="px-4 py-2 text-right whitespace-nowrap">
                      <button
                        className="btn-ghost text-xs"
                        title="Editar usuario"
                        onClick={() => setEditTarget(u)}
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                      <button
                        className="btn-ghost text-xs ml-1"
                        title="Cambiar contraseña"
                        onClick={() => setResetTarget(u)}
                      >
                        <KeyRound className="w-3.5 h-3.5" />
                      </button>
                      <button
                        className="btn-ghost text-xs text-state-bad ml-1"
                        title="Eliminar"
                        onClick={() => setDeleteTarget(u)}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {resetTarget && (
        <ResetPasswordModal
          user={resetTarget}
          onClose={() => setResetTarget(null)}
        />
      )}

      {editTarget && (
        <EditUserModal
          user={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={refresh}
        />
      )}

      <ConfirmModal
        open={!!deleteTarget}
        title="Eliminar usuario"
        description={
          deleteTarget
            ? `¿Seguro que quieres eliminar a ${deleteTarget.email}? Esta acción no se puede deshacer.`
            : ""
        }
        confirmLabel="Eliminar"
        cancelLabel="Cancelar"
        tone="danger"
        busy={busy}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

function ResetPasswordModal({
  user,
  onClose,
}: {
  user: User;
  onClose: () => void;
}) {
  const [pass, setPass] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (pass.length < 8) {
      setError("Mínimo 8 caracteres.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await resetUserPassword(user.id, pass);
      setDone(true);
      window.setTimeout(onClose, 1200);
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "Error al actualizar");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <form
        onSubmit={submit}
        className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-sm flex flex-col max-h-[90vh] overflow-auto"
      >
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <KeyRound className="w-4 h-4 text-brand-ink" />
          <h2 className="font-display text-lg text-ink flex-1">Cambiar contraseña</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-ink3 hover:text-ink"
            aria-label="Cerrar"
          >
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <div className="text-sm text-ink2">
            Para <b className="text-ink">{user.email}</b>
          </div>
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={10}
            value={pass}
            onChange={(e) => setPass(e.target.value)}
            placeholder="Nueva contraseña (mín. 10, letras y números)"
            className="input w-full font-mono text-xs"
            autoFocus
          />
          {error && <div className="text-state-bad text-sm">{error}</div>}
          {done && <div className="text-state-ok text-sm">Contraseña actualizada.</div>}
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cancelar
          </button>
          <button type="submit" disabled={busy || done} className="btn-primary">
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Guardar
          </button>
        </footer>
      </form>
    </div>
  );
}

function EditUserModal({
  user,
  onClose,
  onSaved,
}: {
  user: User;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [email, setEmail] = useState(user.email);
  const [nombre, setNombre] = useState(user.nombre || "");
  const [role, setRole] = useState<UserRole>(user.role);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await updateUser(user.id, {
        email: email.trim().toLowerCase(),
        nombre: nombre.trim() || null,
        role,
      });
      onSaved();
      onClose();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "Error al guardar");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <form
        onSubmit={submit}
        className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-sm flex flex-col max-h-[90vh] overflow-auto"
      >
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Pencil className="w-4 h-4 text-brand-ink" />
          <h2 className="font-display text-lg text-ink flex-1">Editar usuario</h2>
          <button type="button" onClick={onClose} className="text-ink3 hover:text-ink" aria-label="Cerrar">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">Email</label>
            <input className="input w-full" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">Nombre</label>
            <input className="input w-full" value={nombre} onChange={(e) => setNombre(e.target.value)} />
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">Rol</label>
            <select className="input w-full" value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
              <option value="cliente">Cliente</option>
              <option value="admin">Admin</option>
            </select>
            <p className="text-[11px] text-ink3 mt-1">No puedes cambiar tu propio rol ni desactivarte (lo impide el servidor).</p>
          </div>
          {error && <div className="text-state-bad text-sm">{error}</div>}
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">Cancelar</button>
          <button type="submit" disabled={busy} className="btn-primary">
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Guardar
          </button>
        </footer>
      </form>
    </div>
  );
}
