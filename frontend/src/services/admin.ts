import { api } from "@/services/api";
import type { Page, User } from "@/types";

export async function listUsers(): Promise<User[]> {
  const { data } = await api.get<User[]>("/admin/users");
  return data;
}

export async function createUser(body: { email: string; password: string; role: "admin" | "cliente"; nombre?: string }): Promise<User> {
  const { data } = await api.post<User>("/admin/users", body);
  return data;
}

export async function updateUser(id: string, body: Partial<User>): Promise<User> {
  const { data } = await api.patch<User>(`/admin/users/${id}`, body);
  return data;
}

export async function deleteUser(id: string): Promise<void> {
  await api.delete(`/admin/users/${id}`);
}

export async function resetUserPassword(id: string, new_password: string): Promise<void> {
  await api.post(`/admin/users/${id}/reset-password`, { new_password });
}

export interface CredentialOut {
  key: string;
  descripcion: string | null;
  value_masked: string;
  updated_at: string;
}

export async function listCredentials(): Promise<CredentialOut[]> {
  const { data } = await api.get<CredentialOut[]>("/admin/credentials");
  return data;
}

export async function updateCredential(key: string, value: string, descripcion?: string): Promise<CredentialOut> {
  const { data } = await api.put<CredentialOut>(`/admin/credentials/${key}`, { value, descripcion });
  return data;
}

export async function testCredential(key: string): Promise<{ ok: boolean; message: string }> {
  const { data } = await api.post<{ ok: boolean; message: string }>(`/admin/credentials/${key}/test`);
  return data;
}

export interface AgentConfig {
  id: string;
  prompt_system: string;
  model_name: string;
  temperature: number;
  max_tokens: number;
  buffer_seconds: number;
  response_split_max_parts: number;
  context_window: number;
  handoff_bridge_message?: string | null;
  monthly_budget_usd?: number | null;
  is_active: boolean;
  version: number;
  created_at: string;
}

export async function getActiveAgentConfig(): Promise<AgentConfig | null> {
  const { data } = await api.get<AgentConfig | null>("/admin/agent/config");
  return data;
}

export async function getAgentConfigHistory(): Promise<AgentConfig[]> {
  const { data } = await api.get<AgentConfig[]>("/admin/agent/config/history");
  return data;
}

export async function createAgentConfig(body: Partial<AgentConfig> & { activate?: boolean }): Promise<AgentConfig> {
  const { data } = await api.post<AgentConfig>("/admin/agent/config", body);
  return data;
}

export async function activateAgentConfig(id: string): Promise<AgentConfig> {
  const { data } = await api.post<AgentConfig>(`/admin/agent/config/${id}/activate`);
  return data;
}

export interface AuditEntry {
  id: string;
  user_id: string | null;
  action: string;
  entity: string;
  entity_id: string | null;
  created_at: string;
}

export async function listAudit(params: { page?: number; page_size?: number } = {}): Promise<Page<AuditEntry>> {
  const { data } = await api.get<Page<AuditEntry>>("/admin/audit", { params });
  return data;
}

export async function adminHealth(): Promise<Record<string, { ok: boolean; detail: string }>> {
  const { data } = await api.get<Record<string, { ok: boolean; detail: string }>>("/admin/health");
  return data;
}

// Informe de almacenamiento (Salud → Almacenamiento): tamaño de la BD, top
// tablas, volumen de audios, disco y política de retención efectiva.
export interface StorageReport {
  db_total_bytes: number;
  tables: { name: string; total_bytes: number; rows: number }[];
  audio_dir_bytes: number;
  disk_free_bytes: number | null;
  disk_total_bytes: number | null;
  counts: Record<string, number>;
  retention: Record<string, number>;
}

export async function adminStorageReport(): Promise<StorageReport> {
  const { data } = await api.get<StorageReport>("/admin/health/storage");
  return data;
}

export interface ChannelCount {
  canal: string;
  count: number;
}

export interface HourPoint {
  hour: number;
  today: number;
  avg_7d: number;
}

export interface DashboardSummary {
  range: string;
  timezone: string;
  conversations_total: number;
  conversations_handed_off: number;
  handoff_rate: number;
  new_contacts: number;
  handed_off_ever: number;
  ai_resolved: number;
  ai_resolved_rate: number;
  by_channel: ChannelCount[];
  conversations_today: number;
  conversations_yesterday: number;
  hourly: HourPoint[];
}

export async function dashboardSummary(range: string = "30d"): Promise<DashboardSummary> {
  const { data } = await api.get<DashboardSummary>("/admin/dashboard/summary", { params: { range } });
  return data;
}

// Ajustes globales del panel (app_settings). Hoy solo la zona horaria del
// dashboard; timezone_options es la lista para el desplegable.
export interface AppSettings {
  dashboard_timezone: string;
  timezone_options: string[];
}

export async function getAppSettings(): Promise<AppSettings> {
  const { data } = await api.get<AppSettings>("/admin/settings");
  return data;
}

export async function updateAppSettings(dashboardTimezone: string): Promise<AppSettings> {
  const { data } = await api.put<AppSettings>("/admin/settings", {
    dashboard_timezone: dashboardTimezone,
  });
  return data;
}

