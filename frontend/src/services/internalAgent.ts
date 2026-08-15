// Cliente del Agente Interno (chat read-only del admin).
//
// Usa fetch con streaming (ReadableStream) en vez de EventSource porque
// EventSource no soporta headers custom (Authorization). El parser SSE
// es minimo: dividimos por "\n\n" y leemos lineas "event:" y "data:".

import { api, apiBaseHttp } from "./api";
import { getToken } from "@/lib/session";

export type KBProposalPayload = {
  proposal_id: string;
  kind: "edit" | "create";
  document_id: string | null;
  document_nombre: string | null;
  titulo: string | null;
  contenido: string;
  motivo: string | null;
  created_at: string;
};

export type InternalAgentEvent =
  | { event: "status"; data: { message: string } }
  | { event: "tool_call"; data: { name: string; args: Record<string, unknown> } }
  | {
      event: "tool_result";
      data: { name: string; ok: boolean; summary: string };
    }
  | { event: "proposal"; data: KBProposalPayload }
  | { event: "message"; data: { content: string } }
  | {
      event: "done";
      data: {
        usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
        iterations: number;
      };
    }
  | { event: "error"; data: { message: string } };

export type InternalAgentHistoryMsg = { role: "user" | "assistant"; content: string };

export type InternalAgentConfig = {
  model_name: string;
  temperature: number;
  max_tokens: number;
  monthly_budget_usd: number | null;
  daily_query_limit: number;
  system_prompt: string;
  llm_provider_id: string | null;
  updated_at: string;
};

export type InternalAgentStatus = {
  daily_used: number;
  daily_limit: number;
  monthly_cost_usd: number;
  monthly_budget_usd: number | null;
  model_name: string;
};

// ------------------------------ Config / Status ------------------------------

export async function getInternalAgentConfig(): Promise<InternalAgentConfig> {
  const { data } = await api.get<InternalAgentConfig>("/admin/internal-agent/config");
  return data;
}

export async function updateInternalAgentConfig(
  patch: Partial<InternalAgentConfig> & { clear_monthly_budget?: boolean }
): Promise<InternalAgentConfig> {
  const { data } = await api.put<InternalAgentConfig>(
    "/admin/internal-agent/config",
    patch
  );
  return data;
}

export async function getInternalAgentStatus(): Promise<InternalAgentStatus> {
  const { data } = await api.get<InternalAgentStatus>("/admin/internal-agent/status");
  return data;
}

export async function getInternalAgentDefaultPrompt(): Promise<string> {
  const { data } = await api.get<{ system_prompt: string }>(
    "/admin/internal-agent/config/default-prompt"
  );
  return data.system_prompt;
}

// ------------------------------ Propuestas de KB ------------------------------

export type KBProposalOut = {
  id: string;
  kind: "edit" | "create";
  document_id: string | null;
  document_nombre: string | null;
  titulo: string | null;
  contenido: string;
  motivo: string | null;
  status: "pendiente" | "aplicada" | "descartada";
  created_at: string;
  applied_document_id: string | null;
};

export async function applyKBProposal(id: string): Promise<KBProposalOut> {
  const { data } = await api.post<KBProposalOut>(`/admin/kb-proposals/${id}/apply`);
  return data;
}

export async function discardKBProposal(id: string): Promise<KBProposalOut> {
  const { data } = await api.post<KBProposalOut>(`/admin/kb-proposals/${id}/discard`);
  return data;
}

// ------------------------------ Ask (SSE) ------------------------------

export type AskParams = {
  question: string;
  history: InternalAgentHistoryMsg[];
  onEvent: (ev: InternalAgentEvent) => void;
  signal?: AbortSignal;
};

export async function askInternalAgent({
  question,
  history,
  onEvent,
  signal,
}: AskParams): Promise<void> {
  const token = getToken();
  const url = `${apiBaseHttp()}/api/v1/admin/internal-agent/ask`;

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: token ? `Bearer ${token}` : "",
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ question, history }),
    signal,
  });

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const j = await resp.json();
      if (j?.detail) detail = String(j.detail);
    } catch {
      // ignore
    }
    onEvent({ event: "error", data: { message: detail } });
    return;
  }

  if (!resp.body) {
    onEvent({ event: "error", data: { message: "Sin body en la respuesta SSE" } });
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE: eventos separados por "\n\n". Procesamos los completos y
      // dejamos el resto (parcial) en el buffer.
      let sepIdx: number;
      while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);
        const ev = parseSseChunk(raw);
        if (ev) onEvent(ev);
      }
    }
    // Resto en buffer si el server cerro sin "\n\n" final.
    if (buffer.trim()) {
      const ev = parseSseChunk(buffer);
      if (ev) onEvent(ev);
    }
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    onEvent({
      event: "error",
      data: { message: (err as Error).message || "Error leyendo SSE" },
    });
  }
}

function parseSseChunk(raw: string): InternalAgentEvent | null {
  // Formato: "event: name\ndata: {...json...}\n"
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) return null;
  try {
    const data = JSON.parse(dataLines.join("\n"));
    return { event, data } as InternalAgentEvent;
  } catch {
    return null;
  }
}
