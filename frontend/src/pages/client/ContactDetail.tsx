import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import {
  ChevronRight,
  MessageSquare,
  MessagesSquare,
  Phone,
  Mail,
  Tag as TagIcon,
  Save,
  Trash2,
  Download,
  Send,
  X,
  Activity,
  Compass,
  CalendarClock,
  Clock,
  Sparkles,
  UserPlus,
  ArrowRightLeft,
  ArrowUpRight,
  StickyNote,
  History,
  Radio,
  Building2,
  BriefcaseBusiness,
  Globe,
  IdCard,
  MapPin,
  BellOff,
  BellRing,
  Merge,
} from "lucide-react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { TagPicker } from "@/components/TagPicker";
import { ESTADO_LABEL, ESTADO_COLOR } from "@/components/EstadoSelect";
import { useAuth } from "@/store/auth";
import { usePrivacy } from "@/store/privacy";
import { maskName, maskPhone, maskEmail, maskInitials } from "@/lib/mask";
import { errorDetail } from "@/lib/errors";
import {
  listWhatsappTemplates,
  outboundSend,
  getTemplateVars,
  checkOutboundOptOut,
  createOutboundOptOut,
  deleteOutboundOptOut,
  CONTACT_FIELD_OPTIONS,
  type WhatsappTemplate,
  type TemplateVar,
} from "@/services/admin";
import {
  InlineTemplateFill,
  TemplatePreview,
  emptyVarNumbers,
} from "@/components/TemplateVarsFiller";
import {
  addContactNote,
  addContactTag,
  createTag,
  deleteContact,
  exportContactData,
  deleteContactNote,
  deleteTag,
  getContact,
  getContactActivity,
  getContactConversations,
  getContactNotes,
  listContacts,
  listTags,
  mergeContacts,
  removeContactTag,
  updateContact,
  type ContactConversationMini,
} from "@/services/contacts";
import type { Contact, ContactActivity, ContactNote, Tag } from "@/types";

const ESTADO_OPTIONS = [
  { value: "contacto", label: "Contacto" },
  { value: "solicitud_presupuesto", label: "Solicitud presupuesto" },
  { value: "seguimiento", label: "Seguimiento" },
  { value: "cliente", label: "Cliente" },
  { value: "perdido", label: "Perdido" },
  { value: "no_cualifica", label: "No cualifica" },
];

/** Respuesta de `checkOutboundOptOut` (baja permanente de difusiones). */
type OptOutState = Awaited<ReturnType<typeof checkOutboundOptOut>>;

const SECTIONS = [
  { id: "resumen", label: "Resumen" },
  { id: "datos", label: "Datos" },
  { id: "etiquetas", label: "Etiquetas" },
  { id: "conversaciones", label: "Conversaciones" },
  { id: "notas", label: "Notas" },
  { id: "actividad", label: "Actividad" },
];

const AVATAR_PALETTE = ["#DBE09E", "#9DA362", "#6C7BFF", "#9B8AFB", "#25D366", "#E58A2F"];