// Horario de atención del negocio (claves `calendar.*`). Es lo que el agente
// usa para ofrecer huecos y para saber cuándo está cerrado. Antes solo se podía
// cambiar tocando la base de datos a mano.
export interface CalendarSettings {
  timezone: string;
  timezone_options: string[];
  /** isoweekday: 1 = lunes … 7 = domingo. */
  work_days: number[];
  /** Tramos "HH:MM-HH:MM". Varios = jornada partida. */
  work_hours: string[];
  /** Festivos en ISO (AAAA-MM-DD): cerrado aunque sea laborable. */
  holidays: string[];
  /** Cada cuántos minutos se ofrece un hueco. */
  slot_step_min: number;
  /** Antelación mínima en minutos para poder coger una cita. */
  min_notice_min: number;
}

export type CalendarSettingsUpdate = Omit<CalendarSettings, "timezone_options">;

export async function getCalendarSettings(): Promise<CalendarSettings> {
  const { data } = await api.get<CalendarSettings>("/admin/settings/calendar");
  return data;
}

export async function updateCalendarSettings(
  body: CalendarSettingsUpdate,
): Promise<CalendarSettings> {
  const { data } = await api.put<CalendarSettings>("/admin/settings/calendar", body);
  return data;
}

export interface AttentionItem {
  conversation_id: string;
  canal: string;
  status: string;
  contact_name: string | null;
  contact_phone: string | null;
  assigned_to: string | null;
  waiting_minutes: number;
  last_message_at: string | null;
  derivada_a_humano_at: string | null;
  preview: string;
}

export interface AttentionResponse {
  stale_minutes: number;
  handoffs: AttentionItem[];
  waiting: AttentionItem[];
  handoffs_count: number;
  waiting_count: number;
}

export async function dashboardAttention(staleMinutes: number = 10): Promise<AttentionResponse> {
  const { data } = await api.get<AttentionResponse>("/admin/dashboard/attention", {
    params: { stale_minutes: staleMinutes },
  });
  return data;
}

// ---- Onboarding (checklist de primera ejecución) ----
export interface OnboardingStep {
  key: string;
  title: string;
  description: string;
  done: boolean;
  action_path: string;
  action_label: string;
}

export interface OnboardingStatus {
  complete: boolean;
  done_count: number;
  total: number;
  steps: OnboardingStep[];
}

export async function onboardingStatus(): Promise<OnboardingStatus> {
  const { data } = await api.get<OnboardingStatus>("/admin/onboarding");
  return data;
}

export interface BlockedPhone {
  phone: string;
  reason: string;
  ttl_seconds: number;
}

export async function listBlockedPhones(): Promise<BlockedPhone[]> {
  const { data } = await api.get<BlockedPhone[]>("/admin/blocked-phones");
  return data;
}

export async function unblockPhone(phone: string): Promise<void> {
  await api.delete(`/admin/blocked-phones/${encodeURIComponent(phone)}`);
}

export async function blockPhone(phone: string, reason?: string, days?: number): Promise<void> {
  await api.post("/admin/blocked-phones", { phone, reason, days });
}

export interface ClassifierConfig {
  enabled: boolean;
  channels: string[];
  instructions: string;
  model_name: string;
  temperature: number;
  llm_provider_id: string | null;
  // Qué se archiva y etiqueta en el buzón real de Gmail: rules = solo las
  // reglas duras (por defecto) · all = también lo que decide la IA · off = no
  // tocar Gmail nunca.
  gmail_action: "rules" | "all" | "off";
  gmail_label: string;
}

export async function getClassifierConfig(): Promise<ClassifierConfig> {
  const { data } = await api.get<ClassifierConfig>("/admin/classifier");
  return data;
}

export async function updateClassifierConfig(body: Partial<ClassifierConfig>): Promise<ClassifierConfig> {
  const { data } = await api.put<ClassifierConfig>("/admin/classifier", body);
  return data;
}

// Reglas duras del clasificador: lo que en Gmail sería un filtro. Se evalúan
// antes del modelo, así que no cuestan tokens, y no interpretan nada.
export interface ClassifierRule {
  id: string;
  enabled: boolean;
  canal: string | null;
  campo: "remitente" | "dominio" | "asunto";
  valor: string;
  nota: string | null;
  hits: number;
  last_hit_at: string | null;
  created_at: string;
}

export async function listClassifierRules(): Promise<ClassifierRule[]> {
  const { data } = await api.get<ClassifierRule[]>("/admin/classifier/rules");
  return data;
}

export async function createClassifierRule(body: {
  campo: string;
  valor: string;
  canal?: string | null;
  nota?: string | null;
}): Promise<ClassifierRule> {
  const { data } = await api.post<ClassifierRule>("/admin/classifier/rules", body);
  return data;
}

export async function updateClassifierRule(
  id: string,
  body: { valor?: string; nota?: string | null; enabled?: boolean },
): Promise<ClassifierRule> {
  const { data } = await api.put<ClassifierRule>(`/admin/classifier/rules/${id}`, body);
  return data;
}

export async function deleteClassifierRule(id: string): Promise<void> {
  await api.delete(`/admin/classifier/rules/${id}`);
}


// Estado de 3 vias por canal: Activo / Entrenamiento / Pausado.
export type ChannelMode = "active" | "training" | "paused";

