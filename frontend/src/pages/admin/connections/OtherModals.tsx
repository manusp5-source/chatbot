import { useMemo, useState } from "react";
import { Instagram, Loader2, Mic, Plug, Save } from "lucide-react";
import type {
  ChannelOut,
  InstagramProvisionIn,
  InstagramProvisionOut,
  RetellProvisionIn,
  RetellProvisionOut,
  WebchatProvision,
} from "@/services/admin";
import { errorDetail } from "@/lib/errors";

/**
 * Los avisos y formularios de los canales que no son WhatsApp. Salen tal cual
 * estaban: al rehacer Conexiones solo se mudaron de fichero, porque el de la
 * pantalla se había ido a 2.800 líneas y no había quien lo leyera.
 */

/** Lee `config.allowed_domains` de un canal. El backend acepta lista o cadena
 * separada por comas/saltos de línea, así que aquí se normaliza igual. */
export function readAllowedDomains(channel: ChannelOut | null | undefined): string[] {
  const raw = channel?.config?.allowed_domains;
  if (Array.isArray(raw)) {
    return raw.map((d) => String(d).trim()).filter(Boolean);
  }
  if (typeof raw === "string") {
    return raw.split(/[\n,]/).map((d) => d.trim()).filter(Boolean);
  }
  return [];
}

function gen(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(24)))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// ---------------------------------------------------------------------------
// Instagram
// ---------------------------------------------------------------------------

