import { api } from "@/services/api";

// Autoaprendizaje · Fases 2 y 3 — "Aprendizajes". Huecos de conocimiento que la
// operadora revisa: el agente derivó a un humano ("handoff"), la base de
// conocimiento no tuvo resultados ("kb_miss"), la operadora corrigió lo mismo
// varias veces ("correction"), o una pregunta se repite mucho entre clientes
// ("faq", Fase 3). Al aprobar, según `proposal_kind`:
//   - "content" (o sin valor): la respuesta se añade a la base de conocimiento.
//   - "style": la propuesta se guarda como regla de estilo y se inyecta en el
//     prompt del agente (cambia su tono).
export interface KnowledgeGap {
  id: string;
  trigger: "handoff" | "kb_miss" | "correction" | "faq";
  proposal_kind: "content" | "style" | null;
  question: string;
  suggested_answer: string | null;
  status: "pendiente" | "aprobado" | "descartado";
  conversation_id: string | null;
  created_at: string;
  // Solo en la respuesta de "aprobar": aviso si el agente aún no recupera la
  // Q&A recién aprendida (embeddings sin clave, respuesta muy corta). null = OK.
  verification_warning?: string | null;
}

// Regla de estilo aprendida y activa (se inyecta en el prompt del agente).
export interface LearnedRule {
  id: string;
  text: string;
  active: boolean;
  source_gap_id: string | null;
  created_at: string;
}

export async function listGaps(status = "pendiente"): Promise<KnowledgeGap[]> {
  const { data } = await api.get<KnowledgeGap[]>("/admin/learning/gaps", {
    params: { status },
  });
  return data;
}

export async function approveGap(id: string, answer: string): Promise<KnowledgeGap> {
  const { data } = await api.post<KnowledgeGap>(`/admin/learning/gaps/${id}/approve`, {
    answer,
  });
  return data;
}

export async function discardGap(id: string): Promise<void> {
  await api.post(`/admin/learning/gaps/${id}/discard`);
}

// Correcciones EN CRUDO de la operadora (materia prima del aprendizaje). Sirven
// para ver al instante que el feedback se guarda, sin esperar al detector horario.
export interface AgentCorrection {
  id: string;
  canal: string | null;
  instruction: string;
  manual: boolean; // edición a mano del borrador
  original_preview: string | null;
  resulting_preview: string | null;
  processed: boolean;
  promoted_to: "style" | "content" | null; // convertida a mano en aprendizaje
  created_at: string;
}

export interface CorrectionsResponse {
  total: number;
  pending: number;
  items: AgentCorrection[];
}

export async function listCorrections(limit = 50): Promise<CorrectionsResponse> {
  const { data } = await api.get<CorrectionsResponse>("/admin/learning/corrections", {
    params: { limit },
  });
  return data;
}

// Convierte una corrección suelta en aprendizaje al instante (sin esperar a las
// 2 repeticiones). kind: "style" → prompt del agente; "content" → base de conocimiento.
export async function promoteCorrection(
  id: string,
  kind: "style" | "content",
  text: string,
): Promise<AgentCorrection> {
  const { data } = await api.post<AgentCorrection>(
    `/admin/learning/corrections/${id}/promote`,
    { kind, text },
  );
  return data;
}

export async function listRules(): Promise<LearnedRule[]> {
  const { data } = await api.get<LearnedRule[]>("/admin/learning/rules", {
    params: { active_only: true },
  });
  return data;
}

export async function deactivateRule(id: string): Promise<void> {
  await api.post(`/admin/learning/rules/${id}/deactivate`);
}