export interface ChannelPauseInfo {
  canal: string;
  paused: boolean;
  self_paused: boolean;
  // Modo Entrenamiento (sombra) EFECTIVO: el agente procesa pero no envia,
  // genera una sugerencia/borrador para revision humana. Estado de 3 vias:
  // paused -> "Pausado"; training -> "Entrenamiento"; ninguno -> "Activo".
  training: boolean;
  demo_count: number;
  // Transcripcion de notas de voz del canal (encendida por defecto). Es
  // independiente del modo: un canal pausado sigue transcribiendo para que la
  // nota se pueda leer en el inbox.
  transcribe_audio: boolean;
}

export interface AgentPauseState {
  paused: boolean;
  demo_conversations: string[];
  channels: ChannelPauseInfo[];
}

// Deriva el estado de 3 vias de un canal a partir de su info de pausa.
export function channelMode(ch: ChannelPauseInfo): ChannelMode {
  if (ch.paused) return "paused";
  if (ch.training) return "training";
  return "active";
}

export async function getAgentPause(): Promise<AgentPauseState> {
  const { data } = await api.get<AgentPauseState>("/admin/agent/pause");
  return data;
}

export async function setAgentPause(paused: boolean): Promise<AgentPauseState> {
  const { data } = await api.put<AgentPauseState>("/admin/agent/pause", { paused });
  return data;
}

export async function setChannelPause(
  canal: string,
  paused: boolean,
): Promise<AgentPauseState> {
  const { data } = await api.put<AgentPauseState>(
    `/admin/agent/pause/${encodeURIComponent(canal)}`,
    { paused },
  );
  return data;
}

// Cambia el estado de 3 vias de un canal (Activo / Entrenamiento / Pausado).
// El backend garantiza la exclusion mutua entre pausa y entrenamiento.
export async function setChannelMode(
  canal: string,
  mode: ChannelMode,
): Promise<AgentPauseState> {
  const { data } = await api.put<AgentPauseState>(
    `/admin/agent/mode/${encodeURIComponent(canal)}`,
    { mode },
  );
  return data;
}

// Enciende/apaga la transcripcion de notas de voz de un canal. No toca el modo:
// apagarla solo evita el gasto, encenderla no hace que el bot responda.
export async function setChannelTranscription(
  canal: string,
  enabled: boolean,
): Promise<AgentPauseState> {
  const { data } = await api.put<AgentPauseState>(
    `/admin/agent/transcription/${encodeURIComponent(canal)}`,
    { enabled },
  );
  return data;
}


export interface UsagePoint {
  day: string;
  source: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
  cost_usd: number;
}

