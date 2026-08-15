import { useState } from "react";
import {
  Calendar,
  Mail,
  MessageSquare,
  Mic,
  Instagram,
  Phone,
  Server,
  type LucideIcon,
} from "lucide-react";
import { apiBaseHttp, apiBaseWs } from "@/services/api";
import type {
  ChannelOut,
  ChannelType,
  CredentialOut,
  WhatsappProviderName,
} from "@/services/admin";

/**
 * Piezas compartidas por la pantalla de Conexiones.
 *
 * La pantalla se lee con dos reglas, y las dos ordenan lo de aquí:
 *
 * 1. UN SERVICIO, UNA TARJETA. Todo lo de un servicio (su estado, sus claves,
 *    las URLs que hay que pegar en su panel y sus botones) vive en un único
 *    sitio. Antes las claves de WhatsApp salían en la tarjeta del canal Y otra
 *    vez en una lista de "todas las credenciales" al final, y las de Google
 *    llegaban a aparecer en cinco sitios: quien está aprendiendo no sabía cuál
 *    era el bueno.
 *
 * 2. CONECTAR Y OPERAR SON DOS COSAS. Conectar (claves, tokens y las URLs que
 *    se pegan en el panel del proveedor) es de Servicios. Operar (si el canal
 *    está encendido, en qué modo está el bot y qué agente responde) es de
 *    Canales. Nada aparece en las dos pestañas.
 */

// ---------------------------------------------------------------------------
// Proveedores de WhatsApp
// ---------------------------------------------------------------------------

/** Las dos formas de conectar WhatsApp, con las claves que pide cada una. */
export const WHATSAPP_PROVIDERS: Record<
  WhatsappProviderName,
  { label: string; short: string; description: string; keys: string[]; panel: string }
> = {
  ycloud: {
    label: "YCloud",
    short: "YCloud",
    description:
      "Un intermediario oficial de WhatsApp. Se contrata en su web, el alta es rápida y ellos se encargan del papeleo con Meta. Cobran su parte por mensaje.",
    keys: ["ycloud_api_key", "ycloud_webhook_secret", "ycloud_phone_number"],
    panel: "el panel de YCloud",
  },
  meta: {
    label: "API oficial de Meta",
    short: "Meta",
    description:
      "Conexión directa con Meta, sin intermediario. Pagas a Meta lo que marque su tarifa, pero hay que crear la app y el usuario del sistema a mano en Meta for Developers.",
    keys: [
      "meta_wa_phone_number_id",
      "meta_wa_business_account_id",
      "meta_wa_access_token",
      "meta_wa_app_secret",
      "meta_wa_verify_token",
    ],
    panel: "Meta for Developers",
  },
};

/** Qué proveedor tiene puesto el canal de WhatsApp. Las instalaciones creadas
 * antes de que hubiera elección no tienen el campo: esas son de YCloud. */
export function whatsappProviderOf(channel: ChannelOut | null | undefined): WhatsappProviderName {
  const raw = String(channel?.config?.provider ?? "").trim().toLowerCase();
  return raw === "meta" ? "meta" : "ycloud";
}

// ---------------------------------------------------------------------------
// Catálogo de canales
// ---------------------------------------------------------------------------

export const CHANNEL_ORDER: ChannelType[] = [
  "whatsapp",
  "webchat",
  "instagram_dm",
  "email",
  "retell_voice",
];

export const CHANNEL_META: Record<
  ChannelType,
  { label: string; Icon: LucideIcon; tone: string; description: string }
> = {
  whatsapp: {
    label: "WhatsApp",
    Icon: Phone,
    tone: "text-[#25D366]",
    description: "Mensajes de WhatsApp, entrantes y salientes.",
  },
  webchat: {
    label: "Chat en tu web",
    Icon: MessageSquare,
    tone: "text-[#6C7BFF]",
    description: "Un botón de chat flotante que se pega en cualquier web con un trozo de código.",
  },
  instagram_dm: {
    label: "Mensajes de Instagram",
    Icon: Instagram,
    tone: "text-[#E4405F]",
    description: "Los mensajes directos de tu cuenta de Instagram profesional.",
  },
  email: {
    label: "Correo (Gmail)",
    Icon: Mail,
    tone: "text-[#EA4335]",
    description: "El correo que llega a tu Gmail entra como una conversación más.",
  },
  retell_voice: {
    label: "Llamadas de voz",
    Icon: Mic,
    tone: "text-[#9B8AFB]",
    description: "Llamadas de teléfono que atiende el agente hablando, con Retell.",
  },
};

