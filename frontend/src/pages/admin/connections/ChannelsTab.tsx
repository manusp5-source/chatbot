import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bot,
  Check,
  Globe,
  Loader2,
  Mail,
  Mic,
  Power,
  Save,
  Settings2,
  Trash2,
} from "lucide-react";
import { CREDENTIAL_LABELS } from "@/components/CredentialFields";
import {
  channelMode,
  getAgentPause,
  type AgentOut,
  type AgentPauseState,
  type ChannelOut,
  type ChannelType,
  type CredentialOut,
} from "@/services/admin";
import {
  CHANNEL_META,
  CHANNEL_ORDER,
  EstadoPill,
  QueFalta,
  WHATSAPP_PROVIDERS,
  clavesDelServicio,
  whatsappProviderOf,
  type EstadoServicio,
} from "./shared";
import { readAllowedDomains } from "./OtherModals";

/**
 * Canales: UNA tarjeta por canal, siempre las cinco, existan o no.
 *
 * Esta pestaña es SOLO operación: si el canal está encendido, en qué modo está
 * el bot, qué agente responde y lo que dice al saludar o al firmar. Ni una
 * clave, ni una URL de webhook, ni un botón de conectar — todo eso vive en
 * Servicios, que es donde se conecta cada cosa. Si a un canal le falta algo de
 * allí, la tarjeta lo dice y lleva directa a su sitio.
 */

export type ChannelsTabProps = {
  channels: ChannelOut[];
  agents: AgentOut[];
  creds: CredentialOut[];
  onToggleEnabled: (c: ChannelOut, enabled: boolean) => Promise<void> | void;
  onChangeAgent: (c: ChannelOut, agentId: string | null) => Promise<void> | void;
  onSaveGreeting: (c: ChannelOut, greeting: string) => Promise<void>;
  onSaveEmailSignature: (c: ChannelOut, firma: string) => Promise<void>;
  onDeleteChannel: (c: ChannelOut) => void;
  /** Para el aviso del canal de correo, que depende de Google. */
  gmailConectado: boolean;
  /** Salta a la tarjeta de este servicio en la pestaña Servicios. */
  onIrAServicio: (id: string) => void;
};

// El nombre del canal en la pausa del agente no siempre coincide con el tipo.
const PAUSE_CANAL: Record<string, string> = {
  whatsapp: "whatsapp",
  webchat: "web",
  instagram_dm: "instagram_dm",
  email: "email",
};

/** En qué tarjeta de Servicios se conecta cada canal. */
const SERVICIO_DE: Record<ChannelType, string> = {
  whatsapp: "whatsapp",
  webchat: "webchat",
  instagram_dm: "instagram_dm",
  email: "google",
  retell_voice: "retell_voice",
};

const NOMBRE_SERVICIO: Record<ChannelType, string> = {
  whatsapp: "WhatsApp",
  webchat: "Chat en tu web",
  instagram_dm: "Instagram",
  email: "Google",
  retell_voice: "Llamadas de voz",
};

