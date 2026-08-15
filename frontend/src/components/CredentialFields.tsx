import { useEffect, useMemo, useState } from "react";
import { Check, X, Eye, EyeOff, Save, Loader2 } from "lucide-react";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  listCredentials,
  testCredential,
  updateCredential,
  type CredentialOut,
} from "@/services/admin";
import { errorDetail } from "@/lib/errors";

/**
 * Campos para pegar claves guardadas cifradas, para incrustar dentro de la
 * tarjeta del servicio que las usa.
 *
 * Vivían en `pages/admin/CredentialsPage.tsx`, que además pintaba la lista
 * completa de todas las claves al final de Conexiones. Esa lista repetía lo
 * que ya salía en cada tarjeta —las mismas claves de WhatsApp aparecían dos
 * veces, y las de Google hasta cinco— así que se quitó y estos campos se
 * quedaron aquí, que es donde tienen sentido: son un componente, no una
 * página.
 */

/** Nombre legible de cada clave, tal y como se llama en el panel del
 * proveedor. Si aquí no está, la pantalla enseñaría el nombre interno, que a
 * quien está configurando no le dice nada. */
export const CREDENTIAL_LABELS: Record<string, string> = {
  // WhatsApp vía YCloud
  ycloud_api_key: "Clave de API",
  ycloud_webhook_secret: "Secreto del webhook",
  ycloud_phone_number: "Número de WhatsApp (con prefijo)",
  // WhatsApp vía la API oficial de Meta
  meta_wa_phone_number_id: "Identificador del número",
  meta_wa_business_account_id: "Identificador de la cuenta de WhatsApp Business",
  meta_wa_access_token: "Token de acceso permanente",
  meta_wa_app_secret: "Clave secreta de la app",
  meta_wa_verify_token: "Palabra de verificación del webhook",
  // openai_api_key se gestiona en Conexiones → Proveedores LLM.
  resend_api_key: "Clave de API",
  resend_from_email: "Remitente (De:)",
  smtp_host: "Servidor",
  smtp_port: "Puerto",
  smtp_user: "Usuario",
  smtp_password: "Contraseña",
  smtp_from: "Remitente (De:)",
  google_oauth_client_id: "ID de cliente",
  google_oauth_client_secret: "Secreto de cliente",
  instagram_oauth_client_id: "Identificador de la app",
  instagram_oauth_client_secret: "Clave secreta de la app",
};

/** De dónde se saca cada clave. Es la línea que evita la pregunta "¿y esto de
 * dónde lo cojo?", que es donde se atasca todo el mundo la primera vez. */
export const CREDENTIAL_HINTS: Record<string, string> = {
  ycloud_api_key: "Panel de YCloud → Settings → API Key.",
  ycloud_webhook_secret:
    "Te lo inventas tú y lo repites en YCloud al crear el endpoint del webhook. Sirve para comprobar que los mensajes vienen de verdad de YCloud.",
  ycloud_phone_number: "El número que tienes dado de alta en YCloud, con prefijo: +34600000000.",
  meta_wa_phone_number_id:
    "Meta for Developers → tu app → WhatsApp → Configuración de la API. Es el número largo que sale bajo el teléfono, no el teléfono.",
  meta_wa_business_account_id:
    "En la misma pantalla, «Identificador de la cuenta de WhatsApp Business». Hace falta para poder listar tus plantillas.",
  meta_wa_access_token:
    "Un token permanente de usuario del sistema (Business Manager → Configuración del negocio → Usuarios del sistema). El token de prueba de 24 h sirve para probar, pero se caduca solo.",
  meta_wa_app_secret:
    "Meta for Developers → tu app → Configuración → Básica → Clave secreta de la app. Es con lo que se comprueba que los mensajes vienen de verdad de Meta.",
  meta_wa_verify_token:
    "Te la inventas tú y la repites en Meta al guardar la URL del webhook. Tienen que ser idénticas.",
  resend_api_key: "Panel de Resend → API Keys.",
  resend_from_email: "La dirección desde la que salen los correos. Su dominio tiene que estar verificado en Resend.",
  smtp_host: "Para Gmail o Google Workspace: smtp.gmail.com.",
  smtp_port: "587 con STARTTLS es lo habitual.",
  smtp_password:
    "No es la contraseña de la cuenta: es una contraseña de aplicación generada en la seguridad de la cuenta de Google.",
  google_oauth_client_id:
    "Google Cloud Console → APIs y servicios → Credenciales → cliente de OAuth de tipo Web.",
  google_oauth_client_secret: "El mismo cliente de OAuth, justo debajo del identificador.",
  instagram_oauth_client_id: "Meta for Developers → tu app → Configuración → Básica.",
  instagram_oauth_client_secret: "En la misma pantalla, debajo del identificador.",
};

/** Una fila de credencial: nombre, si está puesta o no, el campo para pegar el
 * valor (oculto por defecto), Guardar y Probar. Es presentacional a propósito:
 * el estado vive en quien la usa. */