export interface AgentUsageBucket {
  key: string;
  label: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface ModelUsageBucket {
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface UsageSummary {
  range_days: number;
  total_calls: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  by_source: Record<string, { calls: number; prompt_tokens: number; completion_tokens: number; total_tokens: number; cost_usd: number }>;
  by_agent: AgentUsageBucket[];
  by_model: ModelUsageBucket[];
  series: UsagePoint[];
}

export async function getAgentUsage(range: "24h" | "7d" | "30d" | "month" = "7d"): Promise<UsageSummary> {
  const { data } = await api.get<UsageSummary>("/admin/agent/usage", { params: { range } });
  return data;
}

// ---- Precios de modelos (estimación de coste) ----
export interface ModelPrice {
  model: string;
  input_per_1m: number;
  output_per_1m: number;
  source: "seed" | "openrouter" | "manual" | string;
  openrouter_id: string | null;
  updated_at: string | null;
}

export interface PriceRefreshResult {
  updated: string[];
  unmatched: string[];
  skipped_manual?: string[];
  openrouter_models: number;
  error?: string | null;
}

export async function getModelPrices(): Promise<ModelPrice[]> {
  const { data } = await api.get<ModelPrice[]>("/admin/model-prices");
  return data;
}

export async function refreshModelPrices(): Promise<PriceRefreshResult> {
  const { data } = await api.post<PriceRefreshResult>("/admin/model-prices/refresh");
  return data;
}

export async function updateModelPrice(
  model: string,
  body: { input_per_1m: number; output_per_1m: number }
): Promise<ModelPrice> {
  const { data } = await api.put<ModelPrice>(
    `/admin/model-prices/${encodeURIComponent(model)}`,
    body
  );
  return data;
}

export interface CredentialTestResult {
  key: string;
  ok: boolean;
  message: string;
}

export async function testAllCredentials(): Promise<CredentialTestResult[]> {
  const { data } = await api.post<CredentialTestResult[]>("/admin/credentials/test-all");
  return data;
}


export async function deleteAgentConfig(id: string): Promise<void> {
  await api.delete(`/admin/agent/config/${id}`);
}

export interface BudgetStatus {
  month: string;
  cost_usd: number;
  budget_usd: number | null;
  percent: number | null;
  exceeded: boolean;
}

export async function getBudgetStatus(): Promise<BudgetStatus> {
  const { data } = await api.get<BudgetStatus>("/admin/agent/budget");
  return data;
}


export interface RuntimeLogEntry {
  ts: number;
  level: string;
  event: string;
  message?: string;
  [k: string]: unknown;
}

export async function getRuntimeLogs(
  params: { limit?: number; level?: string; event_prefix?: string } = {}
): Promise<RuntimeLogEntry[]> {
  const { data } = await api.get<RuntimeLogEntry[]>("/admin/system/logs", { params });
  return data;
}

export type TraceStepKind =
  | "message_in"
  | "message_out"
  | "llm_call"
  | "tool"
  | "kb"
  | "router"
  | "error"
  | "audit";

export type TraceLevel = "info" | "warn" | "error";

export interface TraceStep {
  kind: TraceStepKind;
  ts: string;
  level: TraceLevel;
  label: string;
  detail?: string | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  cost_usd?: number | null;
  model?: string | null;
  source?: string | null;
  latency_ms?: number | null;
  // Payload completo del evento (para item expandible). Forma libre por tipo.
  payload?: Record<string, unknown> | null;
}

export interface TraceQuery {
  level?: TraceLevel;
  kind?: TraceStepKind;
}

export async function getConversationTrace(
  conversationId: string,
  params?: TraceQuery,
): Promise<TraceStep[]> {
  const { data } = await api.get<TraceStep[]>(
    `/admin/conversations/${conversationId}/trace`,
    { params },
  );
  return data;
}

// ---------- F2 — Connections (Channels + External APIs) ----------

export type ChannelType =
  | "whatsapp"
  | "webchat"
  | "retell_voice"
  | "instagram_dm"
  | "email";

export interface ChannelOut {
  id: string;
  type: ChannelType;
  name: string;
  enabled: boolean;
  agent_id: string | null;
  agent_name: string | null;
  config: Record<string, unknown>;
  legacy_credentials_keys: string[];
  created_at: string;
}

export async function listChannels(): Promise<ChannelOut[]> {
  const { data } = await api.get<ChannelOut[]>("/admin/channels");
  return data;
}

export interface ChannelUpdate {
  name?: string;
  enabled?: boolean;
  agent_id?: string | null;
  // Saludo inicial (V-03). Solo aplica a canales de voz. Se guarda en
  // Channel.config["greeting"]. "" lo limpia (vuelve al saludo por defecto).
  greeting?: string;
  // Firma de los correos salientes (canal de email). "" la limpia y vuelve al
  // valor de la variable de entorno EMAIL_SIGNATURE.
  email_signature?: string;
  // Dominios donde se acepta el widget web (canal webchat). Lista vacía = sin
  // restricción. Se admite "ejemplo.com" y "*.ejemplo.com". El backend los
  // normaliza al guardarlos, así que da igual pegar la URL entera.
  allowed_domains?: string[];
}

export async function updateChannel(
  id: string,
  body: ChannelUpdate,
): Promise<ChannelOut> {
  const { data } = await api.patch<ChannelOut>(`/admin/channels/${id}`, body);
  return data;
}

export async function deleteChannel(id: string): Promise<void> {
  await api.delete(`/admin/channels/${id}`);
}

export async function startInstagramOAuth(): Promise<string> {
  const { data } = await api.post<{ auth_url: string }>(
    "/admin/oauth/instagram/start",
    undefined,
    // withCredentials: el backend responde con la cookie que ata el `state` de
    // OAuth a ESTE navegador (si no coincide en el callback, no se guardan
    // credenciales). El panel y la API viven en subdominios distintos, y el
    // navegador IGNORA el Set-Cookie de una petición cross-origin si no se
    // manda con credenciales — sin esto la cookie nunca llegaría y conectar
    // Instagram fallaría siempre.
    { withCredentials: true },
  );
  return data.auth_url;
}

export interface InstagramWebhookInfo {
  webhook_url: string;
  verify_token: string;
  app_secret_set: boolean;
  connected: boolean;
}

export async function getInstagramWebhookInfo(): Promise<InstagramWebhookInfo> {
  const { data } = await api.get<InstagramWebhookInfo>(
    "/admin/channels/instagram/webhook-info",
  );
  return data;
}

export interface WebchatProvision {
  channel_id: string;
  api_key: string;
  snippet: string;
}

export async function provisionWebchatChannel(): Promise<WebchatProvision> {
  const { data } = await api.post<WebchatProvision>(
    "/admin/channels/webchat/provision",
  );
  return data;
}

export async function getWebchatSnippet(): Promise<WebchatProvision> {
  const { data } = await api.get<WebchatProvision>(
    "/admin/channels/webchat/snippet",
  );
  return data;
}

// ---------- WhatsApp (YCloud) ----------
//
// No existía: las credenciales se metían sueltas en "APIs externas" y nunca se
// creaba la fila de canal, así que Conexiones jamás enseñaba un WhatsApp al que
// asignarle agente. Los campos de secreto se pueden dejar vacíos al EDITAR: el
// backend solo pisa lo que llega con valor.

/** Con quién habla el canal de WhatsApp: el revendedor YCloud o la API Cloud
 * oficial de Meta. No es una variable de entorno — lo elige cada instalación
 * desde Conexiones y se guarda en el canal. */
export type WhatsappProviderName = "ycloud" | "meta";

export interface WhatsappProvisionIn {
  provider: WhatsappProviderName;
  // --- YCloud ---
  api_key?: string;
  webhook_secret?: string;
  /** E.164. En YCloud es obligatorio (viaja como remitente); en Meta solo
   * sirve para poder mostrarlo en el panel. */
  phone_number?: string;
  // --- API Cloud de Meta ---
  phone_number_id?: string;
  business_account_id?: string;
  access_token?: string;
  app_secret?: string;
  verify_token?: string;
  agent_id?: string | null;
}

export interface WhatsappProvisionOut {
  channel_id: string;
  provider: WhatsappProviderName;
  /** URL que hay que pegar en el panel del proveedor. Cada uno tiene la suya. */
  webhook_url: string;
  /** Solo Meta: la palabra que hay que repetir en su panel para que dé por
   * bueno el webhook. */
  verify_token: string;
  phone_number: string;
  agent_id: string | null;
}

export async function provisionWhatsappChannel(
  body: WhatsappProvisionIn,
): Promise<WhatsappProvisionOut> {
  const { data } = await api.post<WhatsappProvisionOut>(
    "/admin/channels/whatsapp/provision",
    body,
  );
  return data;
}

export interface InstagramProvisionIn {
  page_id: string;
  page_access_token: string;
  verify_token: string;
  app_secret: string;
}

export interface InstagramProvisionOut {
  channel_id: string;
  webhook_url: string;
  verify_token: string;
}

export async function provisionInstagramChannel(
  body: InstagramProvisionIn,
): Promise<InstagramProvisionOut> {
  const { data } = await api.post<InstagramProvisionOut>(
    "/admin/channels/instagram/provision",
    body,
  );
  return data;
}

export interface RetellProvisionIn {
  api_key: string;
  agent_id_retell: string;
  voice_id: string;
  phone_number: string;
  // Saludo inicial opcional (V-03). Se persiste en Channel.config["greeting"].
  greeting?: string;
}

export interface RetellProvisionOut {
  channel_id: string;
  /** URL 1 — Custom LLM (WebSocket). Es quien contesta a quien llama. */
  llm_webhook_url: string;
  /**
   * URL 2 — Webhook del agente (HTTP). Es quien trae duración, grabación,
   * resumen y motivo de fin al terminar la llamada. Sin ella las llamadas
   * funcionan pero llegan sin datos y se quedan abiertas en la bandeja.
   */
  call_webhook_url: string;
  /** Grabaciones con URL firmada activadas en el Agent de Retell (no es retroactivo). */
  signed_recordings_enabled: boolean;
  signed_recordings_detail: string;
}

export async function provisionRetellChannel(
  body: RetellProvisionIn,
): Promise<RetellProvisionOut> {
  const { data } = await api.post<RetellProvisionOut>(
    "/admin/channels/retell/provision",
    body,
  );
  return data;
}

// ---------- F5a — Email (Gmail) channel ----------

export interface EmailProvisionOut {
  channel_id: string;
  gmail_email_address: string | null;
  connected: boolean;
}

export async function provisionEmailChannel(
  agentId?: string | null,
): Promise<EmailProvisionOut> {
  const { data } = await api.post<EmailProvisionOut>(
    "/admin/channels/email/provision",
    { agent_id: agentId ?? null },
  );
  return data;
}

// ---------- F8b — Google OAuth ----------

export type GoogleProvider = "google_calendar" | "google_gmail" | "google_drive";

export async function startGoogleOAuth(provider: GoogleProvider): Promise<string> {
  const { data } = await api.post<{ auth_url: string }>(
    "/admin/oauth/google/start",
    { provider },
    // Ver startInstagramOAuth: sin withCredentials el navegador descartaría la
    // cookie de `state` que devuelve el backend y el callback rechazaría la
    // conexión.
    { withCredentials: true },
  );
  return data.auth_url;
}

// ---------- F8a — Outbound (WhatsApp templates) ----------

export interface WhatsappTemplate {
  name: string;
  language: string;
  category?: string | null;
  // Estado de aprobación de Meta: APPROVED / PENDING / REJECTED / … Se PINTA en
  // la lista y bloquea el envío si no está aprobada.
  status?: string | null;
  body?: string | null;
  variables: number;
  // "positional" ({{1}}) o "named" ({{nombre}}, lo que genera hoy el asistente
  // de Meta). Con nombre, cada hueco viaja con su parameter_name.
  param_format: "positional" | "named";
  variable_names: string[];
  header_format?: "text" | "image" | "video" | "document" | null;
  header_text?: string | null;
  header_variables: number;
  header_variable_names: string[];
  button_variables: number;
  footer?: string | null;
}

// Estados con los que la plantilla se puede enviar (espejo de
// TEMPLATE_SENDABLE_STATUSES en el backend).
const TEMPLATE_SENDABLE = new Set(["approved", "active", "enabled"]);

/** Motivo por el que NO se puede enviar con esta plantilla, o null. */
export function templateBlockReason(t: WhatsappTemplate): string | null {
  const s = (t.status || "").trim().toLowerCase();
  if (!s || TEMPLATE_SENDABLE.has(s)) {
    if (t.button_variables > 0) {
      return "Tiene variables en los botones y el panel todavía no sabe rellenarlas.";
    }
    return null;
  }
  const legible: Record<string, string> = {
    pending: "Pendiente de aprobación por Meta.",
    in_appeal: "En apelación.",
    rejected: "Rechazada por Meta.",
    disabled: "Deshabilitada.",
    paused: "Pausada por calidad.",
  };
  return legible[s] || `En estado «${t.status}».`;
}

export async function listWhatsappTemplates(): Promise<WhatsappTemplate[]> {
  const { data } = await api.get<WhatsappTemplate[]>("/admin/whatsapp/templates");
  return data;
}

export interface OutboundRecipient {
  phone: string;
  variables: string[];
}

// Envío directo desde la ficha del contacto (uno o unos pocos). Para una
// difusión, createOutboundJob: va poco a poco y se puede cancelar.
export interface OutboundSendBody {
  template_name: string;
  language: string;
  recipients: OutboundRecipient[];
}

export interface OutboundSendResult {
  phone: string;
  status: "ok" | "error";
  external_id?: string | null;
  error?: string | null;
}

export async function outboundSend(body: OutboundSendBody): Promise<OutboundSendResult[]> {
  const { data } = await api.post<OutboundSendResult[]>("/admin/outbound/send", body);
  return data;
}

// Comprobaciones previas: sin esto la campaña se aceptaba y se quedaba "En
// cola" para siempre si el worker no estaba levantado.
export interface OutboundPreflight {
  can_send: boolean;
  credentials_ok: boolean;
  credentials_detail: string;
  worker_ok: boolean;
  worker_detail: string;
}

export async function getOutboundPreflight(): Promise<OutboundPreflight> {
  const { data } = await api.get<OutboundPreflight>("/admin/outbound/preflight");
  return data;
}

export interface OutboundHeaderMedia {
  media_id: string;
  filename: string;
  format: string;
}

/** Sube el fichero de cabecera de la plantilla. Se reutiliza en los N envíos. */
export async function uploadOutboundHeaderMedia(
  formato: "image" | "video" | "document",
  file: File,
): Promise<OutboundHeaderMedia> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<OutboundHeaderMedia>(
    "/admin/outbound/header-media",
    form,
    { params: { format: formato } },
  );
  return data;
}

// ---------- Variables de plantilla amigables + autorrelleno ----------
//
// El envío real (outboundSend) sigue recibiendo un array ORDENADO de strings:
// variables[idx-1] -> {{idx}}. Estos endpoints solo mejoran cómo se RECOGEN
// esos valores: dan nombres amigables por variable y, opcionalmente, enlazan
// cada {{idx}} a un campo del contacto para autorrellenarlo.

// Config de una variable de plantilla. idx es 1-based ({{idx}} del cuerpo).
export interface TemplateVar {
  idx: number;
  nombre: string;
  contact_field: string | null;
}

// Resultado de resolver valores desde el contacto por teléfono. `variables`
// solo trae los idx enlazados con valor (clave = idx como string).
export interface ResolveResult {
  phone: string;
  contact_found: boolean;
  variables: Record<string, string>;
}

// Campos del contacto enlazables. Debe coincidir con ALLOWED_CONTACT_FIELDS del
// backend: cualquier otro valor se rechaza. "" = ninguno (relleno manual).
export interface ContactFieldOption {
  value: string; // "" = ninguno (manual)
  label: string;
}

export const CONTACT_FIELD_OPTIONS: ContactFieldOption[] = [
  { value: "", label: "Ninguno (manual)" },
  { value: "nombre", label: "Nombre" },
  { value: "email", label: "Email" },
  { value: "telefono", label: "Teléfono" },
  { value: "servicio_interes", label: "Servicio de interés" },
  { value: "origen", label: "Origen" },
];

export async function getTemplateVars(name: string, language: string): Promise<TemplateVar[]> {
  const { data } = await api.get<TemplateVar[]>(
    `/admin/whatsapp/templates/${encodeURIComponent(name)}/vars`,
    { params: { language } },
  );
  return data;
}

export async function putTemplateVars(
  name: string,
  body: { language: string; vars: TemplateVar[] },
): Promise<TemplateVar[]> {
  const { data } = await api.put<TemplateVar[]>(
    `/admin/whatsapp/templates/${encodeURIComponent(name)}/vars`,
    body,
  );
  return data;
}

export async function resolveTemplateVars(
  name: string,
  body: { language: string; phones: string[] },
): Promise<ResolveResult[]> {
  const { data } = await api.post<ResolveResult[]>(
    `/admin/whatsapp/templates/${encodeURIComponent(name)}/resolve`,
    body,
  );
  return data;
}

// ---------- Envío masivo en segundo plano (jobs) ----------
//
// A diferencia de outboundSend (síncrono, para envíos individuales), aquí
// creamos un JOB que el backend procesa "poco a poco" en segundo plano. La UI
// hace polling de getOutboundJob() para ver el progreso.

export interface OutboundJob {
  id: string;
  template_name: string;
  language: string;
  status: "queued" | "running" | "canceled" | "done" | "failed";
  total: number;
  sent_ok: number;
  sent_error: number;
  throttle_min_seconds: number;
  throttle_max_seconds: number;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
}

export interface OutboundJobRecipientResult {
  phone: string;
  status: string;
  external_id?: string | null;
  error?: string | null;
  sent_at?: string | null;
}

export interface OutboundJobDetail extends OutboundJob {
  errors: OutboundJobRecipientResult[];
  // El recorte de errores ahora se anuncia (antes cortaba a 200 en silencio).
  errors_total: number;
  errors_truncated: boolean;
  pending: number;
}

export interface OutboundJobHeader {
  format: "text" | "image" | "video" | "document";
  media_id?: string | null;
  link?: string | null;
  filename?: string | null;
  variables?: string[];
}

export interface CreateOutboundJobBody {
  template_name: string;
  language: string;
  throttle_min_seconds: number;
  throttle_max_seconds: number;
  recipients: OutboundRecipient[];
  header?: OutboundJobHeader | null;
}

export async function createOutboundJob(body: CreateOutboundJobBody): Promise<OutboundJob> {
  const { data } = await api.post<OutboundJob>("/admin/outbound/jobs", body);
  return data;
}

export async function listOutboundJobs(
  page = 1,
  pageSize = 20,
): Promise<Page<OutboundJob>> {
  const { data } = await api.get<Page<OutboundJob>>("/admin/outbound/jobs", {
    params: { page, page_size: pageSize },
  });
  return data;
}

export async function getOutboundJob(id: string): Promise<OutboundJobDetail> {
  const { data } = await api.get<OutboundJobDetail>(`/admin/outbound/jobs/${id}`);
  return data;
}

/** Destinatarios del envío. Sin filtro trae todos; con "ok", quién SÍ recibió. */
export async function listOutboundJobRecipients(
  id: string,
  opts: { status?: "ok" | "error" | "pending"; page?: number; page_size?: number } = {},
): Promise<Page<OutboundJobRecipientResult>> {
  const { data } = await api.get<Page<OutboundJobRecipientResult>>(
    `/admin/outbound/jobs/${id}/recipients`,
    { params: { status: opts.status, page: opts.page ?? 1, page_size: opts.page_size ?? 50 } },
  );
  return data;
}

/** Descarga el CSV de resultados. Va por axios para que lleve el token. */
export async function downloadOutboundJobCsv(id: string): Promise<Blob> {
  const { data } = await api.get<Blob>(`/admin/outbound/jobs/${id}/export.csv`, {
    responseType: "blob",
  });
  return data;
}

/** Crea un envío NUEVO con los destinatarios que fallaron o quedaron pendientes. */
export async function retryOutboundJob(id: string): Promise<OutboundJob> {
  const { data } = await api.post<OutboundJob>(`/admin/outbound/jobs/${id}/retry`);
  return data;
}

export async function cancelOutboundJob(id: string): Promise<OutboundJob> {
  const { data } = await api.post<OutboundJob>(`/admin/outbound/jobs/${id}/cancel`);
  return data;
}

// ---------- Baja permanente de difusiones (opt-out) ----------
//
// La blocklist de Redis caduca (24 h / 30 días); esto no. Quien pide la baja no
// vuelve a entrar en ninguna campaña.

export interface OutboundOptOut {
  phone: string;
  source: string;
  reason?: string | null;
  created_at: string;
}

export async function listOutboundOptOuts(
  opts: { search?: string; page?: number; page_size?: number } = {},
): Promise<Page<OutboundOptOut>> {
  const { data } = await api.get<Page<OutboundOptOut>>("/admin/outbound/optouts", {
    params: { search: opts.search, page: opts.page ?? 1, page_size: opts.page_size ?? 50 },
  });
  return data;
}

export async function checkOutboundOptOut(
  phone: string,
): Promise<{ opted_out: boolean; source?: string; reason?: string | null; created_at?: string }> {
  const { data } = await api.get("/admin/outbound/optouts/check", { params: { phone } });
  return data;
}

export async function createOutboundOptOut(
  phone: string,
  reason?: string,
): Promise<OutboundOptOut> {
  const { data } = await api.post<OutboundOptOut>("/admin/outbound/optouts", {
    phone,
    reason,
  });
  return data;
}

export async function deleteOutboundOptOut(phone: string): Promise<void> {
  await api.delete("/admin/outbound/optouts", { params: { phone } });
}

export interface ExternalAPIOut {
  id: string;
  provider: string;
  name: string;
  is_active: boolean;
  extra: Record<string, unknown>;
  legacy_credentials_keys: string[];
  created_at: string;
}

export async function listExternalApis(): Promise<ExternalAPIOut[]> {
  const { data } = await api.get<ExternalAPIOut[]>("/admin/external-apis");
  return data;
}

// ---------- F3A — Agents CRUD ----------

export interface AgentChannelRef {
  id: string;
  name: string;
  type: string;
  enabled: boolean;
}

// Naturaleza del agente: atiende TEXTO (WhatsApp, widget web, Instagram,
// email) o VOZ (llamadas). No es cosmético: la resolución por canal del backend
// lo respeta, así que un agente de voz no atiende un chat ni al revés.
export type AgentKind = "text" | "voice";

export interface AgentOut {
  id: string;
  name: string;
  kind: AgentKind;
  prompt_system: string;
  model_name: string;
  temperature: number;
  max_tokens: number;
  buffer_seconds: number;
  response_split_max_parts: number;
  context_window: number;
  handoff_bridge_message: string | null;
  monthly_budget_usd: number | null;
  tools_enabled: string[] | null;
  is_active: boolean;
  llm_provider_id: string | null;
  fallback_provider_id: string | null;
  fallback_model: string | null;
  created_at: string;
  updated_at: string;
  channels: AgentChannelRef[];
}

export interface AgentIn {
  name: string;
  // Omitirlo al crear = texto; omitirlo al editar = se conserva el que tuviera.
  kind?: AgentKind;
  prompt_system: string;
  model_name: string;
  temperature?: number | null;
  max_tokens?: number | null;
  buffer_seconds: number;
  response_split_max_parts: number;
  context_window: number;
  handoff_bridge_message?: string | null;
  monthly_budget_usd?: number | null;
  tools_enabled?: string[] | null;
  is_active: boolean;
  llm_provider_id?: string | null;
  fallback_provider_id?: string | null;
  fallback_model?: string | null;
}

// --- Proveedores LLM configurables --------------------------------------
export interface LLMProviderOut {
  id: string;
  name: string;
  base_url: string | null;
  api_key_masked: string | null;
  has_key: boolean;
  accepts_temperature: boolean;
  is_default: boolean;
  is_fallback: boolean;
  fallback_model: string | null;
}

export interface LLMProviderIn {
  name: string;
  base_url?: string | null;
  api_key?: string | null; // null = no tocar en update; "" = borrar
  accepts_temperature: boolean;
  is_default: boolean;
  is_fallback: boolean;
  fallback_model: string | null;
}

export interface LLMProviderTest {
  ok: boolean;
  message: string;
  models: string[];
}

export async function listLLMProviders(): Promise<LLMProviderOut[]> {
  const { data } = await api.get<LLMProviderOut[]>("/admin/llm-providers");
  return data;
}

export async function createLLMProvider(body: LLMProviderIn): Promise<LLMProviderOut> {
  const { data } = await api.post<LLMProviderOut>("/admin/llm-providers", body);
  return data;
}

export async function updateLLMProvider(id: string, body: LLMProviderIn): Promise<LLMProviderOut> {
  const { data } = await api.put<LLMProviderOut>(`/admin/llm-providers/${id}`, body);
  return data;
}

export async function deleteLLMProvider(id: string): Promise<void> {
  await api.delete(`/admin/llm-providers/${id}`);
}

export async function testLLMProvider(id: string): Promise<LLMProviderTest> {
  const { data } = await api.post<LLMProviderTest>(`/admin/llm-providers/${id}/test`);
  return data;
}

export async function listAgents(): Promise<AgentOut[]> {
  const { data } = await api.get<AgentOut[]>("/admin/agents");
  return data;
}

export async function getAgent(id: string): Promise<AgentOut> {
  const { data } = await api.get<AgentOut>(`/admin/agents/${id}`);
  return data;
}

// Lo que recibe el modelo: seguridad + prompt del agente + reglas aprendidas
// (+ el resumen rodante, si se pide para una conversación concreta).
export interface EffectivePrompt {
  security: string;
  base: string;
  learned_rules: string;
  /** Resumen rodante de la conversación pedida. "" si no se pidió ninguna. */
  rolling_summary: string;
  effective: string;
}

/**
 * Con `conversationId`, el prompt incluye además el resumen rodante de esa
 * conversación, en la misma posición en que lo mete el runtime. Sin él, en una
 * conversación larga lo que se enseña NO es lo que recibe el modelo.
 */
export async function getAgentEffectivePrompt(
  id: string,
  conversationId?: string | null,
): Promise<EffectivePrompt> {
  const { data } = await api.get<EffectivePrompt>(
    `/admin/agents/${id}/effective-prompt`,
    conversationId ? { params: { conversation_id: conversationId } } : undefined,
  );
  return data;
}

// Herramientas REALMENTE registradas en el backend. Antes la lista estaba
// escrita a mano en la pantalla de Agentes y se desincronizaba sola.
export interface ToolOut {
  name: string;
  description: string;
}

export async function listRegisteredTools(): Promise<ToolOut[]> {
  const { data } = await api.get<ToolOut[]>("/admin/tools");
  return data;
}

export async function createAgent(body: AgentIn): Promise<AgentOut> {
  const { data } = await api.post<AgentOut>("/admin/agents", body);
  return data;
}

export async function updateAgent(id: string, body: AgentIn): Promise<AgentOut> {
  const { data } = await api.patch<AgentOut>(`/admin/agents/${id}`, body);
  return data;
}

export async function deleteAgent(id: string): Promise<void> {
  await api.delete(`/admin/agents/${id}`);
}

export interface AgentPromptHistoryOut {
  id: string;
  prompt_system: string;
  model_name: string | null;
  created_at: string;
  created_by_email: string | null;
}

export async function getAgentPromptHistory(agentId: string): Promise<AgentPromptHistoryOut[]> {
  const { data } = await api.get<AgentPromptHistoryOut[]>(`/admin/agents/${agentId}/prompt-history`);
  return data;
}

export async function restoreAgentPrompt(agentId: string, historyId: string): Promise<AgentOut> {
  const { data } = await api.post<AgentOut>(
    `/admin/agents/${agentId}/prompt-history/${historyId}/restore`,
  );
  return data;
}

// ── Tokens de agente (API para agentes externos) ────────────────────────────

export interface AgentTokenOut {
  id: string;
  name: string;
  token_prefix: string;
  scopes: string[];
  active: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface AgentTokenCreatedOut extends AgentTokenOut {
  /** Token en claro. SOLO llega en la respuesta de creación. */
  token: string;
}

export async function listAgentTokens(): Promise<AgentTokenOut[]> {
  const { data } = await api.get<AgentTokenOut[]>("/admin/agent-tokens");
  return data;
}

export async function createAgentToken(body: {
  name: string;
  scopes: string[];
  expires_days?: number | null;
}): Promise<AgentTokenCreatedOut> {
  const { data } = await api.post<AgentTokenCreatedOut>("/admin/agent-tokens", body);
  return data;
}

export async function revokeAgentToken(id: string): Promise<AgentTokenOut> {
  const { data } = await api.post<AgentTokenOut>(`/admin/agent-tokens/${id}/revoke`);
  return data;
}
