import { useState } from "react";
import { Loader2, Phone } from "lucide-react";
import type {
  AgentOut,
  WhatsappProviderName,
  WhatsappProvisionIn,
} from "@/services/admin";
import { SetupUrlRow, WHATSAPP_PROVIDERS, channelSetupUrls } from "./shared";
import { errorDetail } from "@/lib/errors";

/**
 * Alta y edición del canal de WhatsApp, con las DOS formas de conectarlo:
 * YCloud (un intermediario) o la API Cloud oficial de Meta (directo).
 *
 * Lo primero que se elige es con quién se habla, porque de eso depende todo lo
 * demás: qué datos se piden, qué URL hay que pegar fuera y en qué panel. Al
 * editar se pueden dejar los secretos en blanco —el backend solo pisa lo que
 * llega con valor—, así que cambiar de agente no obliga a reteclear la clave.
 */
export function WhatsappFormModal({
  agents,
  proveedorActual,
  yaConectado,
  onClose,
  onSubmit,
}: {
  agents: AgentOut[];
  /** El que tiene puesto el canal ahora mismo (o "ycloud" si aún no hay canal). */
  proveedorActual: WhatsappProviderName;
  /** True si el canal ya existe: cambia los textos y permite dejar huecos. */
  yaConectado: boolean;
  onClose: () => void;
  onSubmit: (body: WhatsappProvisionIn) => Promise<void>;
}) {
  const [provider, setProvider] = useState<WhatsappProviderName>(proveedorActual);
  const [form, setForm] = useState<WhatsappProvisionIn>({
    provider: proveedorActual,
    api_key: "",
    webhook_secret: "",
    phone_number: "",
    phone_number_id: "",
    business_account_id: "",
    access_token: "",
    app_secret: "",
    verify_token: "",
    agent_id: null,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cambiaDeProveedor = yaConectado && provider !== proveedorActual;
  const setup = channelSetupUrls("whatsapp", provider)[0];

  function gen(): string {
    return Array.from(crypto.getRandomValues(new Uint8Array(24)))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }

  function set(campo: keyof WhatsappProvisionIn, valor: string) {
    setForm((f) => ({ ...f, [campo]: valor }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSubmit({ ...form, provider });
    } catch (e: unknown) {
      setError(errorDetail(e, "No se pudo guardar la conexión de WhatsApp."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <form
        onSubmit={submit}
        className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-xl flex flex-col max-h-[90vh] overflow-auto"
      >
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Phone className="w-4 h-4 text-[#25D366]" />
          <h2 className="font-display text-xl text-ink flex-1">
            {yaConectado ? "Editar la conexión de WhatsApp" : "Conectar WhatsApp"}
          </h2>
        </header>

        <div className="p-5 space-y-4">
          {/* 1 · Con quién se habla. Va primero porque manda sobre el resto. */}
          <div>
            <div className="text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1.5">
              ¿Cómo quieres conectarlo?
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {(Object.keys(WHATSAPP_PROVIDERS) as WhatsappProviderName[]).map((p) => {
                const meta = WHATSAPP_PROVIDERS[p];
                const activo = provider === p;
                return (
                  <button
                    key={p}
                    type="button"
                    onClick={() => setProvider(p)}
                    aria-pressed={activo}
                    className={`text-left rounded-coro-sm border p-3 transition ${
                      activo
                        ? "border-brand-ink bg-brand/5"
                        : "border-line bg-paper2/40 hover:bg-paper2"
                    }`}
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="font-medium text-sm text-ink">{meta.label}</span>
                      {p === proveedorActual && yaConectado && (
                        <span className="pill bg-paper3 text-ink3 text-[10px]">Actual</span>
                      )}
                    </div>
                    <p className="text-[11px] text-ink3 mt-1 leading-relaxed">{meta.description}</p>
                  </button>
                );
              })}
            </div>
          </div>

          {cambiaDeProveedor && (
            <p className="text-[11px] text-ink2 bg-state-warn/10 border border-state-warn/25 rounded-coro-sm px-3 py-2">
              Vas a cambiar de {WHATSAPP_PROVIDERS[proveedorActual].label} a{" "}
              {WHATSAPP_PROVIDERS[provider].label}. A partir de que guardes, los mensajes salen por
              el nuevo, así que <b>tienes que pegar la URL de abajo en {WHATSAPP_PROVIDERS[provider].panel}</b> o
              dejarán de entrar mensajes. Lo que tenías guardado del otro no se borra: si vuelves,
              sigue ahí.
            </p>
          )}

          {yaConectado && !cambiaDeProveedor && (
            <p className="text-[11px] text-ink2 bg-paper2 border border-line rounded-coro-sm px-3 py-2">
              Rellena <b>solo lo que quieras cambiar</b> y deja el resto en blanco: lo que ya está
              guardado no se borra.
            </p>
          )}

          {/* 2 · Los datos del proveedor elegido. */}
          {provider === "ycloud" ? (
            <div className="space-y-3">
              <p className="text-sm text-ink2">
                Necesitas una cuenta de YCloud con un número de WhatsApp Business ya verificado. Las
                claves se guardan cifradas y no se vuelven a mostrar.
              </p>
              <Campo
                etiqueta="Clave de API"
                ayuda="Panel de YCloud → Developers → API Key. La genera YCloud; tú la copias."
                secreto
                value={form.api_key ?? ""}
                onChange={(v) => set("api_key", v)}
              />
              <Campo
                etiqueta="Secreto del webhook"
                ayuda="Lo genera YCloud, no tú: al crear el endpoint aparece en la lista de Webhooks como «Secret: whsec_…», con un botón para copiarlo. Pégalo aquí tal cual. Sirve para comprobar que los mensajes vienen de verdad de YCloud: si no coincide con el suyo, se rechazan todos."
                secreto
                value={form.webhook_secret ?? ""}
                onChange={(v) => set("webhook_secret", v)}
              />
              <Campo
                etiqueta="Número de WhatsApp (con prefijo)"
                ayuda="El que tienes dado de alta en YCloud."
                placeholder="+34600000000"
                value={form.phone_number ?? ""}
                onChange={(v) => set("phone_number", v)}
              />
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-sm text-ink2">
                Necesitas una app en{" "}
                <a
                  href="https://developers.facebook.com/apps"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-brand-ink underline"
                >
                  Meta for Developers
                </a>{" "}
                con el producto WhatsApp añadido y un número verificado. Los cuatro primeros datos
                salen de ahí; la palabra de verificación te la inventas tú.
              </p>
              <Campo
                etiqueta="Identificador del número"
                ayuda="WhatsApp → Configuración de la API. Es el número largo que sale debajo del teléfono, no el teléfono."
                value={form.phone_number_id ?? ""}
                onChange={(v) => set("phone_number_id", v)}
                placeholder="123456789012345"
              />
              <Campo
                etiqueta="Identificador de la cuenta de WhatsApp Business"
                ayuda="En la misma pantalla. Hace falta para poder listar tus plantillas."
                value={form.business_account_id ?? ""}
                onChange={(v) => set("business_account_id", v)}
                placeholder="123456789012345"
              />
              <Campo
                etiqueta="Token de acceso permanente"
                ayuda="Business Manager → Configuración del negocio → Usuarios del sistema → Generar token. El token de prueba que sale en la pantalla de la API caduca a las 24 horas: sirve para probar, no para dejarlo puesto."
                secreto
                value={form.access_token ?? ""}
                onChange={(v) => set("access_token", v)}
                placeholder="EAAG..."
              />
              <Campo
                etiqueta="Clave secreta de la app"
                ayuda="Configuración → Básica → Clave secreta de la app. Es con lo que se comprueba que los mensajes vienen de verdad de Meta."
                secreto
                value={form.app_secret ?? ""}
                onChange={(v) => set("app_secret", v)}
              />
              <Campo
                etiqueta="Palabra de verificación del webhook"
                ayuda="Te la inventas tú y la escribes también en Meta al guardar la URL de abajo. Tienen que ser idénticas o Meta no deja guardar."
                value={form.verify_token ?? ""}
                onChange={(v) => set("verify_token", v)}
                onGenerar={() => set("verify_token", gen())}
                placeholder="Una cadena larga"
              />
              <Campo
                etiqueta="Número de WhatsApp (opcional)"
                ayuda="Solo para verlo en el panel. Meta no lo necesita para enviar."
                placeholder="+34600000000"
                value={form.phone_number ?? ""}
                onChange={(v) => set("phone_number", v)}
              />
            </div>
          )}

          {/* 3 · Quién contesta. */}
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">
              Agente que responde
            </label>
            <select
              value={form.agent_id || ""}
              onChange={(e) =>
                setForm((f) => ({ ...f, agent_id: e.target.value === "" ? null : e.target.value }))
              }
              className="input w-full text-xs"
            >
              <option value="">— Sin asignar —</option>
              {agents
                .filter((a) => a.is_active)
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.model_name})
                  </option>
                ))}
            </select>
          </div>

          {/* 4 · Lo que hay que pegar fuera. */}
          <SetupUrlRow label={setup.label} url={setup.url} donde={setup.donde} />

          {error && <div className="text-state-bad text-xs">{error}</div>}
        </div>

        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cancelar
          </button>
          <button type="submit" disabled={busy} className="btn-primary inline-flex items-center gap-1.5">
            {busy && <Loader2 className="w-4 h-4 animate-spin" />}
            {yaConectado ? "Guardar" : "Conectar"}
          </button>
        </footer>
      </form>
    </div>
  );
}

/** Un campo del formulario con su explicación debajo del rótulo. La ayuda no
 * es adorno: es la diferencia entre saber de dónde sacar el dato y no saberlo. */
function Campo({
  etiqueta,
  ayuda,
  value,
  onChange,
  onGenerar,
  secreto,
  placeholder,
}: {
  etiqueta: string;
  ayuda: string;
  value: string;
  onChange: (v: string) => void;
  onGenerar?: () => void;
  secreto?: boolean;
  placeholder?: string;
}) {
  return (
    <div>
      <label className="block text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">
        {etiqueta}
      </label>
      <div className="flex gap-2">
        <input
          type={secreto ? "password" : "text"}
          autoComplete="off"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="input w-full font-mono text-xs flex-1"
          placeholder={placeholder}
        />
        {onGenerar && (
          <button type="button" onClick={onGenerar} className="btn-ghost text-xs shrink-0">
            Generar
          </button>
        )}
      </div>
      <p className="text-[10px] text-ink3 mt-1 leading-relaxed">{ayuda}</p>
    </div>
  );
}
