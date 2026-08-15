// Tokens de agente: credenciales para los asistentes externos del operador
// que consumen /api/v1/agent-api. El token en claro se
// muestra UNA sola vez al crearlo; después solo queda el prefijo.

import { useEffect, useState } from "react";
import { Bot, Check, Copy, Loader2, Plug, Plus, ShieldOff } from "lucide-react";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  createAgentToken,
  listAgentTokens,
  revokeAgentToken,
  type AgentTokenOut,
} from "@/services/admin";
import { mcpEndpoint } from "@/services/api";
import { errorDetail } from "@/lib/errors";

const SCOPES: Array<{ key: string; label: string; hint: string }> = [
  {
    key: "monitor:read",
    label: "Monitorización (lectura)",
    hint: "Métricas agregadas y salud: conversaciones, coste LLM, estado de canales, colas. Sin contenido de mensajes.",
  },
  {
    key: "interactions:read",
    label: "Interacciones · metadata",
    hint: "Lista de conversaciones SOLO con metadata (canal, estado, tiempos). Sin contenido ni datos del contacto.",
  },
  {
    key: "kb:read",
    label: "KB · leer",
    hint: "Listar, leer y probar la búsqueda de la base de conocimiento.",
  },
  {
    key: "kb:write",
    label: "KB · escribir",
    hint: "Crear y editar documentos de texto (con versionado, reversible).",
  },
  {
    key: "prompts:write",
    label: "Prompts de agentes",
    hint: "Leer y mejorar los prompts (el anterior queda en el historial, restaurable).",
  },
];

