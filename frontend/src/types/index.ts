export type UserRole = "admin" | "cliente";

export interface User {
  id: string;
  email: string;
  role: UserRole;
  nombre: string | null;
  activo: boolean;
  last_login?: string | null;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export type ConversationStatus = "bot" | "humano" | "cerrada";
export type ConversationCanal =
  | "whatsapp"
  | "web"
  | "instagram_dm"
  | "email"
  | "retell_voice";

export interface Contact {
  id: string;
  telefono: string;
  email: string | null;
  nombre: string | null;
  // Instagram: el @usuario, para mostrarlo bajo el nombre real en la bandeja.
  social_handle?: string | null;
  estado: string;
  origen: string;
  servicio_interes: string | null;
  // Ficha CRM (todos opcionales).
  empresa?: string | null;
  cargo?: string | null;
  web?: string | null;
  nif?: string | null;
  direccion?: string | null;
  ultimo_mensaje_at: string | null;
  notas_internas: string | null;
  in_crm?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface Tag {
  id: string;
  nombre: string;
  color: string;
}

export interface ContactNoteAuthor {
  id: string;
  nombre: string | null;
  role: UserRole;
}

export interface ContactNote {
  id: string;
  texto: string;
  created_at: string;
  author: ContactNoteAuthor | null;
}

// Actor de un evento de actividad (mismo shape que el autor de una nota).
// null = sistema/agente (p. ej. conversación iniciada por el cliente).
export interface ContactActivityActor {
  id: string;
  nombre: string | null;
  role: UserRole;
}

// Un evento del timeline de actividad de la ficha. `meta` es flexible: payload
// pequeño SIN PII que varía según `tipo` (estados, nombre de etiqueta, canal,
// ids). El frontend lo interpreta para componer una etiqueta legible.
export interface ContactActivity {
  id: string;
  tipo: string;
  meta: Record<string, unknown> | null;
  created_at: string;
  actor: ContactActivityActor | null;
}

export interface Conversation {
  id: string;
  contact_id: string;
  contact?: Contact;
  canal: ConversationCanal;
  status: ConversationStatus;
  demo_active?: boolean;
  archived?: boolean;
  quarantined?: boolean;
  quarantine_reason?: string | null;
  started_at: string;
  ended_at: string | null;
  last_message_at: string | null;
  resumen: string | null;
  // Asunto del correo (canal Email, F5a). NULL en el resto de canales.
  subject?: string | null;
  // Canal Voz (Retell) — metadatos de la llamada (sección "Llamadas"). NULL en
  // el resto de canales. El fin de la llamada llega en `ended_at` y el resumen
  // en `resumen`.
  call_duration_seconds?: number | null;
  // URL de la grabación en Retell. Se reproduce/enlaza directamente (no se
  // descarga). Puede caducar (es un enlace de Retell).
  call_recording_url?: string | null;
  call_ended_reason?: string | null;
  last_message_preview?: string | null;
  // 5d — True si el hilo tiene un borrador del agente sin enviar. La lista
  // pinta una etiqueta "Borrador".
  has_pending_draft?: boolean;
  // Sugerencia del modo Entrenamiento sin resolver (pestaña "Sugerencias").
  has_training_suggestion?: boolean;
  unread_count?: number;
  // Señal "pendiente": necesita acción de una persona (sugerencia sin resolver,
  // cliente sin contestar, o derivada a humano sin respuesta de un operador).
  needs_action?: boolean;
  tags?: Tag[];
}

export type MessageRole = "user" | "assistant" | "operator" | "system";

export type MediaKind = "image" | "audio" | "video" | "document" | "sticker";

export interface Message {
  id: string;
  conversation_id: string;
  rol: MessageRole;
  contenido: string | null;
  audio_url: string | null;
  audio_transcript: string | null;
  created_at: string;
  metadata?: Record<string, unknown>;
  media_type?: MediaKind | null;
  // Ruta relativa al endpoint protegido `/api/v1/uploads/{filename}`.
  // El frontend la usa con auth (token JWT en header) para mostrar el adjunto.
  media_url?: string | null;
  media_mime?: string | null;
  media_size?: number | null;
  media_filename?: string | null;
  media_duration_seconds?: number | null;
  // F5c — Borrador del agente para email. Si is_draft && !draft_sent, el panel
  // lo muestra como tarjeta editable con botón "Enviar" (no es un mensaje ya
  // enviado, sino una propuesta pendiente de revisión humana).
  is_draft?: boolean;
  draft_sent?: boolean;
  gmail_draft_id?: string | null;
  // Modo Entrenamiento (sombra) — true si la sugerencia la generó el agente en
  // un canal NO email mientras el canal estaba en Entrenamiento. Usa la misma
  // tarjeta de borrador, pero la copy habla de "sugerencia" y al enviar se
  // enruta por el canal (no por Gmail).
  training?: boolean;
  // 5d — HTML del correo (canal Email) para renderizado seguro con DOMPurify.
  // NULL/ausente en mensajes sin HTML; el frontend cae a `contenido`.
  html_body?: string | null;
  // Retención email — True si el contenido se purgó de la BD (a los 6 meses).
  // El panel muestra un stub "Contenido archivado" con botón para recuperar el
  // cuerpo desde Gmail bajo demanda (no se vuelve a persistir).
  purged?: boolean;
  // Entrega en WhatsApp. Que el proveedor acepte un mensaje no significa que el
  // cliente lo reciba: Meta responde 200 y minutos después avisa por webhook de
  // que no ha podido entregarlo (número que no existe, cliente que bloqueó a la
  // empresa, ventana de 24 h cerrada, plantilla pausada). Ausente en los canales
  // sin acuse (web, correo, Instagram) y en los mensajes anteriores a esto.
  delivery_status?: "enviado" | "entregado" | "leido" | "fallido" | null;
  // Por qué no llegó. Solo viene con delivery_status === "fallido".
  delivery_detail?: string | null;
}

export interface Document {
  id: string;
  nombre: string;
  formato: string;
  tamano_bytes: number;
  status: "procesando" | "indexado" | "error";
  error_msg: string | null;
  num_chunks: number;
  uploaded_at: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}