/**
 * Las claves que pide cada canal. Se editan SOLO en la pestaña Servicios; en
 * Canales se usa esta misma lista para decir si falta alguna y llevar allí.
 *
 * La voz no aparece: sus claves no son credenciales sueltas, se guardan de una
 * vez por su formulario de conexión. El chat web no tiene claves secretas (su
 * clave es pública por diseño) y el correo usa la conexión de Google.
 */
export function clavesDelServicio(
  type: ChannelType,
  waProvider: WhatsappProviderName = "ycloud",
): string[] {
  if (type === "whatsapp") return WHATSAPP_PROVIDERS[waProvider].keys;
  if (type === "instagram_dm")
    return ["instagram_oauth_client_id", "instagram_oauth_client_secret"];
  return [];
}

/** Cuántas de esas claves están puestas. */
export function clavesPuestas(keys: string[], creds: CredentialOut[]): number {
  const puestas = new Set(creds.filter((c) => c.value_masked).map((c) => c.key));
  return keys.filter((k) => puestas.has(k)).length;
}

// ---------------------------------------------------------------------------
// URLs que hay que pegar en el panel del proveedor
// ---------------------------------------------------------------------------

/** Una URL que hay que copiar y pegar fuera. `donde` dice en qué pantalla del
 * proveedor se pega, que es la parte que siempre falta. */
export type SetupUrl = { label: string; url: string; donde: string };

/**
 * URLs de configuración de cada canal. Se calculan de la URL pública de la API
 * —la misma de la que las calcula el backend—, así que siguen siendo correctas
 * si la instalación cambia de dominio.
 *
 * Están SIEMPRE a la vista dentro de la tarjeta. Antes solo salían en el aviso
 * que aparece justo después de dar de alta el canal: quien lo cerraba sin
 * copiarlas se quedaba con un canal "conectado" que no recibía nada.
 */
export function channelSetupUrls(
  type: ChannelType,
  whatsappProvider: WhatsappProviderName = "ycloud",
): SetupUrl[] {
  const base = apiBaseHttp().replace(/\/$/, "");
  const ws = apiBaseWs().replace(/\/$/, "");
  switch (type) {
    case "whatsapp":
      return whatsappProvider === "meta"
        ? [
            {
              label: "URL del webhook",
              url: `${base}/api/v1/webhooks/whatsapp/meta`,
              donde:
                "Meta for Developers → tu app → WhatsApp → Configuración → Webhook → Editar. Pega la URL, escribe debajo la misma palabra de verificación que guardaste aquí, y después suscríbete al campo «messages».",
            },
          ]
        : [
            {
              label: "URL del webhook",
              url: `${base}/api/v1/webhooks/ycloud`,
              donde:
                "Panel de YCloud → Developers → Webhooks → New endpoint. Pega la URL y suscríbete a los eventos de mensaje entrante y de estado (whatsapp.inbound_message.received y whatsapp.message.updated).",
            },
          ];
    case "instagram_dm":
      return [
        {
          label: "URL del webhook",
          url: `${base}/api/v1/webhooks/instagram`,
          donde:
            "Meta for Developers → tu app → Webhooks → Instagram → Callback URL. Después suscribe la cuenta al campo «messages».",
        },
      ];
    case "retell_voice":
      return [
        {
          label: "Dirección del cerebro del agente (WebSocket)",
          url: `${ws}/api/v1/voice/retell/llm-ws`,
          donde:
            "Panel de Retell → tu agente → Response Engine → Custom LLM → Websocket URL. Retell le añade el identificador de la llamada al final; tú pégala tal cual.",
        },
        {
          label: "URL de avisos de la llamada",
          url: `${base}/api/v1/voice/retell/webhook`,
          donde:
            "Panel de Retell → tu agente → Webhook Settings → Agent Level Webhook URL. Es distinta de la anterior: hay que pegar las dos.",
        },
      ];
    case "email":
      return [
        {
          label: "Dirección de vuelta de Google",
          url: `${base}/api/v1/oauth/google/callback`,
          donde:
            "Google Cloud Console → APIs y servicios → Credenciales → tu cliente de OAuth de tipo Web → URIs de redirección autorizados.",
        },
      ];
    default:
      return [];
  }
}

