import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Send,
  Loader2,
  MessageSquare,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Info,
  Settings2,
  ChevronDown,
  ChevronRight,
  Save,
  Sparkles,
  Users,
  Gauge,
  Clock,
  Ban,
  ArrowLeft,
  History,
  Paperclip,
  Download,
  RefreshCw,
  ShieldAlert,
  Eye,
  X,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  listWhatsappTemplates,
  getTemplateVars,
  putTemplateVars,
  resolveTemplateVars,
  createOutboundJob,
  getOutboundJob,
  listOutboundJobs,
  listOutboundJobRecipients,
  cancelOutboundJob,
  retryOutboundJob,
  downloadOutboundJobCsv,
  getOutboundPreflight,
  uploadOutboundHeaderMedia,
  templateBlockReason,
  listOutboundOptOuts,
  createOutboundOptOut,
  deleteOutboundOptOut,
  type OutboundOptOut,
  CONTACT_FIELD_OPTIONS,
  type WhatsappTemplate,
  type OutboundJob,
  type OutboundJobDetail,
  type OutboundJobHeader,
  type OutboundJobRecipientResult,
  type OutboundPreflight,
  type TemplateVar,
} from "@/services/admin";
import {
  InlineTemplateFill,
  TemplatePreview,
  emptyVarNumbers,
} from "@/components/TemplateVarsFiller";
import { ContactPicker, type PickedContact } from "@/components/ContactPicker";
import { errorDetail } from "@/lib/errors";
import { usePrivacy } from "@/store/privacy";
import { maskName, maskPhone } from "@/lib/mask";

/**
 * Envío masivo de plantillas WhatsApp, práctico y "poco a poco".
 *
 * Flujo:
 *  1. Elige una plantilla aprobada (YCloud). Menos variables = menos fricción.
 *  2. Selecciona contactos del CRM (filtros + "seleccionar todos del filtro").
 *  3. Autorrellena las variables desde la ficha del contacto; los que les falte
 *     algún dato se marcan para completarlos a mano (no se envía con huecos).
 *  4. Elige el ritmo (lento y seguro por defecto) y envía.
 *  5. El envío corre en SEGUNDO PLANO (job + tarea Celery que se re-encola con
 *     un retardo entre mensajes). Aquí solo vemos el progreso por polling.
 *
 * CONTRATO de envío: por destinatario, un array ORDENADO `variables[idx-1] ->
 * {{idx}}`. Los nombres/enlaces de variables son solo UI.
 */

// Presets de ritmo (segundos entre mensajes). "Lento y seguro" por defecto.
const THROTTLE_PRESETS = {
  lento: { min: 20, max: 40, label: "Lento y seguro" },
  normal: { min: 6, max: 12, label: "Normal" },
} as const;
type ThrottleMode = keyof typeof THROTTLE_PRESETS | "custom";

// Coste aproximado de la llamada al proveedor por mensaje (s). Espejo de
// OUTBOUND_SEND_OVERHEAD_SECONDS en el backend.
const SEND_OVERHEAD_SECONDS = 1.5;
// Sondeo del progreso: cada 3 s. Tras QUEUE_STUCK_POLLS seguido en "En cola",
// no es que vaya lento, es que no hay nadie ejecutándolo. Y MAX_POLLS corta el
// sondeo eterno de una pestaña olvidada (3 s × 1200 ≈ 1 h).
const QUEUE_STUCK_POLLS = 15;
const MAX_POLLS = 1200;
const HISTORY_PAGE_SIZE = 10;

// Formatos que WhatsApp acepta en la cabecera de cada tipo. Espejo de
// OUTBOUND_HEADER_MIME_BY_FORMAT en el backend.
const HEADER_ACCEPT: Record<string, string> = {
  image: "image/jpeg,image/png",
  video: "video/mp4",
  document: "application/pdf",
};

/** ¿La plantilla lleva cabecera con FICHERO (no de texto)? */
function needsHeaderFile(t: WhatsappTemplate): boolean {
  return t.header_format === "image" || t.header_format === "video" || t.header_format === "document";
}

/** Sustituye los huecos de la cabecera de texto para la vista previa. */
function fillHeaderText(texto: string, values: string[]): string {
  let i = 0;
  return texto.replace(/\{\{\s*[A-Za-z0-9_]+\s*\}\}/g, () => {
    const v = (values[i] ?? "").trim();
    i += 1;
    return v || "▢";
  });
}

/**
 * Estado de aprobación de la plantilla. El backend ya lo traía —y el propio
 * proveedor dice que devuelve las pendientes y rechazadas a propósito "porque
 * la interfaz muestra el estado"— pero la interfaz no lo pintaba: con una
 * plantilla rechazada lanzabas a 300 y salían 300 errores de uno en uno.
 */
function TemplateStatusPill({ status }: { status?: string | null }) {
  const s = (status || "").trim().toLowerCase();
  if (!s) return null;
  const ok = s === "approved" || s === "active" || s === "enabled";
  const pendiente = s === "pending" || s === "in_appeal";
  const cls = ok
    ? "bg-state-ok/15 text-state-ok"
    : pendiente
      ? "bg-state-warn/15 text-state-warn"
      : "bg-state-bad/15 text-state-bad";
  const label: Record<string, string> = {
    approved: "aprobada",
    active: "activa",
    enabled: "activa",
    pending: "pendiente",
    in_appeal: "en apelación",
    rejected: "rechazada",
    disabled: "deshabilitada",
    paused: "pausada",
  };
  return <span className={`pill text-[10px] ${cls}`}>{label[s] ?? s}</span>;
}

