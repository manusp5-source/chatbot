import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  ArchiveRestore,
  Bot as BotIcon,
  CheckCircle2,
  FlaskConical,
  Pause,
  Pencil,
  Search,
  Send,
  ShieldCheck,
  User,
  UserCheck,
  ArrowLeft,
  CheckSquare,
  Download,
  Paperclip,
  WifiOff,
  Mail,
  ChevronUp,
} from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { Sparkles } from "lucide-react";
import { ConversationTracePanel } from "@/components/ConversationTracePanel";
import { errorDetail } from "@/lib/errors";
import clsx from "clsx";
import {
  archiveConversation,
  bulkArchiveConversations,
  closeConversation,
  demoDisable,
  demoEnable,
  getConversation,
  getConversationCounts,
  getMessages,
  listConversations,
  markRead,
  releaseQuarantine,
  returnToBot,
  sendMessage,
  takeOver,
  unarchiveConversation,
} from "@/services/conversations";
import type { InboxCounts, InboxSort } from "@/services/conversations";
import { INBOX_SORTS, isInboxSort } from "@/services/conversations";
import { api } from "@/services/api";
import { getAgentPause, type AgentPauseState } from "@/services/admin";
import { addContactToCrm } from "@/services/contacts";
import { useInboxSocket } from "@/hooks/useInboxSocket";
import { MessageBubble } from "@/components/MessageBubble";
import { DraftCard } from "@/components/DraftCard";
import { AttachmentComposer } from "@/components/AttachmentComposer";
import { usePrivacy } from "@/store/privacy";
import { useAuth } from "@/store/auth";
import { maskContact, maskInitials } from "@/lib/mask";
import type { Conversation, Message } from "@/types";

// Conversaciones por página de la bandeja. Antes era un 50 fijo y sin
// paginación: la 51 no existía y nada lo indicaba.
const LIST_PAGE_SIZE = 50;
// Mensajes por tramo del hilo. El backend admite hasta 200 por petición.
const MESSAGE_PAGE_SIZE = 100;
// Ventana para agrupar los refrescos que dispara el websocket. Sin ella, cada
// evento lanzaba 4 peticiones, y por cada pestaña abierta.
const WS_REFRESH_DEBOUNCE_MS = 250;
// Con el tiempo real caído, cada cuánto se consulta la lista para no quedarse
// mirando datos viejos.
const OFFLINE_POLL_MS = 20000;

const STATUS_FILTERS = [
  // "Para hacer" (vista POR DEFECTO): una sola cola con lo que necesita persona
  // (Pendientes) Y las sugerencias del modo Entrenamiento. Cada fila va
  // etiquetada ("Pendiente" / "Sugerencia") para distinguirlas de un vistazo,
  // así no hay que saltar entre pestañas ni adivinar la diferencia.
  { value: "__action", label: "Para hacer" },
  { value: "__active", label: "Activas" },
  { value: "bot", label: "Bot" },
  { value: "humano", label: "Humano" },
  { value: "__archived", label: "Archivadas" },
  { value: "__quarantine", label: "Descartados" },
];

// Pestañas de canal arriba del inbox. La vista activa se sincroniza con
// la URL (?canal=...), así el filtro persiste al recargar y se puede
// bookmarkear / compartir.
const CHANNEL_TABS = [
  { value: "", label: "Todas", color: null },
  { value: "whatsapp", label: "WhatsApp", color: "#25D366" },
  { value: "web", label: "Web", color: "#6C7BFF" },
  { value: "instagram_dm", label: "Instagram", color: "#E4405F" },
  { value: "email", label: "Email", color: "#EA4335" },
] as const;

const CHANNEL_META: Record<string, { label: string; color: string }> = {
  whatsapp: { label: "WhatsApp", color: "#25D366" },
  web: { label: "Chat web", color: "#6C7BFF" },
  webchat: { label: "Chat web", color: "#6C7BFF" },
  instagram_dm: { label: "Instagram", color: "#E4405F" },
  retell_voice: { label: "Voz", color: "#9B8AFB" },
  email: { label: "Email", color: "#EA4335" },
};

const STATUS_BADGE: Record<string, string> = {
  bot: "bg-brand/15 text-brand-ink",
  humano: "bg-state-warn/15 text-state-warn",
  cerrada: "bg-paper3 text-ink3",
};

const AVATAR_PALETTE = ["#DBE09E", "#9DA362", "#6C7BFF", "#9B8AFB", "#25D366", "#E58A2F"];

function initialsOf(nombre: string | null | undefined, telefono: string | null | undefined): string {
  const src = (nombre || telefono || "?").trim();
  const parts = src.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return src.slice(0, 2).toUpperCase();
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? parts[0]?.[1] ?? "")).toUpperCase();
}

function colorFor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_PALETTE[h % AVATAR_PALETTE.length];
}

