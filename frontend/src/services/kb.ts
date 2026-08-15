import { api } from "@/services/api";
import type { Document } from "@/types";

export async function listDocuments(): Promise<Document[]> {
  const { data } = await api.get<Document[]>("/kb/documents");
  return data;
}

export async function uploadDocument(file: File): Promise<Document> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<Document>("/kb/documents", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export interface UploadResult {
  file: File;
  ok: boolean;
  document?: Document;
  error?: string;
}

/**
 * Sube varios documentos a la vez al endpoint individual existente.
 * Concurrencia limitada (3 en paralelo) para no saturar el backend ni el navegador.
 * Devuelve un resultado por archivo, sin lanzar excepcion: los fallos se reportan en el resultado.
 */
export async function uploadDocuments(
  files: File[],
  onProgress?: (done: number, total: number) => void,
): Promise<UploadResult[]> {
  const CONCURRENCY = 3;
  const results: UploadResult[] = new Array(files.length);
  let nextIndex = 0;
  let done = 0;

  async function worker(): Promise<void> {
    while (true) {
      const idx = nextIndex++;
      if (idx >= files.length) return;
      const f = files[idx];
      try {
        const doc = await uploadDocument(f);
        results[idx] = { file: f, ok: true, document: doc };
      } catch (e: unknown) {
        const msg =
          (e && typeof e === "object" && "response" in e
            ? (e as { response?: { data?: { detail?: string } } }).response?.data?.detail
            : null) || (e as Error)?.message || "Error";
        results[idx] = { file: f, ok: false, error: msg };
      }
      done++;
      onProgress?.(done, files.length);
    }
  }

  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, files.length) }, () => worker()));
  return results;
}

export async function deleteDocument(id: string): Promise<void> {
  await api.delete(`/kb/documents/${id}`);
}

export interface KBSearchResult {
  chunk_id: string;
  document_id: string;
  contenido: string;
  similarity: number;
  metadata?: Record<string, unknown>;
}

export async function searchKB(query: string, top_k = 5): Promise<KBSearchResult[]> {
  const { data } = await api.post<KBSearchResult[]>("/kb/search", { query, top_k });
  return data;
}


export interface DocumentChunk {
  id: string;
  contenido: string;
  metadata?: Record<string, unknown> | null;
}

export async function getDocumentChunks(documentId: string): Promise<DocumentChunk[]> {
  const { data } = await api.get<DocumentChunk[]>(`/kb/documents/${documentId}/chunks`);
  return data;
}

// ── Edición de documentos de texto + versionado ─────────────────────────────

export async function createNote(titulo: string, contenido: string): Promise<Document> {
  const { data } = await api.post<Document>("/kb/notes", { titulo, contenido });
  return data;
}

export interface DocumentContent {
  id: string;
  nombre: string;
  formato: string;
  contenido: string;
}

export async function getDocumentContent(documentId: string): Promise<DocumentContent> {
  const { data } = await api.get<DocumentContent>(`/kb/documents/${documentId}/content`);
  return data;
}

export async function updateDocumentContent(
  documentId: string,
  contenido: string,
): Promise<Document> {
  const { data } = await api.put<Document>(`/kb/documents/${documentId}`, { contenido });
  return data;
}

export interface DocumentVersion {
  id: string;
  contenido: string;
  created_at: string;
}

export async function listDocumentVersions(documentId: string): Promise<DocumentVersion[]> {
  const { data } = await api.get<DocumentVersion[]>(`/kb/documents/${documentId}/versions`);
  return data;
}

export async function restoreDocumentVersion(
  documentId: string,
  versionId: string,
): Promise<Document> {
  const { data } = await api.post<Document>(
    `/kb/documents/${documentId}/versions/${versionId}/restore`,
  );
  return data;
}
