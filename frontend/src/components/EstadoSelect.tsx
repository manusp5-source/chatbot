import { useEffect, useState } from "react";
import { updateContact } from "@/services/contacts";

// Constantes de estado (pipeline del CRM), centralizadas para reutilizar en la
// lista de contactos y en la ficha.
export const ESTADO_OPTIONS = [
  { value: "contacto", label: "Contacto" },
  { value: "solicitud_presupuesto", label: "Solicitud presupuesto" },
  { value: "seguimiento", label: "Seguimiento" },
  { value: "cliente", label: "Cliente" },
  { value: "perdido", label: "Perdido" },
  { value: "no_cualifica", label: "No cualifica" },
] as const;

export const ESTADO_LABEL: Record<string, string> = Object.fromEntries(
  ESTADO_OPTIONS.map((o) => [o.value, o.label]),
);

export const ESTADO_COLOR: Record<string, string> = {
  contacto: "bg-paper2 text-ink2",
  solicitud_presupuesto: "bg-state-warn/15 text-state-warn",
  seguimiento: "bg-brand/15 text-brand-ink",
  cliente: "bg-state-ok/15 text-state-ok",
  perdido: "bg-state-bad/15 text-state-bad",
  no_cualifica: "bg-paper3 text-ink3",
};

interface Props {
  contactId: string;
  value: string;
  onChanged?: (estado: string) => void;
  className?: string;
}

/**
 * Selector de estado editable inline: un <select> con el color del estado que,
 * al cambiar, persiste con PATCH /contacts/{id} de forma optimista (revierte si
 * falla). La flecha nativa del select señala que es editable.
 */
export function EstadoSelect({ contactId, value, onChanged, className }: Props) {
  const [estado, setEstado] = useState(value);
  const [busy, setBusy] = useState(false);

  useEffect(() => setEstado(value), [value]);

  async function change(next: string) {
    if (next === estado) return;
    const prev = estado;
    setEstado(next);
    setBusy(true);
    try {
      await updateContact(contactId, { estado: next } as never);
      onChanged?.(next);
    } catch {
      setEstado(prev); // revertir si la API falla
    } finally {
      setBusy(false);
    }
  }

  return (
    <select
      value={estado}
      disabled={busy}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => {
        e.stopPropagation();
        void change(e.target.value);
      }}
      title="Cambiar estado"
      className={
        "text-[11px] font-medium rounded-full pl-2 pr-1 py-0.5 cursor-pointer border-0 outline-none focus:ring-1 focus:ring-line " +
        (ESTADO_COLOR[estado] ?? "bg-paper2 text-ink3") +
        (busy ? " opacity-60" : "") +
        (className ? " " + className : "")
      }
    >
      {ESTADO_OPTIONS.map((o) => (
        <option key={o.value} value={o.value} className="bg-paper text-ink">
          {o.label}
        </option>
      ))}
    </select>
  );
}
