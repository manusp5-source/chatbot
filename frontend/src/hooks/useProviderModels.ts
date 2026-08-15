import { useEffect, useState } from "react";
import { testLLMProvider } from "@/services/admin";

/**
 * Modelos REALES del proveedor LLM elegido, para las sugerencias del campo
 * "Modelo" (agentes, clasificador, agente interno). Consulta el endpoint de
 * test del proveedor (`GET {base_url}/models`). Best-effort: si el proveedor
 * no expone la lista (p. ej. Z.AI Coding Plan) devuelve [] y el llamante cae
 * a sus sugerencias estáticas — el campo sigue siendo texto libre.
 */
export function useProviderModels(providerId: string | null | undefined) {
  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!providerId) {
      setModels([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    testLLMProvider(providerId)
      .then((r) => {
        if (!cancelled) setModels(r.ok ? r.models : []);
      })
      .catch(() => {
        if (!cancelled) setModels([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [providerId]);

  return { models, loading };
}
