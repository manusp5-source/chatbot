import { api } from "@/services/api";
import type { Contact, ContactActivity, ContactNote, Page, Tag } from "@/types";

export async function listContacts(params: {
  search?: string;
  estado?: string;
  origen?: string;
  tag_id?: string;
  segment?: string;
  sort?: string;
  include_outside_crm?: boolean;
  page?: number;
  page_size?: number;
} = {}): Promise<Page<Contact & { tags: Tag[] }>> {
  const { data } = await api.get<Page<Contact & { tags: Tag[] }>>("/contacts", { params });
  return data;
}

export interface ContactStats {
  total: number;
  nuevos_30d: number;
  activos_30d: number;
  by_estado: Record<string, number>;
  by_origen: Record<string, number>;
}

export interface ImportResult {
  created: number;
  updated: number;
  skipped: number;
  errors: string[];
}

/**
 * Descarga los contactos del CRM como CSV (compatible con Excel).
 *
 * Por defecto saca lo MISMO que enseña la pantalla (los que están en el CRM):
 * antes exportaba también los que no lo están, así que el fichero traía más
 * filas de las que la pantalla decía tener y no había manera de saber por qué.
 */
export async function exportContacts(includeOutsideCrm = false): Promise<void> {
  const res = await api.get("/contacts/export", {
    responseType: "blob",
    params: includeOutsideCrm ? { include_outside_crm: true } : {},
  });
  const url = URL.createObjectURL(res.data as Blob);
  const a = document.createElement("a");
  a.href = url;
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  a.download = `contactos_${today}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Importa contactos desde un CSV. Dedup por teléfono. */
export async function importContacts(file: File): Promise<ImportResult> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<ImportResult>("/contacts/import", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function contactStats(): Promise<ContactStats> {
  const { data } = await api.get<ContactStats>("/contacts/stats");
  return data;
}

export async function addContactToCrm(id: string): Promise<Contact & { tags: Tag[] }> {
  const { data } = await api.post<Contact & { tags: Tag[] }>(`/contacts/${id}/add-to-crm`);
  return data;
}

export async function getContact(id: string): Promise<Contact & { tags: Tag[] }> {
  const { data } = await api.get<Contact & { tags: Tag[] }>(`/contacts/${id}`);
  return data;
}

export async function createContact(body: Partial<Contact>): Promise<Contact> {
  const { data } = await api.post<Contact>("/contacts", body);
  return data;
}

export async function updateContact(id: string, body: Partial<Contact>): Promise<Contact> {
  const { data } = await api.patch<Contact>(`/contacts/${id}`, body);
  return data;
}

export async function deleteContact(id: string): Promise<void> {
  await api.delete(`/contacts/${id}`);
}

/**
 * Fusiona dos fichas del mismo cliente: `sourceId` se absorbe en `id` y
 * desaparece. Se mueven conversaciones, notas y actividad; las etiquetas se
 * suman; los huecos de la ficha que sobrevive se rellenan con lo que tuviera la
 * otra (nunca se pisa lo que ya había). Solo admin.
 */
export async function mergeContacts(
  id: string,
  sourceId: string,
): Promise<Contact & { tags: Tag[] }> {
  const { data } = await api.post<Contact & { tags: Tag[] }>(`/contacts/${id}/merge`, {
    source_id: sourceId,
  });
  return data;
}

// RGPD Art. 15/20: descarga el JSON con todos los datos del contacto
// (ficha + conversaciones + mensajes + notas + actividad).
export async function exportContactData(id: string): Promise<void> {
  const { data, headers } = await api.get(`/contacts/${id}/export`, {
    responseType: "blob",
  });
  const dispo: string = headers["content-disposition"] || "";
  const match = /filename="?([^";]+)"?/.exec(dispo);
  const filename = match?.[1] || "datos-contacto.json";
  const url = URL.createObjectURL(data as Blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export async function addContactTag(contactId: string, tagId: string): Promise<void> {
  await api.post(`/contacts/${contactId}/tags/${tagId}`);
}

export async function removeContactTag(contactId: string, tagId: string): Promise<void> {
  await api.delete(`/contacts/${contactId}/tags/${tagId}`);
}

export async function listTags(): Promise<Tag[]> {
  const { data } = await api.get<Tag[]>("/tags");
  return data;
}

export async function createTag(body: { nombre: string; color: string }): Promise<Tag> {
  const { data } = await api.post<Tag>("/tags", body);
  return data;
}

export async function updateTag(id: string, body: Partial<Tag>): Promise<Tag> {
  const { data } = await api.patch<Tag>(`/tags/${id}`, body);
  return data;
}

export async function deleteTag(id: string): Promise<void> {
  await api.delete(`/tags/${id}`);
}


export interface ContactConversationMini {
  id: string;
  canal: string;
  status: string;
  started_at: string;
  last_message_at: string | null;
  archived: boolean;
  // Vista previa (texto truncado) del último mensaje del hilo, ya existente
  // en BD. Puede faltar si el hilo no tiene mensajes todavía.
  last_message_preview?: string | null;
}

export async function getContactConversations(contactId: string): Promise<ContactConversationMini[]> {
  const { data } = await api.get<ContactConversationMini[]>(`/contacts/${contactId}/conversations`);
  return data;
}

export async function getContactNotes(contactId: string): Promise<ContactNote[]> {
  const { data } = await api.get<ContactNote[]>(`/contacts/${contactId}/notes`);
  return data;
}

export async function addContactNote(contactId: string, texto: string): Promise<ContactNote> {
  const { data } = await api.post<ContactNote>(`/contacts/${contactId}/notes`, { texto });
  return data;
}

export async function deleteContactNote(contactId: string, noteId: string): Promise<void> {
  await api.delete(`/contacts/${contactId}/notes/${noteId}`);
}

export async function getContactActivity(contactId: string): Promise<ContactActivity[]> {
  const { data } = await api.get<ContactActivity[]>(`/contacts/${contactId}/activity`);
  return data;
}