export function ChannelsTab(props: ChannelsTabProps) {
  // El estado del bot por canal (Activo / Entrenamiento / Pausado) vive en
  // otro endpoint: se pide aparte y si falla, la tarjeta sencillamente no lo
  // pinta — no es motivo para dejar la pantalla sin canales.
  const [pauseState, setPauseState] = useState<AgentPauseState | null>(null);
  useEffect(() => {
    getAgentPause().then(setPauseState).catch(() => {});
  }, []);

  return (
    <div className="max-w-4xl">
      <p className="text-xs text-ink3 mb-3">
        Aquí se opera lo que ya está conectado: encendido, modo del bot y quién responde. Las claves
        y las URLs de cada servicio están en{" "}
        <button
          type="button"
          onClick={() => props.onIrAServicio("")}
          className="underline hover:text-ink"
        >
          Servicios
        </button>
        .
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 items-start">
        {CHANNEL_ORDER.map((type) => (
          <ChannelCard
            key={type}
            type={type}
            channel={props.channels.find((c) => c.type === type) ?? null}
            pauseState={pauseState}
            {...props}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function ChannelCard({
  type,
  channel,
  pauseState,
  ...p
}: ChannelsTabProps & {
  type: ChannelType;
  channel: ChannelOut | null;
  pauseState: AgentPauseState | null;
}) {
  const meta = CHANNEL_META[type];
  const Icon = meta.Icon;
  const waProvider = whatsappProviderOf(channel);
  const { estado, falta, faltaEnServicios } = estadoDelCanal(
    type,
    channel,
    p.creds,
    waProvider,
    p.gmailConectado,
  );

  return (
    <div className="border border-line rounded-coro bg-card p-4 flex flex-col gap-3">
      {/* Cabecera: qué es y cómo está. */}
      <div className="flex items-start gap-3">
        <div
          className={`w-9 h-9 rounded-coro-sm bg-paper2 flex items-center justify-center ${meta.tone}`}
        >
          <Icon className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-medium text-ink truncate">{meta.label}</div>
          <div className="text-xs text-ink3 mt-0.5">{subtitulo(type, channel, waProvider)}</div>
        </div>
        <EstadoPill estado={estado} />
      </div>

      <div className="text-xs text-ink3">{meta.description}</div>

      <QueFalta items={falta}>
        {faltaEnServicios && (
          <button
            type="button"
            onClick={() => p.onIrAServicio(SERVICIO_DE[type])}
            className="btn-ghost text-xs inline-flex items-center gap-1.5"
          >
            <Settings2 className="w-3.5 h-3.5" />
            Ir a Servicios · {NOMBRE_SERVICIO[type]}
          </button>
        )}
      </QueFalta>

      {channel && (
        <>
          {/* Quién responde. */}
          <div className="flex items-center gap-2 text-xs">
            <Bot className="w-3.5 h-3.5 text-ink3 flex-none" />
            <span className="text-ink3">Responde:</span>
            <select
              value={channel.agent_id || ""}
              onChange={(e) =>
                void p.onChangeAgent(channel, e.target.value === "" ? null : e.target.value)
              }
              className="input text-xs py-1 flex-1 min-w-0"
            >
              <option value="">— Sin asignar —</option>
              {p.agents
                .filter((a) => a.is_active)
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.model_name})
                  </option>
                ))}
            </select>
          </div>

          {/* Estado del bot (distinto de canal activo/apagado). */}
          {PAUSE_CANAL[type] && channel.enabled && (
            <EstadoDelBot type={type} pauseState={pauseState} />
          )}

          {/* Ajustes propios de cada canal. Son de conversación, no de
              conexión: por eso se quedan aquí. */}
          {type === "retell_voice" && (
            <VoiceGreetingEditor channel={channel} onSave={p.onSaveGreeting} />
          )}
          {type === "email" && (
            <EmailSignatureEditor channel={channel} onSave={p.onSaveEmailSignature} />
          )}
          {type === "webchat" && (
            <div className="flex items-center gap-2 text-xs">
              <Globe className="w-3.5 h-3.5 text-ink3 flex-none" />
              <span className="text-ink3">
                Dominios permitidos: {readAllowedDomains(channel).length || "todos"}
              </span>
              <button
                type="button"
                onClick={() => p.onIrAServicio("webchat")}
                className="underline text-ink3 hover:text-ink ml-auto"
              >
                gestionar en Servicios
              </button>
            </div>
          )}
        </>
      )}

      {/* Botones. Encender, apagar y borrar: nada de conectar. */}
      <div className="flex justify-end gap-2 pt-1 flex-wrap">
        {!channel ? (
          <button
            type="button"
            onClick={() => p.onIrAServicio(SERVICIO_DE[type])}
            className="btn-primary text-xs inline-flex items-center gap-1.5"
          >
            <Settings2 className="w-3.5 h-3.5" />
            Conectar en Servicios
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => void p.onToggleEnabled(channel, !channel.enabled)}
              className={`btn-ghost text-xs inline-flex items-center gap-1.5 ${
                channel.enabled ? "" : "text-state-ok"
              }`}
              title={
                channel.enabled
                  ? "Apagar el canal: deja de recibir mensajes. (Para pausar solo el bot y atender a mano, usa el panel del agente)"
                  : "Volver a encender el canal"
              }
            >
              <Power className="w-3.5 h-3.5" />
              {channel.enabled ? "Apagar" : "Encender"}
            </button>
            <button
              type="button"
              onClick={() => p.onDeleteChannel(channel)}
              className="btn-ghost text-xs inline-flex items-center gap-1.5 text-state-bad hover:text-state-bad"
              title="Eliminar este canal"
            >
              <Trash2 className="w-3.5 h-3.5" />
              Borrar
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function EstadoDelBot({
  type,
  pauseState,
}: {
  type: ChannelType;
  pauseState: AgentPauseState | null;
}) {
  const info = pauseState?.channels?.find((ci) => ci.canal === PAUSE_CANAL[type]);
  const modo = pauseState?.paused ? "paused" : info ? channelMode(info) : "active";
  const cls =
    modo === "paused"
      ? "bg-state-warn/10 text-state-warn"
      : modo === "training"
        ? "bg-brand/10 text-brand-ink"
        : "bg-state-ok/10 text-state-ok";
  const txt = modo === "paused" ? "Pausado" : modo === "training" ? "Entrenamiento" : "Activo";
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-ink3">Bot:</span>
      <span className={`pill ${cls}`}>{txt}</span>
      <Link
        to="/admin/agent/dashboard"
        className="text-ink3 hover:text-ink underline"
        title="Cambiar el modo del bot por canal (Activo / Entrenamiento / Pausado)"
      >
        gestionar
      </Link>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Estado y textos
// ---------------------------------------------------------------------------

/** Qué falta para que el canal funcione, dicho como se le diría a una persona.
 * `faltaEnServicios` marca si lo que falta se arregla en la otra pestaña, que
 * es cuando tiene sentido enseñar el botón que lleva allí. */
function estadoDelCanal(
  type: ChannelType,
  channel: ChannelOut | null,
  creds: CredentialOut[],
  waProvider: "ycloud" | "meta",
  gmailConectado: boolean,
): { estado: EstadoServicio; falta: string[]; faltaEnServicios: boolean } {
  if (!channel) {
    return {
      estado: "sin-empezar",
      falta: ["Este canal todavía no está conectado: se conecta en la pestaña Servicios."],
      faltaEnServicios: true,
    };
  }

  const falta: string[] = [];
  let faltaEnServicios = false;

  const claves = clavesDelServicio(type, waProvider);
  const puestas = new Set(creds.filter((c) => c.value_masked).map((c) => c.key));
  const sinPoner = claves.filter((k) => !puestas.has(k));
  if (sinPoner.length) {
    falta.push(
      "Faltan claves en Servicios: " +
        sinPoner.map((k) => CREDENTIAL_LABELS[k] || k).join(", ") +
        ".",
    );
    faltaEnServicios = true;
  }
  if (type === "email" && !gmailConectado) {
    falta.push("Autoriza Gmail en Servicios: este canal lee el correo de ahí.");
    faltaEnServicios = true;
  }
  if (!channel.agent_id) falta.push("Elige aquí abajo qué agente responde en este canal.");
  if (!channel.enabled) falta.push("El canal está apagado: enciéndelo para que entren mensajes.");

  return { estado: falta.length ? "medias" : "ok", falta, faltaEnServicios };
}

function subtitulo(
  type: ChannelType,
  channel: ChannelOut | null,
  waProvider: "ycloud" | "meta",
): string {
  if (!channel) return "Todavía sin conectar";
  if (type === "whatsapp") {
    const numero = String(channel.config?.phone_number ?? "").trim();
    return numero || `Vía ${WHATSAPP_PROVIDERS[waProvider].short}`;
  }
  return channel.name;
}

// ---------------------------------------------------------------------------
// Piezas sueltas
// ---------------------------------------------------------------------------

/** El saludo de voz. Vive en la config del canal de voz y se edita sin tocar
 * claves. Guardar solo se habilita cuando hay cambios. */
function VoiceGreetingEditor({
  channel,
  onSave,
}: {
  channel: ChannelOut;
  onSave: (c: ChannelOut, greeting: string) => Promise<void>;
}) {
  return (
    <EditorDeTexto
      icono={<Mic className="w-3.5 h-3.5 text-ink3 flex-none" />}
      titulo="Saludo de voz"
      coletilla="— lo que dice al descolgar"
      persisted={
        typeof channel.config?.greeting === "string" ? (channel.config.greeting as string) : ""
      }
      placeholder="Hola, soy el asistente virtual. ¿En qué puedo ayudarte?"
      filas={2}
      pieVacio="Vacío = saludo por defecto."
      etiquetaGuardar="Guardar saludo"
      onSave={(v) => onSave(channel, v)}
    />
  );
}

/** La firma del correo. Se añade al final de lo que responde el agente. */
function EmailSignatureEditor({
  channel,
  onSave,
}: {
  channel: ChannelOut;
  onSave: (c: ChannelOut, firma: string) => Promise<void>;
}) {
  return (
    <EditorDeTexto
      icono={<Mail className="w-3.5 h-3.5 text-ink3 flex-none" />}
      titulo="Firma del correo"
      coletilla="— se añade al final de cada respuesta"
      persisted={
        typeof channel.config?.email_signature === "string"
          ? (channel.config.email_signature as string)
          : ""
      }
      placeholder={"Un saludo,\nEquipo de atención al cliente\n+34 600 000 000"}
      filas={3}
      pieVacio="Vacío = sin firma."
      etiquetaGuardar="Guardar firma"
      onSave={(v) => onSave(channel, v)}
    />
  );
}

/** El saludo de voz y la firma del correo eran dos componentes calcados con
 * distinto rótulo. Es el mismo: un texto que se guarda en la config del canal. */
function EditorDeTexto({
  icono,
  titulo,
  coletilla,
  persisted,
  placeholder,
  filas,
  pieVacio,
  etiquetaGuardar,
  onSave,
}: {
  icono: React.ReactNode;
  titulo: string;
  coletilla: string;
  persisted: string;
  placeholder: string;
  filas: number;
  pieVacio: string;
  etiquetaGuardar: string;
  onSave: (valor: string) => Promise<void>;
}) {
  const [value, setValue] = useState(persisted);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Si el canal se refresca desde fuera, re-sincronizamos con lo persistido.
  useEffect(() => {
    setValue(persisted);
  }, [persisted]);

  const dirty = value.trim() !== persisted.trim();

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await onSave(value.trim());
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1500);
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setError(ax.response?.data?.detail || "No se pudo guardar");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-coro-sm bg-paper2/60 border border-line p-2.5 flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5 text-xs text-ink2">
        {icono}
        <span className="font-medium">{titulo}</span>
        <span className="text-ink3">{coletilla}</span>
      </div>
      <textarea
        rows={filas}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="input w-full text-sm leading-relaxed"
        placeholder={placeholder}
      />
      {error && <div className="text-state-bad text-[11px]">{error}</div>}
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-ink3 italic">{pieVacio}</span>
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy || !dirty}
          className="btn-ghost text-xs inline-flex items-center gap-1.5 disabled:opacity-40"
        >
          {busy ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : saved ? (
            <Check className="w-3.5 h-3.5 text-state-ok" />
          ) : (
            <Save className="w-3.5 h-3.5" />
          )}
          {saved ? "Guardado" : etiquetaGuardar}
        </button>
      </div>
    </div>
  );
}