export default function OutboundPage() {
  // Modo privacidad: esta pantalla es justo la que se graba y se compartía en
  // claro nombre y teléfono de cada contacto. El selector ya enmascaraba; aquí
  // no, así que quedaba incoherente además de expuesto.
  const priv = usePrivacy((s) => s.enabled);

  const [templates, setTemplates] = useState<WhatsappTemplate[]>([]);
  const [loadingTpl, setLoadingTpl] = useState(true);
  const [tplError, setTplError] = useState<string | null>(null);

  const [selected, setSelected] = useState<WhatsappTemplate | null>(null);

  // Estado del sistema (credenciales + worker vivo). Si el worker está caído,
  // la campaña se quedaría "En cola" para siempre: mejor decirlo antes.
  const [preflight, setPreflight] = useState<OutboundPreflight | null>(null);

  // Cabecera con fichero (imagen/vídeo/PDF) cuando la plantilla la lleva.
  const [headerMedia, setHeaderMedia] = useState<{ media_id: string; filename: string } | null>(null);
  const [headerVars, setHeaderVars] = useState<string[]>([]);
  const [uploadingHeader, setUploadingHeader] = useState(false);
  const [headerError, setHeaderError] = useState<string | null>(null);

  // Confirmación antes de disparar. Un clic son N WhatsApps irreversibles.
  const [confirmSend, setConfirmSend] = useState(false);

  // Config de variables (nombres amigables + enlace a campo de contacto).
  const [vars, setVars] = useState<TemplateVar[]>([]);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingVars, setSavingVars] = useState(false);
  const [varsSaved, setVarsSaved] = useState<string | null>(null);
  const [varsError, setVarsError] = useState<string | null>(null);

  // Destinatarios elegidos del CRM + sus valores de variables (por contact id).
  const [picked, setPicked] = useState<PickedContact[]>([]);
  const [recipientVars, setRecipientVars] = useState<Record<string, string[]>>({});
  const [resolving, setResolving] = useState(false);
  const [resolveNote, setResolveNote] = useState<string | null>(null);

  // Ritmo.
  const [throttleMode, setThrottleMode] = useState<ThrottleMode>("lento");
  const [customMin, setCustomMin] = useState(20);
  const [customMax, setCustomMax] = useState(40);

  // Envío + job activo (vista de progreso).
  const [creating, setCreating] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);

  // Reutilizable para el botón "Reintentar" del banner de error.
  const refreshTemplates = useCallback(async () => {
    setLoadingTpl(true);
    setTplError(null);
    try {
      setTemplates(await listWhatsappTemplates());
    } catch (e) {
      setTplError(errorDetail(e, "No se pudieron cargar las plantillas."));
    } finally {
      setLoadingTpl(false);
    }
  }, []);

  useEffect(() => {
    void refreshTemplates();
    getOutboundPreflight().then(setPreflight).catch(() => setPreflight(null));
  }, [refreshTemplates]);

  // Clave de la plantilla elegida. Es lo que marca "esta es OTRA plantilla":
  // con solo mirar el objeto, un refresco de la lista disparaba el reseteo.
  const templateKey = selected ? `${selected.name}::${selected.language}` : null;

  // Al cambiar de plantilla: carga la config guardada de variables y TIRA los
  // valores de la anterior. Antes se preservaban y solo se ajustaba la
  // longitud: elegías A, rellenabas, cambiabas a B y los huecos seguían con lo
  // de A mientras la pantalla decía "0 con datos faltantes". Con el envío sin
  // confirmación de al lado, eso son N mensajes con el texto de otra campaña.
  useEffect(() => {
    setRecipientVars({});
    setHeaderMedia(null);
    setHeaderVars([]);
    setHeaderError(null);
    setSendError(null);
    setVarsSaved(null);
    setVarsError(null);
    setResolveNote(null);

    if (!selected || selected.variables < 1) {
      setVars([]);
      return;
    }
    let cancelled = false;
    getTemplateVars(selected.name, selected.language)
      .then((saved) => {
        if (!cancelled) setVars(mergeVars(saved, selected.variables));
      })
      .catch(() => {
        if (!cancelled) setVars(mergeVars([], selected.variables));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateKey]);

  // Mantén recipientVars sincronizado con (contactos seleccionados × Nvariables),
  // preservando los valores ya escritos DE ESTA plantilla.
  useEffect(() => {
    const n = selected?.variables ?? 0;
    setRecipientVars((prev) => {
      const next: Record<string, string[]> = {};
      for (const c of picked) next[c.id] = padOrTrim(prev[c.id] ?? [], n, "");
      return next;
    });
  }, [picked, selected]);

  const throttle = useMemo(() => {
    if (throttleMode === "custom") {
      const min = Math.max(0, Math.floor(customMin));
      const max = Math.max(min, Math.floor(customMax));
      return { min, max };
    }
    return THROTTLE_PRESETS[throttleMode];
  }, [throttleMode, customMin, customMax]);

  function updateVarField(idx: number, patch: Partial<TemplateVar>) {
    setVars((prev) => prev.map((v) => (v.idx === idx ? { ...v, ...patch } : v)));
    setVarsSaved(null);
  }

  async function onSaveVars() {
    if (!selected) return;
    setSavingVars(true);
    setVarsError(null);
    setVarsSaved(null);
    try {
      const out = await putTemplateVars(selected.name, {
        language: selected.language,
        vars: vars.map((v) => ({
          idx: v.idx,
          nombre: v.nombre.trim(),
          contact_field: v.contact_field || null,
        })),
      });
      setVars(mergeVars(out, selected.variables));
      setVarsSaved("Configuración guardada.");
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } }; message?: string };
      setVarsError(ax.response?.data?.detail || ax.message || "No se pudo guardar la configuración");
    } finally {
      setSavingVars(false);
    }
  }

  // Autorrellena las variables enlazadas desde el CRM por teléfono.
  async function onAutofill() {
    if (!selected || selected.variables < 1) return;
    if (picked.length === 0) {
      setResolveNote("Selecciona contactos primero.");
      return;
    }
    setResolving(true);
    setResolveNote(null);
    try {
      const phones = picked.map((c) => c.telefono.trim());
      const res = await resolveTemplateVars(selected.name, { language: selected.language, phones });
      const byPhone = new Map(res.map((r) => [r.phone.trim(), r]));
      setRecipientVars((prev) => {
        const next = { ...prev };
        for (const c of picked) {
          const arr = [...(next[c.id] ?? new Array(selected.variables).fill(""))];
          const hit = byPhone.get(c.telefono.trim());
          if (hit && hit.contact_found) {
            for (const [idxStr, value] of Object.entries(hit.variables)) {
              const pos = Number(idxStr) - 1;
              if (pos >= 0 && pos < arr.length) arr[pos] = value;
            }
          }
          next[c.id] = arr;
        }
        return next;
      });
      const anyLinked = vars.some((v) => v.contact_field);
      if (!anyLinked) {
        setResolveNote(
          "Ninguna variable está enlazada a un campo del contacto. Enlázalas en «Configurar variables» para autorrellenar.",
        );
      } else {
        // El backend ya decía si encontró ficha y la pantalla lo tiraba: el
        // mensaje era siempre el mismo y no se sabía POR QUÉ faltaban datos.
        const sinFicha = res.filter((r) => !r.contact_found).length;
        const sinDatos = res.filter(
          (r) => r.contact_found && Object.keys(r.variables).length === 0,
        ).length;
        const partes = ["Valores autorrellenados desde los contactos."];
        if (sinFicha > 0) {
          partes.push(`${sinFicha} sin ficha en el CRM (ese hueco va a mano).`);
        }
        if (sinDatos > 0) {
          partes.push(`${sinDatos} con ficha pero sin el dato enlazado guardado.`);
        }
        setResolveNote(partes.join(" "));
      }
    } catch (e: unknown) {
      setResolveNote(errorDetail(e, "No se pudo autorrellenar"));
    } finally {
      setResolving(false);
    }
  }

  function updateContactVar(id: string, varNumber: number, value: string) {
    setRecipientVars((prev) => {
      const arr = [...(prev[id] ?? [])];
      arr[varNumber - 1] = value;
      return { ...prev, [id]: arr };
    });
  }

  function removeContact(id: string) {
    setPicked((prev) => prev.filter((c) => c.id !== id));
  }

  // Contactos a los que les falta algún dato requerido por la plantilla.
  const missing = useMemo(() => {
    if (!selected || selected.variables < 1) return [];
    return picked.filter(
      (c) => emptyVarNumbers(recipientVars[c.id] ?? [], selected.variables).length > 0,
    );
  }, [picked, recipientVars, selected]);

  const readyCount = picked.length - missing.length;

  // Todo lo que impide enviar, en un solo sitio. El botón lo usa para
  // deshabilitarse y el modal de confirmación para no abrirse a ciegas.
  const blockReason = useMemo(() => {
    if (!selected) return "Elige una plantilla.";
    const tplBlocked = templateBlockReason(selected);
    if (tplBlocked) return `No se puede enviar con esta plantilla. ${tplBlocked}`;
    if (picked.length === 0) return "Selecciona al menos un contacto.";
    if (selected.variables > 0 && missing.length > 0) {
      return `Faltan datos en ${missing.length} contacto(s). Complétalos abajo o quítalos antes de enviar.`;
    }
    if (needsHeaderFile(selected) && !headerMedia) {
      return "Esta plantilla lleva una cabecera con fichero y todavía no has adjuntado ninguno.";
    }
    if (
      selected.header_variables > 0 &&
      headerVars.slice(0, selected.header_variables).some((v) => !v.trim())
    ) {
      return "Rellena las variables de la cabecera de la plantilla.";
    }
    if (preflight && !preflight.can_send) {
      return preflight.credentials_ok ? preflight.worker_detail : preflight.credentials_detail;
    }
    return null;
  }, [selected, picked.length, missing.length, headerMedia, headerVars, preflight]);

  function buildHeaderPayload(): OutboundJobHeader | null {
    if (!selected || !selected.header_format) return null;
    if (needsHeaderFile(selected)) {
      if (!headerMedia) return null;
      return {
        format: selected.header_format,
        media_id: headerMedia.media_id,
        filename: headerMedia.filename,
      };
    }
    if (selected.header_variables > 0) {
      return {
        format: "text",
        variables: headerVars.slice(0, selected.header_variables),
      };
    }
    return null;
  }

  async function onConfirmSend() {
    if (!selected) return;
    setConfirmSend(false);
    setSendError(null);
    setCreating(true);
    try {
      const recipients = picked.map((c) => ({
        phone: c.telefono,
        variables: (recipientVars[c.id] ?? []).slice(0, selected.variables),
      }));
      const job = await createOutboundJob({
        template_name: selected.name,
        language: selected.language,
        throttle_min_seconds: throttle.min,
        throttle_max_seconds: throttle.max,
        recipients,
        header: buildHeaderPayload(),
      });
      setActiveJobId(job.id);
    } catch (e: unknown) {
      setSendError(errorDetail(e, "No se pudo crear el envío"));
    } finally {
      setCreating(false);
    }
  }

  async function onUploadHeader(file: File) {
    if (!selected || !needsHeaderFile(selected)) return;
    setUploadingHeader(true);
    setHeaderError(null);
    try {
      const res = await uploadOutboundHeaderMedia(
        selected.header_format as "image" | "video" | "document",
        file,
      );
      setHeaderMedia({ media_id: res.media_id, filename: res.filename });
    } catch (e) {
      setHeaderError(errorDetail(e, "No se pudo subir el fichero de la cabecera"));
    } finally {
      setUploadingHeader(false);
    }
  }

  function resetCompose() {
    setActiveJobId(null);
    setPicked([]);
    setRecipientVars({});
    setResolveNote(null);
    setSendError(null);
    setHeaderMedia(null);
    setHeaderVars([]);
  }

  // ---- Vista de progreso (job activo) ----
  if (activeJobId) {
    return (
      <div className="h-full flex flex-col">
        <PageHeader
          title="Envío masivo (plantillas)"
          description="Enviando en segundo plano, poco a poco para no disparar baneos."
        />
        <div className="flex-1 overflow-auto p-4 max-w-2xl">
          <JobProgress jobId={activeJobId} onNew={resetCompose} />
        </div>
      </div>
    );
  }

  // Duración estimada. Antes solo contaba el retardo entre mensajes y siempre
  // se quedaba corta: la llamada al proveedor también tarda, y a 300 mensajes
  // eso son minutos de diferencia.
  const estSeconds =
    picked.length * ((throttle.min + throttle.max) / 2 + SEND_OVERHEAD_SECONDS);
  // Contacto sobre el que se pinta la vista previa: el primero de la lista.
  const previewContact = picked[0] ?? null;

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Envío masivo (plantillas)"
        description="Plantillas WhatsApp pre-aprobadas para mensajes fuera de la ventana 24h."
      />
      <div className="flex-1 overflow-auto p-4 max-w-5xl grid md:grid-cols-[18rem_1fr] gap-4">
        {/* Columna 1: plantillas */}
        <section className="card p-4 flex flex-col gap-2 self-start">
          <h3 className="font-display text-lg text-ink mb-1 inline-flex items-center gap-1.5">
            <MessageSquare className="w-4 h-4 text-brand-ink" /> 1 · Plantilla
          </h3>
          {loadingTpl ? (
            <div className="text-ink3 text-sm inline-flex items-center gap-1.5">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> Cargando…
            </div>
          ) : tplError ? (
            <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
              <span className="flex-1">{tplError}</span>
              <button type="button" onClick={() => void refreshTemplates()} className="font-semibold hover:underline shrink-0">
                Reintentar
              </button>
            </div>
          ) : templates.length === 0 ? (
            <div className="text-ink3 text-sm italic">
              YCloud no devolvió plantillas. Configura las creds YCloud o aprueba alguna plantilla en su panel.
            </div>
          ) : (
            <ul className="space-y-1.5">
              {templates.map((t) => {
                const bloqueo = templateBlockReason(t);
                const totalVars = t.variables + t.header_variables;
                return (
                  <li key={t.name + t.language}>
                    <button
                      type="button"
                      onClick={() => setSelected(t)}
                      className={`w-full text-left p-2.5 rounded-coro-sm border transition ${
                        selected?.name === t.name && selected?.language === t.language
                          ? "border-brand-ink bg-brand-soft"
                          : "border-line hover:bg-paper2"
                      } ${bloqueo ? "opacity-70" : ""}`}
                    >
                      <div className="flex items-baseline gap-2 flex-wrap">
                        <span className="font-medium text-sm text-ink">{t.name}</span>
                        <span className="text-[10px] font-mono text-ink3">{t.language}</span>
                        <TemplateStatusPill status={t.status} />
                      </div>
                      <div className="flex items-baseline gap-1.5 flex-wrap mt-1">
                        <span
                          className={`pill text-[10px] ${
                            totalVars === 0
                              ? "bg-state-ok/15 text-state-ok"
                              : "bg-paper3 text-ink3"
                          }`}
                        >
                          {totalVars === 0 ? "sin variables" : `${totalVars} var`}
                        </span>
                        {t.param_format === "named" && (
                          <span className="pill text-[10px] bg-paper3 text-ink3">
                            con nombre
                          </span>
                        )}
                        {t.header_format && (
                          <span className="pill text-[10px] bg-paper3 text-ink3">
                            cabecera {t.header_format}
                          </span>
                        )}
                      </div>
                      {t.body && <div className="text-xs text-ink3 mt-1 line-clamp-2">{t.body}</div>}
                      {bloqueo && (
                        <div className="text-[10px] text-state-bad mt-1">{bloqueo}</div>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          <RecentJobs onOpen={setActiveJobId} />
          <OptOutsPanel />
        </section>

        {/* Columna 2: contactos + variables + ritmo + envío */}
        <section className="card p-4 flex flex-col gap-3">
          {!selected ? (
            <div className="text-ink3 text-sm italic inline-flex items-center gap-1.5">
              <Info className="w-3.5 h-3.5" />
              Elige una plantilla a la izquierda para empezar.
            </div>
          ) : (
            <>
              {preflight && !preflight.can_send && (
                <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
                  <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" />
                  <span className="flex-1">
                    {preflight.credentials_ok
                      ? preflight.worker_detail
                      : preflight.credentials_detail}
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      getOutboundPreflight().then(setPreflight).catch(() => undefined);
                    }}
                    className="font-semibold hover:underline shrink-0"
                  >
                    Revisar
                  </button>
                </div>
              )}

              {templateBlockReason(selected) && (
                <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 inline-flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                  <span>
                    No se puede enviar con esta plantilla. {templateBlockReason(selected)}
                  </span>
                </div>
              )}

              <div className="text-xs text-ink3 bg-paper2 border border-line rounded-coro-sm px-2.5 py-2">
                <b className="text-ink2">{selected.name}</b> ({selected.language}) —{" "}
                {selected.variables} variable(s) en el cuerpo
                {selected.header_variables > 0 && ` + ${selected.header_variables} en la cabecera`}
                .
                {selected.header_text && (
                  <div className="mt-1 font-medium text-ink2">{selected.header_text}</div>
                )}
                {selected.body && <div className="mt-1 italic line-clamp-3">{selected.body}</div>}
                {selected.footer && <div className="mt-1 text-[11px]">{selected.footer}</div>}
              </div>

              {/* Cabecera con fichero: sin esto la plantilla era, sencillamente,
                  no enviable desde ninguna pantalla. */}
              {needsHeaderFile(selected) && (
                <div className="border border-line rounded-coro-sm px-2.5 py-2 space-y-1.5">
                  <div className="text-sm text-ink inline-flex items-center gap-1.5 font-medium">
                    <Paperclip className="w-4 h-4 text-brand-ink" />
                    Fichero de la cabecera ({selected.header_format})
                  </div>
                  <p className="text-[11px] text-ink3">
                    Se sube una vez y va en todos los mensajes de este envío.
                    {selected.header_format === "image" && " JPG o PNG."}
                    {selected.header_format === "video" && " MP4."}
                    {selected.header_format === "document" && " PDF."}
                  </p>
                  <input
                    type="file"
                    accept={HEADER_ACCEPT[selected.header_format ?? ""] ?? undefined}
                    disabled={uploadingHeader}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void onUploadHeader(f);
                    }}
                    className="text-xs"
                  />
                  {uploadingHeader && (
                    <div className="text-[11px] text-ink3 inline-flex items-center gap-1">
                      <Loader2 className="w-3.5 h-3.5 animate-spin" /> Subiendo…
                    </div>
                  )}
                  {headerMedia && (
                    <div className="text-[11px] text-state-ok inline-flex items-center gap-1">
                      <CheckCircle2 className="w-3.5 h-3.5" /> {headerMedia.filename} listo
                    </div>
                  )}
                  {headerError && (
                    <div className="text-[11px] text-state-bad">{headerError}</div>
                  )}
                </div>
              )}

              {/* Cabecera de TEXTO con variables: es igual de obligatoria que
                  las del cuerpo y antes ni se contaba. */}
              {selected.header_variables > 0 && (
                <div className="border border-line rounded-coro-sm px-2.5 py-2 space-y-1.5">
                  <div className="text-sm text-ink font-medium">Variables de la cabecera</div>
                  {Array.from({ length: selected.header_variables }).map((_, i) => (
                    <input
                      key={i}
                      type="text"
                      value={headerVars[i] ?? ""}
                      placeholder={selected.header_variable_names[i] || `Variable ${i + 1}`}
                      onChange={(e) =>
                        setHeaderVars((prev) => {
                          const next = [...prev];
                          next[i] = e.target.value;
                          return next;
                        })
                      }
                      className="input w-full text-xs"
                    />
                  ))}
                </div>
              )}

              {/* Configurar variables (definir una vez) */}
              {selected.variables > 0 && (
                <div className="border border-line rounded-coro-sm">
                  <button
                    type="button"
                    onClick={() => setConfigOpen((o) => !o)}
                    className="w-full flex items-center gap-1.5 px-2.5 py-2 text-sm text-ink2 hover:bg-paper2 rounded-coro-sm"
                  >
                    {configOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                    <Settings2 className="w-3.5 h-3.5 text-brand-ink" />
                    <span className="font-medium">Configurar variables</span>
                    <span className="text-ink3 text-xs ml-auto">nombres y autorrelleno</span>
                  </button>
                  {configOpen && (
                    <div className="px-2.5 pb-2.5 pt-1 space-y-2 border-t border-line">
                      <p className="text-[11px] text-ink3">
                        Ponle un nombre a cada hueco y, si quieres, enlázalo a un campo del contacto para autorrellenarlo desde el CRM.
                      </p>
                      {vars.map((v) => (
                        <div key={v.idx} className="flex flex-col sm:flex-row sm:items-center gap-1.5">
                          <span className="font-mono text-[11px] text-ink3 sm:w-9 flex-none">{`{{${v.idx}}}`}</span>
                          <input
                            type="text"
                            value={v.nombre}
                            onChange={(e) => updateVarField(v.idx, { nombre: e.target.value })}
                            placeholder="p. ej. Nombre del contacto"
                            className="input flex-1 text-xs"
                          />
                          <select
                            value={v.contact_field ?? ""}
                            onChange={(e) => updateVarField(v.idx, { contact_field: e.target.value || null })}
                            className="input text-xs w-full sm:w-[9.5rem] sm:flex-none"
                          >
                            {CONTACT_FIELD_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>{o.label}</option>
                            ))}
                          </select>
                        </div>
                      ))}
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={onSaveVars}
                          disabled={savingVars}
                          className="btn-ghost text-xs inline-flex items-center gap-1"
                        >
                          {savingVars ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                          Guardar
                        </button>
                        {varsSaved && (
                          <span className="text-state-ok text-xs inline-flex items-center gap-1">
                            <CheckCircle2 className="w-3.5 h-3.5" /> {varsSaved}
                          </span>
                        )}
                        {varsError && (
                          <span className="text-state-bad text-xs inline-flex items-center gap-1">
                            <AlertTriangle className="w-3.5 h-3.5" /> {varsError}
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Paso 2: contactos */}
              <div className="space-y-2">
                <h4 className="font-medium text-sm text-ink inline-flex items-center gap-1.5">
                  <Users className="w-4 h-4 text-brand-ink" /> 2 · Contactos
                </h4>
                <ContactPicker selected={picked} onChange={setPicked} />
              </div>

              {/* Paso 3: variables (autorrelleno + faltantes) */}
              {selected.variables > 0 && picked.length > 0 && (
                <div className="space-y-2 border-t border-line pt-3">
                  <h4 className="font-medium text-sm text-ink inline-flex items-center gap-1.5">
                    <Sparkles className="w-4 h-4 text-brand-ink" /> 3 · Variables
                  </h4>
                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      type="button"
                      onClick={onAutofill}
                      disabled={resolving}
                      className="btn-ghost text-xs inline-flex items-center gap-1"
                    >
                      {resolving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
                      Autorrellenar desde contactos
                    </button>
                    <span className="text-xs text-ink3">
                      <b className="text-state-ok">{readyCount}</b> listo(s) ·{" "}
                      <b className={missing.length ? "text-state-warn" : "text-ink3"}>{missing.length}</b> con datos faltantes
                    </span>
                  </div>
                  {resolveNote && (
                    <div className="text-[11px] text-ink3 bg-paper2 border border-line rounded-coro-sm px-2.5 py-1.5">
                      {resolveNote}
                    </div>
                  )}
                  {missing.length > 0 && (
                    <div className="space-y-2">
                      <p className="text-[11px] text-state-warn inline-flex items-center gap-1">
                        <AlertTriangle className="w-3.5 h-3.5" /> Completa los huecos vacíos o quita el contacto:
                      </p>
                      {missing.map((c) => (
                        <div key={c.id} className="border border-state-warn/40 rounded-coro-sm p-2 space-y-1.5">
                          <div className="flex items-center gap-2 text-xs">
                            <span className="font-medium text-ink2 truncate">
                              {maskName(c.nombre, priv) || "(sin nombre)"}
                            </span>
                            <span className="font-mono text-ink3 truncate">
                              {maskPhone(c.telefono, priv)}
                            </span>
                            <button
                              type="button"
                              onClick={() => removeContact(c.id)}
                              className="btn-ghost text-xs text-state-bad ml-auto"
                            >
                              Quitar
                            </button>
                          </div>
                          <InlineTemplateFill
                            body={selected.body}
                            values={recipientVars[c.id] ?? []}
                            vars={vars}
                            onChange={(varNumber, value) => updateContactVar(c.id, varNumber, value)}
                            compact
                          />
                          {!hasSlots(selected.body) &&
                            (recipientVars[c.id] ?? []).map((v, j) => (
                              <input
                                key={j}
                                type="text"
                                placeholder={vars.find((x) => x.idx === j + 1)?.nombre?.trim() || `Variable ${j + 1}`}
                                value={v}
                                onChange={(e) => updateContactVar(c.id, j + 1, e.target.value)}
                                className="input w-full text-xs"
                              />
                            ))}
                          <TemplatePreview body={selected.body} values={recipientVars[c.id] ?? []} vars={vars} compact />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Paso 4: ritmo */}
              {picked.length > 0 && (
                <div className="space-y-2 border-t border-line pt-3">
                  <h4 className="font-medium text-sm text-ink inline-flex items-center gap-1.5">
                    <Gauge className="w-4 h-4 text-brand-ink" /> 4 · Ritmo de envío
                  </h4>
                  <div className="flex items-center gap-1.5 flex-wrap">
                    {(Object.keys(THROTTLE_PRESETS) as Array<keyof typeof THROTTLE_PRESETS>).map((k) => (
                      <button
                        key={k}
                        type="button"
                        onClick={() => setThrottleMode(k)}
                        className={`pill text-xs ${
                          throttleMode === k ? "bg-brand-ink text-paper" : "bg-paper3 text-ink2 hover:bg-paper2"
                        }`}
                      >
                        {THROTTLE_PRESETS[k].label} ({THROTTLE_PRESETS[k].min}-{THROTTLE_PRESETS[k].max}s)
                      </button>
                    ))}
                    <button
                      type="button"
                      onClick={() => setThrottleMode("custom")}
                      className={`pill text-xs ${
                        throttleMode === "custom" ? "bg-brand-ink text-paper" : "bg-paper3 text-ink2 hover:bg-paper2"
                      }`}
                    >
                      Personalizado
                    </button>
                  </div>
                  {throttleMode === "custom" && (
                    <div className="flex items-center gap-2 text-xs text-ink3">
                      <span>Entre</span>
                      <input
                        type="number"
                        min={0}
                        value={customMin}
                        onChange={(e) => setCustomMin(Number(e.target.value))}
                        className="input text-xs w-16"
                      />
                      <span>y</span>
                      <input
                        type="number"
                        min={0}
                        value={customMax}
                        onChange={(e) => setCustomMax(Number(e.target.value))}
                        className="input text-xs w-16"
                      />
                      <span>segundos entre mensajes</span>
                    </div>
                  )}
                  <div className="text-[11px] text-ink3 inline-flex items-center gap-1">
                    <Clock className="w-3.5 h-3.5" />
                    {picked.length} mensaje(s) · duración estimada ≈ {formatDuration(estSeconds)}
                  </div>
                </div>
              )}

              {/* Paso 5: VISTA PREVIA del mensaje montado. Antes solo se
                  renderizaba dentro del bloque de contactos a los que faltaban
                  datos: si el autorrelleno funcionaba, el operador no llegaba a
                  ver nunca el mensaje final, solo el cuerpo crudo con los
                  huecos y recortado a tres líneas. */}
              {picked.length > 0 && (
                <div className="space-y-2 border-t border-line pt-3">
                  <h4 className="font-medium text-sm text-ink inline-flex items-center gap-1.5">
                    <Eye className="w-4 h-4 text-brand-ink" /> 5 · Vista previa
                  </h4>
                  <p className="text-[11px] text-ink3">
                    Así le llegará a{" "}
                    <b className="text-ink2">
                      {maskName(previewContact?.nombre ?? null, priv) ||
                        maskPhone(previewContact?.telefono ?? "", priv)}
                    </b>
                    , el primero de la lista.
                  </p>
                  {selected.header_text && (
                    <div className="rounded-coro-sm bg-paper2 px-2.5 py-1.5 text-xs font-medium text-ink">
                      {fillHeaderText(selected.header_text, headerVars)}
                    </div>
                  )}
                  {needsHeaderFile(selected) && (
                    <div className="text-[11px] text-ink3 inline-flex items-center gap-1">
                      <Paperclip className="w-3.5 h-3.5" />
                      {headerMedia ? headerMedia.filename : "(sin fichero adjunto todavía)"}
                    </div>
                  )}
                  <TemplatePreview
                    body={selected.body}
                    values={previewContact ? recipientVars[previewContact.id] ?? [] : []}
                    vars={vars}
                  />
                  {selected.footer && (
                    <div className="text-[11px] text-ink3">{selected.footer}</div>
                  )}
                </div>
              )}

              {sendError && (
                <div className="text-state-bad text-sm inline-flex items-center gap-1">
                  <AlertTriangle className="w-3.5 h-3.5" /> {sendError}
                </div>
              )}
              {blockReason && !sendError && (
                <div className="text-[11px] text-ink3 self-end">{blockReason}</div>
              )}
              <button
                type="button"
                onClick={() => setConfirmSend(true)}
                disabled={creating || blockReason !== null}
                className="btn-primary self-end inline-flex items-center gap-1.5"
              >
                {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                Enviar a {picked.length} contacto(s)
              </button>

              {/* Un clic eran N WhatsApps irreversibles sin ninguna pregunta:
                  el único modal que existía era el de CANCELAR. */}
              <ConfirmModal
                open={confirmSend}
                title={`¿Enviar a ${picked.length} contacto(s)?`}
                description={
                  `Se enviará la plantilla «${selected.name}» (${selected.language}) a ` +
                  `${picked.length} persona(s), una cada ${throttle.min}-${throttle.max} s ` +
                  `(≈ ${formatDuration(estSeconds)}). Los mensajes enviados NO se pueden retirar.`
                }
                confirmLabel="Sí, enviar"
                cancelLabel="Revisar antes"
                tone="danger"
                busy={creating}
                onConfirm={onConfirmSend}
                onCancel={() => setConfirmSend(false)}
              />
            </>
          )}
        </section>
      </div>
    </div>
  );
}

// ---------- Vista de progreso de un job ----------

function JobProgress({ jobId, onNew }: { jobId: string; onNew: () => void }) {
  const priv = usePrivacy((s) => s.enabled);
  const [job, setJob] = useState<OutboundJobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [polls, setPolls] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [sentList, setSentList] = useState<OutboundJobRecipientResult[] | null>(null);
  const [loadingSent, setLoadingSent] = useState(false);

  const terminal = job ? ["done", "failed", "canceled"].includes(job.status) : false;
  // Si el envío sigue "En cola" tras un rato, es que nadie lo está ejecutando.
  // Antes la pantalla sondeaba para siempre sin decir nada.
  const stuckInQueue = job?.status === "queued" && polls >= QUEUE_STUCK_POLLS;
  // Dejamos de sondear cuando ya no tiene sentido: ni terminado ni atascado.
  const stopPolling = terminal || stuckInQueue || polls >= MAX_POLLS;

  const fetchJob = useCallback(async () => {
    try {
      const d = await getOutboundJob(jobId);
      setJob(d);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudo cargar el envío"));
    }
  }, [jobId]);

  useEffect(() => {
    setPolls(0);
    void fetchJob();
  }, [fetchJob]);

  // Polling mientras no haya terminado (y con final: ni infinito ni mudo).
  useEffect(() => {
    if (stopPolling) return;
    const t = setInterval(() => {
      setPolls((p) => p + 1);
      void fetchJob();
    }, 3000);
    return () => clearInterval(t);
  }, [stopPolling, fetchJob]);

  async function onCancel() {
    setConfirmCancel(false);
    setCanceling(true);
    setActionError(null);
    try {
      await cancelOutboundJob(jobId);
      await fetchJob();
    } catch (e) {
      // Sin catch, la confirmación se cerraba, no pasaba nada visible y la
      // difusión seguía mandando WhatsApps creyendo que estaba cancelada.
      setActionError(errorDetail(e, "No se pudo cancelar el envío. Sigue en marcha."));
    } finally {
      setCanceling(false);
    }
  }

  async function onRetry() {
    setRetrying(true);
    setActionError(null);
    try {
      await retryOutboundJob(jobId);
      await fetchJob();
      setActionError(null);
      onNew();
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo reintentar el envío"));
    } finally {
      setRetrying(false);
    }
  }

  async function onExport() {
    setActionError(null);
    try {
      const blob = await downloadOutboundJobCsv(jobId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `envio-${jobId}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo descargar el CSV"));
    }
  }

  async function onLoadSent() {
    setLoadingSent(true);
    setActionError(null);
    try {
      const res = await listOutboundJobRecipients(jobId, { status: "ok", page_size: 200 });
      setSentList(res.items);
    } catch (e) {
      setActionError(errorDetail(e, "No se pudo cargar quién recibió el mensaje"));
    } finally {
      setLoadingSent(false);
    }
  }

  if (error) return <div className="text-state-bad text-sm">{error}</div>;
  if (!job)
    return (
      <div className="text-ink3 text-sm inline-flex items-center gap-1.5">
        <Loader2 className="w-4 h-4 animate-spin" /> Cargando…
      </div>
    );

  const done = job.sent_ok + job.sent_error;
  const pct = job.total > 0 ? Math.round((done / job.total) * 100) : 0;

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="font-medium text-ink">{job.template_name}</div>
          <div className="text-xs text-ink3">{job.language} · {STATUS_LABEL[job.status] ?? job.status}</div>
        </div>
        <StatusBadge status={job.status} />
      </div>

      {/* Barra de progreso */}
      <div>
        <div className="flex justify-between text-xs text-ink3 mb-1">
          <span>{done} de {job.total}</span>
          <span>{pct}%</span>
        </div>
        <div className="h-2 rounded-full bg-paper3 overflow-hidden">
          <div className="h-full bg-brand-ink transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      <div className="flex gap-4 text-sm">
        <span className="inline-flex items-center gap-1 text-state-ok">
          <CheckCircle2 className="w-4 h-4" /> {job.sent_ok} enviado(s)
        </span>
        <span className="inline-flex items-center gap-1 text-state-bad">
          <XCircle className="w-4 h-4" /> {job.sent_error} con error
        </span>
      </div>

      {job.error && <div className="text-state-bad text-xs">{job.error}</div>}
      {actionError && <div className="text-state-bad text-xs">{actionError}</div>}

      {stuckInQueue && (
        <div className="text-xs text-state-warn bg-state-warn/10 border border-state-warn/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
          <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" />
          <span className="flex-1">
            Lleva más de {Math.round((QUEUE_STUCK_POLLS * 3) / 60) || 1} min «En cola»
            sin empezar: el trabajo en segundo plano (worker) no lo está cogiendo.
            No se ha enviado nada. Arranca el worker y usa «Reintentar los que
            faltan».
          </span>
          <button type="button" onClick={() => { setPolls(0); void fetchJob(); }} className="font-semibold hover:underline shrink-0">
            Reintentar
          </button>
        </div>
      )}

      {/* Errores por destinatario */}
      {job.errors.length > 0 && (
        <div className="border-t border-line pt-2 space-y-1">
          <div className="text-xs text-ink2 font-medium">
            Errores {job.errors_total > job.errors.length && `(${job.errors.length} de ${job.errors_total})`}:
          </div>
          <ul className="space-y-1 text-xs max-h-48 overflow-auto">
            {job.errors.map((r, i) => (
              <li key={i} className="flex items-center gap-1.5 font-mono text-ink2">
                <XCircle className="w-3.5 h-3.5 text-state-bad flex-none" />
                <span className="truncate">{maskPhone(r.phone, priv)}</span>
                {r.error && <span className="text-state-bad ml-auto truncate">{r.error}</span>}
              </li>
            ))}
          </ul>
          {job.errors_truncated && (
            <p className="text-[11px] text-state-warn">
              Se muestran los {job.errors.length} primeros de {job.errors_total}.
              Descarga el CSV para verlos todos.
            </p>
          )}
        </div>
      )}

      {/* Quién SÍ recibió el mensaje. Antes no había forma de saberlo. */}
      {job.sent_ok > 0 && (
        <div className="border-t border-line pt-2 space-y-1">
          {sentList === null ? (
            <button
              type="button"
              onClick={onLoadSent}
              disabled={loadingSent}
              className="btn-ghost text-xs inline-flex items-center gap-1"
            >
              {loadingSent ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
              Ver quién lo recibió ({job.sent_ok})
            </button>
          ) : (
            <>
              <div className="text-xs text-ink2 font-medium">Entregados al proveedor:</div>
              <ul className="space-y-1 text-xs max-h-48 overflow-auto">
                {sentList.map((r, i) => (
                  <li key={i} className="flex items-center gap-1.5 font-mono text-ink2">
                    <CheckCircle2 className="w-3.5 h-3.5 text-state-ok flex-none" />
                    <span className="truncate">{maskPhone(r.phone, priv)}</span>
                    {r.sent_at && (
                      <span className="text-ink3 ml-auto">
                        {new Date(r.sent_at).toLocaleString("es-ES")}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      <ConfirmModal
        open={confirmCancel}
        title="¿Cancelar este envío masivo?"
        description={
          `Quedan ${job.pending} destinatario(s) sin enviar y se descartan. ` +
          "Los mensajes ya enviados no se pueden retirar."
        }
        confirmLabel="Cancelar envío"
        cancelLabel="Seguir enviando"
        tone="danger"
        busy={canceling}
        onConfirm={onCancel}
        onCancel={() => setConfirmCancel(false)}
      />
      <div className="flex items-center gap-2 flex-wrap pt-1">
        {!terminal && (
          <button
            type="button"
            onClick={() => setConfirmCancel(true)}
            disabled={canceling}
            className="btn-ghost text-sm inline-flex items-center gap-1 text-state-bad"
          >
            {canceling ? <Loader2 className="w-4 h-4 animate-spin" /> : <Ban className="w-4 h-4" />}
            Cancelar envío
          </button>
        )}
        {job.sent_error + job.pending > 0 && (
          <button
            type="button"
            onClick={onRetry}
            disabled={retrying || !terminal}
            title={terminal ? undefined : "Espera a que termine o cancélalo antes de reintentar"}
            className="btn-ghost text-sm inline-flex items-center gap-1"
          >
            {retrying ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            Reintentar los que faltan ({job.sent_error + job.pending})
          </button>
        )}
        <button type="button" onClick={onExport} className="btn-ghost text-sm inline-flex items-center gap-1">
          <Download className="w-4 h-4" /> Descargar CSV
        </button>
        <button type="button" onClick={onNew} className="btn-ghost text-sm inline-flex items-center gap-1 ml-auto">
          <ArrowLeft className="w-4 h-4" /> Nuevo envío
        </button>
      </div>
      {!terminal && !stuckInQueue && (
        <p className="text-[11px] text-ink3">
          El envío continúa en segundo plano aunque cierres esta pantalla.
        </p>
      )}
      {stopPolling && !terminal && (
        <p className="text-[11px] text-ink3">
          Se ha dejado de actualizar solo.{" "}
          <button type="button" onClick={() => { setPolls(0); void fetchJob(); }} className="underline">
            Actualizar ahora
          </button>
        </p>
      )}
    </div>
  );
}

// ---------- Lista de envíos recientes ----------

function RecentJobs({ onOpen }: { onOpen: (id: string) => void }) {
  const [jobs, setJobs] = useState<OutboundJob[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState(false);
  // Sin esto, un fallo de la API hacía desaparecer la sección sin ningún aviso.
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await listOutboundJobs(page, HISTORY_PAGE_SIZE);
      setJobs(res.items);
      setTotal(res.total);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar los envíos recientes."));
    }
  }, [page]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // El histórico se congelaba hasta recargar la página: un envío en curso se
  // quedaba en "En cola" en la lista aunque ya fuera por la mitad.
  useEffect(() => {
    if (!open) return;
    const t = setInterval(() => void refresh(), 10000);
    return () => clearInterval(t);
  }, [open, refresh]);

  const totalPages = Math.max(1, Math.ceil(total / HISTORY_PAGE_SIZE));

  if (error) {
    return (
      <div className="border-t border-line pt-2 mt-1">
        <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
          <span className="flex-1">{error}</span>
          <button type="button" onClick={() => void refresh()} className="font-semibold hover:underline shrink-0">
            Reintentar
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="border-t border-line pt-2 mt-1">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-1.5 text-xs text-ink3 hover:text-ink"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        <History className="w-3.5 h-3.5" /> Envíos recientes
      </button>
      {open && (
        <ul className="space-y-1 mt-1.5">
          {/* Con cero envíos la sección entera desaparecía, así que quien
              buscaba "dónde veo lo que ya he enviado" no encontraba ni el
              sitio donde mirar. */}
          {jobs.length === 0 && (
            <li className="px-1.5 py-2 text-[11px] text-ink3 italic">
              Todavía no has hecho ningún envío. Aquí irán apareciendo, con su estado y cuántos
              mensajes salieron bien.
            </li>
          )}
          {jobs.map((j) => (
            <li key={j.id}>
              <button
                type="button"
                onClick={() => onOpen(j.id)}
                className="w-full text-left p-1.5 rounded-coro-sm hover:bg-paper2 text-xs"
              >
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-ink2 flex-1">{j.template_name}</span>
                  <StatusBadge status={j.status} small />
                </div>
                <div className="text-[10px] text-ink3">
                  {j.sent_ok}✓ {j.sent_error}✗ de {j.total} ·{" "}
                  {new Date(j.created_at).toLocaleDateString("es-ES", {
                    day: "2-digit",
                    month: "2-digit",
                    year: "2-digit",
                  })}
                </div>
              </button>
            </li>
          ))}
          {totalPages > 1 && (
            <li className="flex items-center justify-between text-[10px] text-ink3 pt-1">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="btn-ghost text-[10px] disabled:opacity-40"
              >
                ← Anterior
              </button>
              <span>
                {page} / {totalPages}
              </span>
              <button
                type="button"
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="btn-ghost text-[10px] disabled:opacity-40"
              >
                Siguiente →
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

// ---------- Bajas permanentes ----------

/**
 * Quién ha pedido no recibir difusiones. Esto NO es la blocklist de abuso: esa
 * caduca (24 h automática, 30 días manual) y vive en Redis, así que quien pedía
 * la baja volvía a entrar en la campaña de dentro de dos meses. Estas bajas no
 * caducan y se respetan en todo envío masivo.
 */
function OptOutsPanel() {
  const priv = usePrivacy((s) => s.enabled);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<OutboundOptOut[]>([]);
  const [total, setTotal] = useState(0);
  const [phone, setPhone] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await listOutboundOptOuts({ page_size: 20 });
      setItems(res.items);
      setTotal(res.total);
      setError(null);
    } catch (e) {
      setError(errorDetail(e, "No se pudieron cargar las bajas."));
    }
  }, []);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  async function onAdd() {
    if (!phone.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await createOutboundOptOut(phone.trim(), "Alta manual desde el panel");
      setPhone("");
      await refresh();
    } catch (e) {
      setError(errorDetail(e, "No se pudo dar de baja"));
    } finally {
      setBusy(false);
    }
  }

  async function onRemove(p: string) {
    setBusy(true);
    setError(null);
    try {
      await deleteOutboundOptOut(p);
      await refresh();
    } catch (e) {
      setError(errorDetail(e, "No se pudo reactivar"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-t border-line pt-2 mt-1">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-1.5 text-xs text-ink3 hover:text-ink"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        <Ban className="w-3.5 h-3.5" /> Bajas de difusiones{open && total > 0 && ` (${total})`}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5">
          <p className="text-[10px] text-ink3">
            No caducan. Quien está aquí no entra en ninguna campaña.
          </p>
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+34600000000"
              className="input text-xs flex-1"
            />
            <button
              type="button"
              onClick={onAdd}
              disabled={busy || !phone.trim()}
              className="btn-ghost text-xs"
            >
              Dar de baja
            </button>
          </div>
          {error && <div className="text-[10px] text-state-bad">{error}</div>}
          <ul className="space-y-0.5 max-h-40 overflow-auto">
            {items.map((o) => (
              <li key={o.phone} className="flex items-center gap-1.5 text-[11px]">
                <span className="font-mono text-ink2 truncate flex-1">
                  {maskPhone(o.phone, priv)}
                </span>
                <span className="text-ink3">{OPTOUT_SOURCE_LABEL[o.source] ?? o.source}</span>
                <button
                  type="button"
                  onClick={() => onRemove(o.phone)}
                  disabled={busy}
                  title="Reactivar (solo si lo pide esa persona)"
                  className="text-ink3 hover:text-state-bad"
                >
                  <X className="w-3 h-3" />
                </button>
              </li>
            ))}
          </ul>
          {items.length === 0 && (
            <div className="text-[10px] text-ink3 italic">Nadie de baja todavía.</div>
          )}
        </div>
      )}
    </div>
  );
}

const OPTOUT_SOURCE_LABEL: Record<string, string> = {
  manual: "manual",
  reply: "pidió la baja",
  import: "importada",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "En cola",
  running: "Enviando",
  done: "Completado",
  canceled: "Cancelado",
  failed: "Fallido",
};

function StatusBadge({ status, small }: { status: string; small?: boolean }) {
  const cls =
    status === "done"
      ? "bg-state-ok/15 text-state-ok"
      : status === "running" || status === "queued"
        ? "bg-brand-soft text-brand-ink"
        : status === "failed"
          ? "bg-state-bad/15 text-state-bad"
          : "bg-paper3 text-ink3";
  return <span className={`pill ${small ? "text-[10px]" : "text-xs"} ${cls}`}>{STATUS_LABEL[status] ?? status}</span>;
}

// ---------- helpers ----------

function padOrTrim(arr: string[], targetLen: number, fill: string): string[] {
  if (arr.length === targetLen) return arr;
  if (arr.length > targetLen) return arr.slice(0, targetLen);
  return [...arr, ...new Array(targetLen - arr.length).fill(fill)];
}

function mergeVars(saved: TemplateVar[], count: number): TemplateVar[] {
  const byIdx = new Map(saved.map((v) => [v.idx, v]));
  const out: TemplateVar[] = [];
  for (let idx = 1; idx <= count; idx++) {
    const s = byIdx.get(idx);
    out.push({ idx, nombre: s?.nombre ?? "", contact_field: s?.contact_field ?? null });
  }
  return out;
}

function hasSlots(body: string | null | undefined): boolean {
  return !!body && /\{\{\s*\d+\s*\}\}/.test(body);
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = seconds / 60;
  if (mins < 60) return `${Math.round(mins)} min`;
  const hours = Math.floor(mins / 60);
  const rem = Math.round(mins % 60);
  return rem ? `${hours}h ${rem}min` : `${hours}h`;
}
