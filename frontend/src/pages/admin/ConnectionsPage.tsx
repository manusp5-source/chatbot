import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Bot, Cpu, KeyRound, Loader2, Plug } from "lucide-react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { PageHeader } from "@/components/PageHeader";
import { LLMProvidersPanel } from "./LLMProvidersPanel";
import { AgentTokensPanel } from "./AgentTokensPanel";
import { ChannelsTab } from "./connections/ChannelsTab";
import { ServicesTab } from "./connections/ServicesTab";
import { WhatsappFormModal } from "./connections/WhatsappModal";
import {
  InstagramFormModal,
  InstagramWebhookModal,
  RetellFormModal,
  RetellWebhookModal,
  WebchatSnippetModal,
} from "./connections/OtherModals";
import { whatsappProviderOf } from "./connections/shared";
import { errorDetail } from "@/lib/errors";
import {
  listChannels,
  listExternalApis,
  listAgents,
  listCredentials,
  updateChannel,
  provisionWebchatChannel,
  getWebchatSnippet,
  provisionInstagramChannel,
  provisionRetellChannel,
  provisionEmailChannel,
  provisionWhatsappChannel,
  startGoogleOAuth,
  startInstagramOAuth,
  getInstagramWebhookInfo,
  deleteChannel,
  type AgentOut,
  type GoogleProvider,
  type ChannelOut,
  type ChannelUpdate,
  type CredentialOut,
  type ExternalAPIOut,
  type WebchatProvision,
  type InstagramProvisionIn,
  type InstagramProvisionOut,
  type RetellProvisionIn,
  type RetellProvisionOut,
  type WhatsappProvisionIn,
} from "@/services/admin";

/**
 * Conexiones: canales de entrada del agente, servicios externos, proveedores
 * de modelo y tokens de API/MCP.
 *
 * La pantalla se ordena con dos reglas:
 *
 * 1. CONECTAR Y OPERAR SON DOS COSAS. Servicios tiene todas las credenciales de
 *    todo —WhatsApp, Instagram y la voz incluidas— y las URLs que hay que pegar
 *    en el panel de cada proveedor. Canales es solo operación: encendido, modo
 *    del bot y qué agente responde. Cuando a un canal le falta algo de la otra
 *    pestaña, lo dice y lleva a la tarjeta exacta.
 *
 * 2. UN SERVICIO, UNA TARJETA. Antes las claves se editaban en la tarjeta del
 *    canal Y otra vez en una lista de "todas las credenciales" al final, así que
 *    las mismas claves salían dos veces (las de Google, cinco) y no había forma
 *    de saber cuál era el sitio bueno. Esa lista ya no existe: lo que hacía
 *    falta de ella —que ninguna clave se quede sin sitio— lo cubre la tarjeta
 *    "Otras claves", que solo aparece si de verdad sobra alguna.
 *
 * La pantalla estaba en un único fichero de 2.800 líneas. Ahora las tarjetas y
 * los avisos viven en `./connections/`, y aquí queda lo que es de la pantalla:
 * cargar los datos, las pestañas y quién abre qué.
 */

// El horario de atención ya no vive aquí: se fue a Agentes → Horarios, que es
// donde se configura lo que el agente hace y dice. El enlace viejo
// (?tab=schedule) sigue funcionando: redirige allí.
type Tab = "channels" | "services" | "providers" | "agent-tokens";

const TABS_VALIDAS: Tab[] = ["channels", "services", "providers", "agent-tokens"];

const HORARIO_NUEVA_RUTA = "/admin/agent/agents?tab=schedule";