export function InstagramFormModal({
  onClose,
  onSubmit,
}: {
  onClose: () => void;
  onSubmit: (body: InstagramProvisionIn) => Promise<void>;
}) {
  const [form, setForm] = useState<InstagramProvisionIn>({
    page_id: "",
    page_access_token: "",
    verify_token: "",
    app_secret: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSubmit(form);
    } catch (e: unknown) {
      setError(errorDetail(e, "No se pudo guardar la conexión de Instagram."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <form
        onSubmit={submit}
        className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-lg flex flex-col max-h-[90vh] overflow-auto"
      >
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Instagram className="w-4 h-4 text-[#E4405F]" />
          <h2 className="font-display text-xl text-ink flex-1">Conectar Instagram a mano</h2>
        </header>
        <div className="p-5 space-y-3">
          <p className="text-sm text-ink2">
            Necesitas una app de Meta (Facebook for Developers) con permiso{" "}
            <code className="text-xs">instagram_basic + pages_messaging</code> sobre la página de
            Instagram que quieras conectar.
          </p>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Identificador de la página
            </label>
            <input
              required
              value={form.page_id}
              onChange={(e) => setForm({ ...form, page_id: e.target.value })}
              className="input w-full font-mono text-xs"
              placeholder="123456789012345"
            />
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Token de acceso de la página
            </label>
            <input
              required
              type="password"
              value={form.page_access_token}
              onChange={(e) => setForm({ ...form, page_access_token: e.target.value })}
              className="input w-full font-mono text-xs"
              placeholder="EAAB..."
            />
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Clave secreta de la app
            </label>
            <input
              required
              type="password"
              value={form.app_secret}
              onChange={(e) => setForm({ ...form, app_secret: e.target.value })}
              className="input w-full font-mono text-xs"
              placeholder="abcd..."
            />
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Palabra de verificación (la eliges tú)
            </label>
            <div className="flex gap-2">
              <input
                required
                value={form.verify_token}
                onChange={(e) => setForm({ ...form, verify_token: e.target.value })}
                className="input w-full font-mono text-xs flex-1"
                placeholder="Cualquier cadena larga"
              />
              <button
                type="button"
                onClick={() => setForm({ ...form, verify_token: gen() })}
                className="btn-ghost text-xs"
              >
                Generar
              </button>
            </div>
            <p className="text-[11px] text-ink3 mt-1 italic">
              La escribes aquí Y en el panel de Meta — deben coincidir para activar el webhook.
            </p>
          </div>
          {error && <div className="text-state-bad text-sm">{error}</div>}
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cancelar
          </button>
          <button type="submit" disabled={busy} className="btn-primary">
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plug className="w-4 h-4" />}
            Conectar
          </button>
        </footer>
      </form>
    </div>
  );
}

export function InstagramWebhookModal({
  data,
  onClose,
}: {
  data: InstagramProvisionOut;
  onClose: () => void;
}) {
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [copiedToken, setCopiedToken] = useState(false);
  function copy(text: string, which: "url" | "token") {
    void navigator.clipboard.writeText(text).then(() => {
      if (which === "url") {
        setCopiedUrl(true);
        window.setTimeout(() => setCopiedUrl(false), 1500);
      } else {
        setCopiedToken(true);
        window.setTimeout(() => setCopiedToken(false), 1500);
      }
    });
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <div className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-xl flex flex-col max-h-[90vh] overflow-auto">
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Instagram className="w-4 h-4 text-[#E4405F]" />
          <h2 className="font-display text-xl text-ink flex-1">Instagram conectado</h2>
        </header>
        <div className="p-5 space-y-3">
          <p className="text-sm text-ink2">
            La cuenta ya está autorizada, pero <b>todavía no entra ningún mensaje</b>. Falta el
            webhook en el panel de Meta: es lo que hace que Meta nos avise cuando alguien te
            escribe. Sin este paso el canal se ve conectado y la bandeja se queda vacía.
          </p>
          <ol className="text-sm text-ink2 list-decimal pl-5 space-y-1">
            <li>Ve a Meta for Developers → tu app → Webhooks → Instagram.</li>
            <li>Pega esta URL como Callback URL:</li>
          </ol>
          <div className="flex gap-2 items-center">
            <code className="flex-1 text-[11px] font-mono bg-paper2 border border-line rounded-coro-sm px-3 py-2 break-all">
              {data.webhook_url}
            </code>
            <button
              type="button"
              onClick={() => copy(data.webhook_url, "url")}
              className="btn-ghost text-xs"
            >
              {copiedUrl ? "Copiada" : "Copiar"}
            </button>
          </div>
          <p className="text-sm text-ink2">
            Y esta palabra de verificación en el campo <b>Verify Token</b>, justo debajo:
          </p>
          <div className="flex gap-2 items-center">
            <code className="flex-1 text-[11px] font-mono bg-paper2 border border-line rounded-coro-sm px-3 py-2 break-all">
              {data.verify_token}
            </code>
            <button
              type="button"
              onClick={() => copy(data.verify_token, "token")}
              className="btn-ghost text-xs"
            >
              {copiedToken ? "Copiada" : "Copiar"}
            </button>
          </div>
          <p className="text-sm text-ink2">
            Dale a verificar y guardar, y después suscribe la cuenta al campo{" "}
            <code className="text-xs">messages</code>. En cuanto Meta lo confirme, los mensajes
            empezarán a llegar a la bandeja y el agente responderá igual que en WhatsApp.
          </p>
          <p className="text-[11px] text-ink3">
            Esto no se pierde: lo tienes siempre en la tarjeta de Instagram, dentro de Servicios.
          </p>
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-primary">
            Cerrar
          </button>
        </footer>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Retell (voz)
// ---------------------------------------------------------------------------

export function RetellFormModal({
  onClose,
  onSubmit,
}: {
  onClose: () => void;
  onSubmit: (body: RetellProvisionIn) => Promise<void>;
}) {
  const [form, setForm] = useState<RetellProvisionIn>({
    api_key: "",
    agent_id_retell: "",
    voice_id: "",
    phone_number: "",
    greeting: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSubmit(form);
    } catch (e: unknown) {
      setError(errorDetail(e, "No se pudo guardar la conexión de Retell."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <form
        onSubmit={submit}
        className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-lg flex flex-col max-h-[90vh] overflow-auto"
      >
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Mic className="w-4 h-4 text-[#9B8AFB]" />
          <h2 className="font-display text-xl text-ink flex-1">Conectar las llamadas de voz</h2>
        </header>
        <div className="p-5 space-y-3">
          <p className="text-sm text-ink2">
            Llamadas de voz por teléfono. Necesitas una cuenta en{" "}
            <a
              href="https://retellai.com"
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand-ink underline"
            >
              retellai.com
            </a>
            : crea un agente con LLM personalizado y trae los valores de su panel.
          </p>
          <p className="text-[11px] text-ink2 bg-paper2 border border-line rounded-coro-sm px-3 py-2">
            Solo hacen falta la clave de API y el identificador del agente. La voz y el teléfono son
            para tenerlos apuntados, así que puedes conectar antes de comprar número. ¿Editando?
            Rellena <b>solo lo que quieras cambiar</b> y deja el resto en blanco — la clave actual no
            se borra.
          </p>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Clave de API de Retell
            </label>
            <input
              type="password"
              value={form.api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              className="input w-full font-mono text-xs"
              placeholder="key_..."
            />
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Identificador del agente (en Retell)
            </label>
            <input
              value={form.agent_id_retell}
              onChange={(e) => setForm({ ...form, agent_id_retell: e.target.value })}
              className="input w-full font-mono text-xs"
              placeholder="agent_..."
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Voz (opcional)
              </label>
              <input
                value={form.voice_id}
                onChange={(e) => setForm({ ...form, voice_id: e.target.value })}
                className="input w-full font-mono text-xs"
                placeholder="11labs-Adrian o similar"
              />
            </div>
            <div>
              <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
                Teléfono virtual (opcional)
              </label>
              <input
                value={form.phone_number}
                onChange={(e) => setForm({ ...form, phone_number: e.target.value })}
                className="input w-full font-mono text-xs"
                placeholder="+34..."
              />
            </div>
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Saludo inicial
            </label>
            <textarea
              rows={2}
              value={form.greeting ?? ""}
              onChange={(e) => setForm({ ...form, greeting: e.target.value })}
              className="input w-full text-sm leading-relaxed"
              placeholder="Hola, soy el asistente virtual. ¿En qué puedo ayudarte?"
            />
            <p className="text-[11px] text-ink3 mt-1 italic">
              Lo que dice el agente nada más descolgar. Déjalo vacío para usar el saludo por
              defecto. Puedes cambiarlo luego desde la tarjeta del canal.
            </p>
          </div>
          {error && <div className="text-state-bad text-sm">{error}</div>}
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cancelar
          </button>
          <button type="submit" disabled={busy} className="btn-primary">
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plug className="w-4 h-4" />}
            Conectar
          </button>
        </footer>
      </form>
    </div>
  );
}

export function RetellWebhookModal({
  data,
  onClose,
}: {
  data: RetellProvisionOut;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState<"llm" | "webhook" | null>(null);
  function copy(which: "llm" | "webhook", text: string) {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(which);
      window.setTimeout(() => setCopied(null), 1500);
    });
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <div className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-xl flex flex-col max-h-[90vh] overflow-auto">
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Mic className="w-4 h-4 text-[#9B8AFB]" />
          <h2 className="font-display text-xl text-ink flex-1">Voz conectada</h2>
        </header>
        <div className="p-5 space-y-4">
          <p className="text-sm text-ink2">
            Faltan <b>dos URLs</b> en el panel de Retell, dentro de tu agente. Son distintas y hacen
            cosas distintas: hay que pegar las dos.
          </p>

          <div className="border border-line rounded-coro-sm p-3 space-y-2 bg-paper2/40">
            <div className="text-[12px] font-semibold text-ink">
              1 · Cerebro del agente <span className="text-ink3 font-normal">(WebSocket)</span>
            </div>
            <p className="text-[12px] text-ink2">
              Es quien <b>contesta</b> a quien llama. En Retell: tu agente → <i>Response Engine</i>{" "}
              → Custom LLM → <i>Websocket URL</i>.
            </p>
            <div className="flex gap-2 items-center">
              <code className="flex-1 text-[11px] font-mono bg-paper2 border border-line rounded-coro-sm px-3 py-2 break-all">
                {data.llm_webhook_url}
              </code>
              <button
                type="button"
                onClick={() => copy("llm", data.llm_webhook_url)}
                className="btn-ghost text-xs"
              >
                {copied === "llm" ? "Copiada" : "Copiar"}
              </button>
            </div>
          </div>

          <div className="border border-line rounded-coro-sm p-3 space-y-2 bg-paper2/40">
            <div className="text-[12px] font-semibold text-ink">
              2 · Avisos de la llamada <span className="text-ink3 font-normal">(HTTP)</span>
            </div>
            <p className="text-[12px] text-ink2">
              Es quien trae, al colgar, la <b>duración</b>, la <b>grabación</b>, el <b>resumen</b> y
              el <b>motivo de fin</b> de cada llamada. En Retell: tu agente → <i>Webhook Settings</i>{" "}
              → <i>Agent Level Webhook URL</i>. Si no la pegas, las llamadas funcionan igual pero la
              sección Llamadas se queda sin esos datos.
            </p>
            <div className="flex gap-2 items-center">
              <code className="flex-1 text-[11px] font-mono bg-paper2 border border-line rounded-coro-sm px-3 py-2 break-all">
                {data.call_webhook_url}
              </code>
              <button
                type="button"
                onClick={() => copy("webhook", data.call_webhook_url)}
                className="btn-ghost text-xs"
              >
                {copied === "webhook" ? "Copiada" : "Copiar"}
              </button>
            </div>
          </div>

          {data.signed_recordings_enabled ? (
            <p className="text-[12px] text-ink2 bg-state-ok/10 border border-state-ok/30 rounded-coro-sm px-3 py-2">
              <b>Grabaciones protegidas.</b> Hemos activado en tu agente las grabaciones con enlace
              firmado (caduca y no se puede compartir) y lo hemos publicado. Ojo:{" "}
              <b>no es retroactivo</b> — las grabaciones hechas antes de ahora siguen siendo enlaces
              públicos.
            </p>
          ) : (
            <p className="text-[12px] text-ink2 bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2">
              <b>Grabaciones sin proteger.</b> No hemos podido activar el enlace firmado en tu
              agente
              {data.signed_recordings_detail ? `: ${data.signed_recordings_detail}` : "."} Mientras
              tanto, la URL de cada grabación es pública: quien la tenga escucha la llamada entera
              sin contraseña. Actívalo en Retell → agente → <i>opt-in signed URL</i> y publica el
              agente, o vuelve a guardar aquí las claves.
            </p>
          )}

          <p className="text-sm text-ink2">
            Con las dos URLs guardadas, haz una prueba llamando al número virtual. El agente
            responderá igual que en WhatsApp, pero por voz. Las transcripciones aparecen en la
            bandeja con canal <b>Voz</b> y en la sección <b>Llamadas</b>.
          </p>
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-primary">
            Cerrar
          </button>
        </footer>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chat en tu web
// ---------------------------------------------------------------------------

/** Atributos `data-*` de personalización que lee widget.js (ver
 * backend/app/static/widget.js). El backend ya emite estos atributos con sus
 * valores por defecto, así que lo de aquí SUSTITUYE el valor que traen; no se
 * añade otra vez. Cuando se añadían al final, el navegador se quedaba con la
 * primera aparición —la del valor por defecto— y lo que se escribía en el panel
 * no se veía en ninguna parte. */
const WIDGET_FIELDS = [
  { attr: "data-title", label: "Título", placeholder: "Chat" },
  { attr: "data-greeting", label: "Saludo inicial", placeholder: "Hola, ¿en qué te ayudo?" },
  { attr: "data-brand", label: "Color de marca", placeholder: "#DBE09E" },
  {
    attr: "data-subtitle",
    label: "Subtítulo",
    placeholder: "Asistente virtual · respuestas automáticas",
  },
  { attr: "data-privacy-url", label: "Enlace de privacidad", placeholder: "https://tuweb.com/privacidad" },
] as const;

type WidgetFieldAttr = (typeof WIDGET_FIELDS)[number]["attr"];

/** Un valor dentro de un atributo HTML: fuera comillas y ángulos, que romperían
 * el snippet al pegarlo. */
function sanitizeAttrValue(v: string): string {
  return v.replace(/["'<>]/g, "").trim();
}

export function WebchatSnippetModal({
  data,
  channel,
  onSaveDomains,
  onClose,
}: {
  data: WebchatProvision;
  channel: ChannelOut | null;
  onSaveDomains: (channelId: string, domains: string[]) => Promise<void>;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState<"snippet" | "key" | null>(null);
  const [custom, setCustom] = useState<Partial<Record<WidgetFieldAttr, string>>>({});
  const [domains, setDomains] = useState(() => readAllowedDomains(channel).join("\n"));
  const [savingDomains, setSavingDomains] = useState(false);
  const [domainsMsg, setDomainsMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // El snippet que se copia = el del backend, con el valor de cada data-* de
  // personalización SUSTITUIDO por lo que se haya escrito aquí. Si el backend
  // no trajera ese atributo (versión antigua), se añade al final.
  const snippet = useMemo(() => {
    let out = data.snippet;
    for (const { attr } of WIDGET_FIELDS) {
      const v = sanitizeAttrValue(custom[attr] ?? "");
      if (!v) continue;
      const existente = new RegExp(`${attr}="[^"]*"`);
      if (existente.test(out)) {
        out = out.replace(existente, `${attr}="${v}"`);
        continue;
      }
      const head = out.replace(/>\s*<\/script>\s*$/, "");
      if (head === out) continue; // formato inesperado: no tocar
      out = `${head}\n        ${attr}="${v}"></script>`;
    }
    return out;
  }, [data.snippet, custom]);

  const parsedDomains = domains
    .split("\n")
    .map((d) => d.trim())
    .filter(Boolean);

  function copy(text: string, what: "snippet" | "key") {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(what);
      window.setTimeout(() => setCopied(null), 1500);
    });
  }

  async function saveDomains() {
    if (!channel) return;
    setSavingDomains(true);
    setDomainsMsg(null);
    try {
      await onSaveDomains(channel.id, parsedDomains);
      setDomainsMsg({
        ok: true,
        text: parsedDomains.length
          ? `Guardado: el chat solo funcionará en ${parsedDomains.length} dominio(s).`
          : "Guardado: sin restricción de dominios (el chat funciona en cualquier web).",
      });
    } catch (e) {
      setDomainsMsg({ ok: false, text: errorDetail(e, "No se pudieron guardar los dominios.") });
    } finally {
      setSavingDomains(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4">
      <div className="bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-xl flex flex-col max-h-[90vh] overflow-auto">
        <header className="px-5 py-3 border-b border-line flex items-center gap-2">
          <Plug className="w-4 h-4 text-brand-ink" />
          <h2 className="font-display text-xl text-ink flex-1">Chat en tu web</h2>
        </header>
        <div className="p-5 space-y-4">
          <p className="text-sm text-ink2">
            Pega este trozo de código en cualquier web donde quieras el chat. Aparecerá un botón
            flotante abajo a la derecha.
          </p>
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Código para pegar
            </label>
            <div className="flex gap-2 items-start">
              <pre className="flex-1 text-[11px] font-mono bg-paper2 border border-line rounded-coro-sm p-3 overflow-auto max-h-40 whitespace-pre-wrap break-words">
                {snippet}
              </pre>
              <button
                type="button"
                onClick={() => copy(snippet, "snippet")}
                className="btn-ghost text-xs"
              >
                {copied === "snippet" ? "Copiado" : "Copiar"}
              </button>
            </div>
          </div>

          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Personalización (opcional)
            </label>
            <p className="text-[11px] text-ink3 mb-2">
              Lo que rellenes se añade al código de arriba. En blanco, el chat usa su valor por
              defecto.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {WIDGET_FIELDS.map(({ attr, label, placeholder }) => (
                <label key={attr} className="block">
                  <span className="block text-[11px] text-ink3 mb-0.5">{label}</span>
                  <input
                    className="input text-xs w-full"
                    value={custom[attr] ?? ""}
                    placeholder={placeholder}
                    onChange={(e) => setCustom((prev) => ({ ...prev, [attr]: e.target.value }))}
                  />
                </label>
              ))}
            </div>
          </div>

          {/* Dominios permitidos: el único freno real al gasto de modelo. */}
          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Dominios permitidos
            </label>
            <p className="text-[11px] text-ink3 mb-2">
              Uno por línea. Acepta <code className="font-mono">ejemplo.com</code> y{" "}
              <code className="font-mono">*.ejemplo.com</code>. Si lo dejas vacío, el chat funciona
              en cualquier web que pegue el código — y el consumo de modelo lo pagas tú.
            </p>
            <textarea
              className="input text-xs font-mono w-full"
              rows={4}
              value={domains}
              placeholder={"ejemplo.com\n*.ejemplo.com"}
              disabled={!channel}
              onChange={(e) => setDomains(e.target.value)}
            />
            <div className="flex items-center gap-2 mt-2">
              <button
                type="button"
                onClick={() => void saveDomains()}
                disabled={savingDomains || !channel}
                className="btn-ghost text-xs"
                title={!channel ? "Recarga la página para poder editar los dominios" : undefined}
              >
                {savingDomains ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Save className="w-3.5 h-3.5" />
                )}
                Guardar dominios
              </button>
              {parsedDomains.length === 0 && (
                <span className="text-[11px] text-state-warn">Sin restricción</span>
              )}
            </div>
            {domainsMsg && (
              <p
                className={"text-[11px] mt-1 " + (domainsMsg.ok ? "text-state-ok" : "text-state-bad")}
              >
                {domainsMsg.text}
              </p>
            )}
          </div>

          <div>
            <label className="block text-xs uppercase tracking-wider text-ink3 font-medium mb-1">
              Clave pública del chat
            </label>
            <div className="flex gap-2 items-center">
              <code className="flex-1 text-[11px] font-mono bg-paper2 border border-line rounded-coro-sm px-3 py-2 truncate">
                {data.api_key}
              </code>
              <button
                type="button"
                onClick={() => copy(data.api_key, "key")}
                className="btn-ghost text-xs"
              >
                {copied === "key" ? "Copiada" : "Copiar"}
              </button>
            </div>
            <p className="text-[11px] text-ink3 mt-1 italic">
              Es pública por diseño: va dentro del HTML de la web, así que cualquier visitante puede
              leerla. No la trates como un secreto — lo que impide que otra web use tu agente (y te
              gaste el presupuesto de modelo) es la lista de dominios permitidos. Regenerarla sirve
              para ROTARLA: corta las sesiones abiertas y obliga a actualizar el código en todas tus
              webs.
            </p>
          </div>
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-primary">
            Cerrar
          </button>
        </footer>
      </div>
    </div>
  );
}
