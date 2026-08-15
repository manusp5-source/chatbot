import { api } from "@/services/api";
import type { Conversation, Message, Page } from "@/types";

export interface ListParams {
  status?: string;
  canal?: string;
  archived?: "hide" | "only" | "all";
  quarantine?: "hide" | "only" | "all";
  active_only?: boolean;
  unread_only?: boolean;
  // Vista "Pendientes": solo lo accionable (humano + email draft + sin contestar).
  pending_only?: boolean;
  // Vista "Sugerencias": solo sugerencias del modo Entrenamiento sin resolver.
  suggestions_only?: boolean;
  // Vista "Para hacer": Pendientes + Sugerencias en una sola cola.
  action_only?: boolean;
  search?: string;
  /**
   * Orden de la bandeja. Lo decide el BACKEND y se aplica ANTES de paginar.
   * (Antes el backend ordenaba de una manera y el frontend volvía a ordenar de
   * otra las 50 filas que le habían llegado: lo urgente que cayera en el puesto
   * 51 no subía nunca porque no había llegado.)
   */
  sort?: InboxSort;
  page?: number;
  page_size?: number;
}

/** Criterios de orden de la bandeja. Los mismos nombres que acepta el backend. */
export type InboxSort = "recent" | "pending" | "waiting";

export const INBOX_SORTS: { value: InboxSort; label: string; hint: string }[] = [
  { value: "recent", label: "Más recientes", hint: "Lo último que se ha movido, arriba" },
  { value: "pending", label: "Pendientes primero", hint: "Lo que necesita a una persona, arriba" },
  { value: "waiting", label: "Llevan más esperando", hint: "Lo que lleva más tiempo sin atender, arriba" },
];

export function isInboxSort(v: string | null | undefined): v is InboxSort {
  return v === "recent" || v === "pending" || v === "waiting";
}

export async function listConversations(params: ListParams = {}): Promise<Page<Conversation>> {
  const { data } = await api.get<Page<Conversation>>("/conversations", { params });
  return data;
}

export interface InboxCounts {
  action: number;
  pending: number;
  suggestions: number;
  /** Conversaciones derivadas a humano (badge de la pestaña Humano). */
  humano: number;
  quarantine: number;
}

export async function getConversationCounts(canal?: string): Promise<InboxCounts> {
  const { data } = await api.get<InboxCounts>("/conversations/counts", {
    params: canal ? { canal } : {},
  });
  return data;
}

export async function getConversation(id: string): Promise<Conversation> {
  const { data } = await api.get<Conversation>(`/conversations/${id}`);
  return data;
}

/**
 * Mensajes de una conversación, del más antiguo al más nuevo.
 *
 * `before` (fecha ISO del mensaje más antiguo que ya tienes) pide el tramo
 * ANTERIOR. El backend siempre lo soportó y nadie lo usaba: en un hilo largo
 * solo se veían los últimos 100 mensajes y no había forma de subir más.
 */
export async function getMessages(
  id: string,
  limit = 50,
  before?: string,
): Promise<Message[]> {
  const { data } = await api.get<Message[]>(`/conversations/${id}/messages`, {
    params: before ? { limit, before } : { limit },
  });
  return data;
}

export async function sendMessage(id: string, contenido: string): Promise<Message> {
  const { data } = await api.post<Message>(`/conversations/${id}/messages`, { contenido });
  return data;
}

export async function sendAttachment(
  id: string,
  file: File | Blob,
  opts: { caption?: string; filename?: string } = {},
): Promise<Message> {
  const fd = new FormData();
  // Si recibimos Blob (audio grabado por MediaRecorder), respetamos el
  // filename que nos pasen; si no, le ponemos uno con extensión razonable.
  const fname = opts.filename || (file instanceof File ? file.name : "audio.webm");
  fd.append("file", file, fname);
  if (opts.caption) fd.append("caption", opts.caption);
  const { data } = await api.post<Message>(`/conversations/${id}/attachment`, fd, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function takeOver(id: string): Promise<void> {
  await api.post(`/conversations/${id}/take-over`);
}

export async function returnToBot(id: string): Promise<void> {
  await api.post(`/conversations/${id}/return-to-bot`);
}

export async function closeConversation(
  id: string,
  body: { resumen?: string; enviar_resumen_email?: boolean } = {}
): Promise<void> {
  await api.post(`/conversations/${id}/close`, body);
}

export async function markRead(id: string): Promise<void> {
  await api.post(`/conversations/${id}/mark-read`);
}


export async function demoEnable(id: string): Promise<void> {
  await api.post(`/conversations/${id}/demo-enable`);
}

export async function demoDisable(id: string): Promise<void> {
  await api.delete(`/conversations/${id}/demo-enable`);
}


export async function archiveConversation(id: string): Promise<void> {
  await api.post(`/conversations/${id}/archive`);
}

export async function bulkArchiveConversations(ids: string[]): Promise<void> {
  await api.post(`/conversations/bulk-archive`, { ids });
}

export async function unarchiveConversation(id: string): Promise<void> {
  await api.delete(`/conversations/${id}/archive`);
}

export async function releaseQuarantine(id: string): Promise<void> {
  await api.post(`/conversations/${id}/release-quarantine`);
}

// F5c — Borradores del agente (canal Email). Enviar el borrador con el texto
// final (posiblemente editado por la operadora) o descartarlo.
export async function sendDraft(
  conversationId: string,
  messageId: string,
  text: string,
): Promise<Message> {
  const { data } = await api.post<Message>(
    `/conversations/${conversationId}/drafts/${messageId}/send`,
    { text },
  );
  return data;
}

// Autoaprendizaje Fase 1 — "Corrige y re-redacta": en vez de reescribir tú el
// texto, le das una instrucción en lenguaje natural y el agente regenera el
// borrador/sugerencia (sigue sin enviarse). Devuelve el Message actualizado.
export async function refineDraft(
  conversationId: string,
  messageId: string,
  instruction: string,
): Promise<Message> {
  const { data } = await api.post<Message>(
    `/conversations/${conversationId}/drafts/${messageId}/refine`,
    { instruction },
  );
  return data;
}

export async function discardDraft(
  conversationId: string,
  messageId: string,
): Promise<void> {
  await api.post(`/conversations/${conversationId}/drafts/${messageId}/discard`);
}

// Retención email — Recupera bajo demanda el contenido de un correo archivado
// (purgado a los 6 meses) desde Gmail. El backend lo trae por su id y lo
// devuelve SOLO para mostrar; no se vuelve a guardar en la BD.
export interface RecoveredEmail {
  contenido: string;
  html_body: string | null;
}

export async function recoverMessage(
  conversationId: string,
  messageId: string,
): Promise<RecoveredEmail> {
  const { data } = await api.post<RecoveredEmail>(
    `/conversations/${conversationId}/messages/${messageId}/recover`,
  );
  return data;
}