function timeAgo(iso?: string | null): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "ahora";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h`;
  const d = Math.floor(h / 24);
  return `${d} d`;
}

function dedupeById<T extends { id: string }>(list: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of list) {
    if (!seen.has(item.id)) {
      seen.add(item.id);
      out.push(item);
    }
  }
  return out;
}

// "Necesita acción de una persona": lo calcula el backend (needs_action) —
// sugerencia/borrador sin resolver, o el cliente escribió lo último sin que
// nadie contestara, o derivada a humano y aún sin respuesta de un operador. En
// cuanto el operador responde, deja de estar pendiente. Lo que el bot ya
// contestó NO grita.
function needsAction(c: Conversation): boolean {
  return !!c.needs_action;
}

// ── El orden ────────────────────────────────────────────────────────────────
//
// AQUÍ NO SE ORDENA NADA, y es a propósito. Antes había un `workQueueOrder` que
// volvía a ordenar la lista al pintarla, con un criterio distinto del que usaba
// el backend. Dos problemas, uno cosmético y otro grave:
//
//   1. Se pisaban: el backend ponía primero las derivadas a humano y el frontend
//      las que necesitaban acción. El resultado no era ninguno de los dos.
//   2. El backend PAGINA con su criterio y el frontend solo tenía las 50 filas
//      que le habían llegado. Una conversación que necesita acción y cae en el
//      puesto 51 no la sube nadie: no está. Y como no había paginación, era
//      sencillamente invisible.
//
// De paso, aquel orden comparaba fechas como TEXTO (`localeCompare`), que solo
// funciona mientras todo venga en UTC y se rompe con cualquier otro desfase.
//
// Ahora el criterio se pide al backend (`sort`), se aplica ANTES de paginar, y
// la lista se pinta exactamente en el orden en que llega.

// ── Adjuntos de correo ─────────────────────────────────────────────────────
//
// El backend guarda la ficha de cada adjunto de Gmail en el `extra` del mensaje
// (que viaja al panel como `metadata.attachments`): filename, mime_type, size y
// `attachment_id`. El binario NO se guarda: se pide a Gmail bajo demanda.
//
// OJO con el histórico: los correos ingeridos ANTES de que se empezara a
// guardar el `attachment_id` solo tienen el nombre del fichero. Esos no se
// pueden descargar por mucho que se pinte un enlace, así que se listan igual
// (saber que el correo traía un fichero importa) pero sin enlace y con el
// motivo escrito — un enlace que siempre falla es peor que no tenerlo.

type EmailAttachment = {
  filename: string;
  mime_type: string | null;
  size: number | null;
  attachment_id: string | null;
};

function parseEmailAttachments(meta: Record<string, unknown> | undefined): EmailAttachment[] {
  const raw = meta?.attachments;
  if (!Array.isArray(raw)) return [];
  const out: EmailAttachment[] = [];
  for (const item of raw) {
    // Formato antiguo: la lista era de nombres sueltos (strings).
    if (typeof item === "string") {
      out.push({ filename: item, mime_type: null, size: null, attachment_id: null });
      continue;
    }
    if (!item || typeof item !== "object") continue;
    const o = item as Record<string, unknown>;
    const filename = typeof o.filename === "string" ? o.filename : "";
    if (!filename) continue;
    out.push({
      filename,
      mime_type: typeof o.mime_type === "string" ? o.mime_type : null,
      size: typeof o.size === "number" ? o.size : null,
      attachment_id: typeof o.attachment_id === "string" && o.attachment_id ? o.attachment_id : null,
    });
  }
  return out;
}

function fmtAttachmentSize(n: number | null): string | null {
  if (n == null || n < 0) return null;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
  return `${n} B`;
}

function EmailAttachments({
  conversationId,
  messageId,
  attachments,
}: {
  conversationId: string;
  messageId: string;
  attachments: EmailAttachment[];
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function download(att: EmailAttachment) {
    if (!att.attachment_id) return;
    setBusy(att.attachment_id);
    setError(null);
    try {
      // OJO: cuando se escribió esto el endpoint de descarga aún NO existía en
      // el backend (solo el cliente de Gmail, `get_attachment`). La ruta sigue
      // la forma del resto de acciones sobre un mensaje
      // (/{conv}/messages/{msg}/recover). Si el backend la publica con otra
      // firma, hay que ajustar ESTA línea; el 404 se explica abajo en vez de
      // dejar un error crudo.
      const res = await api.get(
        `/conversations/${conversationId}/messages/${messageId}/attachments/${att.attachment_id}`,
        { responseType: "blob" },
      );
      const url = URL.createObjectURL(res.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = att.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      setError(
        status === 404
          ? "Este servidor todavía no sirve la descarga de adjuntos de correo. Ábrelo desde Gmail mientras tanto."
          : errorDetail(e, "No se pudo descargar el adjunto."),
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-1 mb-2 flex flex-col gap-1">
      {attachments.map((att, i) => {
        const size = fmtAttachmentSize(att.size);
        const meta = [att.mime_type, size].filter(Boolean).join(" · ");
        return (
          <div
            key={`${att.attachment_id || att.filename}-${i}`}
            className="flex items-center gap-2 text-[12px] bg-paper2 border border-line rounded-coro-sm px-2.5 py-1.5"
          >
            <Paperclip className="w-3.5 h-3.5 text-ink4 shrink-0" />
            <span className="text-ink2 truncate flex-1" title={att.filename}>
              {att.filename}
            </span>
            {meta && <span className="text-ink4 shrink-0 hidden sm:inline">{meta}</span>}
            {att.attachment_id ? (
              <button
                type="button"
                onClick={() => void download(att)}
                disabled={busy === att.attachment_id}
                className="btn-ghost btn-sm shrink-0"
              >
                <Download className="w-3.5 h-3.5" />
                <span className="hidden md:inline">
                  {busy === att.attachment_id ? "Descargando…" : "Descargar"}
                </span>
              </button>
            ) : (
              <span
                className="text-ink4 shrink-0 italic"
                title="Este correo se recibió antes de que se guardara el identificador del adjunto en Gmail, así que no se puede pedir el fichero. Ábrelo desde Gmail."
              >
                solo en Gmail
              </span>
            )}
          </div>
        );
      })}
      {error && (
        <div className="text-[11px] text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-2.5 py-1.5">
          {error}
        </div>
      )}
    </div>
  );
}

export default function Inbox() {
  const priv = usePrivacy((s) => s.enabled);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  // Contadores para los badges de las pestañas (Para hacer / Revisión) + el
  // desglose "X esperando · Y sugerencias".
  const [counts, setCounts] = useState<InboxCounts | null>(null);
  const [pause, setPause] = useState<AgentPauseState | null>(null);
  // Filtros leídos de la URL (bookmarkable + shareable). Cambios al filtro
  // empujan a la URL con `setSearchParams`, así un refresh mantiene la vista.
  const [searchParams, setSearchParams] = useSearchParams();
  // Sin parámetro en la URL, la vista por defecto es "Para hacer" (foco). Las
  // vistas antiguas (__pending / __suggestions) se mapean a "Para hacer", que
  // ahora las junta (compat con enlaces/bookmarks viejos).
  const rawStatus = searchParams.get("status") || "__action";
  const statusFilter =
    rawStatus === "__pending" || rawStatus === "__suggestions" ? "__action" : rawStatus;
  const channelFilter = searchParams.get("canal") || "";
  // Orden elegido, también en la URL: así se conserva al recargar (y se puede
  // compartir el enlace con la vista ya puesta). Un valor raro cae a "recent".
  const rawSort = searchParams.get("orden");
  const sortFilter: InboxSort = isInboxSort(rawSort) ? rawSort : "recent";
  // Filtro "Solo sin leer": el backend siempre soportó `unread_only` y no había
  // manera de activarlo desde la pantalla, así que no se podía separar lo que te
  // falta por leer de lo que el bot ya atendió.
  const unreadOnly = searchParams.get("no_leidas") === "1";
  const [search, setSearch] = useState("");
  // Buscador con debounce: evita lanzar una petición por cada tecla (y las
  // carreras que eso provoca). refreshList usa `debouncedSearch`, no `search`.
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Conversación seleccionada que NO está en la lista filtrada (otro canal,
  // archivada, deep-link). Se guarda aparte para poder abrirla en el panel sin
  // inyectarla en la lista (eso mezclaba canales).
  const [selectedFallback, setSelectedFallback] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  // Paginación de la LISTA: cuántas páginas se han cargado y cuántas hay en
  // total. Antes había un tope duro de 50 y el `total` del backend se tiraba a
  // la basura: con 51 conversaciones vivas, la 51 no existía y nada lo decía.
  const [pagesLoaded, setPagesLoaded] = useState(1);
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  // Paginación del HILO: se cargaban los últimos 100 mensajes y punto. El
  // backend ya sabía paginar hacia atrás (`before`) y nadie lo usaba.
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  // Error visible de la última acción (enviar mensaje, tomar control, archivar…).
  // Se pinta como banner sobre el composer, con el detail del backend si llega.
  const [actionError, setActionError] = useState<string | null>(null);
  // Error de carga de la LISTA: sin él, una caída de la API se pintaba como
  // bandeja vacía ("Todo al día ✨"). Se limpia en el siguiente refresh OK.
  const [listError, setListError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Contador de peticiones de lista: "gana la última". Evita que una respuesta
  // lenta de un filtro anterior machaque la del filtro actual (conversaciones
  // mezcladas / filtros pillados).
  const reqSeq = useRef(0);
  // Mismo patrón para los MENSAJES del hilo abierto. Sin esto, una respuesta
  // lenta de la conversación anterior (o de un evento del websocket) llegaba
  // DESPUÉS de cambiar de chat y pintaba sus mensajes bajo la conversación ya
  // seleccionada: en una bandeja con varios clientes eso es enseñar la
  // conversación de uno dentro de la ficha de otro, no un fallo cosmético.
  const msgSeq = useRef(0);
  // El id de la conversación abierta AHORA. Los callbacks asíncronos cierran
  // sobre el `selectedId` del render en que se crearon; tras un `await` hay que
  // mirar aquí para saber si su respuesta sigue siendo la buena.
  const selectedIdRef = useRef<string | null>(null);
  // Espejo de `pagesLoaded` para que refreshList (que se recrea con los
  // filtros, no con la página) sepa cuántas páginas hay que volver a traer sin
  // encoger la lista que ya estaba en pantalla.
  const pagesLoadedRef = useRef(1);
  // Conversación que quedó por marcar leída porque la pestaña no estaba a la
  // vista. Se salda al volver.
  const pendingReadRef = useRef<string | null>(null);
  // Agrupador de refrescos del websocket (ver scheduleRefresh).
  const refreshTimerRef = useRef<number | null>(null);
  const refreshMessagesPendingRef = useRef(false);
  const [showTrace, setShowTrace] = useState(false);
  // Modal "Cerrar conversación": resumen opcional + envío opcional del resumen
  // por email al cliente (solo si el contacto tiene email).
  const [closeOpen, setCloseOpen] = useState(false);
  const [closeResumen, setCloseResumen] = useState("");
  const [closeEmail, setCloseEmail] = useState(false);
  const [closeBusy, setCloseBusy] = useState(false);
  // Archivar en bloque y sacar de la cuarentena antispam son admin-only en el
  // backend (`require_admin` en /conversations/bulk-archive y en
  // /{id}/release-quarantine): sin esta comprobación una operadora ve botones
  // que solo devuelven 403. Mismo patrón que el menú de admin de Layout.tsx.
  const isAdmin = useAuth((s) => s.user?.role) === "admin";
  // Selección múltiple para archivar en bloque (p.ej. vaciar "Revisión").
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  function exitSelection() {
    setSelectionMode(false);
    setSelectedIds(new Set());
  }

  function toggleSelect(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleSelectAll() {
    setSelectedIds((prev) =>
      prev.size === conversations.length && conversations.length > 0
        ? new Set()
        : new Set(conversations.map((c) => c.id))
    );
  }

  async function onBulkArchive() {
    if (selectedIds.size === 0) return;
    setBulkBusy(true);
    try {
      await bulkArchiveConversations([...selectedIds]);
      exitSelection();
      await refreshList();
    } catch (e) {
      // Sin catch, un fallo dejaba la selección abierta sin ningún aviso: la
      // operadora no sabía si las conversaciones se archivaron o no.
      setActionError(errorDetail(e, "No se pudieron archivar las conversaciones seleccionadas."));
    } finally {
      setBulkBusy(false);
    }
  }

  function setStatusFilter(value: string) {
    exitSelection();
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set("status", value);
      else next.delete("status");
      return next;
    });
  }

  function setChannelFilter(value: string) {
    exitSelection();
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set("canal", value);
      else next.delete("canal");
      return next;
    });
  }

  // El orden vive en la URL (?orden=…) para que sobreviva a un refresco.
  function setSortFilter(value: InboxSort) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value && value !== "recent") next.set("orden", value);
      else next.delete("orden");
      return next;
    });
  }

  function toggleUnreadOnly() {
    exitSelection();
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (unreadOnly) next.delete("no_leidas");
      else next.set("no_leidas", "1");
      return next;
    });
  }

  // Debounce del buscador (300ms): un solo refresh cuando dejas de teclear.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  // Espejo de `selectedId` para los callbacks asíncronos. Se declara ANTES que
  // los efectos que cargan mensajes, así ya está actualizado cuando corren.
  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    pagesLoadedRef.current = pagesLoaded;
  }, [pagesLoaded]);

  // Carga los mensajes de `convId` y los pinta SOLO si al llegar la respuesta
  // sigue siendo la conversación abierta y no se ha pedido otra por detrás
  // ("gana la última", igual que refreshList). Devuelve false si se descartó.
  const loadMessages = useCallback(async (convId: string): Promise<boolean> => {
    const seq = ++msgSeq.current;
    const msgs = await getMessages(convId, MESSAGE_PAGE_SIZE);
    if (seq !== msgSeq.current || convId !== selectedIdRef.current) return false;
    setMessages(dedupeById(msgs));
    // Si el backend devolvió la página entera, es que hay más hacia atrás.
    setHasOlder(msgs.length >= MESSAGE_PAGE_SIZE);
    return true;
  }, []);

  // Tramo ANTERIOR del hilo: pide los mensajes previos al más antiguo que
  // tenemos y los pega por arriba, conservando la posición de lectura.
  const loadOlderMessages = useCallback(async () => {
    const convId = selectedIdRef.current;
    const oldest = messages[0];
    if (!convId || !oldest || loadingOlder) return;
    setLoadingOlder(true);
    const box = scrollRef.current;
    const prevHeight = box?.scrollHeight ?? 0;
    try {
      const older = await getMessages(convId, MESSAGE_PAGE_SIZE, oldest.created_at);
      if (convId !== selectedIdRef.current) return;
      setMessages((prev) => dedupeById([...older, ...prev]));
      setHasOlder(older.length >= MESSAGE_PAGE_SIZE);
      // Sin esto, meter mensajes por arriba te teletransporta a otro punto del
      // hilo: mantenemos a la vista lo que estabas leyendo.
      requestAnimationFrame(() => {
        if (box) box.scrollTop += box.scrollHeight - prevHeight;
      });
    } catch (e) {
      setActionError(errorDetail(e, "No se pudieron cargar los mensajes anteriores."));
    } finally {
      setLoadingOlder(false);
    }
  }, [messages, loadingOlder]);

  // Parámetros de la consulta de la lista, derivados de los filtros actuales.
  // Se sacan aparte porque los usan igual el refresco y el "Ver más".
  const listQuery = useCallback(
    (page: number, pageSize: number) => {
      const isArchivedView = statusFilter === "__archived";
      const isQuarantineView = statusFilter === "__quarantine";
      const isActionView = statusFilter === "__action";
      const isActiveView = statusFilter === "__active";
      const isSpecialView =
        isArchivedView || isQuarantineView || isActionView || isActiveView;
      return {
        status: isSpecialView ? undefined : (statusFilter || undefined),
        canal: channelFilter || undefined,
        archived: (isArchivedView ? "only" : "hide") as "only" | "hide",
        // En 'Archivadas' mostramos también las que estaban en cuarentena (al
        // archivar desde 'Revisión' siguen siendo recuperables aquí).
        quarantine: (isQuarantineView ? "only" : isArchivedView ? "all" : "hide") as
          | "only"
          | "all"
          | "hide",
        // 'Para hacer': Pendientes + Sugerencias en una sola cola (cada fila se
        // etiqueta abajo con su badge).
        action_only: isActionView ? true : undefined,
        active_only: isActionView || isActiveView ? true : undefined,
        unread_only: unreadOnly ? true : undefined,
        search: debouncedSearch || undefined,
        // Un único criterio de orden, el que haya elegido la operadora, y lo
        // aplica el BACKEND antes de paginar.
        sort: sortFilter,
        page,
        page_size: pageSize,
      };
    },
    [statusFilter, channelFilter, debouncedSearch, sortFilter, unreadOnly],
  );

  // Recarga la lista desde la página 1 hasta la última que estuviera cargada,
  // para que un refresco (websocket, reconexión…) no encoja lo que ya se veía.
  const refreshList = useCallback(async () => {
    const seq = ++reqSeq.current;
    const pages = pagesLoadedRef.current;
    let data;
    try {
      data = await listConversations(listQuery(1, LIST_PAGE_SIZE * pages));
    } catch {
      // Sin catch, una caída de la API dejaba la lista vacía y se mostraba el
      // estado "Todo al día ✨" — lo contrario de la realidad.
      if (seq === reqSeq.current) setListError("No se pudo cargar la bandeja. Reintentando…");
      return;
    }
    // "Gana la última": ignora la respuesta si ya se lanzó otra petición
    // después (cambio de filtro/canal o refresh por websocket). Sin esto, una
    // respuesta lenta del filtro anterior machacaba la lista actual.
    if (seq === reqSeq.current) {
      setConversations(data.items);
      // El `total` del backend se descartaba: ahora es lo que permite decir
      // "50 de 137" y saber si queda algo por cargar.
      setTotal(data.total);
      setListError(null);
    }
    // Contadores de pestañas: respetan el canal seleccionado (así "Para hacer N"
    // cuadra con el filtro). Barato y se refresca con cada cambio de filtro /
    // evento de websocket (refreshList es el punto central de refresco).
    void getConversationCounts(channelFilter || undefined).then(setCounts).catch(() => {});
  }, [listQuery, channelFilter]);

  // "Ver más": trae la siguiente página y la añade al final. La lista sigue el
  // orden del backend, así que basta con concatenar.
  const loadMoreConversations = useCallback(async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    const seq = ++reqSeq.current;
    try {
      const next = pagesLoadedRef.current + 1;
      const data = await listConversations(listQuery(next, LIST_PAGE_SIZE));
      if (seq !== reqSeq.current) return;
      setConversations((prev) => dedupeById([...prev, ...data.items]));
      setTotal(data.total);
      // El ref se actualiza AQUÍ y no solo en el efecto espejo: si esperáramos
      // al siguiente render, un refresco que llegara entre medias volvería a
      // pedir el tamaño antiguo y encogería la lista recién ampliada.
      pagesLoadedRef.current = next;
      setPagesLoaded(next);
      setListError(null);
    } catch (e) {
      setListError(errorDetail(e, "No se pudieron cargar más conversaciones."));
    } finally {
      setLoadingMore(false);
    }
  }, [listQuery, loadingMore]);

  // Cambiar de filtro, de canal, de orden o de búsqueda vuelve a la página 1:
  // seguir en la 3 de una lista que ya no existe no significa nada. El ref se
  // pone a 1 en el mismo momento para que el refresco que viene detrás pida ya
  // el tamaño correcto.
  useEffect(() => {
    pagesLoadedRef.current = 1;
    setPagesLoaded(1);
  }, [statusFilter, channelFilter, debouncedSearch, sortFilter, unreadOnly]);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  useEffect(() => {
    void (async () => {
      try { setPause(await getAgentPause()); } catch { /* not admin */ }
    })();
  }, []);

  const selected = useMemo(
    () =>
      conversations.find((c) => c.id === selectedId) ||
      (selectedFallback && selectedFallback.id === selectedId ? selectedFallback : null),
    [conversations, selectedId, selectedFallback]
  );
  // Contacto del chat abierto con la identidad enmascarada cuando el modo
  // privacidad está activo (solo afecta a lo que se PINTA en la cabecera).
  const selectedContact = useMemo(
    () => maskContact(selected?.contact, priv),
    [selected?.contact, priv]
  );

  // Abrir directamente una conversación si llegamos con ?conversation=<id>
  // (p.ej. desde la ficha del contacto). Se consume una vez y se limpia la URL.
  useEffect(() => {
    const wanted = searchParams.get("conversation");
    if (!wanted) return;
    setSelectedId(wanted);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("conversation");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Si la conversación seleccionada no está en la lista filtrada (otro canal,
  // archivada, deep-link ?conversation=…), la traemos SUELTA a un estado aparte
  // para poder abrirla en el panel — SIN inyectarla en la lista. Inyectarla era
  // lo que mezclaba canales: con un email abierto, al pasar a WhatsApp el email
  // se colaba en lo alto de la lista.
  useEffect(() => {
    if (!selectedId || conversations.some((c) => c.id === selectedId)) {
      setSelectedFallback(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const conv = await getConversation(selectedId);
        if (!cancelled) setSelectedFallback(conv);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId, conversations]);

  // Marcar leído SOLO si de verdad estás mirando la pantalla.
  //
  // Antes se marcaba a ciegas: dejabas la bandeja abierta con un chat dentro, te
  // ibas a comer, y todo lo que llegaba entre medias quedaba leído sin que nadie
  // lo hubiera visto. Ahora hace falta que la pestaña esté visible; si no lo
  // está, se apunta y se salda en cuanto vuelves.
  const markReadIfVisible = useCallback(async (convId: string) => {
    if (typeof document !== "undefined" && document.visibilityState !== "visible") {
      pendingReadRef.current = convId;
      return;
    }
    pendingReadRef.current = null;
    try {
      await markRead(convId);
      // Optimista: limpia el badge de "no leídas" al abrir la conversación.
      setConversations((prev) =>
        prev.map((c) => (c.id === convId ? { ...c, unread_count: 0 } : c))
      );
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    // El error pertenece a la conversación en la que pasó: al cambiar, fuera.
    setActionError(null);
    if (!selectedId) {
      // Bumpea el contador: una carga en vuelo ya no puede repintar el hilo
      // que acabamos de cerrar.
      msgSeq.current++;
      setMessages([]);
      return;
    }
    void (async () => {
      try {
        if (!(await loadMessages(selectedId))) return;
      } catch (e) {
        // Sin catch, el hilo se quedaba con los mensajes de la conversación
        // ANTERIOR sin ningún aviso.
        if (selectedId !== selectedIdRef.current) return;
        setActionError(errorDetail(e, "No se pudieron cargar los mensajes de la conversación."));
        return;
      }
      await markReadIfVisible(selectedId);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // Al volver a la pestaña: se salda lo que quedó pendiente de marcar y se pide
  // la lista fresca (mientras no mirabas pudo cambiar todo).
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      const pending = pendingReadRef.current;
      if (pending && pending === selectedIdRef.current) void markReadIfVisible(pending);
      void refreshList();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [markReadIfVisible, refreshList]);

  // Bajar al último mensaje solo cuando entra uno NUEVO al final (o cambias de
  // conversación). Si no, cargar el tramo anterior del hilo te lanzaba abajo
  // justo después de subir a leerlo.
  const lastMessageId = messages.length ? messages[messages.length - 1].id : null;
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [lastMessageId, selectedId]);

  // ── Refrescos del websocket, agrupados ───────────────────────────────────
  //
  // Cada evento disparaba un refresco completo sin agrupar: un mensaje entrante
  // significaba 4 peticiones (lista + contadores + mensajes + mark-read), y eso
  // POR CADA pestaña abierta. Con una conversación movida, el mismo trabajo
  // repetido cuatro veces. Ahora se juntan en una ventana de 250 ms: llegue un
  // evento o lleguen diez seguidos, se refresca una sola vez.
  const scheduleRefresh = useCallback(
    (alsoMessages: boolean) => {
      if (alsoMessages) refreshMessagesPendingRef.current = true;
      if (refreshTimerRef.current !== null) return;
      refreshTimerRef.current = window.setTimeout(() => {
        refreshTimerRef.current = null;
        const withMessages = refreshMessagesPendingRef.current;
        refreshMessagesPendingRef.current = false;
        void refreshList();
        const convId = selectedIdRef.current;
        if (withMessages && convId) {
          void (async () => {
            try {
              if (await loadMessages(convId)) await markReadIfVisible(convId);
            } catch {
              /* ignore */
            }
          })();
        }
      }, WS_REFRESH_DEBOUNCE_MS);
    },
    [refreshList, loadMessages, markReadIfVisible],
  );

  useEffect(() => {
    return () => {
      if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    };
  }, []);

  const socketStatus = useInboxSocket(
    (ev) => {
      if (ev.type === "message.new") {
        const convId = ev.payload.conversation_id as string;
        scheduleRefresh(convId === selectedIdRef.current);
      } else if (ev.type === "conversation.updated" || ev.type === "message.updated") {
        scheduleRefresh(!!selectedIdRef.current);
      }
    },
    // Al RECONECTAR hay que ponerse al día: durante el corte no llegó ningún
    // evento y la bandeja se quedó congelada con datos viejos, con toda la
    // pinta de estar bien.
    () => scheduleRefresh(true),
  );

  // Red de seguridad mientras el tiempo real está caído: una consulta cada 20 s
  // para que la bandeja siga viva aunque el websocket no vuelva.
  useEffect(() => {
    if (socketStatus !== "offline") return;
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible") void refreshList();
    }, OFFLINE_POLL_MS);
    return () => window.clearInterval(t);
  }, [socketStatus, refreshList]);

  const isEmail = selected?.canal === "email";
  // Cada canal tiene su propia ventana de mensajería:
  //  - WhatsApp: 24h (política de su API; fuera → solo plantillas aprobadas).
  //  - Instagram (Meta): 24h estándar, ampliable a 7 días respondiendo como
  //    agente humano (etiqueta HUMAN_AGENT, la aplica el backend). Pasados 7
  //    días Meta NO permite responder → bloqueamos para no arriesgar el baneo.
  //  - Email y web: sin ventana.
  const isWhatsapp = selected?.canal === "whatsapp";
  const isInstagram = selected?.canal === "instagram_dm";
  const lastUserMsg = [...messages].reverse().find((m) => m.rol === "user");
  const hoursAgo = lastUserMsg
    ? (Date.now() - new Date(lastUserMsg.created_at).getTime()) / 3600000
    : Infinity;
  // Instagram entre 24h y 7 días: se puede responder, pero va como agente
  // humano (lo mostramos como aviso informativo, no bloquea).
  const igHumanAgent = isInstagram && hoursAgo > 24 && hoursAgo <= 24 * 7;
  const withinWindow = isWhatsapp
    ? hoursAgo <= 24
    : isInstagram
      ? hoursAgo <= 24 * 7
      : true;
  // Email (F5b): la operadora puede responder SIN "Tomar control" — el agente
  // solo redacta borradores, no hay piloto automático que interrumpir. El resto
  // de canales sigue requiriendo pasar a humano. En ambos, nunca si está cerrada.
  const canSend =
    !!selected &&
    withinWindow &&
    (isEmail ? selected.status !== "cerrada" : selected.status === "humano");

  async function onSend() {
    if (!selectedId || !draft.trim()) return;
    setSending(true);
    setActionError(null);
    try {
      await sendMessage(selectedId, draft.trim());
      setDraft("");
      await loadMessages(selectedId);
    } catch (e) {
      if (selectedId !== selectedIdRef.current) return;
      // Antes el fallo era silencioso: la operadora no sabía que su respuesta
      // NO se había enviado. Mostramos el motivo del backend si llega.
      setActionError(errorDetail(e, "No se pudo enviar el mensaje. Inténtalo de nuevo."));
    } finally {
      setSending(false);
    }
  }

  function openCloseModal() {
    if (!selected) return;
    // Prerrellena con el resumen existente (lo genera el cierre automático) para
    // que la operadora pueda revisarlo/editarlo antes de cerrar a mano.
    setCloseResumen(selected.resumen ?? "");
    setCloseEmail(false);
    setActionError(null);
    setCloseOpen(true);
  }

  async function onCloseConversation() {
    if (!selectedId) return;
    setCloseBusy(true);
    setActionError(null);
    try {
      await closeConversation(selectedId, {
        resumen: closeResumen.trim() || undefined,
        enviar_resumen_email: closeEmail,
      });
      setCloseOpen(false);
      setSelectedId(null);
      void refreshList();
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo cerrar la conversación."));
    } finally {
      setCloseBusy(false);
    }
  }


  return (
    <div className="h-full flex flex-col bg-paper">
      {/* Banner pausa global */}
      {pause?.paused && (
        <div className="bg-state-warn/10 border-b border-state-warn/30 px-4 py-2 text-xs text-state-warn flex items-center gap-2">
          <Pause className="w-4 h-4" />
          Agente PAUSADO globalmente. Solo responde en {pause.demo_conversations.length} conversación{pause.demo_conversations.length === 1 ? "" : "es"} en modo demo.
        </div>
      )}

      {/* Enlace en tiempo real caído. Antes no se decía NADA: la bandeja se
          congelaba con datos viejos y con aspecto de estar perfectamente. */}
      {socketStatus === "offline" && (
        <div className="bg-state-warn/10 border-b border-state-warn/30 px-4 py-2 text-xs text-state-warn flex items-center gap-2">
          <WifiOff className="w-4 h-4 shrink-0" />
          <span className="flex-1">
            Sin conexión en tiempo real. Reintentando… mientras tanto la bandeja se
            actualiza cada 20 segundos.
          </span>
          <button
            type="button"
            onClick={() => void refreshList()}
            className="font-semibold hover:underline shrink-0"
          >
            Actualizar ahora
          </button>
        </div>
      )}

      <div className="flex-1 flex min-h-0">
        {/* Lista lateral */}
        <aside
          className={clsx(
            "flex flex-col bg-card border-r border-line",
            // En mobile: ocupa todo el espacio si no hay conversación seleccionada;
            // si hay selección, se oculta y el chat ocupa todo
            selected ? "hidden md:flex" : "flex w-full md:w-auto",
            "md:w-80 md:shrink-0"
          )}
        >
          {/* Pestañas de canal — vista filtrada persistida en la URL */}
          <div className="flex border-b border-line bg-paper2/40 overflow-x-auto">
            {CHANNEL_TABS.map((tab) => {
              const active = channelFilter === tab.value;
              return (
                <button
                  key={tab.value}
                  type="button"
                  onClick={() => setChannelFilter(tab.value)}
                  className={clsx(
                    "px-3 py-2 text-xs font-medium border-b-2 -mb-px transition whitespace-nowrap inline-flex items-center gap-1.5",
                    active
                      ? "border-brand-ink text-ink bg-card"
                      : "border-transparent text-ink3 hover:text-ink2",
                  )}
                  title={tab.value ? `Filtrar solo ${tab.label}` : "Ver todas"}
                >
                  {tab.color && (
                    <span
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ background: tab.color }}
                    />
                  )}
                  {tab.label}
                </button>
              );
            })}
          </div>

          <div className="px-3 py-3 border-b border-line space-y-2">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink3" />
              {/* Ahora el buscador hace lo que promete: nombre, teléfono, email
                  y @usuario del contacto, asunto del correo y texto de los
                  mensajes. (Las transcripciones de audio no: están cifradas.) */}
              <input
                placeholder="Buscar mensajes, contactos…"
                title="Busca en el nombre, teléfono, email y @usuario del contacto, en el asunto del correo y dentro del texto de los mensajes."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="input pl-9 w-full"
              />
            </div>
            {/* Orden de la lista. Un único criterio, decidido en el servidor y
                aplicado ANTES de paginar; se guarda en la URL, así aguanta un
                refresco. */}
            <div className="flex items-center gap-2">
              <label className="text-[11px] text-ink3 shrink-0" htmlFor="inbox-sort">
                Ordenar por
              </label>
              <select
                id="inbox-sort"
                className="input text-xs py-1 flex-1"
                value={sortFilter}
                onChange={(e) => setSortFilter(e.target.value as InboxSort)}
                title={INBOX_SORTS.find((s) => s.value === sortFilter)?.hint}
              >
                {INBOX_SORTS.map((s) => (
                  <option key={s.value} value={s.value} title={s.hint}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex gap-1 flex-wrap">
              {STATUS_FILTERS.map((f) => {
                const active = statusFilter === f.value;
                // Badge con el número de la cola (solo donde aporta: lo accionable,
                // lo derivado a humano y la cola de Revisión).
                const count =
                  f.value === "__action"
                    ? counts?.action
                    : f.value === "humano"
                      ? counts?.humano
                      : f.value === "__quarantine"
                        ? counts?.quarantine
                        : undefined;
                return (
                  <button
                    key={f.value}
                    type="button"
                    onClick={() => setStatusFilter(f.value)}
                    className={clsx(
                      "inline-flex items-center gap-1 px-2.5 py-1 rounded-coro-sm text-[12px] font-medium transition-colors",
                      active
                        ? "bg-brand text-brand-on"
                        : "bg-paper2 text-ink2 hover:bg-paper3"
                    )}
                  >
                    {f.label}
                    {typeof count === "number" && count > 0 && (
                      <span
                        className={clsx(
                          "numbers text-[10px] leading-none px-1.5 py-0.5 rounded-full",
                          active ? "bg-brand-on/20 text-brand-on" : "bg-paper3 text-ink2"
                        )}
                      >
                        {count}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
            {/* "Solo sin leer": el backend siempre supo filtrarlo y no había
                forma de pedirlo desde aquí, así que no se podía separar lo que
                te falta por leer de lo que el bot ya atendió. */}
            <button
              type="button"
              onClick={toggleUnreadOnly}
              aria-pressed={unreadOnly}
              className={clsx(
                "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-coro-sm text-[12px] font-medium transition-colors",
                unreadOnly
                  ? "bg-brand text-brand-on"
                  : "bg-paper2 text-ink2 hover:bg-paper3",
              )}
              title="Mostrar solo las conversaciones con mensajes del cliente sin leer, respondiera el bot o no"
            >
              <Mail className="w-3.5 h-3.5" />
              Solo sin leer
            </button>
            {statusFilter === "__action" && counts && (counts.pending > 0 || counts.suggestions > 0) && (
              <div className="text-[11px] text-ink3 px-0.5">
                {counts.pending} esperando · {counts.suggestions} sugerencia{counts.suggestions === 1 ? "" : "s"}
              </div>
            )}
          </div>

          {/* Selección múltiple para archivar en bloque (p.ej. vaciar Revisión). */}
          {selectionMode && (
            <div className="px-3 py-2 border-b border-line bg-brand/5 flex items-center gap-2">
              <label className="inline-flex items-center gap-1.5 text-[12px] text-ink2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={conversations.length > 0 && selectedIds.size === conversations.length}
                  onChange={toggleSelectAll}
                  className="accent-brand w-4 h-4"
                />
                {selectedIds.size > 0
                  ? `${selectedIds.size} seleccionada${selectedIds.size === 1 ? "" : "s"}`
                  : "Seleccionar todas"}
              </label>
              <div className="flex-1" />
              <button
                type="button"
                onClick={onBulkArchive}
                disabled={selectedIds.size === 0 || bulkBusy}
                className="btn-primary btn-sm"
              >
                <Archive className="w-3.5 h-3.5" /> Archivar
              </button>
              <button type="button" onClick={exitSelection} className="btn-ghost btn-sm">
                Cancelar
              </button>
            </div>
          )}
          {/* Error del archivado masivo: el banner del footer solo existe con un
              chat abierto; aquí la acción se hace desde la lista. */}
          {selectionMode && actionError && (
            <div className="mx-3 mt-2 text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
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
          {/* El archivado en bloque es admin-only: si no lo eres, ni siquiera
              se ofrece entrar en modo selección (acabaría en un 403). */}
          {!selectionMode && isAdmin && (
            <div className="px-3 py-1.5 border-b border-line flex justify-end">
              <button
                type="button"
                onClick={() => setSelectionMode(true)}
                className="text-[12px] text-ink3 hover:text-ink2 inline-flex items-center gap-1"
                title="Seleccionar varias conversaciones para archivar a la vez"
              >
                <CheckSquare className="w-3.5 h-3.5" /> Seleccionar
              </button>
            </div>
          )}

          <div className="flex-1 overflow-auto">
            {/* Si la carga FALLÓ, nunca mostramos el estado vacío celebratorio:
                sería mentira. El websocket/el siguiente refresh lo reintenta. */}
            {listError && (
              <div className="mx-3 mt-3 text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
                <span className="flex-1">{listError}</span>
                <button
                  type="button"
                  onClick={() => void refreshList()}
                  className="font-semibold hover:underline shrink-0"
                >
                  Reintentar
                </button>
              </div>
            )}
            {/* Estado vacío. Buscar y no encontrar NO es "todo al día": son dos
                cosas distintas y decirlo mal es justo el pantallazo que no
                quieres. Lo mismo con el filtro de no leídas. */}
            {conversations.length === 0 && !listError && (
              <div className="text-center text-ink3 text-sm py-12 px-4 italic font-display">
                {debouncedSearch ? (
                  <>
                    <div>Sin resultados para “{debouncedSearch}”.</div>
                    <button
                      type="button"
                      onClick={() => setSearch("")}
                      className="mt-2 not-italic font-medium text-ink2 hover:underline text-[12px]"
                    >
                      Borrar la búsqueda
                    </button>
                  </>
                ) : unreadOnly ? (
                  "No queda nada sin leer."
                ) : statusFilter === "__action" ? (
                  "Todo al día ✨ Nada por hacer."
                ) : statusFilter === "__quarantine" ? (
                  "Nada descartado."
                ) : (
                  "No hay conversaciones aún."
                )}
              </div>
            )}
            {/* SIN reordenar: la lista se pinta tal y como la manda el backend,
                con el criterio elegido arriba. */}
            {conversations.map((c) => {
              const contact = maskContact(c.contact, priv);
              const init = maskInitials(initialsOf(c.contact?.nombre, c.contact?.telefono), priv);
              const color = colorFor(c.contact?.telefono || c.id);
              const isActive = selectedId === c.id;
              const channelMeta = CHANNEL_META[c.canal];
              // Sugerencia de Entrenamiento ya REVISADA = la has abierto (al
              // abrir se marca leída → unread_count 0). Sin abrir = por revisar.
              const isSuggestion = !!c.has_training_suggestion;
              const suggestionReviewed = isSuggestion && (c.unread_count ?? 0) === 0;
              // Negrita para lo accionable: lo que necesita a una persona
              // (needs_action) o una sugerencia de Entrenamiento SIN revisar.
              // Lo ya respondido por el bot o lo ya revisado no se destaca.
              const pending = needsAction(c) || (isSuggestion && !suggestionReviewed);
              return (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => (selectionMode ? toggleSelect(c.id) : setSelectedId(c.id))}
                  className={clsx(
                    "w-full text-left px-3 py-2.5 border-b border-line2 hover:bg-paper2 transition-colors flex items-start gap-3",
                    (isActive || (selectionMode && selectedIds.has(c.id))) && "bg-brand/10"
                  )}
                >
                  {selectionMode && (
                    <div className="shrink-0 flex items-center self-center">
                      <input
                        type="checkbox"
                        readOnly
                        checked={selectedIds.has(c.id)}
                        className="pointer-events-none accent-brand w-4 h-4"
                      />
                    </div>
                  )}
                  <div className="relative shrink-0">
                    <div
                      className="w-9 h-9 rounded-full flex items-center justify-center text-[11px] font-bold"
                      style={{ background: color, color: "var(--brand-on)" }}
                    >
                      {init}
                    </div>
                    {channelMeta && (
                      <span
                        className="absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full ring-2 ring-card"
                        style={{ background: channelMeta.color }}
                        title={channelMeta.label}
                      />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className={clsx("text-sm truncate", pending ? "font-bold text-ink" : "font-medium text-ink")}>
                          {contact?.nombre || contact?.telefono || "Sin nombre"}
                        </div>
                        {contact?.social_handle &&
                          contact.social_handle !== contact?.nombre && (
                            <div className="text-[11px] text-ink3 truncate leading-tight">
                              {contact.social_handle}
                            </div>
                          )}
                      </div>
                      <div className="flex items-center gap-1.5 shrink-0">
                        <div className="text-[10px] text-ink4 font-mono">{timeAgo(c.last_message_at)}</div>
                        {/* Sin leer es sin leer, conteste el bot o no. Antes el
                            badge solo se pintaba si la conversación además
                            necesitaba acción, así que en cuanto el bot
                            respondía desaparecía aunque el mensaje del cliente
                            siguiera sin que lo hubiera visto nadie — que es el
                            caso más común de todos. */}
                        {(c.unread_count ?? 0) > 0 && (
                          <span
                            className={clsx(
                              "rounded-full font-semibold text-[10px] leading-none px-1.5 py-0.5 min-w-[16px] text-center",
                              pending
                                ? "bg-brand text-brand-on"
                                : "bg-paper3 text-ink2 border border-line",
                            )}
                            title={
                              pending
                                ? `${c.unread_count} mensaje(s) del cliente sin leer · necesita a una persona`
                                : `${c.unread_count} mensaje(s) del cliente sin leer (el bot ya ha contestado)`
                            }
                          >
                            {c.unread_count}
                          </span>
                        )}
                      </div>
                    </div>
                    {/* Email: el asunto del correo va antes del preview del cuerpo. */}
                    {c.canal === "email" && c.subject && (
                      <div className="text-[12px] text-ink2 font-medium truncate mt-0.5">
                        {c.subject}
                      </div>
                    )}
                    <div className={clsx("text-[12px] truncate mt-0.5", pending ? "text-ink2 font-medium" : "text-ink3")}>
                      {(c as Conversation & { last_message_preview?: string }).last_message_preview || "—"}
                    </div>
                    <div className="mt-1.5 flex items-center gap-1 flex-wrap">
                      <span className={"badge text-[10px] border-transparent " + (STATUS_BADGE[c.status] || "bg-paper2 text-ink3")}>
                        {c.status}
                      </span>
                      {channelMeta && (
                        <span
                          className="badge text-[10px] border-transparent"
                          style={{
                            background: channelMeta.color + "22",
                            color: channelMeta.color,
                          }}
                        >
                          {channelMeta.label}
                        </span>
                      )}
                      {isSuggestion ? (
                        <span
                          className={clsx(
                            "badge text-[10px] border-transparent",
                            suggestionReviewed
                              ? "bg-paper3 text-ink3"
                              : "bg-ch-web/15 text-ch-web"
                          )}
                          title={
                            suggestionReviewed
                              ? "Sugerencia ya revisada (pendiente de enviar o descartar)"
                              : "Sugerencia del agente (modo Entrenamiento) SIN revisar"
                          }
                        >
                          <Pencil className="w-2.5 h-2.5" />{" "}
                          {suggestionReviewed ? "Sugerencia ✓" : "Sugerencia"}
                        </span>
                      ) : c.has_pending_draft ? (
                        <span
                          className="badge text-[10px] border-transparent bg-state-warn/15 text-state-warn"
                          title="Borrador del agente pendiente de enviar o descartar"
                        >
                          <Pencil className="w-2.5 h-2.5" /> Pendiente
                        </span>
                      ) : null}
                      {c.demo_active && (
                        <span className="badge text-[10px] border-transparent bg-ch-voice/15 text-ch-voice">
                          <FlaskConical className="w-2.5 h-2.5" /> demo
                        </span>
                      )}
                      {c.archived && (
                        <span className="badge text-[10px] border-transparent bg-paper3 text-ink3">
                          <Archive className="w-2.5 h-2.5" /> archivada
                        </span>
                      )}
                    </div>
                  </div>
                </button>
              );
            })}
            {/* Paginación de la bandeja. Antes había un tope duro de 50 y el
                `total` que manda el backend se tiraba: con 51 conversaciones
                vivas, la 51 no existía y nada lo indicaba. */}
            {conversations.length > 0 && (
              <div className="px-3 py-3 text-center border-t border-line2">
                <div className="text-[11px] text-ink3">
                  {conversations.length} de <b className="text-ink2 numbers">{total}</b>
                </div>
                {conversations.length < total && (
                  <button
                    type="button"
                    onClick={() => void loadMoreConversations()}
                    disabled={loadingMore}
                    className="btn-ghost btn-sm mt-1.5"
                  >
                    {loadingMore
                      ? "Cargando…"
                      : `Ver más (quedan ${total - conversations.length})`}
                  </button>
                )}
              </div>
            )}
          </div>
        </aside>

        {/* Panel del chat */}
        <main
          className={clsx(
            "flex-1 flex flex-col bg-paper min-w-0",
            !selected && "hidden md:flex"
          )}
        >
          {!selected && (
            <div className="flex-1 flex flex-col items-center justify-center text-ink3 text-sm gap-2 px-6 text-center">
              <BotIcon className="w-12 h-12 text-ink4" />
              <div className="font-display text-xl italic text-ink2">
                Selecciona una conversación
              </div>
              <div className="text-xs max-w-xs">
                La actividad de WhatsApp aparece en tiempo real en la lista de la izquierda.
              </div>
            </div>
          )}
          {selected && (
            <>
              <header className="px-3 py-3 md:px-4 bg-card border-b border-line flex items-center justify-between gap-2">
                {/* Volver a la lista de chats — solo móvil. Etiquetado y con
                    fondo para que sea claramente pulsable. */}
                <button
                  type="button"
                  onClick={() => setSelectedId(null)}
                  className="md:hidden inline-flex items-center gap-1 h-9 px-2 rounded-coro-sm border border-line bg-paper2 hover:bg-paper3 text-ink2 shrink-0"
                  aria-label="Volver a los chats"
                >
                  <ArrowLeft className="w-4 h-4" />
                  <span className="text-xs font-medium">Chats</span>
                </button>

                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <div
                    className="w-9 h-9 rounded-full flex items-center justify-center text-[11px] font-bold shrink-0"
                    style={{ background: colorFor(selected.contact?.telefono || selected.id), color: "var(--brand-on)" }}
                  >
                    {maskInitials(initialsOf(selected.contact?.nombre, selected.contact?.telefono), priv)}
                  </div>
                  <div className="min-w-0">
                    <div className="font-semibold text-ink truncate flex items-center gap-1.5">
                      <span className="truncate">
                        {selectedContact?.nombre ||
                          (selected.canal === "email"
                            ? selectedContact?.email
                            : selectedContact?.telefono) ||
                          "Sin nombre"}
                      </span>
                      {selected.contact?.in_crm === false && (
                        <span
                          className="pill bg-paper3 text-ink3 text-[10px]"
                          title="Aún no está en el CRM"
                        >
                          Fuera del CRM
                        </span>
                      )}
                    </div>
                    {selectedContact?.social_handle &&
                      selectedContact.social_handle !== selectedContact?.nombre && (
                        <div className="text-[12px] text-ink3 truncate">
                          {selectedContact.social_handle}
                        </div>
                      )}
                    {/* Email: mostramos el asunto del hilo bajo el remitente. */}
                    {selected.canal === "email" && selected.subject && (
                      <div className="text-[12px] text-ink2 font-medium truncate">
                        {selected.subject}
                      </div>
                    )}
                    <div className="text-[11px] text-ink3 truncate font-mono">
                      {selected.canal === "email"
                        ? selectedContact?.email
                        : selectedContact?.telefono}
                    </div>
                  </div>
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  {selected.contact?.in_crm === false && selected.contact?.id && (
                    <button
                      type="button"
                      onClick={async () => {
                        const id = selected.contact!.id;
                        try {
                          await addContactToCrm(id);
                          await refreshList();
                        } catch (e) {
                          setActionError(errorDetail(e, "No se pudo añadir el contacto al CRM."));
                        }
                      }}
                      className="btn-primary btn-sm hidden sm:inline-flex"
                      title="Añadir este contacto al CRM"
                    >
                      <UserCheck /> <span className="hidden md:inline">Añadir al CRM</span>
                    </button>
                  )}
                  <Link
                    to={`/contacts/${selected.contact?.id}`}
                    className="btn-ghost btn-sm hidden sm:inline-flex"
                    title="Ver ficha del contacto"
                  >
                    <User /> <span className="hidden md:inline">Ficha</span>
                  </Link>
                  {selected.demo_active ? (
                    <button
                      type="button"
                      onClick={() =>
                        demoDisable(selected.id)
                          .then(refreshList)
                          .catch((e) => setActionError(errorDetail(e, "No se pudo quitar el modo demo.")))
                      }
                      className="btn-ghost btn-sm text-ch-voice"
                      title="Quitar de la whitelist demo"
                    >
                      <FlaskConical /> <span className="hidden md:inline">Quitar demo</span>
                    </button>
                  ) : (
                    (pause?.paused ||
                      pause?.channels?.find((c) => c.canal === selected.canal)
                        ?.self_paused) &&
                    selected.status === "bot" && (
                      <button
                        type="button"
                        onClick={() =>
                          demoEnable(selected.id)
                            .then(refreshList)
                            .catch((e) => setActionError(errorDetail(e, "No se pudo activar el modo demo.")))
                        }
                        className="btn-ghost btn-sm text-ch-voice"
                        title="Activar el agente solo aquí"
                      >
                        <FlaskConical /> <span className="hidden md:inline">Activar demo</span>
                      </button>
                    )
                  )}
                  {selected.status === "bot" && !isEmail && (
                    <button
                      type="button"
                      onClick={() =>
                        takeOver(selected.id)
                          .then(refreshList)
                          .catch((e) => setActionError(errorDetail(e, "No se pudo tomar el control.")))
                      }
                      className="btn-ghost btn-sm"
                    >
                      <UserCheck /> <span className="hidden md:inline">Tomar control</span>
                    </button>
                  )}
                  {selected.status === "humano" && (
                    <button
                      type="button"
                      onClick={() =>
                        returnToBot(selected.id)
                          .then(refreshList)
                          .catch((e) => setActionError(errorDetail(e, "No se pudo devolver la conversación al bot.")))
                      }
                      className="btn-ghost btn-sm"
                    >
                      <BotIcon /> <span className="hidden md:inline">Al bot</span>
                    </button>
                  )}
                  {/* Sacar de la cuarentena antispam: admin-only en el backend. */}
                  {selected.quarantined && isAdmin && (
                    <button
                      type="button"
                      onClick={() =>
                        releaseQuarantine(selected.id)
                          .then(refreshList)
                          .catch((e) => setActionError(errorDetail(e, "No se pudo liberar la conversación.")))
                      }
                      className="btn-primary btn-sm"
                      title="No es spam: libera la conversación y deja que el bot responda"
                    >
                      <ShieldCheck /> <span className="hidden md:inline">No es spam</span>
                    </button>
                  )}
                  {!selected.archived && selected.status !== "cerrada" && (
                    <button
                      type="button"
                      onClick={openCloseModal}
                      className="btn-ghost btn-sm"
                      title="Marcar como resuelta (opcional: enviar resumen al cliente)"
                    >
                      <CheckCircle2 /> <span className="hidden md:inline">Cerrar</span>
                    </button>
                  )}
                  {selected.archived ? (
                    <button
                      type="button"
                      onClick={() =>
                        unarchiveConversation(selected.id)
                          .then(() => { setSelectedId(null); void refreshList(); })
                          .catch((e) => setActionError(errorDetail(e, "No se pudo desarchivar la conversación.")))
                      }
                      className="btn-ghost btn-sm"
                      title="Devolver a la bandeja"
                    >
                      <ArchiveRestore /> <span className="hidden md:inline">Desarchivar</span>
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() =>
                        archiveConversation(selected.id)
                          .then(() => { setSelectedId(null); void refreshList(); })
                          .catch((e) => setActionError(errorDetail(e, "No se pudo archivar la conversación.")))
                      }
                      className="btn-ghost btn-sm"
                      title="Ocultar (vuelve si el cliente escribe)"
                    >
                      <Archive />
                    </button>
                  )}
                </div>
              </header>

              {selected.quarantined && (
                <div className="px-4 py-2 bg-state-warn/10 border-b border-state-warn/30 text-[12px] text-state-warn flex items-center gap-2">
                  <ShieldCheck className="w-3.5 h-3.5 shrink-0" />
                  <span className="flex-1">
                    Retenida por el clasificador{selected.quarantine_reason ? `: ${selected.quarantine_reason}` : ""}. El bot no responde hasta que se libere
                    {isAdmin
                      ? ' con "No es spam".'
                      : ". Solo un administrador puede sacarla de aquí."}
                  </span>
                </div>
              )}

              <div ref={scrollRef} className="flex-1 overflow-auto px-4 py-4 space-y-2">
                {messages.length === 0 && (
                  <div className="text-center text-ink3 text-sm italic font-display py-8">
                    Sin mensajes aún.
                  </div>
                )}
                {/* Tramo anterior del hilo. Solo se veían los últimos 100
                    mensajes: el backend ya sabía paginar hacia atrás y no lo
                    usaba nadie, así que el histórico de un cliente antiguo era
                    inalcanzable desde aquí. */}
                {hasOlder && messages.length > 0 && (
                  <div className="text-center pb-2">
                    <button
                      type="button"
                      onClick={() => void loadOlderMessages()}
                      disabled={loadingOlder}
                      className="btn-ghost btn-sm"
                    >
                      <ChevronUp className="w-3.5 h-3.5" />
                      {loadingOlder ? "Cargando…" : "Ver mensajes anteriores"}
                    </button>
                  </div>
                )}
                {messages.map((m) => {
                  if (m.is_draft && !m.draft_sent) {
                    return (
                      <DraftCard
                        key={m.id}
                        message={m}
                        onSent={async () => {
                          await loadMessages(selected.id);
                          void refreshList();
                        }}
                        onDiscarded={async () => {
                          await loadMessages(selected.id);
                          void refreshList();
                        }}
                      />
                    );
                  }
                  // Los adjuntos del correo se pintan bajo la burbuja: son del
                  // mensaje, pero no forman parte del cuerpo renderizado.
                  const atts = isEmail ? parseEmailAttachments(m.metadata) : [];
                  return (
                    <div key={m.id}>
                      <MessageBubble message={m} isEmail={selected.canal === "email"} />
                      {atts.length > 0 && (
                        <EmailAttachments
                          conversationId={selected.id}
                          messageId={m.id}
                          attachments={atts}
                        />
                      )}
                    </div>
                  );
                })}
              </div>

              <footer className="p-3 bg-card border-t border-line">
                {selected.status === "bot" && !isEmail && (
                  <div className="text-xs text-ink3 bg-paper2 border border-line rounded-coro-sm px-3 py-2 mb-2">
                    El bot está atendiendo esta conversación. Pulsa <b className="text-ink2">Tomar control</b> para responder tú.
                  </div>
                )}
                {selected.status === "humano" && isWhatsapp && hoursAgo > 24 && (
                  <div className="text-xs text-state-warn bg-state-warn/10 border border-state-warn/30 rounded-coro-sm px-3 py-2 mb-2">
                    Han pasado más de 24h desde el último mensaje del usuario. WhatsApp solo permite responder con plantillas aprobadas (próximamente).
                  </div>
                )}
                {/* Instagram entre 24h y 7 días: se puede responder, pero Meta lo
                    trata como respuesta de agente humano. Aviso informativo. */}
                {selected.status === "humano" && igHumanAgent && (
                  <div className="text-xs text-ink3 bg-paper2 border border-line rounded-coro-sm px-3 py-2 mb-2">
                    Han pasado más de 24h. Tu respuesta se enviará como <b className="text-ink2">agente humano</b> (Instagram lo permite hasta 7 días desde el último mensaje del cliente).
                  </div>
                )}
                {/* Instagram pasados 7 días: Meta no permite responder. */}
                {selected.status === "humano" && isInstagram && hoursAgo > 24 * 7 && (
                  <div className="text-xs text-state-warn bg-state-warn/10 border border-state-warn/30 rounded-coro-sm px-3 py-2 mb-2">
                    Han pasado más de 7 días desde el último mensaje del cliente. Instagram no permite responder fuera de esa ventana (política de Meta).
                  </div>
                )}
                {/* Error visible de la última acción (envío fallido, etc.) —
                    mismo estilo que el aviso de 24h, en tono de error. */}
                {actionError && (
                  <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 mb-2 flex items-start gap-2">
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
                <div className="flex gap-2 items-end">
                  <textarea
                    rows={2}
                    placeholder={isEmail ? "Escribe tu respuesta por email…" : "Escribe un mensaje…"}
                    value={draft}
                    disabled={!canSend}
                    onChange={(e) => setDraft(e.target.value)}
                    className="input resize-none flex-1"
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        void onSend();
                      }
                    }}
                  />
                  <div className="flex flex-col items-end gap-1">
                    <button
                      type="button"
                      onClick={onSend}
                      disabled={!canSend || sending || !draft.trim()}
                      className="btn-primary h-[44px]"
                    >
                      <Send />
                    </button>
                    {/* Adjuntos: solo WhatsApp (sube media a YCloud). Para email
                        aún no soportamos adjuntos (queda fuera del alcance de 5b). */}
                    {!isEmail && (
                      <AttachmentComposer
                        conversationId={selected.id}
                        disabled={!canSend}
                        onSent={(msg) => {
                          // La subida puede tardar: si ya se cambió de chat,
                          // este mensaje NO va aquí (es de otro cliente).
                          if (selected.id !== selectedIdRef.current) return;
                          setMessages((prev) => dedupeById([...prev, msg]));
                        }}
                      />
                    )}
                  </div>
                </div>
                {/* Acceso discreto a la traza técnica del agente (antes botón
                    "Trace" en la cabecera): enlace pequeño al pie del panel. */}
                <div className="mt-2 flex justify-end">
                  <button
                    type="button"
                    onClick={() => setShowTrace(true)}
                    className="inline-flex items-center gap-1 text-[12px] text-ink4 hover:text-ink2 transition-colors"
                    title="Ver la traza técnica de la ejecución del agente"
                  >
                    <Sparkles className="w-3.5 h-3.5" /> Actividad del agente
                  </button>
                </div>
              </footer>
            </>
          )}
        </main>
      </div>
      {showTrace && selected && (
        <ConversationTracePanel
          conversationId={selected.id}
          onClose={() => setShowTrace(false)}
        />
      )}
      {closeOpen && selected && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4"
          onClick={() => !closeBusy && setCloseOpen(false)}
        >
          <div
            className="w-full max-w-md max-h-[90vh] overflow-auto rounded-2xl bg-paper p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-ink flex items-center gap-2">
              <CheckCircle2 className="w-4 h-4 text-state-ok" /> Cerrar conversación
            </h3>
            <p className="mt-1 text-[13px] text-ink3">
              Marca la conversación como resuelta. Puedes añadir un resumen y, si el
              cliente tiene email, enviárselo.
            </p>
            <label className="mt-4 block text-[13px] font-medium text-ink2">
              Resumen (opcional)
            </label>
            <textarea
              value={closeResumen}
              onChange={(e) => setCloseResumen(e.target.value)}
              rows={4}
              placeholder="Breve resumen de la consulta y su resolución…"
              className="mt-1 w-full rounded-lg border border-line bg-paper2 px-3 py-2 text-[14px] text-ink resize-y focus:outline-none focus:ring-2 focus:ring-brand/40"
            />
            <label
              className={clsx(
                "mt-3 flex items-center gap-2 text-[13px]",
                selected.contact?.email ? "text-ink2" : "text-ink4"
              )}
              title={
                selected.contact?.email
                  ? undefined
                  : "Este contacto no tiene email"
              }
            >
              <input
                type="checkbox"
                checked={closeEmail}
                disabled={!selected.contact?.email}
                onChange={(e) => setCloseEmail(e.target.checked)}
                className="rounded border-line"
              />
              Enviar el resumen por email al cliente
              {selected.contact?.email ? (
                <span className="text-ink4">({selected.contact.email})</span>
              ) : null}
            </label>
            {closeEmail && !closeResumen.trim() && (
              <p className="mt-1 text-[12px] text-state-warn">
                Añade un resumen para poder enviar el email.
              </p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setCloseOpen(false)}
                disabled={closeBusy}
                className="btn-ghost btn-sm"
              >
                Cancelar
              </button>
              <button
                type="button"
                onClick={onCloseConversation}
                disabled={closeBusy || (closeEmail && !closeResumen.trim())}
                className="btn-primary btn-sm"
              >
                {closeBusy ? "Cerrando…" : "Cerrar conversación"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
