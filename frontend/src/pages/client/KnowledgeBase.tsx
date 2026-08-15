import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Upload,
  FileText,
  Trash2,
  Search,
  AlertCircle,
  CheckCircle2,
  X,
  Loader2,
  Pencil,
  History,
  PenLine,
  RefreshCw,
} from "lucide-react";
import clsx from "clsx";
import { PageHeader } from "@/components/PageHeader";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  createNote,
  deleteDocument,
  getDocumentChunks,
  getDocumentContent,
  listDocuments,
  listDocumentVersions,
  restoreDocumentVersion,
  searchKB,
  updateDocumentContent,
  uploadDocuments,
  type DocumentChunk,
  type DocumentVersion,
  type KBSearchResult,
  type UploadResult,
} from "@/services/kb";
import { api } from "@/services/api";
import { errorDetail } from "@/lib/errors";
import type { Document } from "@/types";

const EDITABLE_FORMATS = new Set(["txt", "md"]);

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const STATUS_LABEL: Record<string, string> = {
  procesando: "procesando",
  indexado: "indexado",
  // Troceado y guardado, pero SIN vectores reales: se encuentra por palabras,
  // no por significado. Antes esto salía en verde como si estuviera bien.
  indexado_sin_semantica: "sin búsqueda semántica",
  error: "error",
};

function statusClass(status: string): string {
  switch (status) {
    case "indexado":
      return "bg-state-ok/15 text-state-ok";
    case "procesando":
    case "indexado_sin_semantica":
      return "bg-state-warn/15 text-state-warn";
    case "error":
      return "bg-state-bad/15 text-state-bad";
    default:
      return "bg-paper2 text-ink3";
  }
}

// Estados que se arreglan reindexando (es lo que hay que hacer después de
// configurar la clave de OpenAI).
const NEEDS_REINDEX = new Set(["indexado_sin_semantica", "error"]);

// Llamadas a los endpoints de reindexado. Viven aquí y no en `services/kb.ts`
// para no tocar ese fichero; muévelas allí cuando se pueda.
interface KBStatus {
  embeddings_configurados: boolean;
  total: number;
  indexados: number;
  sin_semantica: number;
  procesando: number;
  con_error: number;
}

async function fetchKBStatus(): Promise<KBStatus> {
  const { data } = await api.get<KBStatus>("/kb/status");
  return data;
}

async function reindexDocument(id: string): Promise<void> {
  await api.post(`/kb/documents/${id}/reindex`);
}

async function reindexPending(): Promise<{ reindexados: number; detalle: string }> {
  const { data } = await api.post<{ reindexados: number; detalle: string }>(
    "/kb/reindex",
    { todos: false },
  );
  return data;
}