export default function ConnectionsPage() {
  // La pestaña se puede fijar por la URL. Hacía falta: la ruta antigua
  // /admin/credentials redirige aquí con ?tab=… y hasta ahora ese parámetro se
  // ignoraba, así que quien seguía el enlace aterrizaba en otra pestaña.
  const [searchParams, setSearchParams] = useSearchParams();
  const tabDeLaUrl = searchParams.get("tab") as Tab | null;
  const [tab, setTabState] = useState<Tab>(
    tabDeLaUrl && TABS_VALIDAS.includes(tabDeLaUrl) ? tabDeLaUrl : "channels",
  );
  function setTab(t: Tab) {
    setTabState(t);
    setSearchParams(t === "channels" ? {} : { tab: t }, { replace: true });
  }

  // Enlaces guardados a la pestaña de Horario, que ya no está aquí.
  const navigate = useNavigate();
  useEffect(() => {
    if (searchParams.get("tab") === "schedule") navigate(HORARIO_NUEVA_RUTA, { replace: true });
  }, [searchParams, navigate]);

  // Tarjeta de Servicios a la que saltar cuando se llega desde Canales. Sin
  // esto, "lo arreglas en Servicios" deja al usuario delante de ocho tarjetas
  // iguales buscando cuál era la suya.
  const [focoServicio, setFocoServicio] = useState<string | null>(null);
  function irAServicio(id: string) {
    setFocoServicio(id || null);
    setTab("services");
  }

  const [channels, setChannels] = useState<ChannelOut[]>([]);
  const [apis, setApis] = useState<ExternalAPIOut[]>([]);
  const [agents, setAgents] = useState<AgentOut[]>([]);
  // La lista de claves se carga UNA vez aquí y se reparte: es lo que permite
  // que cada tarjeta diga qué le falta sin que cada una pida lo mismo por su
  // cuenta.
  const [creds, setCreds] = useState<CredentialOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [provisionOut, setProvisionOut] = useState<WebchatProvision | null>(null);
  const [provisioning, setProvisioning] = useState(false);
  const [igFormOpen, setIgFormOpen] = useState(false);
  const [igProvisionOut, setIgProvisionOut] = useState<InstagramProvisionOut | null>(null);
  const [retellFormOpen, setRetellFormOpen] = useState(false);
  const [retellProvisionOut, setRetellProvisionOut] = useState<RetellProvisionOut | null>(null);
  const [waFormOpen, setWaFormOpen] = useState(false);
  const [deletingChannel, setDeletingChannel] = useState<ChannelOut | null>(null);
  const [busyDelete, setBusyDelete] = useState(false);

  const whatsappChannel = channels.find((c) => c.type === "whatsapp") ?? null;
  const gmailConectado = apis.some((a) => a.provider === "google_gmail" && a.is_active);

  async function refresh() {
    setError(null);
    try {
      const [ch, ap, ag, cr] = await Promise.all([
        listChannels(),
        listExternalApis(),
        listAgents(),
        listCredentials(),
      ]);
      setChannels(ch);
      setApis(ap);
      setAgents(ag);
      setCreds(cr);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar las conexiones."));
    } finally {
      setLoading(false);
    }
  }

  async function reloadCreds() {
    try {
      setCreds(await listCredentials());
    } catch {
      /* el detalle ya sale en la propia tarjeta al guardar */
    }
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await refresh();
      if (cancelled) return;
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Escucha el aviso del callback de OAuth para refrescar cuando Google o Meta
  // confirmen la conexión.
  //
  // Con Instagram, además, hay que abrir el aviso del webhook: entrar con
  // Instagram deja el canal en verde pero NO configura el webhook en Meta, así
  // que el canal parecía funcionar y no entraba ni un mensaje.
  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      const data = ev.data as { type?: string; provider?: string } | null;
      if (!data || data.type !== "chatbot.oauth.ok") return;
      void (async () => {
        await refresh();
        if ((data.provider || "").startsWith("instagram")) {
          await onShowIgWebhookInfo();
        }
      })();
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  function detalleDeError(e: unknown, porDefecto: string): string {
    const ax = e as { response?: { data?: { detail?: string } } };
    return ax.response?.data?.detail || porDefecto;
  }

  async function onToggleEnabled(channel: ChannelOut, enabled: boolean) {
    try {
      const updated = await updateChannel(channel.id, { enabled });
      setChannels((prev) => prev.map((c) => (c.id === channel.id ? updated : c)));
    } catch (e: unknown) {
      setError(detalleDeError(e, "Error actualizando el canal"));
    }
  }

  async function onChangeAgent(channel: ChannelOut, agentId: string | null) {
    try {
      const updated = await updateChannel(channel.id, { agent_id: agentId });
      setChannels((prev) => prev.map((c) => (c.id === channel.id ? updated : c)));
    } catch (e: unknown) {
      setError(detalleDeError(e, "Error asignando el agente"));
    }
  }

  async function onSaveGreeting(channel: ChannelOut, greeting: string): Promise<void> {
    const updated = await updateChannel(channel.id, { greeting });
    setChannels((prev) => prev.map((c) => (c.id === channel.id ? updated : c)));
  }

  async function onSaveEmailSignature(channel: ChannelOut, firma: string): Promise<void> {
    const updated = await updateChannel(channel.id, { email_signature: firma });
    setChannels((prev) => prev.map((c) => (c.id === channel.id ? updated : c)));
  }

  async function onProvisionWebchat() {
    setProvisioning(true);
    setError(null);
    try {
      const out = await provisionWebchatChannel();
      setProvisionOut(out);
      await refresh();
    } catch (e: unknown) {
      setError(detalleDeError(e, "Error creando el chat web"));
    } finally {
      setProvisioning(false);
    }
  }

  // Lista de dominios donde se acepta el chat web. El campo lo añade otro
  // frente al tipo compartido `ChannelUpdate`; hasta que llegue, se ensancha
  // en local para no depender del orden de los cambios.
  async function onSaveAllowedDomains(channelId: string, domains: string[]): Promise<void> {
    const body = { allowed_domains: domains } as ChannelUpdate & { allowed_domains?: string[] };
    const updated = await updateChannel(channelId, body);
    setChannels((prev) => prev.map((c) => (c.id === channelId ? updated : c)));
  }

  async function onShowWebchatSnippet() {
    setError(null);
    try {
      setProvisionOut(await getWebchatSnippet());
    } catch (e: unknown) {
      setError(detalleDeError(e, "No se pudo recuperar el código del chat"));
    }
  }

  async function onProvisionInstagram(body: InstagramProvisionIn) {
    setError(null);
    try {
      const out = await provisionInstagramChannel(body);
      setIgFormOpen(false);
      setIgProvisionOut(out);
      await refresh();
    } catch (e: unknown) {
      throw new Error(detalleDeError(e, "Error conectando Instagram"));
    }
  }

  // Alta o edición del canal de WhatsApp, con el proveedor que se haya elegido
  // (YCloud o la API oficial de Meta). Guarda las claves cifradas, crea el
  // canal y le asigna agente en un solo paso.
  async function onProvisionWhatsapp(body: WhatsappProvisionIn) {
    setError(null);
    try {
      await provisionWhatsappChannel(body);
      setWaFormOpen(false);
      await refresh();
    } catch (e: unknown) {
      throw new Error(detalleDeError(e, "Error conectando WhatsApp"));
    }
  }

  async function onProvisionRetell(body: RetellProvisionIn) {
    setError(null);
    try {
      const out = await provisionRetellChannel(body);
      setRetellFormOpen(false);
      setRetellProvisionOut(out);
      await refresh();
    } catch (e: unknown) {
      throw new Error(detalleDeError(e, "Error conectando la voz"));
    }
  }

  // El correo no pide claves: reutiliza la conexión de Google. Si Google no
  // está conectado, se dice dónde hacerlo en vez de fallar con un error seco.
  async function onProvisionEmail() {
    setError(null);
    if (!gmailConectado) {
      setError("Autoriza Gmail primero, en esta misma tarjeta de Google.");
      irAServicio("google");
      return;
    }
    setProvisioning(true);
    try {
      await provisionEmailChannel();
      await refresh();
    } catch (e: unknown) {
      setError(detalleDeError(e, "Error conectando el correo"));
    } finally {
      setProvisioning(false);
    }
  }

  async function startGoogleAuthFlow(provider: GoogleProvider) {
    setError(null);
    try {
      const url = await startGoogleOAuth(provider);
      window.open(url, "_blank", "noopener,noreferrer,width=600,height=720");
    } catch (e: unknown) {
      setError(detalleDeError(e, "Error autorizando con Google"));
    }
  }

  async function startInstagramOAuthFlow() {
    setError(null);
    try {
      const url = await startInstagramOAuth();
      window.open(url, "_blank", "noopener,noreferrer,width=600,height=720");
    } catch (e: unknown) {
      setError(detalleDeError(e, "Error autorizando con Instagram"));
    }
  }

  async function onShowIgWebhookInfo() {
    setError(null);
    try {
      const info = await getInstagramWebhookInfo();
      setIgProvisionOut({
        channel_id: "current",
        webhook_url: info.webhook_url,
        verify_token: info.verify_token,
      });
    } catch (e: unknown) {
      setError(detalleDeError(e, "No se pudo recuperar la información del webhook"));
    }
  }

  async function confirmDeleteChannel() {
    if (!deletingChannel) return;
    setBusyDelete(true);
    setError(null);
    try {
      await deleteChannel(deletingChannel.id);
      setDeletingChannel(null);
      await refresh();
    } catch (e: unknown) {
      setError(detalleDeError(e, "Error eliminando el canal"));
    } finally {
      setBusyDelete(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Conexiones"
        description="Por dónde te escriben tus clientes y qué servicios externos usa el agente."
      />

      <div className="px-4 pt-3 flex gap-1.5 border-b border-line overflow-x-auto">
        <TabButton active={tab === "channels"} onClick={() => setTab("channels")}>
          <Plug className="w-3.5 h-3.5" /> Canales
        </TabButton>
        <TabButton active={tab === "services"} onClick={() => setTab("services")}>
          <KeyRound className="w-3.5 h-3.5" /> Servicios
        </TabButton>
        <TabButton active={tab === "providers"} onClick={() => setTab("providers")}>
          <Cpu className="w-3.5 h-3.5" /> Proveedores LLM
        </TabButton>
        <TabButton active={tab === "agent-tokens"} onClick={() => setTab("agent-tokens")}>
          <Bot className="w-3.5 h-3.5" /> API / MCP
        </TabButton>
      </div>

      <div className="flex-1 overflow-auto p-4">
        {loading ? (
          <div className="flex items-center gap-2 text-ink3 py-10 justify-center">
            <Loader2 className="w-4 h-4 animate-spin" /> Cargando conexiones…
          </div>
        ) : error ? (
          <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2 max-w-4xl mb-4">
            <span className="flex-1">{error}</span>
            <button
              type="button"
              onClick={() => void refresh()}
              className="font-semibold hover:underline shrink-0"
            >
              Reintentar
            </button>
          </div>
        ) : null}

        {!loading && tab === "channels" && (
          <ChannelsTab
            channels={channels}
            agents={agents}
            creds={creds}
            onToggleEnabled={onToggleEnabled}
            onChangeAgent={onChangeAgent}
            onSaveGreeting={onSaveGreeting}
            onSaveEmailSignature={onSaveEmailSignature}
            onDeleteChannel={(c) => setDeletingChannel(c)}
            gmailConectado={gmailConectado}
            onIrAServicio={irAServicio}
          />
        )}
        {!loading && tab === "services" && (
          <ServicesTab
            channels={channels}
            apis={apis}
            creds={creds}
            onReloadCreds={() => void reloadCreds()}
            startGoogleAuthFlow={startGoogleAuthFlow}
            foco={focoServicio}
            onFocoAtendido={() => setFocoServicio(null)}
            onOpenWhatsapp={() => setWaFormOpen(true)}
            onProvisionWebchat={onProvisionWebchat}
            provisioningWebchat={provisioning}
            onShowWebchatSnippet={onShowWebchatSnippet}
            onOpenInstagramManual={() => setIgFormOpen(true)}
            onInstagramOAuth={startInstagramOAuthFlow}
            onShowIgWebhookInfo={onShowIgWebhookInfo}
            onOpenRetell={() => setRetellFormOpen(true)}
            onProvisionEmail={onProvisionEmail}
            provisioningEmail={provisioning}
          />
        )}
        {tab === "providers" && <LLMProvidersPanel />}
        {tab === "agent-tokens" && <AgentTokensPanel />}

        {provisionOut && (
          <WebchatSnippetModal
            data={provisionOut}
            channel={channels.find((c) => c.type === "webchat") ?? null}
            onSaveDomains={onSaveAllowedDomains}
            onClose={() => setProvisionOut(null)}
          />
        )}
        {waFormOpen && (
          <WhatsappFormModal
            agents={agents}
            proveedorActual={whatsappProviderOf(whatsappChannel)}
            yaConectado={whatsappChannel !== null}
            onClose={() => setWaFormOpen(false)}
            onSubmit={onProvisionWhatsapp}
          />
        )}
        {igFormOpen && (
          <InstagramFormModal
            onClose={() => setIgFormOpen(false)}
            onSubmit={onProvisionInstagram}
          />
        )}
        {igProvisionOut && (
          <InstagramWebhookModal
            data={igProvisionOut}
            onClose={() => setIgProvisionOut(null)}
          />
        )}
        {retellFormOpen && (
          <RetellFormModal
            onClose={() => setRetellFormOpen(false)}
            onSubmit={onProvisionRetell}
          />
        )}
        {retellProvisionOut && (
          <RetellWebhookModal
            data={retellProvisionOut}
            onClose={() => setRetellProvisionOut(null)}
          />
        )}
        <ConfirmModal
          open={!!deletingChannel}
          title="Eliminar canal"
          description={
            deletingChannel
              ? `¿Seguro que quieres eliminar el canal "${deletingChannel.name}"? Las conversaciones que ya hay no se borran, pero el canal queda desconectado y tendrás que volver a configurarlo si lo creas otra vez.`
              : ""
          }
          confirmLabel="Eliminar"
          cancelLabel="Cancelar"
          tone="danger"
          busy={busyDelete}
          onConfirm={confirmDeleteChannel}
          onCancel={() => setDeletingChannel(null)}
        />
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium border-b-2 -mb-px transition whitespace-nowrap ${
        active ? "border-brand-ink text-ink" : "border-transparent text-ink3 hover:text-ink2"
      }`}
    >
      {children}
    </button>
  );
}