export function AgentTokensPanel() {
  const [tokens, setTokens] = useState<AgentTokenOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Set<string>>(new Set(["monitor:read", "kb:read"]));
  const [expiresDays, setExpiresDays] = useState<string>("");
  const [creating, setCreating] = useState(false);

  // Token recién creado (en claro, solo esta vez).
  const [newToken, setNewToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // Copiado del bloque "Conectar" (URL / comando), keyed para dar feedback por botón.
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const mcpUrl = mcpEndpoint();
  // Si acabamos de crear un token lo insertamos en el comando; si no, placeholder.
  const tokenForCmd = newToken ?? "<tu_token>";
  const mcpAddCommand = `claude mcp add --transport http chatbot ${mcpUrl} --header "Authorization: Bearer ${tokenForCmd}"`;

  async function copyKeyed(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 1500);
    } catch {
      // el usuario puede seleccionarlo a mano
    }
  }

  const [toRevoke, setToRevoke] = useState<AgentTokenOut | null>(null);
  const [revoking, setRevoking] = useState(false);

  async function refresh() {
    try {
      setTokens(await listAgentTokens());
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar los tokens."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  function toggleScope(key: string) {
    setScopes((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function onCreate() {
    if (!name.trim() || scopes.size === 0) return;
    setCreating(true);
    setError(null);
    try {
      const days = expiresDays.trim() ? parseInt(expiresDays, 10) : null;
      const out = await createAgentToken({
        name: name.trim(),
        scopes: Array.from(scopes),
        expires_days: days && days > 0 ? days : null,
      });
      setNewToken(out.token);
      setCopied(false);
      setFormOpen(false);
      setName("");
      setExpiresDays("");
      await refresh();
    } catch (e) {
      setError(errorDetail(e, "No se pudo crear el token."));
    } finally {
      setCreating(false);
    }
  }

  async function onConfirmRevoke() {
    if (!toRevoke) return;
    setRevoking(true);
    try {
      await revokeAgentToken(toRevoke.id);
      setToRevoke(null);
      await refresh();
    } catch (e) {
      setError(errorDetail(e, "No se pudo revocar el token."));
      setToRevoke(null);
    } finally {
      setRevoking(false);
    }
  }

  async function copyToken() {
    if (!newToken) return;
    try {
      await navigator.clipboard.writeText(newToken);
      setCopied(true);
    } catch {
      // el usuario puede seleccionarlo a mano
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs text-ink3 leading-relaxed flex-1">
          Tokens de acceso a la <span className="font-medium text-ink2">API</span> y al{" "}
          <span className="font-medium text-ink2">MCP</span> para tus apps y asistentes externos.
          Acceden solo a lo que
          marques: métricas agregadas, base de conocimiento y prompts —{" "}
          <span className="font-medium text-ink2">
            nunca a conversaciones, contactos ni credenciales
          </span>
          . Cada acción queda auditada con el token que la hizo.
        </p>
        <button type="button" className="btn-primary shrink-0" onClick={() => setFormOpen(true)}>
          <Plus className="w-4 h-4" /> Nuevo token
        </button>
      </div>

      {error && (
        <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
          <span className="flex-1">{error}</span>
          <button type="button" onClick={() => void refresh()} className="font-semibold hover:underline shrink-0">
            Reintentar
          </button>
        </div>
      )}

      {/* Token recién creado: única oportunidad de copiarlo */}
      {newToken && (
        <div className="card p-4 border-brand/50">
          <div className="text-sm font-medium text-ink mb-1">
            Token creado — cópialo ahora, no se volverá a mostrar
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <code className="flex-1 min-w-[220px] text-xs bg-paper2 border border-line rounded-coro-sm px-2 py-1.5 break-all select-all">
              {newToken}
            </code>
            <button type="button" className="btn" onClick={() => void copyToken()}>
              {copied ? <Check className="w-4 h-4 text-state-ok" /> : <Copy className="w-4 h-4" />}
              {copied ? "Copiado" : "Copiar"}
            </button>
            <button type="button" className="btn-ghost" onClick={() => setNewToken(null)}>
              Hecho
            </button>
          </div>
          <p className="text-[11px] text-ink3 mt-2">
            Úsalo como cabecera: <code>Authorization: Bearer {"<token>"}</code> contra{" "}
            <code>/api/v1/agent-api/…</code> (guía en <code>docs/agent_api.md</code> del repositorio).
          </p>
        </div>
      )}

      {/* Formulario de creación */}
      {formOpen && (
        <div className="card p-4 space-y-3">
          <div className="text-sm font-medium text-ink">Nuevo token de acceso (API / MCP)</div>
          <input
            className="input w-full"
            placeholder="Nombre (p. ej. Asistente, Panel interno)"
            aria-label="Nombre del token"
            maxLength={100}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <div className="space-y-2">
            {SCOPES.map((s) => (
              <label key={s.key} className="flex items-start gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={scopes.has(s.key)}
                  onChange={() => toggleScope(s.key)}
                />
                <span className="text-xs">
                  <span className="font-medium text-ink">{s.label}</span>
                  <span className="text-ink3"> — {s.hint}</span>
                </span>
              </label>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <input
              className="input w-28"
              placeholder="Caducidad"
              aria-label="Caducidad en días (opcional)"
              inputMode="numeric"
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value.replace(/\D/g, ""))}
            />
            <span className="text-[11px] text-ink3">días (vacío = sin caducidad)</span>
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" className="btn" onClick={() => setFormOpen(false)} disabled={creating}>
              Cancelar
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onCreate()}
              disabled={creating || !name.trim() || scopes.size === 0}
            >
              {creating ? "Creando…" : "Crear token"}
            </button>
          </div>
        </div>
      )}

      {/* Lista */}
      {loading ? (
        <div className="text-center py-10 text-ink3 inline-flex items-center justify-center gap-2 w-full">
          <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
        </div>
      ) : tokens.length === 0 ? (
        <div className="text-center text-ink3 text-sm italic font-display py-8">
          Sin tokens. Crea uno para conectar a tu asistente.
        </div>
      ) : (
        <div className="space-y-2">
          {tokens.map((t) => (
            <div key={t.id} className="card p-3 flex flex-wrap items-center gap-3">
              <Bot className={"w-4 h-4 shrink-0 " + (t.active ? "text-brand-ink" : "text-ink4")} />
              <div className="flex-1 min-w-[180px]">
                <div className="text-sm font-medium text-ink flex items-center gap-2">
                  {t.name}
                  {!t.active && (
                    <span className="badge text-[10px] bg-state-bad/10 text-state-bad border-transparent">
                      revocado
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-ink3 font-mono">
                  {t.token_prefix}… · {t.scopes.join(", ")}
                </div>
                <div className="text-[11px] text-ink4 mt-0.5">
                  {t.last_used_at
                    ? `Último uso: ${new Date(t.last_used_at).toLocaleString()}`
                    : "Nunca usado"}
                  {t.expires_at && ` · caduca ${new Date(t.expires_at).toLocaleDateString()}`}
                </div>
              </div>
              {t.active && (
                <button
                  type="button"
                  className="btn-ghost text-xs text-state-bad shrink-0"
                  onClick={() => setToRevoke(t)}
                >
                  <ShieldOff className="w-3.5 h-3.5" /> Revocar
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Conectar por MCP: URL del endpoint + comando copiable */}
      <div className="card p-4 space-y-3">
        <div className="flex items-center gap-2 text-sm font-medium text-ink">
          <Plug className="w-4 h-4 text-brand-ink" /> Conectar por MCP
        </div>
        <p className="text-[11px] text-ink3">
          Servidor MCP (transporte HTTP streamable). Añádelo a tu cliente MCP con el
          comando de abajo{newToken ? "" : ", sustituyendo <tu_token> por el token que crees arriba"}.
          El token se envía en la cabecera <code>Authorization: Bearer</code>.
        </p>

        <div>
          <label className="label text-[11px]">URL del endpoint MCP</label>
          <div className="flex flex-wrap items-center gap-2">
            <code className="flex-1 min-w-[220px] text-xs bg-paper2 border border-line rounded-coro-sm px-2 py-1.5 break-all select-all">
              {mcpUrl}
            </code>
            <button type="button" className="btn shrink-0" onClick={() => void copyKeyed(mcpUrl, "url")}>
              {copiedKey === "url" ? <Check className="w-4 h-4 text-state-ok" /> : <Copy className="w-4 h-4" />}
              {copiedKey === "url" ? "Copiado" : "Copiar"}
            </button>
          </div>
        </div>

        <div>
          <label className="label text-[11px]">Comando para registrarlo</label>
          <div className="flex flex-wrap items-center gap-2">
            <code className="flex-1 min-w-[220px] text-xs bg-paper2 border border-line rounded-coro-sm px-2 py-1.5 break-all select-all">
              {mcpAddCommand}
            </code>
            <button type="button" className="btn shrink-0" onClick={() => void copyKeyed(mcpAddCommand, "cmd")}>
              {copiedKey === "cmd" ? <Check className="w-4 h-4 text-state-ok" /> : <Copy className="w-4 h-4" />}
              {copiedKey === "cmd" ? "Copiado" : "Copiar"}
            </button>
          </div>
        </div>
      </div>

      <ConfirmModal
        open={!!toRevoke}
        tone="danger"
        title="Revocar token"
        description={
          <>
            El token <span className="font-semibold text-ink2">{toRevoke?.name}</span> dejará de
            funcionar inmediatamente. Esta acción no se puede deshacer (puedes crear uno nuevo).
          </>
        }
        confirmLabel="Revocar"
        cancelLabel="Cancelar"
        busy={revoking}
        onConfirm={() => void onConfirmRevoke()}
        onCancel={() => setToRevoke(null)}
      />
    </div>
  );
}