export default function KnowledgeBase() {
  const [docs, setDocs] = useState<Document[]>([]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [lastResults, setLastResults] = useState<UploadResult[]>([]);
  const [dragActive, setDragActive] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [chunks, setChunks] = useState<DocumentChunk[]>([]);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [chunksError, setChunksError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KBSearchResult[]>([]);
  const [searching, setSearching] = useState(false);

  const [toDelete, setToDelete] = useState<Document | null>(null);
  const [deleting, setDeleting] = useState(false);

  // "Añadir texto": nota sin archivo → documento .txt indexado.
  const [noteOpen, setNoteOpen] = useState(false);
  const [noteTitle, setNoteTitle] = useState("");
  const [noteText, setNoteText] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteError, setNoteError] = useState<string | null>(null);

  // Panel derecho: chunks (por defecto), edición o historial de versiones.
  const [viewMode, setViewMode] = useState<"chunks" | "edit" | "history">("chunks");
  const [editText, setEditText] = useState("");
  const [editLoading, setEditLoading] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [versions, setVersions] = useState<DocumentVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [toRestore, setToRestore] = useState<DocumentVersion | null>(null);
  const [restoring, setRestoring] = useState(false);

  // Salud de la KB + reindexado.
  const [kbStatus, setKbStatus] = useState<KBStatus | null>(null);
  const [reindexing, setReindexing] = useState<string | null>(null);
  const [reindexMsg, setReindexMsg] = useState<string | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  async function refresh() {
    const d = await listDocuments();
    setDocs(d);
    // Sin bloquear la lista si el endpoint de estado falla.
    try {
      setKbStatus(await fetchKBStatus());
    } catch {
      /* estado no crítico */
    }
  }

  // Refresco periódico: la indexación va en segundo plano, así que el estado
  // del documento cambia solo (procesando → indexado / error).
  useEffect(() => {
    void refresh();
    const i = setInterval(refresh, 5000);
    return () => clearInterval(i);
  }, []);

  async function onReindexDoc(d: Document) {
    setReindexing(d.id);
    setReindexMsg(null);
    try {
      await reindexDocument(d.id);
      setReindexMsg(`«${d.nombre}» se está reindexando.`);
      await refresh();
    } catch (e: unknown) {
      setReindexMsg(errorDetail(e, "No se pudo reindexar"));
    } finally {
      setReindexing(null);
    }
  }

  async function onReindexPending() {
    setReindexing("all");
    setReindexMsg(null);
    try {
      const r = await reindexPending();
      setReindexMsg(r.detalle);
      await refresh();
    } catch (e: unknown) {
      setReindexMsg(errorDetail(e, "No se pudo reindexar"));
    } finally {
      setReindexing(null);
    }
  }

  const loadChunks = useCallback(async (id: string) => {
    setChunksLoading(true);
    setChunksError(null);
    try {
      const c = await getDocumentChunks(id);
      setChunks(c);
    } catch (e) {
      // Sin catch, un fallo al consultar dejaba la lista vacía y la pantalla
      // daba a entender que el documento no se había indexado: la reacción
      // lógica era volver a subirlo, cuando el documento estaba bien.
      setChunks([]);
      setChunksError(errorDetail(e, "No se pudo consultar el contenido indexado del documento."));
    } finally {
      setChunksLoading(false);
    }
  }, []);

  // Carga chunks cuando cambia el seleccionado (y vuelve al modo chunks).
  useEffect(() => {
    setViewMode("chunks");
    setEditError(null);
    if (!selectedId) {
      setChunks([]);
      return;
    }
    void loadChunks(selectedId);
  }, [selectedId, loadChunks]);

  // Mismo tope que el backend (MAX_FILE_BYTES en knowledge_base.py): rechazar
  // aquí evita subir 200 MB enteros solo para recibir un 413.
  const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

  async function uploadFiles(files: File[]) {
    if (files.length === 0) return;
    const tooBig: UploadResult[] = files
      .filter((f) => f.size > MAX_UPLOAD_BYTES)
      .map((f) => ({ file: f, ok: false, error: "Supera el máximo de 25 MB" }));
    const ok = files.filter((f) => f.size <= MAX_UPLOAD_BYTES);
    if (ok.length === 0) {
      setLastResults(tooBig);
      return;
    }
    setUploading(true);
    setLastResults([]);
    setProgress({ done: 0, total: ok.length });
    try {
      const r = await uploadDocuments(ok, (done, total) =>
        setProgress({ done, total })
      );
      setLastResults([...tooBig, ...r]);
      await refresh();
    } finally {
      setUploading(false);
      setProgress(null);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function onPickFiles(e: React.ChangeEvent<HTMLInputElement>) {
    const list = e.target.files;
    if (!list) return;
    void uploadFiles(Array.from(list));
  }

  function onDrop(e: React.DragEvent<HTMLLabelElement>) {
    e.preventDefault();
    setDragActive(false);
    const list = e.dataTransfer.files;
    if (!list) return;
    void uploadFiles(Array.from(list));
  }

  async function onConfirmDelete() {
    if (!toDelete) return;
    setDeleting(true);
    try {
      await deleteDocument(toDelete.id);
      if (selectedId === toDelete.id) setSelectedId(null);
      setToDelete(null);
      await refresh();
    } finally {
      setDeleting(false);
    }
  }

  async function onSearch() {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const r = await searchKB(query.trim(), 5);
      setResults(r);
    } finally {
      setSearching(false);
    }
  }

  const selectedDoc = useMemo(
    () => docs.find((d) => d.id === selectedId) || null,
    [docs, selectedId]
  );
  const selectedEditable = !!selectedDoc && EDITABLE_FORMATS.has(selectedDoc.formato);

  async function onSaveNote() {
    if (!noteTitle.trim() || !noteText.trim()) return;
    setNoteSaving(true);
    setNoteError(null);
    try {
      await createNote(noteTitle.trim(), noteText.trim());
      setNoteOpen(false);
      setNoteTitle("");
      setNoteText("");
      await refresh();
    } catch (e) {
      setNoteError(errorDetail(e, "No se pudo guardar la nota."));
    } finally {
      setNoteSaving(false);
    }
  }

  async function onStartEdit() {
    if (!selectedId) return;
    setViewMode("edit");
    setEditLoading(true);
    setEditError(null);
    try {
      const c = await getDocumentContent(selectedId);
      setEditText(c.contenido);
    } catch (e) {
      setEditError(errorDetail(e, "No se pudo cargar el contenido."));
    } finally {
      setEditLoading(false);
    }
  }

  async function onSaveEdit() {
    if (!selectedId || !editText.trim()) return;
    setEditSaving(true);
    setEditError(null);
    try {
      await updateDocumentContent(selectedId, editText.trim());
      setViewMode("chunks");
      await refresh();
      await loadChunks(selectedId);
    } catch (e) {
      setEditError(errorDetail(e, "No se pudo guardar el documento."));
    } finally {
      setEditSaving(false);
    }
  }

  async function onShowHistory() {
    if (!selectedId) return;
    setViewMode("history");
    setVersionsLoading(true);
    setEditError(null);
    try {
      const v = await listDocumentVersions(selectedId);
      setVersions(v);
    } catch (e) {
      setEditError(errorDetail(e, "No se pudo cargar el historial."));
    } finally {
      setVersionsLoading(false);
    }
  }

  async function onConfirmRestore() {
    if (!selectedId || !toRestore) return;
    setRestoring(true);
    try {
      await restoreDocumentVersion(selectedId, toRestore.id);
      setToRestore(null);
      setViewMode("chunks");
      await refresh();
      await loadChunks(selectedId);
    } catch (e) {
      setEditError(errorDetail(e, "No se pudo restaurar la versión."));
      setToRestore(null);
    } finally {
      setRestoring(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        eyebrow="Agente IA"
        title={<>Base de <span className="accent">conocimiento</span></>}
        description="Documentos que el agente consulta cuando un cliente pregunta algo. Cada documento se trocea en chunks y se indexa con embeddings."
      />

      <div className="flex-1 overflow-y-auto lg:overflow-hidden p-4 md:p-6 flex flex-col gap-4">
        {/* Salud de la KB. Sin clave de embeddings los documentos se indexan
            con vectores de ceros: el agente los encuentra por palabras sueltas
            pero no por significado. Antes esto no se decía en ninguna parte y
            la KB quedaba "en verde". */}
        {kbStatus && (kbStatus.sin_semantica > 0 || !kbStatus.embeddings_configurados) && (
          <div className="shrink-0 rounded-coro-sm border border-state-warn/40 bg-state-warn/10 px-4 py-3 flex items-start gap-3">
            <AlertCircle className="w-4 h-4 text-state-warn shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0 text-xs text-ink2 space-y-1">
              {!kbStatus.embeddings_configurados && (
                <p>
                  <b>No hay clave de OpenAI configurada.</b> Lo que subas ahora
                  se guardará, pero el agente solo lo encontrará por coincidencia
                  de palabras, no por significado. Ponla en{" "}
                  <b>Admin → Credenciales</b>.
                </p>
              )}
              {kbStatus.sin_semantica > 0 && (
                <p>
                  <b>{kbStatus.sin_semantica}</b> documento(s) están indexados sin
                  búsqueda semántica.{" "}
                  {kbStatus.embeddings_configurados
                    ? "Ya tienes la clave puesta: reindéxalos para que funcionen del todo."
                    : "Cuando pongas la clave, reindéxalos desde aquí."}
                </p>
              )}
              {reindexMsg && <p className="text-ink3 italic">{reindexMsg}</p>}
            </div>
            <button
              type="button"
              onClick={() => void onReindexPending()}
              disabled={reindexing !== null}
              className="btn-ghost text-xs shrink-0"
            >
              {reindexing === "all" ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RefreshCw className="w-3.5 h-3.5" />
              )}
              Reindexar pendientes
            </button>
          </div>
        )}

        {/* Upload + buscador (full width) */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 shrink-0">
          {/* Drop zone */}
          <div className="card p-4 lg:col-span-2">
            <label
              onDragOver={(e) => {
                e.preventDefault();
                setDragActive(true);
              }}
              onDragLeave={() => setDragActive(false)}
              onDrop={onDrop}
              className={clsx(
                "flex items-center gap-3 border-2 border-dashed rounded-coro-sm py-4 px-4 cursor-pointer transition",
                dragActive
                  ? "border-brand bg-brand/10"
                  : "border-line hover:border-ink4 hover:bg-paper2",
                uploading && "opacity-60 pointer-events-none"
              )}
            >
              <Upload className="w-5 h-5 text-ink3 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-ink truncate">
                  {uploading
                    ? `Subiendo ${progress?.done ?? 0}/${progress?.total ?? 0}…`
                    : "Suelta archivos aquí o haz click"}
                </div>
                <div className="text-[11px] text-ink3 mt-0.5">
                  Varios a la vez. PDF, DOCX, TXT, MD, CSV, XLSX. Máx 25 MB.
                </div>
              </div>
              <input
                ref={inputRef}
                type="file"
                multiple
                accept=".pdf,.docx,.txt,.md,.csv,.xlsx"
                onChange={onPickFiles}
                hidden
              />
            </label>
            <div className="mt-2">
              <button
                type="button"
                onClick={() => {
                  setNoteError(null);
                  setNoteOpen(true);
                }}
                className="btn-ghost text-xs"
              >
                <PenLine className="w-3.5 h-3.5" /> Añadir texto sin archivo
              </button>
            </div>
            {lastResults.length > 0 && !uploading && (
              <div className="mt-3 space-y-1 max-h-32 overflow-auto">
                {lastResults.map((r, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs">
                    {r.ok ? (
                      <CheckCircle2 className="w-3.5 h-3.5 text-state-ok shrink-0" />
                    ) : (
                      <AlertCircle className="w-3.5 h-3.5 text-state-bad shrink-0" />
                    )}
                    <span className="font-medium text-ink2 truncate">{r.file.name}</span>
                    {!r.ok && <span className="text-state-bad">{r.error}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Buscador */}
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-2">
              <Search className="w-4 h-4 text-brand-ink" />
              <h3 className="font-semibold text-ink text-sm">Probar el RAG</h3>
            </div>
            <div className="flex gap-2">
              <input
                className="input flex-1"
                placeholder="¿Qué incluye el plan premium?"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && onSearch()}
              />
              <button
                onClick={onSearch}
                className="btn-primary"
                disabled={searching}
              >
                {searching ? "..." : "Probar"}
              </button>
            </div>
            {results.length > 0 && (
              <div className="mt-3 space-y-1.5 max-h-32 overflow-auto">
                {results.map((r) => (
                  <div
                    key={r.chunk_id}
                    className="text-[11px] bg-paper2 rounded p-2"
                  >
                    <div className="flex items-center gap-2 text-ink3 mb-1">
                      <span className="font-mono">
                        {(r.similarity * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div className="text-ink2 line-clamp-2">{r.contenido}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Split: lista documentos / chunks del seleccionado */}
        <div className="flex-1 grid grid-cols-1 lg:grid-cols-3 gap-4 lg:overflow-hidden min-h-0">
          {/* Lista docs */}
          <div className="card overflow-hidden flex flex-col min-h-[16rem] lg:min-h-0">
            <div className="px-4 py-3 border-b border-line bg-paper2 flex items-center justify-between">
              <h3 className="font-semibold text-sm text-ink">Documentos</h3>
              <span className="font-mono text-[11px] text-ink3">
                {docs.length}
              </span>
            </div>
            <div className="flex-1 overflow-auto divide-y divide-line2">
              {docs.length === 0 && (
                <div className="px-4 py-8 text-center text-ink3 text-sm italic font-display">
                  Sin documentos. Sube alguno arriba.
                </div>
              )}
              {docs.map((d) => (
                <button
                  key={d.id}
                  type="button"
                  onClick={() => setSelectedId(d.id)}
                  className={clsx(
                    "w-full px-4 py-3 text-left transition-colors flex items-start gap-3",
                    selectedId === d.id
                      ? "bg-brand/10"
                      : "hover:bg-paper2"
                  )}
                >
                  <FileText
                    className={
                      "w-4 h-4 shrink-0 mt-0.5 " +
                      (selectedId === d.id ? "text-brand-ink" : "text-ink3")
                    }
                  />
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm text-ink truncate">
                      {d.nombre}
                    </div>
                    <div className="text-[11px] text-ink3 mt-0.5 flex items-center gap-2 flex-wrap">
                      <span className="font-mono uppercase">{d.formato}</span>
                      <span>·</span>
                      <span className="font-mono">
                        {formatBytes(d.tamano_bytes)}
                      </span>
                      <span>·</span>
                      <span className="font-mono">{d.num_chunks} chunks</span>
                    </div>
                    {d.error_msg && (
                      <div className="text-[11px] text-state-bad mt-1 line-clamp-2">
                        {d.error_msg}
                      </div>
                    )}
                  </div>
                  <div className="flex flex-col items-end gap-1 shrink-0">
                    <span
                      className={
                        "badge text-[10px] border-transparent " +
                        statusClass(d.status)
                      }
                    >
                      {STATUS_LABEL[d.status] || d.status}
                    </span>
                    <div className="flex items-center">
                      {/* Reindexar: única forma de recuperar un PDF/Word que se
                          indexó sin clave de embeddings o que falló. Antes solo
                          se reindexaba editando un fichero de texto, así que la
                          salida era borrarlo y volver a subirlo. */}
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          void onReindexDoc(d);
                        }}
                        disabled={reindexing !== null}
                        className={clsx(
                          "p-2 disabled:opacity-40",
                          NEEDS_REINDEX.has(d.status)
                            ? "text-state-warn hover:text-ink"
                            : "text-ink3 hover:text-ink",
                        )}
                        title="Reindexar este documento"
                        aria-label="Reindexar"
                      >
                        {reindexing === d.id ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <RefreshCw className="w-3.5 h-3.5" />
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setToDelete(d);
                        }}
                        className="text-ink3 hover:text-state-bad p-2"
                        aria-label="Borrar"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Viewer chunks */}
          <div className="card overflow-hidden flex flex-col min-h-[16rem] lg:min-h-0 lg:col-span-2">
            <div className="px-4 py-3 border-b border-line bg-paper2 flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                {selectedDoc ? (
                  <>
                    <h3 className="font-semibold text-sm text-ink truncate">
                      {selectedDoc.nombre}
                    </h3>
                    <div className="text-[11px] text-ink3 font-mono">
                      {chunks.length} chunks · {formatBytes(selectedDoc.tamano_bytes)}
                    </div>
                  </>
                ) : (
                  <h3 className="font-semibold text-sm text-ink3 italic font-display">
                    Selecciona un documento para ver su contenido
                  </h3>
                )}
              </div>
              {selectedDoc && (
                <div className="flex items-center gap-1 shrink-0">
                  {selectedEditable && viewMode === "chunks" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void onStartEdit()}
                        className="btn-ghost text-xs"
                      >
                        <Pencil className="w-3.5 h-3.5" /> Editar
                      </button>
                      <button
                        type="button"
                        onClick={() => void onShowHistory()}
                        className="btn-ghost text-xs"
                      >
                        <History className="w-3.5 h-3.5" /> Historial
                      </button>
                    </>
                  )}
                  {viewMode !== "chunks" && (
                    <button
                      type="button"
                      onClick={() => setViewMode("chunks")}
                      className="btn-ghost text-xs"
                      disabled={editSaving || restoring}
                    >
                      Volver a chunks
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setSelectedId(null)}
                    className="btn-ghost btn-sm"
                    aria-label="Cerrar"
                  >
                    <X />
                  </button>
                </div>
              )}
            </div>
            <div className="flex-1 overflow-auto p-4">
              {!selectedDoc ? (
                <div className="h-full flex items-center justify-center text-ink3 text-sm italic font-display">
                  Aquí verás los chunks (trozos indexados) del documento.
                </div>
              ) : viewMode === "edit" ? (
                <div className="h-full flex flex-col gap-2 min-h-[16rem]">
                  {editError && (
                    <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2">
                      {editError}
                    </div>
                  )}
                  {editLoading ? (
                    <div className="flex items-center justify-center py-12 text-ink3 text-sm gap-2">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Cargando contenido…
                    </div>
                  ) : (
                    <>
                      <textarea
                        className="input flex-1 font-mono text-xs resize-none min-h-[14rem]"
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                        aria-label="Contenido del documento"
                      />
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[11px] text-ink3">
                          Al guardar se reindexa automáticamente y la versión anterior queda en el historial.
                        </span>
                        <div className="flex gap-2 shrink-0">
                          <button
                            type="button"
                            className="btn"
                            onClick={() => setViewMode("chunks")}
                            disabled={editSaving}
                          >
                            Cancelar
                          </button>
                          <button
                            type="button"
                            className="btn-primary"
                            onClick={() => void onSaveEdit()}
                            disabled={editSaving || !editText.trim()}
                          >
                            {editSaving ? "Guardando…" : "Guardar y reindexar"}
                          </button>
                        </div>
                      </div>
                    </>
                  )}
                </div>
              ) : viewMode === "history" ? (
                <div className="space-y-3">
                  {editError && (
                    <div className="text-xs text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2">
                      {editError}
                    </div>
                  )}
                  {versionsLoading ? (
                    <div className="flex items-center justify-center py-12 text-ink3 text-sm gap-2">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Cargando historial…
                    </div>
                  ) : versions.length === 0 ? (
                    <div className="text-center text-ink3 text-sm italic font-display py-12">
                      Sin versiones anteriores. Se guardan automáticamente al editar.
                    </div>
                  ) : (
                    versions.map((v) => (
                      <div
                        key={v.id}
                        className="border border-line rounded-coro-sm bg-paper2 overflow-hidden"
                      >
                        <div className="px-3 py-1.5 bg-paper3/60 border-b border-line2 flex items-center justify-between gap-2 text-[11px] text-ink3">
                          <span className="font-mono">
                            {new Date(v.created_at).toLocaleString()}
                          </span>
                          <button
                            type="button"
                            className="btn-ghost text-xs"
                            onClick={() => setToRestore(v)}
                            disabled={restoring}
                          >
                            <History className="w-3.5 h-3.5" /> Restaurar
                          </button>
                        </div>
                        <div className="px-3 py-2 text-xs text-ink2 whitespace-pre-wrap break-words max-h-40 overflow-auto">
                          {v.contenido}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              ) : chunksLoading ? (
                <div className="flex items-center justify-center py-12 text-ink3 text-sm gap-2">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Cargando chunks…
                </div>
              ) : chunksError ? (
                <div className="text-sm text-state-bad bg-state-bad/10 border border-state-bad/30 rounded-coro-sm px-3 py-2 flex items-start gap-2">
                  <span className="flex-1">{chunksError}</span>
                  <button
                    type="button"
                    onClick={() => void loadChunks(selectedDoc.id)}
                    className="font-semibold hover:underline shrink-0"
                  >
                    Reintentar
                  </button>
                </div>
              ) : chunks.length === 0 ? (
                <div className="text-center text-ink3 text-sm italic font-display py-12">
                  {selectedDoc.status === "indexado"
                    ? "Este documento no tiene chunks."
                    : "El documento no se ha indexado todavía."}
                </div>
              ) : (
                <div className="space-y-3">
                  {chunks.map((c, i) => (
                    <div
                      key={c.id}
                      className="border border-line rounded-coro-sm bg-paper2 overflow-hidden"
                    >
                      <div className="px-3 py-1.5 bg-paper3/60 border-b border-line2 flex items-center justify-between text-[11px] text-ink3 font-mono">
                        <span>chunk #{i + 1}</span>
                        {c.metadata && Object.keys(c.metadata).length > 0 && (
                          <span className="flex items-center gap-2 flex-wrap">
                            {Object.entries(c.metadata).map(([k, v]) => (
                              <span key={k}>
                                {k}={String(v)}
                              </span>
                            ))}
                          </span>
                        )}
                      </div>
                      <div className="px-3 py-2 text-sm text-ink whitespace-pre-wrap break-words">
                        {c.contenido}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <ConfirmModal
        open={!!toDelete}
        tone="danger"
        title={<>Borrar documento</>}
        description={
          <>
            Vas a borrar <span className="font-semibold text-ink2">{toDelete?.nombre}</span> y todos sus chunks. El agente dejará de tener acceso a esta información inmediatamente.
            <br />
            <span className="block mt-2 text-ink2 font-medium">
              ¿Seguro que quieres continuar?
            </span>
          </>
        }
        confirmLabel="Sí, borrar"
        cancelLabel="Cancelar"
        busy={deleting}
        onConfirm={onConfirmDelete}
        onCancel={() => setToDelete(null)}
      />

      <ConfirmModal
        open={!!toRestore}
        title={<>Restaurar versión</>}
        description={
          <>
            El contenido actual se sustituirá por la versión del{" "}
            <span className="font-semibold text-ink2">
              {toRestore ? new Date(toRestore.created_at).toLocaleString() : ""}
            </span>{" "}
            y se reindexará. El contenido actual se guardará en el historial, así que
            este paso también es reversible.
          </>
        }
        confirmLabel="Restaurar"
        cancelLabel="Cancelar"
        busy={restoring}
        onConfirm={() => void onConfirmRestore()}
        onCancel={() => setToRestore(null)}
      />

      {noteOpen && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center p-4"
          role="dialog"
          aria-modal="true"
        >
          <div
            className="absolute inset-0 bg-ink/50 backdrop-blur-[2px]"
            onClick={() => !noteSaving && setNoteOpen(false)}
            aria-hidden="true"
          />
          <div className="relative bg-card border border-line rounded-coro shadow-coro-3 w-full max-w-lg p-5 sm:p-6">
            <button
              type="button"
              aria-label="Cerrar"
              onClick={() => setNoteOpen(false)}
              disabled={noteSaving}
              className="absolute top-3 right-3 inline-flex items-center justify-center w-8 h-8 rounded-coro-sm hover:bg-paper2 text-ink3"
            >
              <X className="w-4 h-4" />
            </button>
            <h2 className="font-display text-xl text-ink leading-tight pr-6">
              Añadir texto a la base de conocimiento
            </h2>
            <p className="text-sm text-ink3 mt-1 mb-3">
              Se guarda como documento de texto y se indexa al momento. Podrás editarlo
              o borrarlo desde la lista de documentos.
            </p>
            <input
              className="input w-full mb-2"
              placeholder="Título (p. ej. Envíos a Canarias)"
              aria-label="Título de la nota"
              maxLength={200}
              value={noteTitle}
              onChange={(e) => setNoteTitle(e.target.value)}
            />
            <textarea
              className="input w-full h-48 resize-none text-sm"
              placeholder="Contenido que el agente podrá usar en sus respuestas…"
              aria-label="Contenido de la nota"
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
            />
            {noteError && (
              <div className="text-xs text-state-bad mt-2">{noteError}</div>
            )}
            <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 mt-4">
              <button
                type="button"
                className="btn"
                onClick={() => setNoteOpen(false)}
                disabled={noteSaving}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={() => void onSaveNote()}
                disabled={noteSaving || !noteTitle.trim() || !noteText.trim()}
              >
                {noteSaving ? "Guardando…" : "Guardar e indexar"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