function CredentialRow({
  cred,
  className,
  draft,
  show,
  saving,
  status,
  onDraft,
  onToggleShow,
  onSave,
  onTest,
}: {
  cred: CredentialOut;
  className?: string;
  draft: string | undefined;
  show: boolean;
  saving: boolean;
  status: { ok: boolean; message: string } | null;
  onDraft: (value: string) => void;
  onToggleShow: () => void;
  onSave: () => void;
  onTest: () => void;
}) {
  const label = CREDENTIAL_LABELS[cred.key] || cred.key;
  const hint = CREDENTIAL_HINTS[cred.key];
  return (
    <div id={`cred-${cred.key}`} className={className}>
      <div className="flex items-center justify-between gap-3 mb-1">
        <div className="min-w-0">
          <div className="font-medium text-sm text-ink">{label}</div>
          {hint && <div className="text-[11px] text-ink3 leading-snug mt-0.5">{hint}</div>}
        </div>
        <div className="text-xs shrink-0">
          {cred.value_masked ? (
            <span className="text-state-ok">Guardada</span>
          ) : (
            <span className="text-ink3">Falta</span>
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-2 items-center mt-1.5">
        <input
          type={show ? "text" : "password"}
          placeholder={cred.value_masked || "Pega aquí el valor…"}
          aria-label={`Valor de ${label}`}
          autoComplete="off"
          value={draft ?? ""}
          onChange={(e) => onDraft(e.target.value)}
          className="input flex-1 min-w-[160px] font-mono text-xs"
        />
        <button
          className="btn-ghost"
          type="button"
          aria-label={show ? "Ocultar el valor" : "Mostrar el valor"}
          onClick={onToggleShow}
        >
          {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
        <button
          className="btn-primary"
          type="button"
          disabled={draft === undefined || saving}
          onClick={onSave}
        >
          <Save className="w-4 h-4" /> Guardar
        </button>
        <button className="btn-ghost" type="button" onClick={onTest} disabled={!cred.value_masked}>
          Probar
        </button>
      </div>
      {status && (
        <div
          className={`mt-2 text-xs flex items-center gap-1 ${
            status.ok ? "text-state-ok" : "text-state-bad"
          }`}
        >
          {status.ok ? <Check className="w-3 h-3" /> : <X className="w-3 h-3" />}
          {status.message}
        </div>
      )}
    </div>
  );
}

/**
 * Editor de UN puñado de claves concretas, para incrustarlo dentro de la
 * tarjeta del servicio que las usa. Confirma antes de pisar un valor ya
 * guardado (el anterior no se puede recuperar) y trae el botón de probar al
 * lado de cada clave.
 */
export function InlineCredentials({
  keys,
  onSaved,
}: {
  keys: string[];
  /** Se llama tras guardar: la tarjeta puede querer refrescar su estado. */
  onSaved?: () => void;
}) {
  const [items, setItems] = useState<CredentialOut[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [show, setShow] = useState<Record<string, boolean>>({});
  const [status, setStatus] = useState<Record<string, { ok: boolean; message: string } | null>>({});
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const wanted = useMemo(() => new Set(keys), [keys]);

  async function refresh() {
    try {
      const d = await listCredentials();
      // Se respeta el ORDEN pedido, no el que devuelva el backend: en una
      // tarjeta las claves tienen un orden que se sigue al configurarlas.
      const byKey = new Map(d.map((c) => [c.key, c]));
      setItems(keys.map((k) => byKey.get(k)).filter((c): c is CredentialOut => Boolean(c)));
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar las claves."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys.join(",")]);

  async function save(key: string) {
    const val = draft[key];
    if (val === undefined) return;
    setConfirmKey(null);
    setSaving(true);
    try {
      await updateCredential(key, val);
      setDraft((d) => {
        const { [key]: _drop, ...rest } = d;
        return rest;
      });
      await refresh();
      onSaved?.();
    } catch (e) {
      setError(errorDetail(e, "No se pudo guardar la clave."));
    } finally {
      setSaving(false);
    }
  }

  function onSaveClick(c: CredentialOut) {
    // Solo se confirma si va a PISAR un valor ya guardado, porque el anterior
    // no se puede recuperar.
    if (c.value_masked) setConfirmKey(c.key);
    else void save(c.key);
  }

  async function onTest(key: string) {
    setStatus((s) => ({ ...s, [key]: null }));
    try {
      const r = await testCredential(key);
      setStatus((s) => ({ ...s, [key]: r }));
    } catch (e) {
      setStatus((s) => ({
        ...s,
        [key]: { ok: false, message: errorDetail(e, "No se pudo probar la conexión.") },
      }));
    }
  }

  if (loading) {
    return (
      <div className="text-xs text-ink3 inline-flex items-center gap-1.5 py-2">
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> Cargando claves…
      </div>
    );
  }
  if (!items.length && !error) {
    return (
      <div className="text-xs text-ink3 py-2">
        Este servicio no guarda claves propias: se configura desde su botón de conexión.
      </div>
    );
  }

  return (
    <div className="rounded-coro-sm border border-line bg-paper2/60 divide-y divide-line2">
      <ConfirmModal
        open={confirmKey !== null}
        title="¿Sobrescribir esta clave?"
        description={`Vas a reemplazar el valor guardado de "${confirmKey ? CREDENTIAL_LABELS[confirmKey] || confirmKey : ""}". El valor anterior no se puede recuperar.`}
        confirmLabel="Sobrescribir"
        tone="danger"
        busy={saving}
        onConfirm={() => confirmKey && void save(confirmKey)}
        onCancel={() => setConfirmKey(null)}
      />
      {error && <div className="px-3 py-2 text-xs text-state-bad">{error}</div>}
      {items.map((c) => (
        <CredentialRow
          key={c.key}
          cred={c}
          className="px-3 py-2.5"
          draft={draft[c.key]}
          show={Boolean(show[c.key])}
          saving={saving}
          status={status[c.key] ?? null}
          onDraft={(v) => setDraft((d) => ({ ...d, [c.key]: v }))}
          onToggleShow={() => setShow((s) => ({ ...s, [c.key]: !s[c.key] }))}
          onSave={() => onSaveClick(c)}
          onTest={() => void onTest(c.key)}
        />
      ))}
    </div>
  );
}