function initialsOf(nombre: string | null, telefono: string): string {
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

// Convención de canal coherente con el inbox (Inbox.tsx / AdminHome.tsx).
// Cubre los alias que llegan según el origen del dato: webchat/web→Web, etc.
const CANAL_META: Record<string, { label: string; color: string }> = {
  whatsapp: { label: "WhatsApp", color: "#25D366" },
  web: { label: "Web", color: "#6C7BFF" },
  webchat: { label: "Web", color: "#6C7BFF" },
  instagram_dm: { label: "Instagram", color: "#E4405F" },
  retell_voice: { label: "Voz", color: "#9B8AFB" },
  email: { label: "Email", color: "#EA4335" },
};

function canalMeta(canal: string): { label: string; color: string } {
  return CANAL_META[canal] ?? { label: canal, color: "#9DA362" };
}

// Fecha legible en es-ES (sin hora) para el resumen de la identity card.
function fechaLarga(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("es-ES", { day: "numeric", month: "long", year: "numeric" });
}

// Fecha + hora legible en es-ES para la cabecera de cada nota.
function fechaHora(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("es-ES", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Fecha corta (día + mes) para las filas de conversación / preview de notas.
function fechaCorta(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString("es-ES", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Tiempo relativo aproximado en español (para "Último contacto").
// Solo unidades que existen de verdad a partir de un timestamp ya presente.
function tiempoRelativo(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const diffMs = Date.now() - d.getTime();
  const min = Math.round(diffMs / 60000);
  if (min < 1) return "ahora mismo";
  if (min < 60) return `hace ${min} min`;
  const horas = Math.round(min / 60);
  if (horas < 24) return `hace ${horas} h`;
  const dias = Math.round(horas / 24);
  if (dias < 30) return `hace ${dias} d`;
  const meses = Math.round(dias / 30);
  if (meses < 12) return `hace ${meses} mes${meses === 1 ? "" : "es"}`;
  const anios = Math.round(meses / 12);
  return `hace ${anios} año${anios === 1 ? "" : "s"}`;
}

// Etiqueta amigable del rol del autor (coherente con el selector de UsersPage).
const ROLE_LABEL: Record<string, string> = { admin: "Admin", cliente: "Cliente" };

function roleLabel(role?: string | null): string {
  if (!role) return "";
  return ROLE_LABEL[role] ?? role;
}

// Lee un campo string del `meta` (flexible) de un evento de actividad.
function metaStr(meta: Record<string, unknown> | null, key: string): string | null {
  const v = meta?.[key];
  return typeof v === "string" ? v : null;
}

// Etiqueta amigable de un valor de estado (reusa ESTADO_LABEL si lo conoce).
function estadoLabel(value: string | null): string {
  if (!value) return "—";
  return ESTADO_LABEL[value] ?? value;
}

// Presentación de un evento del timeline según su `tipo` + `meta`:
// icono, color del punto y etiqueta legible en español.
function activityPresentation(ev: ContactActivity): {
  Icon: typeof Activity;
  color: string;
  label: string;
} {
  const m = ev.meta ?? null;
  switch (ev.tipo) {
    case "contact_created":
      return { Icon: UserPlus, color: "#25D366", label: "Contacto creado" };
    case "status_changed":
      return {
        Icon: ArrowRightLeft,
        color: "#6C7BFF",
        label: `Estado cambiado de «${estadoLabel(metaStr(m, "from"))}» a «${estadoLabel(metaStr(m, "to"))}»`,
      };
    case "tag_added":
      return {
        Icon: TagIcon,
        color: "#9B8AFB",
        label: `Etiqueta «${metaStr(m, "tag") ?? "—"}» añadida`,
      };
    case "tag_removed":
      return {
        Icon: TagIcon,
        color: "#9B8AFB",
        label: `Etiqueta «${metaStr(m, "tag") ?? "—"}» quitada`,
      };
    case "note_added":
      return { Icon: StickyNote, color: "#E58A2F", label: "Nota añadida" };
    // Borrar una nota no dejaba rastro, aunque crearla sí: una nota podía
    // desaparecer sin que quedara ni una línea en el sitio donde se mira.
    case "note_deleted":
      return { Icon: StickyNote, color: "#C2564B", label: "Nota borrada" };
    case "contact_imported":
      return { Icon: UserPlus, color: "#9DA362", label: "Ficha actualizada desde un CSV" };
    case "phone_changed": {
      const n =
        m && typeof m.conversaciones_afectadas === "number" ? m.conversaciones_afectadas : null;
      return {
        Icon: Phone,
        color: "#E58A2F",
        label:
          n && n > 0
            ? `Teléfono corregido (${n} conversación${n === 1 ? "" : "es"} afectadas)`
            : "Teléfono corregido",
      };
    }
    case "contacts_merged": {
      const n = m && typeof m.conversaciones_movidas === "number" ? m.conversaciones_movidas : null;
      return {
        Icon: Merge,
        color: "#6C7BFF",
        label:
          n !== null
            ? `Fusionada otra ficha (${n} conversación${n === 1 ? "" : "es"} traídas)`
            : "Fusionada otra ficha",
      };
    }
    case "conversation_started": {
      const canal = metaStr(m, "canal");
      const meta = canal ? canalMeta(canal) : null;
      return {
        Icon: MessageSquare,
        color: meta?.color ?? "#9DA362",
        label: `Conversación iniciada por ${meta?.label ?? canal ?? "canal desconocido"}`,
      };
    }
    default:
      // Defensa: tipo desconocido se muestra con un genérico (sin romper).
      return { Icon: Activity, color: "#9DA362", label: ev.tipo };
  }
}

// Etiqueta amigable del estado de una conversación.
const CONV_STATUS_LABEL: Record<string, string> = {
  bot: "Bot",
  humano: "Humano",
  cerrada: "Cerrada",
};

function convStatusLabel(status: string): string {
  return CONV_STATUS_LABEL[status] ?? status;
}

// Clases del badge de estado de conversación (coherente con el inbox).
function convStatusClass(status: string): string {
  return status === "bot"
    ? "bg-brand/15 text-brand-ink"
    : status === "humano"
      ? "bg-state-warn/15 text-state-warn"
      : "bg-paper2 text-ink3";
}

// Nombre con la última palabra resaltada en color de acento (brand). Replica el
// gesto tipográfico del prototipo sin inventar datos: sólo formatea el nombre.
function NombreConAcento({ nombre }: { nombre: string }) {
  const parts = nombre.trim().split(/\s+/);
  if (parts.length <= 1) return <>{nombre}</>;
  const last = parts.pop();
  return (
    <>
      {parts.join(" ")} <span className="italic">{last}</span>
    </>
  );
}

// Tarjeta de métrica real ("stat-card") con número tabular. Solo presenta
// valores que ya existen; no calcula KPIs inventados.
function StatCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: typeof Activity;
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="card p-5">
      <div className="flex items-center gap-1.5 text-ink4 mb-2">
        <Icon className="w-3.5 h-3.5" />
        <span className="text-[10px] uppercase tracking-wider font-medium">{label}</span>
      </div>
      <div className="text-3xl md:text-4xl text-ink leading-none">{value}</div>
      {hint && <div className="numbers text-[11px] text-ink4 mt-1">{hint}</div>}
    </div>
  );
}

// Encabezado grande (serif) de sección: da jerarquía y "dónde mirar".
function SectionHeading({ children, count }: { children: ReactNode; count?: number }) {
  return (
    <div className="flex items-baseline gap-3 mb-4 px-0.5">
      <h2 className="font-display text-2xl md:text-3xl text-ink leading-tight">{children}</h2>
      {count != null && <span className="numbers text-lg text-ink4 leading-none">{count}</span>}
    </div>
  );
}

// Fila de conversación: marca de canal en color + preview del último mensaje
// (si existe) + estado + fechas. Enlaza al hilo en el inbox. `compact` la usa
// el bloque "Resumen"; sin él se usa en la lista completa.
function ConversationRow({
  conv,
  compact,
}: {
  conv: ContactConversationMini;
  compact?: boolean;
}) {
  const meta = canalMeta(conv.canal);
  const when = fechaCorta(conv.last_message_at) ?? fechaCorta(conv.started_at);
  return (
    <Link
      to={`/inbox?conversation=${conv.id}`}
      className={
        "flex items-start gap-3 hover:bg-paper2 transition-colors group " +
        (compact ? "rounded-coro-sm bg-paper2 p-3" : "px-5 py-3.5")
      }
    >
      {/* Marca de canal en color (icono sobre fondo tintado del canal). */}
      <span
        className="w-9 h-9 rounded-full flex items-center justify-center shrink-0 mt-0.5"
        style={{ background: meta.color + "22", color: meta.color }}
        title={meta.label}
        aria-hidden
      >
        <MessageSquare className="w-4 h-4" />
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-ink" style={{ color: meta.color }}>
            {meta.label}
          </span>
          <span className={"badge text-[10px] " + convStatusClass(conv.status)}>
            {convStatusLabel(conv.status)}
          </span>
          {conv.archived && <span className="badge bg-paper3 text-ink3 text-[10px]">Archivada</span>}
          {when && <span className="numbers text-[11px] text-ink4 ml-auto shrink-0">{when}</span>}
        </div>
        {/* Preview del último mensaje (dato ya existente; puede faltar). */}
        {conv.last_message_preview ? (
          <p className="text-sm text-ink3 mt-1 line-clamp-1 break-words">
            {conv.last_message_preview}
          </p>
        ) : (
          <p className="numbers text-[11px] text-ink4 mt-1">
            Iniciada {fechaCorta(conv.started_at)}
          </p>
        )}
      </div>
      <ArrowUpRight className="w-4 h-4 text-ink4 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 mt-1" />
    </Link>
  );
}

/**
 * Mensaje legible de un error del backend, incluidos los 422 de FastAPI.
 *
 * `errorDetail` (lib/errors) solo usa `detail` cuando es una CADENA. Un 422 de
 * validación llega como LISTA de errores por campo, así que al guardar la ficha
 * con un email mal escrito salía el mensaje genérico y no se decía QUÉ estaba
 * mal. Aquí se traduce también esa forma.
 */
function detalleError(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } } | null)?.response?.data
    ?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const partes = detail
      .map((d) => {
        const item = d as { loc?: unknown[]; msg?: string };
        // loc suele ser ["body", "email"]: nos quedamos con el campo.
        const campo = Array.isArray(item.loc)
          ? item.loc.filter((p) => typeof p === "string" && p !== "body").pop()
          : null;
        const msg = item.msg || "valor no válido";
        return campo ? `${CAMPO_LABEL[String(campo)] || campo}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (partes.length) return partes.join(" · ");
  }
  return fallback;
}

/** Nombre en castellano de los campos, para los mensajes de validación. */
const CAMPO_LABEL: Record<string, string> = {
  email: "Email",
  telefono: "Teléfono",
  nombre: "Nombre",
  web: "Web",
  nif: "NIF",
  direccion: "Dirección",
  empresa: "Empresa",
  cargo: "Cargo",
};

// La llave del contacto no siempre es un teléfono: cada canal guarda la suya
// (`web:`, `ig:`, `email:`, `voice:`, `wa:`). Con esos valores, un enlace
// `tel:` o de WhatsApp no lleva a ningún sitio, así que ni se pintan.
const CHANNEL_ID_PREFIXES = ["web:", "ig:", "email:", "voice:", "wa:"];

function esIdentificadorDeCanal(value: string | null | undefined): boolean {
  const v = (value || "").toLowerCase();
  return CHANNEL_ID_PREFIXES.some((p) => v.startsWith(p));
}

const CANAL_ID_LABEL: Record<string, string> = {
  "web:": "Visitante web",
  "ig:": "Instagram",
  "email:": "Email",
  "voice:": "Llamada",
  "wa:": "WhatsApp",
};

/** Cómo se lee un identificador de canal en pantalla (nunca en crudo). */
function etiquetaIdentificador(value: string | null | undefined): string {
  const v = value || "";
  const prefix = CHANNEL_ID_PREFIXES.find((p) => v.toLowerCase().startsWith(p));
  if (!prefix) return v;
  const rest = v.slice(prefix.length);
  if (prefix === "email:") return rest;
  return `${CANAL_ID_LABEL[prefix]} · ${rest.slice(0, 8)}…`;
}

export default function ContactDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const priv = usePrivacy((s) => s.enabled);
  const role = useAuth((s) => s.user?.role);
  const currentUserId = useAuth((s) => s.user?.id);
  const [showTemplate, setShowTemplate] = useState(false);

  const [contact, setContact] = useState<(Contact & { tags: Tag[] }) | null>(null);
  const [conversations, setConversations] = useState<ContactConversationMini[]>([]);
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [dirty, setDirty] = useState(false);

  // Form fields
  const [nombre, setNombre] = useState("");
  // Teléfono EDITABLE. Antes ni siquiera estaba en el esquema de actualización:
  // un número mal tecleado al crear la ficha solo se arreglaba borrándola y
  // rehaciéndola, y por el camino se perdían conversaciones, notas y actividad.
  // Ojo: es la LLAVE del contacto, así que el backend lo normaliza, comprueba
  // que no choque con otra ficha y lo registra en la actividad.
  const [telefono, setTelefono] = useState("");
  const [email, setEmail] = useState("");
  const [estado, setEstado] = useState("contacto");
  const [servicio, setServicio] = useState("");
  // Ficha CRM (opcionales)
  const [empresa, setEmpresa] = useState("");
  const [cargo, setCargo] = useState("");
  const [web, setWeb] = useState("");
  const [nif, setNif] = useState("");
  const [direccion, setDireccion] = useState("");

  // Notas multi-autor (lista + composer + borrado por nota).
  const [notes, setNotes] = useState<ContactNote[]>([]);
  const [newNote, setNewNote] = useState("");
  const [savingNote, setSavingNote] = useState(false);
  const [noteToDelete, setNoteToDelete] = useState<ContactNote | null>(null);
  const [deletingNote, setDeletingNote] = useState(false);
  // Error visible de la última acción (guardar/borrar/notas/etiquetas).
  const [actionError, setActionError] = useState<string | null>(null);
  // Error de la carga inicial de la ficha (distinto del de acción: aquí no hay
  // nada que pintar debajo, así que ocupa toda la pantalla).
  const [loadError, setLoadError] = useState<string | null>(null);

  // Timeline de actividad (auditoría real, más reciente primero).
  const [activity, setActivity] = useState<ContactActivity[]>([]);

  // Fusión de fichas duplicadas (solo admin).
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeSearch, setMergeSearch] = useState("");
  const [mergeResults, setMergeResults] = useState<Contact[]>([]);
  const [mergeSource, setMergeSource] = useState<Contact | null>(null);
  const [merging, setMerging] = useState(false);

  // ¿La llave de esta ficha es un identificador de canal y no un teléfono?
  const identificadorDeCanal = esIdentificadorDeCanal(contact?.telefono);

  // Baja permanente de difusiones (opt-out). No es la blocklist de Redis (esa
  // caduca): quien está aquí NO vuelve a entrar en ninguna campaña de envío
  // masivo. Los endpoints son admin-only, así que solo se consulta y se pinta
  // para admin.
  const [optOut, setOptOut] = useState<OptOutState | null>(null);
  const [optOutBusy, setOptOutBusy] = useState(false);
  const [confirmOptOut, setConfirmOptOut] = useState<"baja" | "alta" | null>(null);

  const canDeleteNote = (note: ContactNote) =>
    role === "admin" || (note.author != null && note.author.id === currentUserId);

  async function refresh() {
    if (!id) return;
    setLoading(true);
    setLoadError(null);
    try {
      const [c, convs, tags, contactNotes, contactActivity] = await Promise.all([
        getContact(id),
        getContactConversations(id),
        listTags(),
        getContactNotes(id),
        getContactActivity(id),
      ]);
      setContact(c);
      setConversations(convs);
      setAllTags(tags);
      setNotes(contactNotes);
      setActivity(contactActivity);
      setNombre(c.nombre || "");
      setTelefono(c.telefono || "");
      setEmail(c.email || "");
      setEstado(c.estado);
      setServicio(c.servicio_interes || "");
      setEmpresa(c.empresa || "");
      setCargo(c.cargo || "");
      setWeb(c.web || "");
      setNif(c.nif || "");
      setDireccion(c.direccion || "");
      setDirty(false);
      // El estado de baja se pide aparte: es admin-only y no debe tumbar la
      // ficha si falla (o si el backend aún no tiene el endpoint).
      if (role === "admin" && c.telefono) {
        void checkOutboundOptOut(c.telefono)
          .then(setOptOut)
          .catch(() => setOptOut(null));
      }
    } catch (e) {
      // Sin catch, un fallo aquí dejaba `contact` a null y la pantalla se
      // quedaba en "Cargando ficha…" para siempre: ni error, ni reintento, ni
      // forma de volver atrás.
      setLoadError(errorDetail(e, "No se pudo cargar la ficha del contacto."));
    } finally {
      setLoading(false);
    }
  }

  async function onSetOptOut(next: boolean) {
    if (!contact) return;
    setOptOutBusy(true);
    setActionError(null);
    try {
      if (next) await createOutboundOptOut(contact.telefono, "Baja registrada desde la ficha");
      else await deleteOutboundOptOut(contact.telefono);
      setOptOut(await checkOutboundOptOut(contact.telefono));
      setConfirmOptOut(null);
    } catch (e) {
      setActionError(
        errorDetail(
          e,
          next
            ? "No se pudo dar de baja de las difusiones."
            : "No se pudo reactivar las difusiones.",
        ),
      );
    } finally {
      setOptOutBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Scroll-spy: resalta la sección visible y permite saltar pulsando la sub-nav.
  const scrollRef = useRef<HTMLDivElement>(null);
  // La primera sección de SECTIONS, no "datos": al abrir la ficha se ve el
  // Resumen, pero la sub-navegación resaltaba "Datos" hasta que el observador
  // se ponía en marcha. Señalaba una sección distinta de la que estabas viendo.
  const [active, setActive] = useState(SECTIONS[0].id);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root || !contact) return;
    const obs = new IntersectionObserver(
      (entries) => {
        const vis = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (vis[0]) setActive(vis[0].target.id);
      },
      { root, rootMargin: "-8% 0px -70% 0px", threshold: 0 },
    );
    SECTIONS.forEach((s) => {
      const el = document.getElementById(s.id);
      if (el) obs.observe(el);
    });
    return () => obs.disconnect();
  }, [contact]);

  // ── No perder los cambios de la ficha ────────────────────────────────────
  //
  // Había un indicador de "sin guardar" que no bloqueaba nada: cambiabas cuatro
  // campos, pulsabas atrás y se iban sin una palabra. Dos frenos:
  //   - cerrar/recargar la pestaña → aviso del navegador;
  //   - navegar dentro del panel → confirmación antes de irse.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  /** Navegación que respeta los cambios sin guardar. */
  function navegarConAviso(to: string) {
    if (
      dirty &&
      !window.confirm("Tienes cambios sin guardar en la ficha. ¿Salir y perderlos?")
    ) {
      return;
    }
    navigate(to);
  }

  // Buscador de la ficha a fusionar (con espera, como el resto).
  useEffect(() => {
    if (!mergeOpen) return;
    const term = mergeSearch.trim();
    if (term.length < 2) {
      setMergeResults([]);
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      void listContacts({ search: term, page_size: 10, include_outside_crm: true })
        .then((p) => {
          if (cancelled) return;
          // Nunca ofrecer fusionar la ficha consigo misma.
          setMergeResults(p.items.filter((c) => c.id !== id));
        })
        .catch(() => {
          if (!cancelled) setMergeResults([]);
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [mergeSearch, mergeOpen, id]);

  function scrollToSection(secId: string) {
    document.getElementById(secId)?.scrollIntoView({ behavior: "smooth", block: "start" });
    setActive(secId);
  }

  // Antes estas mutaciones eran try/finally sin catch: si la API fallaba, el
  // spinner paraba y NO salía ningún aviso, así que el cambio parecía
  // guardado sin estarlo. Mismo patrón de banner que el Inbox.
  async function onSave() {
    if (!id) return;
    setSaving(true);
    setActionError(null);
    try {
      const telLimpio = telefono.trim();
      await updateContact(id, {
        nombre: nombre.trim() || null,
        // El teléfono solo viaja si de verdad ha cambiado, y nunca en las fichas
        // cuya llave es de canal (web:, ig:, email:, voice:): ahí no hay número
        // que corregir y el backend lo rechazaría.
        ...(!identificadorDeCanal && telLimpio && telLimpio !== (contact?.telefono || "")
          ? { telefono: telLimpio }
          : {}),
        email: email.trim() || null,
        estado,
        servicio_interes: servicio.trim() || null,
        empresa: empresa.trim() || null,
        cargo: cargo.trim() || null,
        web: web.trim() || null,
        nif: nif.trim() || null,
        direccion: direccion.trim() || null,
      } as Partial<Contact>);
      await refresh();
    } catch (e) {
      // `errorDetail` solo sirve el detalle cuando es una CADENA, y un 422 de
      // FastAPI llega como LISTA de errores por campo: con un email mal escrito
      // salía el mensaje genérico y no se decía qué estaba mal. `detalleError`
      // (abajo) traduce también esa lista.
      setActionError(detalleError(e, "No se pudieron guardar los cambios del contacto."));
    } finally {
      setSaving(false);
    }
  }

  // ── Fusionar dos fichas del mismo cliente ────────────────────────────────
  //
  // Como el emparejamiento es por cadena exacta, `+34600111222`,
  // `34600111222` y `600 111 222` conviven como tres personas distintas. Hasta
  // ahora no había forma de unirlas: lo único posible era borrar, y borrar se
  // lleva por delante conversaciones, notas y actividad.
  async function onMerge() {
    if (!id || !mergeSource) return;
    setMerging(true);
    setActionError(null);
    try {
      await mergeContacts(id, mergeSource.id);
      setMergeOpen(false);
      setMergeSource(null);
      setMergeSearch("");
      await refresh();
    } catch (e) {
      setActionError(detalleError(e, "No se pudieron fusionar los contactos."));
    } finally {
      setMerging(false);
    }
  }

  async function onDelete() {
    if (!id) return;
    setDeleting(true);
    setActionError(null);
    try {
      await deleteContact(id);
      navigate("/contacts");
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo eliminar el contacto."));
    } finally {
      setDeleting(false);
    }
  }

  async function onAddNote() {
    if (!id) return;
    const texto = newNote.trim();
    if (!texto) return;
    setSavingNote(true);
    setActionError(null);
    try {
      await addContactNote(id, texto);
      setNewNote("");
      const fresh = await getContactNotes(id);
      setNotes(fresh);
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo guardar la nota."));
    } finally {
      setSavingNote(false);
    }
  }

  async function onDeleteNote() {
    if (!id || !noteToDelete) return;
    setDeletingNote(true);
    setActionError(null);
    try {
      await deleteContactNote(id, noteToDelete.id);
      setNoteToDelete(null);
      const fresh = await getContactNotes(id);
      setNotes(fresh);
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo eliminar la nota."));
    } finally {
      setDeletingNote(false);
    }
  }

  async function onToggleTag(tag: Tag) {
    if (!contact) return;
    setActionError(null);
    try {
      const has = contact.tags.some((t) => t.id === tag.id);
      if (has) {
        await removeContactTag(contact.id, tag.id);
      } else {
        await addContactTag(contact.id, tag.id);
      }
      await refresh();
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo actualizar la etiqueta."));
    }
  }

  async function onCreateTag(nombre: string, color: string) {
    if (!contact) return;
    const tag = await createTag({ nombre, color });
    await addContactTag(contact.id, tag.id);
    await refresh();
  }

  async function onDeleteTag(tag: Tag) {
    await deleteTag(tag.id);
    await refresh();
  }

  const avatar = useMemo(() => {
    if (!contact) return null;
    const init = maskInitials(initialsOf(contact.nombre, contact.telefono), priv);
    return { init, color: colorFor(contact.telefono || contact.id) };
  }, [contact, priv]);

  // Canales distintos en los que el contacto tiene conversaciones (chips del hero).
  const channels = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const c of conversations) {
      if (c.canal && !seen.has(c.canal)) {
        seen.add(c.canal);
        out.push(c.canal);
      }
    }
    return out;
  }, [conversations]);

  // Resumen: 1–2 notas más recientes (las notas llegan ya ordenadas desc).
  const recentNotes = useMemo(() => notes.slice(0, 2), [notes]);

  // Resumen: conversación más reciente (las convs llegan ordenadas desc por
  // started_at). Se muestra como preview enlazada al inbox.
  const latestConversation = conversations[0] ?? null;

  // Si la carga falló no se puede seguir mostrando "Cargando ficha…": era una
  // pantalla muerta sin salida. Mismo banner de error que el resto de la app.
  if (loadError && !contact) {
    return (
      <div className="p-10 max-w-md mx-auto text-center">
        <div className="text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2">
          {loadError}
        </div>
        <div className="mt-4 flex items-center justify-center gap-2">
          <button type="button" className="btn-primary" onClick={() => void refresh()}>
            Reintentar
          </button>
          <button type="button" className="btn-ghost" onClick={() => navigate("/contacts")}>
            Volver a Contactos
          </button>
        </div>
      </div>
    );
  }

  if (loading || !contact) {
    return (
      <div className="p-10 text-center text-ink3 italic font-display">
        Cargando ficha…
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Breadcrumb sticky superior: «Contactos › {Nombre}» + acciones. */}
      <header className="px-4 md:px-6 py-3 border-b border-line bg-paper2/90 backdrop-blur flex items-center justify-between gap-3 shrink-0">
        <nav aria-label="Migas de pan" className="min-w-0 flex items-center gap-1.5 text-sm">
          {/* Volver a Contactos pasa por el aviso de cambios sin guardar: el
              indicador de "sin guardar" existía pero no frenaba nada, así que
              pulsar aquí con la ficha a medias se llevaba el trabajo por
              delante sin decir una palabra. */}
          <button
            type="button"
            onClick={() => navegarConAviso("/contacts")}
            className="text-ink3 hover:text-ink transition-colors shrink-0"
          >
            Contactos
          </button>
          <ChevronRight className="w-3.5 h-3.5 text-ink4 shrink-0" aria-hidden />
          <span className="font-display text-ink truncate text-base">
            {maskName(contact.nombre, priv) || <span className="text-ink3 italic">Sin nombre</span>}
          </span>
          {dirty && (
            <span
              className="ml-1 shrink-0 px-2 py-0.5 rounded-full text-[10px] font-medium bg-state-warn/15 text-state-warn"
              title="Hay cambios en la ficha que aún no se han guardado"
            >
              sin guardar
            </span>
          )}
        </nav>
        <div className="flex items-center gap-2 shrink-0">
          {/* RGPD Art. 15/20: descarga JSON con todos los datos del contacto.
              Solo admin — el backend exige `require_admin` en
              /contacts/{id}/export, así que al resto le daba siempre 403. */}
          {role === "admin" && (
            <button
              type="button"
              className="btn-ghost btn-sm"
              title="Exportar todos los datos del contacto (RGPD)"
              onClick={() => {
                setActionError(null);
                exportContactData(contact.id).catch((e) =>
                  setActionError(errorDetail(e, "No se pudieron exportar los datos."))
                );
              }}
            >
              <Download /> <span className="hidden sm:inline">Exportar</span>
            </button>
          )}
          {/* Borrado RGPD: solo admin (el backend responde 403 al resto).
              Sin esto, una operadora veía un botón que siempre da error. */}
          {role === "admin" && (
            <button type="button" className="btn-ghost btn-sm" onClick={() => setConfirmDelete(true)}>
              <Trash2 /> <span className="hidden sm:inline">Borrar</span>
            </button>
          )}
          <button type="button" className="btn-primary btn-sm" onClick={onSave} disabled={saving || !dirty}>
            <Save /> {saving ? "Guardando…" : "Guardar"}
          </button>
        </div>
      </header>

      {/* Error visible de la última acción — mismo patrón que el Inbox. */}
      {actionError && (
        <div className="mx-4 md:mx-6 mt-3 text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2 shrink-0">
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

      {/* Si el fallo llega en un refresco posterior sí hay ficha en pantalla,
          pero los datos que se ven son los viejos: hay que avisar igual. */}
      {loadError && (
        <div className="mx-4 md:mx-6 mt-3 text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2 shrink-0">
          <span className="flex-1">{loadError}</span>
          <button
            type="button"
            onClick={() => void refresh()}
            className="font-semibold hover:underline shrink-0"
          >
            Reintentar
          </button>
        </div>
      )}

      <div ref={scrollRef} className="flex-1 overflow-auto">
        {/* Sub-navegación con scroll-spy (sticky al hacer scroll) */}
        <div className="sticky top-0 z-10 bg-paper/85 backdrop-blur border-b border-line px-4 md:px-6">
          <nav className="flex gap-0.5 max-w-7xl mx-auto overflow-x-auto">
            {SECTIONS.map((s) => {
              const isActive = active === s.id;
              // Contador solo en pestañas cuya fuente es una lista contable.
              const count =
                s.id === "conversaciones" ? conversations.length :
                s.id === "etiquetas" ? contact.tags.length :
                s.id === "notas" ? notes.length :
                s.id === "actividad" ? activity.length :
                null;
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => scrollToSection(s.id)}
                  className={
                    "px-3 py-3 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors inline-flex items-center gap-1.5 " +
                    (isActive
                      ? "border-brand text-ink font-semibold"
                      : "border-transparent text-ink3 hover:text-ink hover:border-line")
                  }
                >
                  {s.label}
                  {count !== null && (
                    <span
                      className={
                        "numbers min-w-[1.25rem] px-1 py-0.5 rounded-full text-[10px] leading-none text-center " +
                        (isActive ? "bg-brand/15 text-brand-ink" : "bg-paper2 text-ink4")
                      }
                    >
                      {count}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>
        </div>

        <div className="p-4 md:p-6">
          <div className="max-w-7xl mx-auto space-y-6">
            {/* HERO */}
            <div className="card p-5 md:p-6">
              <div className="flex items-start gap-4 md:gap-5">
                {avatar && (
                  <div
                    className="w-20 h-20 md:w-24 md:h-24 rounded-2xl flex items-center justify-center text-2xl md:text-3xl font-display shrink-0 ring-1 ring-line"
                    style={{ background: avatar.color, color: "var(--brand-on)" }}
                    aria-hidden
                  >
                    {avatar.init}
                  </div>
                )}
                <div className="flex-1 min-w-0">
                  <h2 className="font-display text-3xl md:text-5xl leading-[1.05] text-ink break-words">
                    {contact.nombre
                      ? <NombreConAcento nombre={maskName(contact.nombre, priv)!} />
                      : <span className="text-ink3 italic">Sin nombre</span>}
                  </h2>
                  {/* La llave de la ficha. Si es un identificador de canal
                      (web:, ig:, email:, voice:) NO es un teléfono: se enseña
                      etiquetado y SIN enlace `tel:`, que con ese valor no
                      llamaría a nadie. */}
                  {identificadorDeCanal ? (
                    <span
                      className="text-sm text-ink3 inline-flex items-center gap-1.5 mt-0.5"
                      title="Este contacto se identifica por su canal, no por un teléfono"
                    >
                      <Compass className="w-3.5 h-3.5 text-ink4" />
                      {etiquetaIdentificador(contact.telefono)}
                    </span>
                  ) : (
                    <a
                      href={`tel:${contact.telefono}`}
                      className="numbers text-sm text-ink3 hover:text-ink transition-colors inline-flex items-center gap-1.5 mt-0.5"
                    >
                      <Phone className="w-3.5 h-3.5 text-ink4" /> {maskPhone(contact.telefono, priv)}
                    </a>
                  )}
                  <div className="flex items-center gap-x-2 gap-y-1 mt-2.5 flex-wrap text-xs text-ink3">
                    <span className={"px-2 py-0.5 rounded-full font-medium " + (ESTADO_COLOR[estado] ?? "bg-paper2 text-ink3")}>
                      {ESTADO_LABEL[estado] || estado}
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <Compass className="w-3.5 h-3.5 text-ink4" />
                      Origen <span className="numbers text-ink2">{contact.origen}</span>
                    </span>
                    {contact.ultimo_mensaje_at && (
                      <span className="inline-flex items-center gap-1">
                        <Clock className="w-3.5 h-3.5 text-ink4" />
                        Último <span className="numbers text-ink2">{tiempoRelativo(contact.ultimo_mensaje_at)}</span>
                      </span>
                    )}
                    {/* Baja permanente de difusiones: se ve de un vistazo, sin
                        tener que abrir Envío masivo para descubrirlo. */}
                    {optOut?.opted_out && (
                      <span
                        className="px-2 py-0.5 rounded-full font-medium bg-state-bad/15 text-state-bad inline-flex items-center gap-1"
                        title={
                          "Dado de baja de las difusiones" +
                          (optOut.reason ? ` — ${optOut.reason}` : "") +
                          (optOut.created_at
                            ? ` (${new Date(optOut.created_at).toLocaleDateString("es-ES")})`
                            : "")
                        }
                      >
                        <BellOff className="w-3.5 h-3.5" /> Baja de difusiones
                      </span>
                    )}
                  </div>

                  {/* Chips de canal: canales distintos con conversaciones (#2) */}
                  {channels.length > 0 && (
                    <div className="flex items-center gap-1.5 mt-2.5 flex-wrap">
                      {channels.map((canal) => {
                        const meta = canalMeta(canal);
                        return (
                          <span
                            key={canal}
                            className="badge text-[10px] border-transparent inline-flex items-center gap-1"
                            style={{ background: meta.color + "22", color: meta.color }}
                            title={`Conversaciones por ${meta.label}`}
                          >
                            <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: meta.color }} />
                            {meta.label}
                          </span>
                        );
                      })}
                    </div>
                  )}

                  {/* Etiquetas inline + "+ etiqueta" → reutiliza el mismo
                      TagPicker de la sección Etiquetas (#4). Misma fuente de
                      verdad: contact.tags + allTags + onToggle/onCreate/onDelete. */}
                  <div className="mt-3">
                    <TagPicker
                      assigned={contact.tags}
                      all={allTags}
                      onToggle={onToggleTag}
                      onCreate={onCreateTag}
                      onDelete={onDeleteTag}
                    />
                  </div>
                </div>
              </div>
              <div className="flex flex-wrap gap-2 mt-5 pt-4 border-t border-line">
                {latestConversation && (
                  <Link to={`/inbox?conversation=${latestConversation.id}`} className="btn-primary btn-sm">
                    <MessagesSquare className="w-3.5 h-3.5" /> Abrir chat
                  </Link>
                )}
                {/* "Llamar" y "WhatsApp" solo cuando hay un teléfono de
                    verdad. En un contacto de web, email, Instagram o voz se
                    generaban con la llave del canal, así que el enlace `tel:` y
                    el de WhatsApp llevaban a un número inventado. */}
                {!identificadorDeCanal && (
                  <a href={`tel:${contact.telefono}`} className="btn-ghost btn-sm">
                    <Phone className="w-3.5 h-3.5" /> Llamar
                  </a>
                )}
                {contact.email && (
                  <a href={`mailto:${contact.email}`} className="btn-ghost btn-sm">
                    <Mail className="w-3.5 h-3.5" /> Email
                  </a>
                )}
                {!identificadorDeCanal && (
                  <a
                    href={`https://wa.me/${contact.telefono.replace(/[^0-9]/g, "")}`}
                    target="_blank"
                    rel="noreferrer"
                    className="btn-ghost btn-sm"
                  >
                    <MessageSquare className="w-3.5 h-3.5" /> WhatsApp
                  </a>
                )}
                {/* Fusionar fichas duplicadas del mismo cliente. Solo admin. */}
                {role === "admin" && (
                  <button
                    type="button"
                    onClick={() => setMergeOpen(true)}
                    className="btn-ghost btn-sm"
                    title="Unir otra ficha de este mismo cliente con esta: se traen sus conversaciones, notas, etiquetas y actividad"
                  >
                    <Merge className="w-3.5 h-3.5" /> Fusionar
                  </button>
                )}
                {role === "admin" && (
                  <button type="button" onClick={() => setShowTemplate(true)} className="btn-ghost btn-sm">
                    <Send className="w-3.5 h-3.5" /> Plantilla
                  </button>
                )}
                {/* Baja de difusiones (opt-out permanente). Solo admin: los
                    endpoints /admin/outbound/optouts exigen rol admin. */}
                {role === "admin" && optOut && (
                  <button
                    type="button"
                    onClick={() => setConfirmOptOut(optOut.opted_out ? "alta" : "baja")}
                    disabled={optOutBusy}
                    className="btn-ghost btn-sm"
                    title={
                      optOut.opted_out
                        ? "Volver a incluirlo en las difusiones (solo si lo pide esa persona)"
                        : "No volver a incluirlo en ninguna campaña de envío masivo"
                    }
                  >
                    {optOut.opted_out ? (
                      <>
                        <BellRing className="w-3.5 h-3.5" /> Reactivar difusiones
                      </>
                    ) : (
                      <>
                        <BellOff className="w-3.5 h-3.5" /> Dar de baja de difusiones
                      </>
                    )}
                  </button>
                )}
              </div>
            </div>

            {/* STATS — métricas REALES derivadas de los datos ya cargados (#3).
                Conversaciones (nº) · Canales distintos · Cliente desde
                (created_at) · Último contacto (ultimo_mensaje_at). */}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <StatCard
                icon={MessagesSquare}
                label="Conversaciones"
                value={<span className="numbers">{conversations.length}</span>}
                hint={conversations.length === 1 ? "hilo" : "hilos"}
              />
              <StatCard
                icon={Radio}
                label="Canales"
                value={<span className="numbers">{channels.length}</span>}
                hint={channels.length === 1 ? "distinto" : "distintos"}
              />
              <StatCard
                icon={CalendarClock}
                label="Cliente desde"
                value={
                  <span className="numbers text-lg">
                    {fechaLarga(contact.created_at) ?? "—"}
                  </span>
                }
              />
              <StatCard
                icon={Clock}
                label="Último contacto"
                value={
                  <span className="numbers text-lg">
                    {tiempoRelativo(contact.ultimo_mensaje_at) ?? "—"}
                  </span>
                }
                hint={fechaCorta(contact.ultimo_mensaje_at) ?? undefined}
              />
            </div>

            {/* RESUMEN — dos columnas: notas/conversación reciente (izq) +
                datos personales (der). Solo datos que ya existen (#4). */}
            <section id="resumen" className="scroll-mt-16">
              <SectionHeading>Resumen</SectionHeading>
              <div className="grid grid-cols-1 lg:grid-cols-[1fr_minmax(0,22rem)] gap-5 items-start">
              {/* Columna izquierda: actividad reciente legible */}
              <div className="space-y-5">
                {/* Destacado: última conversación — el foco visual de la ficha. */}
                {latestConversation ? (
                  <div className="rounded-coro border border-brand/40 bg-brand/10 p-5 md:p-6">
                    <div className="flex items-center gap-2 mb-2.5">
                      <span className="eyebrow text-brand-ink">Última conversación</span>
                      {(() => {
                        const meta = canalMeta(latestConversation.canal);
                        return (
                          <span
                            className="badge text-[10px] border-transparent inline-flex items-center gap-1"
                            style={{ background: meta.color + "22", color: meta.color }}
                          >
                            <span className="w-1.5 h-1.5 rounded-full" style={{ background: meta.color }} />
                            {meta.label}
                          </span>
                        );
                      })()}
                      <span className="numbers text-[11px] text-ink4 ml-auto">
                        {tiempoRelativo(latestConversation.last_message_at) ?? fechaCorta(latestConversation.started_at)}
                      </span>
                    </div>
                    <p className="font-display text-lg md:text-xl text-ink leading-snug line-clamp-3 break-words">
                      {latestConversation.last_message_preview?.trim()
                        || `Conversación por ${canalMeta(latestConversation.canal).label}`}
                    </p>
                    <div className="flex flex-wrap gap-2 mt-4">
                      <Link to={`/inbox?conversation=${latestConversation.id}`} className="btn-primary btn-sm">
                        <MessagesSquare className="w-3.5 h-3.5" /> Abrir chat
                      </Link>
                      {conversations.length > 1 && (
                        <button
                          type="button"
                          onClick={() => scrollToSection("conversaciones")}
                          className="btn-ghost btn-sm"
                        >
                          Ver las {conversations.length}
                        </button>
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="rounded-coro border border-dashed border-line2 p-6 text-center">
                    <MessagesSquare className="w-6 h-6 text-ink4 mx-auto mb-2" />
                    <p className="text-sm text-ink4 italic font-display">
                      Aún no hay conversaciones con este contacto.
                    </p>
                  </div>
                )}

                {/* Notas recientes (preview) */}
                <div className="card p-5">
                  <div className="flex items-center gap-2 mb-3">
                    <StickyNote className="w-4 h-4 text-ink4" />
                    <div className="eyebrow flex-1">Notas recientes</div>
                    <button
                      type="button"
                      onClick={() => scrollToSection("notas")}
                      className="text-[11px] text-brand-ink hover:underline inline-flex items-center gap-0.5"
                    >
                      Ver todas <ChevronRight className="w-3 h-3" />
                    </button>
                  </div>
                  {recentNotes.length === 0 ? (
                    <p className="text-sm text-ink4 italic font-display">
                      Aún no hay notas sobre este contacto.
                    </p>
                  ) : (
                    <ul className="space-y-2.5">
                      {recentNotes.map((note) => {
                        const authorName = note.author?.nombre || roleLabel(note.author?.role) || "Autor desconocido";
                        return (
                          <li key={note.id} className="rounded-coro-sm bg-paper2 px-3 py-2.5">
                            <div className="flex items-center gap-2 text-[11px] text-ink4 mb-1">
                              <span className="font-medium text-ink3">{authorName}</span>
                              <span>· {fechaCorta(note.created_at)}</span>
                            </div>
                            <p className="text-sm text-ink2 line-clamp-3 whitespace-pre-wrap break-words">
                              {note.texto}
                            </p>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              </div>

              {/* Resumen escaneable de solo lectura (#4). Solo filas con valor. */}
              <aside className="card p-5">
                <div className="eyebrow mb-3">Datos personales</div>
                {(() => {
                  const rows = [
                    // Los contactos de web, email, Instagram y voz no tienen
                    // teléfono: llevan la llave de su canal, y se etiqueta como
                    // tal en vez de pintar un identificador crudo bajo el
                    // rótulo "Teléfono".
                    identificadorDeCanal
                      ? { key: "tel", icon: Compass, label: "Identificador", value: etiquetaIdentificador(contact.telefono), mono: true }
                      : { key: "tel", icon: Phone, label: "Teléfono", value: maskPhone(contact.telefono, priv), mono: true },
                    { key: "email", icon: Mail, label: "Email", value: maskEmail(contact.email, priv), mono: true },
                    { key: "estado", icon: Activity, label: "Estado", value: ESTADO_LABEL[contact.estado] || contact.estado },
                    { key: "origen", icon: Compass, label: "Origen", value: contact.origen, mono: true },
                    { key: "interes", icon: Sparkles, label: "Interés", value: contact.servicio_interes },
                    { key: "empresa", icon: Building2, label: "Empresa", value: contact.empresa },
                    { key: "cargo", icon: BriefcaseBusiness, label: "Cargo", value: contact.cargo },
                    { key: "web", icon: Globe, label: "Web", value: contact.web, mono: true },
                    { key: "nif", icon: IdCard, label: "NIF / CIF", value: contact.nif, mono: true },
                    { key: "direccion", icon: MapPin, label: "Dirección", value: contact.direccion },
                    { key: "desde", icon: CalendarClock, label: "Contacto desde", value: fechaLarga(contact.created_at) },
                  ].filter((r) => r.value != null && String(r.value).trim() !== "");
                  if (rows.length === 0) {
                    return (
                      <div className="text-sm text-ink4 italic font-display">
                        Sin datos adicionales todavía.
                      </div>
                    );
                  }
                  return (
                    <dl className="divide-y divide-line2 -my-2">
                      {rows.map((r) => {
                        const Icon = r.icon;
                        return (
                          <div key={r.key} className="flex items-start gap-2.5 py-2.5">
                            <Icon className="w-4 h-4 text-ink4 mt-0.5 shrink-0" />
                            <div className="min-w-0 flex-1">
                              <dt className="text-[10px] uppercase tracking-wider text-ink4 leading-tight">{r.label}</dt>
                              <dd className={"text-sm text-ink2 break-words mt-0.5 " + (r.mono ? "numbers" : "")}>{r.value}</dd>
                            </div>
                          </div>
                        );
                      })}
                    </dl>
                  );
                })()}
              </aside>
              </div>
            </section>

            {/* DATOS — formulario editable (fuente de verdad de la edición). */}
            <section id="datos" className="scroll-mt-16 card p-5 md:p-6">
              <div className="mb-5">
                <h3 className="font-display text-xl md:text-2xl text-ink">Datos del contacto</h3>
                <p className="text-xs text-ink4 mt-0.5">Edita los campos y pulsa «Guardar» arriba.</p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="label">Nombre</label>
                  <input className="input" value={nombre} onChange={(e) => { setNombre(e.target.value); setDirty(true); }} placeholder="Sin nombre" />
                </div>
                {/* Teléfono editable. Es la LLAVE del contacto: cambiarlo tiene
                    consecuencias, así que se avisa. En las fichas cuya llave es
                    de canal no se puede tocar (no hay número que corregir). */}
                <div>
                  <label className="label">Teléfono</label>
                  {identificadorDeCanal ? (
                    <>
                      <input className="input" value={etiquetaIdentificador(contact.telefono)} disabled readOnly />
                      <p className="text-[11px] text-ink4 mt-1">
                        Este contacto se identifica por su canal, no por un teléfono.
                        Cambiarlo lo dejaría sin sus conversaciones.
                      </p>
                    </>
                  ) : (
                    <>
                      <input
                        className="input numbers"
                        value={telefono}
                        onChange={(e) => { setTelefono(e.target.value); setDirty(true); }}
                        placeholder="+34600000000"
                      />
                      <p className="text-[11px] text-ink4 mt-1">
                        {telefono.trim() !== (contact.telefono || "")
                          ? "Es el identificador del contacto: al cambiarlo, los mensajes nuevos de ese cliente llegarán a esta ficha por el número nuevo. Queda registrado en la actividad."
                          : "Formato internacional. Los espacios y guiones se quitan solos."}
                      </p>
                    </>
                  )}
                </div>
                <div>
                  <label className="label">Email</label>
                  <input className="input" type="email" value={email} onChange={(e) => { setEmail(e.target.value); setDirty(true); }} placeholder="email@ejemplo.com" />
                </div>
                <div>
                  <label className="label">Estado</label>
                  <select className="input" value={estado} onChange={(e) => { setEstado(e.target.value); setDirty(true); }}>
                    {ESTADO_OPTIONS.map((o) => (<option key={o.value} value={o.value}>{o.label}</option>))}
                  </select>
                </div>
                <div>
                  <label className="label">Servicio de interés</label>
                  <input className="input" value={servicio} onChange={(e) => { setServicio(e.target.value); setDirty(true); }} placeholder="Plan premium, Soporte…" />
                </div>
                <div>
                  <label className="label">Empresa</label>
                  <input className="input" value={empresa} onChange={(e) => { setEmpresa(e.target.value); setDirty(true); }} placeholder="Nombre de la empresa" />
                </div>
                <div>
                  <label className="label">Cargo</label>
                  <input className="input" value={cargo} onChange={(e) => { setCargo(e.target.value); setDirty(true); }} placeholder="CEO, Marketing…" />
                </div>
                <div>
                  <label className="label">Web</label>
                  <input className="input" type="url" value={web} onChange={(e) => { setWeb(e.target.value); setDirty(true); }} placeholder="https://ejemplo.com" />
                </div>
                <div>
                  <label className="label">NIF / CIF</label>
                  <input className="input" value={nif} onChange={(e) => { setNif(e.target.value); setDirty(true); }} placeholder="B12345678" />
                </div>
                <div className="sm:col-span-2">
                  <label className="label">Dirección</label>
                  <input className="input" value={direccion} onChange={(e) => { setDireccion(e.target.value); setDirty(true); }} placeholder="Calle, número, CP, ciudad" />
                </div>
              </div>
            </section>

            {/* ETIQUETAS */}
            <section id="etiquetas" className="scroll-mt-16 card p-5">
              <div className="flex items-center gap-2.5 mb-4">
                <TagIcon className="w-5 h-5 text-ink3" />
                <h3 className="font-display text-xl md:text-2xl text-ink flex-1">Etiquetas</h3>
                {contact.tags.length > 0 && (
                  <span className="numbers text-ink4">{contact.tags.length}</span>
                )}
              </div>
              <TagPicker
                assigned={contact.tags}
                all={allTags}
                onToggle={onToggleTag}
                onCreate={onCreateTag}
                onDelete={onDeleteTag}
              />
            </section>

            {/* CONVERSACIONES — cada fila con marca de canal en color + preview
                del último mensaje (cuando existe) + estado/fechas (#5). */}
            <section id="conversaciones" className="scroll-mt-16 card overflow-hidden">
              <div className="px-5 py-3.5 border-b border-line bg-paper2 flex items-center justify-between">
                <h3 className="font-display text-xl md:text-2xl text-ink">Conversaciones</h3>
                <span className="numbers text-[11px] text-ink3 bg-paper px-2 py-0.5 rounded-full border border-line">{conversations.length}</span>
              </div>
              {conversations.length === 0 ? (
                <div className="px-5 py-10 text-center text-ink3 italic font-display text-sm">
                  Aún no hay conversaciones con este contacto.
                </div>
              ) : (
                <ul className="divide-y divide-line2">
                  {conversations.map((c) => (
                    <li key={c.id}>
                      <ConversationRow conv={c} />
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* NOTAS (multi-autor) */}
            <section id="notas" className="scroll-mt-16 card p-5">
              <div className="flex items-center gap-2.5 mb-4">
                <StickyNote className="w-5 h-5 text-ink3" />
                <h3 className="font-display text-xl md:text-2xl text-ink flex-1">Notas internas</h3>
                <span className="text-[11px] text-ink4 hidden sm:inline">Cifradas · privadas del equipo</span>
              </div>

              {/* Composer: nueva nota */}
              <div className="mb-5">
                <label className="label">+ Nueva nota</label>
                <textarea
                  className="input min-h-[90px]"
                  value={newNote}
                  onChange={(e) => setNewNote(e.target.value)}
                  placeholder="Escribe una nota sobre este contacto. No se envía al cliente."
                />
                <div className="flex justify-end mt-2">
                  <button
                    type="button"
                    className="btn-primary btn-sm"
                    onClick={onAddNote}
                    disabled={savingNote || !newNote.trim()}
                  >
                    <Save className="w-3.5 h-3.5" /> {savingNote ? "Guardando…" : "Guardar nota"}
                  </button>
                </div>
              </div>

              {/* Lista de notas */}
              {notes.length === 0 ? (
                <div className="text-center text-ink3 italic font-display text-sm py-6 border-t border-line">
                  Aún no hay notas.
                </div>
              ) : (
                <ul className="space-y-3 border-t border-line pt-4">
                  {notes.map((note) => {
                    const authorName = note.author?.nombre || note.author?.role
                      ? (note.author?.nombre || roleLabel(note.author?.role))
                      : "Autor desconocido";
                    const seed = note.author?.id || note.id;
                    const init = (note.author?.nombre || "?").trim().charAt(0).toUpperCase() || "?";
                    return (
                      <li key={note.id} className="flex items-start gap-3 rounded-coro-sm bg-paper2 p-3">
                        <div
                          className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold shrink-0"
                          style={{ background: colorFor(seed), color: "var(--brand-on)" }}
                          aria-hidden
                        >
                          {init}
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap text-xs">
                            <span className="font-semibold text-ink">{authorName}</span>
                            {note.author?.role && (
                              <span className="badge bg-paper3 text-ink3 text-[10px]">
                                {roleLabel(note.author.role)}
                              </span>
                            )}
                            <span className="text-ink4">· {fechaHora(note.created_at)}</span>
                          </div>
                          <div className="text-sm text-ink2 mt-1 whitespace-pre-wrap break-words">
                            {note.texto}
                          </div>
                        </div>
                        {canDeleteNote(note) && (
                          <button
                            type="button"
                            className="text-ink4 hover:text-state-bad transition-colors shrink-0 p-1"
                            onClick={() => setNoteToDelete(note)}
                            title="Borrar nota"
                            aria-label="Borrar nota"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>

            {/* ACTIVIDAD (timeline de auditoría real) */}
            <section id="actividad" className="scroll-mt-16 card p-5">
              <div className="flex items-center gap-2.5 mb-4">
                <History className="w-5 h-5 text-ink3" />
                <h3 className="font-display text-xl md:text-2xl text-ink flex-1">Actividad</h3>
                <span className="text-[11px] text-ink4 hidden sm:inline">Auditoría del equipo y del sistema</span>
              </div>

              {activity.length === 0 ? (
                <div className="text-center text-ink3 italic font-display text-sm py-6 border-t border-line">
                  Sin actividad todavía.
                </div>
              ) : (
                <ol className="relative border-t border-line pt-4 space-y-4">
                  {activity.map((ev) => {
                    const { Icon, color, label } = activityPresentation(ev);
                    const actorName = ev.actor
                      ? ev.actor.nombre || roleLabel(ev.actor.role) || "Usuario"
                      : "Sistema";
                    return (
                      <li key={ev.id} className="flex items-start gap-3">
                        {/* Punto de color + icono según el tipo de evento */}
                        <div
                          className="w-7 h-7 rounded-full flex items-center justify-center shrink-0 mt-0.5"
                          style={{ background: color + "22", color }}
                          aria-hidden
                        >
                          <Icon className="w-3.5 h-3.5" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="text-sm text-ink2 break-words">{label}</div>
                          <div className="flex items-center gap-1.5 flex-wrap text-[11px] text-ink4 mt-0.5">
                            <span className="font-medium text-ink3">{actorName}</span>
                            {ev.actor?.role && (
                              <span className="badge bg-paper3 text-ink3 text-[10px]">
                                {roleLabel(ev.actor.role)}
                              </span>
                            )}
                            <span>· {fechaHora(ev.created_at)}</span>
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              )}
            </section>
          </div>
        </div>
      </div>

      <ConfirmModal
        open={confirmDelete}
        tone="danger"
        title="Borrar contacto"
        description={
          <>
            Vas a borrar a <span className="font-semibold text-ink2">{maskName(contact.nombre, priv) || maskPhone(contact.telefono, priv)}</span> y todas sus conversaciones asociadas. Esta acción no se puede deshacer.
          </>
        }
        confirmLabel="Sí, borrar"
        busy={deleting}
        onConfirm={onDelete}
        onCancel={() => setConfirmDelete(false)}
      />

      <ConfirmModal
        open={noteToDelete !== null}
        tone="danger"
        title="Borrar nota"
        description="Vas a borrar esta nota. Esta acción no se puede deshacer."
        confirmLabel="Sí, borrar"
        busy={deletingNote}
        onConfirm={onDeleteNote}
        onCancel={() => setNoteToDelete(null)}
      />

      <ConfirmModal
        open={confirmOptOut === "baja"}
        tone="danger"
        title="Dar de baja de las difusiones"
        description={
          <>
            <span className="font-semibold text-ink2">
              {maskName(contact.nombre, priv) || maskPhone(contact.telefono, priv)}
            </span>{" "}
            dejará de entrar en cualquier campaña de envío masivo. Es una baja
            permanente, no una pausa: solo se quita si esa persona lo pide.
          </>
        }
        confirmLabel="Sí, dar de baja"
        busy={optOutBusy}
        onConfirm={() => void onSetOptOut(true)}
        onCancel={() => setConfirmOptOut(null)}
      />

      <ConfirmModal
        open={confirmOptOut === "alta"}
        title="Reactivar las difusiones"
        description="Hazlo solo si esta persona ha pedido volver a recibir difusiones. Volverá a entrar en las campañas de envío masivo."
        confirmLabel="Sí, reactivar"
        busy={optOutBusy}
        onConfirm={() => void onSetOptOut(false)}
        onCancel={() => setConfirmOptOut(null)}
      />

      {showTemplate && (
        <TemplateSendModal contact={contact} onClose={() => setShowTemplate(false)} />
      )}

      {/* Fusionar fichas duplicadas del mismo cliente. */}
      {mergeOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-ink/30"
          onClick={() => !merging && setMergeOpen(false)}
        >
          <div
            className="card p-5 w-full max-w-lg max-h-[90vh] overflow-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-1">
              <h3 className="font-display text-ink" style={{ fontSize: 20 }}>
                Fusionar contactos
              </h3>
              <button
                type="button"
                className="btn-icon"
                onClick={() => setMergeOpen(false)}
                disabled={merging}
                aria-label="Cerrar"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <p className="text-[12px] text-ink3 mb-4">
              Busca la otra ficha de esta misma persona. Sus conversaciones, notas,
              etiquetas y actividad pasan a <b className="text-ink2">esta</b> ficha, y la
              otra desaparece. Lo que esta ficha ya tenga relleno no se toca; solo se
              completan los huecos. No se puede deshacer.
            </p>

            <label className="label">Buscar por nombre, teléfono o email</label>
            <input
              className="input"
              value={mergeSearch}
              onChange={(e) => {
                setMergeSearch(e.target.value);
                setMergeSource(null);
              }}
              placeholder="600 111 222"
              autoFocus
              disabled={merging}
            />

            <div className="mt-3 space-y-1 max-h-64 overflow-auto">
              {mergeSearch.trim().length >= 2 && mergeResults.length === 0 && (
                <div className="text-[12px] text-ink3 italic py-3 text-center">
                  Sin resultados para “{mergeSearch.trim()}”.
                </div>
              )}
              {mergeResults.map((c) => {
                const elegido = mergeSource?.id === c.id;
                return (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => setMergeSource(c)}
                    disabled={merging}
                    className={
                      "w-full text-left px-3 py-2 rounded-coro-sm border transition-colors " +
                      (elegido
                        ? "border-brand bg-brand/10"
                        : "border-line hover:bg-paper2")
                    }
                  >
                    <div className="text-sm text-ink truncate">
                      {c.nombre || <span className="text-ink3 italic">Sin nombre</span>}
                    </div>
                    <div className="text-[11px] text-ink3 numbers truncate">
                      {esIdentificadorDeCanal(c.telefono)
                        ? etiquetaIdentificador(c.telefono)
                        : c.telefono}
                      {c.email ? ` · ${c.email}` : ""}
                    </div>
                  </button>
                );
              })}
            </div>

            {mergeSource && (
              <div className="mt-3 rounded-coro-sm bg-state-warn/10 border border-state-warn/30 px-3 py-2 text-[12px] text-ink2">
                Se fusionará{" "}
                <b>{mergeSource.nombre || mergeSource.telefono}</b> dentro de{" "}
                <b>{contact.nombre || contact.telefono}</b>. La primera ficha desaparece.
              </div>
            )}

            <div className="flex justify-end gap-2 mt-4">
              <button
                type="button"
                className="btn-ghost"
                onClick={() => setMergeOpen(false)}
                disabled={merging}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={() => void onMerge()}
                disabled={!mergeSource || merging}
              >
                {merging ? "Fusionando…" : "Fusionar"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Campos del contacto enlazables (debe coincidir con el set permitido del
// backend). Lee SOLO estos campos de la ficha para autorrellenar; nunca
// atributos arbitrarios del contacto.
const ALLOWED_AUTOFILL_FIELDS = new Set(
  CONTACT_FIELD_OPTIONS.map((o) => o.value).filter((v) => v),
);

// Lee un campo permitido del contacto y lo serializa a string visible.
// (Los enums ya llegan como su valor string en el tipo Contact del front.)
function contactFieldValue(
  contact: Contact & { tags: Tag[] },
  field: string,
): string {
  if (!ALLOWED_AUTOFILL_FIELDS.has(field)) return "";
  const raw = (contact as unknown as Record<string, unknown>)[field];
  if (raw == null) return "";
  return String(raw).trim();
}

function TemplateSendModal({
  contact,
  onClose,
}: {
  contact: Contact & { tags: Tag[] };
  onClose: () => void;
}) {
  const phone = contact.telefono;
  const [templates, setTemplates] = useState<WhatsappTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState<WhatsappTemplate | null>(null);
  // Array ORDENADO: vars[idx-1] -> {{idx}}. Es lo que se envía (sin cambios).
  const [vars, setVars] = useState<string[]>([]);
  // Config de etiquetas amigables + enlaces (para labels y autorrelleno).
  const [varConfig, setVarConfig] = useState<TemplateVar[]>([]);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    listWhatsappTemplates()
      .then(setTemplates)
      .catch(() => setTemplates([]))
      .finally(() => setLoading(false));
  }, []);

  async function pick(name: string) {
    const t = templates.find((x) => x.name === name) || null;
    setSel(t);
    setResult(null);
    if (!t || t.variables < 1) {
      setVars(t ? Array(t.variables).fill("") : []);
      setVarConfig([]);
      return;
    }
    // Inicializa el array ordenado vacío y carga la config; luego autorrellena
    // desde el contacto ya cargado (cliente, sin llamar a /resolve).
    const base = Array(t.variables).fill("");
    setVars(base);
    setVarConfig([]);
    try {
      const cfg = await getTemplateVars(t.name, t.language);
      setVarConfig(cfg);
      const next = [...base];
      for (const v of cfg) {
        if (v.idx < 1 || v.idx > t.variables) continue;
        if (v.contact_field && ALLOWED_AUTOFILL_FIELDS.has(v.contact_field)) {
          const val = contactFieldValue(contact, v.contact_field);
          if (val) next[v.idx - 1] = val;
        }
      }
      setVars(next);
    } catch {
      // Sin config: se queda con labels "Variable n" y relleno manual.
      setVarConfig([]);
    }
  }

  function setVar(varNumber: number, value: string) {
    setVars((prev) => prev.map((v, i) => (i === varNumber - 1 ? value : v)));
  }

  async function send() {
    if (!sel) return;
    if (sel.variables > 0 && emptyVarNumbers(vars, sel.variables).length > 0) {
      setResult({ ok: false, text: "Rellena todas las variables de la plantilla." });
      return;
    }
    setSending(true);
    setResult(null);
    try {
      // CONTRATO: array ORDENADO por idx. NO cambia.
      const res = await outboundSend({
        template_name: sel.name,
        language: sel.language,
        recipients: [{ phone, variables: vars }],
      });
      const r = res[0];
      setResult(
        r && r.status === "ok"
          ? { ok: true, text: "Plantilla enviada." }
          : { ok: false, text: r?.error || "No se pudo enviar." },
      );
    } catch (e) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setResult({ ok: false, text: ax.response?.data?.detail || "Error al enviar." });
    } finally {
      setSending(false);
    }
  }

  const hasSlots = !!sel?.body && /\{\{\s*\d+\s*\}\}/.test(sel.body);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-[1px] p-4"
      onClick={onClose}
    >
      <div className="card w-full max-w-md flex flex-col max-h-[90vh]" onClick={(e) => e.stopPropagation()}>
        <header className="px-5 py-3 border-b border-line flex items-center gap-2 shrink-0">
          <Send className="w-4 h-4 text-brand-ink" />
          <h2 className="font-display text-lg text-ink flex-1">Enviar plantilla por WhatsApp</h2>
          <button type="button" onClick={onClose} className="text-ink3 hover:text-ink" aria-label="Cerrar">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3 overflow-auto">
          <div className="text-sm text-ink2">
            A <b className="text-ink font-mono">{phone}</b>
          </div>
          {loading ? (
            <div className="text-ink3 text-sm italic font-display">Cargando plantillas…</div>
          ) : templates.length === 0 ? (
            <div className="text-ink3 text-sm">
              No hay plantillas disponibles. Revisa las credenciales de YCloud y que tengas plantillas aprobadas.
            </div>
          ) : (
            <>
              <div>
                <label className="label">Plantilla</label>
                <select className="input" value={sel?.name || ""} onChange={(e) => pick(e.target.value)}>
                  <option value="">Elige una…</option>
                  {templates.map((t) => (
                    <option key={t.name + t.language} value={t.name}>
                      {t.name} ({t.language})
                    </option>
                  ))}
                </select>
              </div>
              {sel && sel.variables > 0 && (
                <div className="space-y-2">
                  {/* Relleno "huecos en la frase" con etiquetas amigables. */}
                  <InlineTemplateFill
                    body={sel.body}
                    values={vars}
                    vars={varConfig}
                    onChange={setVar}
                  />
                  {/* Fallback: body sin huecos pero con variables declaradas. */}
                  {!hasSlots &&
                    vars.map((v, i) => (
                      <div key={i}>
                        <label className="label">
                          {varConfig.find((x) => x.idx === i + 1)?.nombre?.trim() || `Variable ${i + 1}`}
                        </label>
                        <input
                          className="input"
                          value={v}
                          onChange={(e) => setVar(i + 1, e.target.value)}
                        />
                      </div>
                    ))}
                </div>
              )}
              {sel && sel.variables > 0 && (
                <TemplatePreview body={sel.body} values={vars} vars={varConfig} />
              )}
              {sel && sel.variables === 0 && sel.body && (
                <div className="rounded-coro-sm bg-paper2 p-3 text-sm text-ink2 whitespace-pre-wrap">
                  {sel.body}
                </div>
              )}
              {result && (
                <div className={result.ok ? "text-state-ok text-sm" : "text-state-bad text-sm"}>{result.text}</div>
              )}
            </>
          )}
        </div>
        <footer className="px-5 py-3 border-t border-line flex justify-end gap-2 bg-paper2">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cerrar
          </button>
          <button type="button" onClick={send} disabled={sending || !sel} className="btn-primary">
            <Send className="w-4 h-4" /> {sending ? "Enviando…" : "Enviar"}
          </button>
        </footer>
      </div>
    </div>
  );
}