/** Una URL para copiar, con el botón al lado y la línea que explica dónde se
 * pega: sin eso, la URL sola no sirve de nada a quien lo monta por primera vez. */
export function SetupUrlRow({
  label,
  url,
  donde,
}: {
  label: string;
  url: string;
  donde?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-ink3 font-medium mb-1">{label}</div>
      <div className="flex gap-1.5 items-center">
        <code className="flex-1 text-[10px] font-mono bg-card border border-line rounded-coro-sm px-2 py-1.5 break-all">
          {url}
        </code>
        <button
          type="button"
          onClick={() =>
            void navigator.clipboard.writeText(url).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            })
          }
          className="btn-ghost text-xs shrink-0"
        >
          {copied ? "Copiada" : "Copiar"}
        </button>
      </div>
      {donde && <div className="text-[10px] text-ink3 mt-1 leading-relaxed">{donde}</div>}
    </div>
  );
}

/** Bloque con todas las URLs de un servicio. */
export function SetupUrlsBlock({
  urls,
  titulo = "Pega esto en el panel del proveedor:",
  children,
}: {
  urls: SetupUrl[];
  titulo?: string;
  children?: React.ReactNode;
}) {
  if (!urls.length && !children) return null;
  return (
    <div className="rounded-coro-sm bg-paper2/60 border border-line p-2.5 space-y-2">
      <div className="text-[11px] text-ink2 font-medium">{titulo}</div>
      {urls.map((u) => (
        <SetupUrlRow key={u.url} label={u.label} url={u.url} donde={u.donde} />
      ))}
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Estado de un servicio, de un vistazo
// ---------------------------------------------------------------------------

/** Los tres estados posibles de una tarjeta. La pantalla entera se lee por
 * esto: qué está funcionando, qué está a medias y qué ni se ha empezado. */
export type EstadoServicio = "ok" | "medias" | "sin-empezar";

export const ESTADO_TEXTO: Record<EstadoServicio, string> = {
  ok: "Funcionando",
  medias: "A medias",
  "sin-empezar": "Sin conectar",
};

export const ESTADO_CLASE: Record<EstadoServicio, string> = {
  ok: "bg-state-ok/10 text-state-ok",
  medias: "bg-state-warn/10 text-state-warn",
  "sin-empezar": "bg-paper3 text-ink3",
};

export function EstadoPill({ estado }: { estado: EstadoServicio }) {
  return <span className={`pill ${ESTADO_CLASE[estado]}`}>{ESTADO_TEXTO[estado]}</span>;
}

/** La lista de "esto te falta". Se pinta solo si hay algo, y en cristiano:
 * nada de nombres internos de credenciales. `children` es para el botón que
 * lleva a donde se arregla, cuando eso está en otra pestaña. */
export function QueFalta({
  items,
  titulo = "Para terminar de conectarlo:",
  children,
}: {
  items: string[];
  titulo?: string;
  children?: React.ReactNode;
}) {
  if (!items.length) return null;
  return (
    <div className="rounded-coro-sm bg-state-warn/10 border border-state-warn/25 px-2.5 py-2">
      <div className="text-[11px] font-medium text-ink2 mb-0.5">{titulo}</div>
      <ul className="text-[11px] text-ink2 list-disc pl-4 space-y-0.5">
        {items.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
      {children && <div className="mt-1.5">{children}</div>}
    </div>
  );
}

/**
 * Estado de una tarjeta. Verde SOLO si el servicio está conectado de verdad y
 * no le falta nada: un servicio que ya funciona no puede salir como "sin
 * conectar" porque sus claves estén puestas por otra vía, y uno con las claves
 * a medias no puede salir en verde.
 */
export function estadoDeTarjeta(
  conectado: boolean,
  keys: string[],
  puestas: Set<string>,
  falta: string[] = [],
): EstadoServicio {
  if (conectado) return falta.length ? "medias" : "ok";
  const n = keys.filter((k) => puestas.has(k)).length;
  return n > 0 ? "medias" : "sin-empezar";
}

// ---------------------------------------------------------------------------
// Catálogo de servicios que no son canales
// ---------------------------------------------------------------------------

export const SERVICE_ICONS: Record<string, { Icon: LucideIcon; tone: string }> = {
  google: { Icon: Calendar, tone: "text-[#4285F4]" },
  resend: { Icon: Mail, tone: "text-[#16A085]" },
  smtp: { Icon: Server, tone: "text-[#EA4335]" },
};
