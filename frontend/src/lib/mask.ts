// Enmascarado de PII para el "Modo privacidad" (ver store/privacy.ts).
// Criterio de enmascarado: dejar 3 caracteres visibles y el resto "***"
// (estilo banca con IBAN/saldos). Funciones PURAS y reactivas al flag `enabled`:
// si está apagado devuelven el valor tal cual, así los sitios de llamada quedan
// limpios — `maskName(v, enabled)`.
//
// Importante: si el valor es vacío/null/undefined se devuelve sin tocar, para
// que las cadenas de fallback existentes (`nombre || telefono || "Sin nombre"`)
// sigan funcionando igual.

import type { Contact } from "@/types";

const VISIBLE = 3;

function head(value: string): string {
  // Mantiene los primeros VISIBLE caracteres "de verdad" y añade "***".
  // Si el valor es más corto, muestra lo que haya + "***".
  return `${value.slice(0, VISIBLE)}***`;
}

/** Nombre real → "And***". */
export function maskName<T extends string | null | undefined>(value: T, enabled: boolean): T {
  if (!enabled || !value) return value;
  return head(value.trim()) as T;
}

/** Teléfono → "+34***" (conserva los 3 primeros caracteres, incluido el "+"). */
export function maskPhone<T extends string | null | undefined>(value: T, enabled: boolean): T {
  if (!enabled || !value) return value;
  return head(value.trim()) as T;
}

/** Email → "and***@***" (oculta también el dominio). */
export function maskEmail<T extends string | null | undefined>(value: T, enabled: boolean): T {
  if (!enabled || !value) return value;
  const v = value.trim();
  const at = v.indexOf("@");
  const local = at >= 0 ? v.slice(0, at) : v;
  return `${local.slice(0, VISIBLE)}***@***` as T;
}

/** @usuario de Instagram → "@usu***" (conserva la arroba inicial). */
export function maskHandle<T extends string | null | undefined>(value: T, enabled: boolean): T {
  if (!enabled || !value) return value;
  const v = value.trim();
  const at = v.startsWith("@");
  const body = at ? v.slice(1) : v;
  return `${at ? "@" : ""}${head(body)}` as T;
}

/**
 * Copia del contacto con los campos de identidad enmascarados. Si el modo está
 * apagado devuelve el MISMO objeto (sin copia). Centraliza el enmascarado para
 * que los componentes solo tengan que envolver el contacto una vez.
 */
export function maskContact<T extends Partial<Contact> | null | undefined>(
  contact: T,
  enabled: boolean
): T {
  if (!enabled || !contact) return contact;
  return {
    ...contact,
    nombre: maskName(contact.nombre ?? null, enabled),
    telefono: maskPhone(contact.telefono ?? "", enabled),
    email: maskEmail(contact.email ?? null, enabled),
    social_handle: maskHandle(contact.social_handle ?? null, enabled),
  } as T;
}

/**
 * Iniciales del avatar para el modo privacidad: glifo neutro que NO revela la
 * primera letra real del nombre. Si el modo está apagado, devuelve las que ya
 * venían calculadas.
 */
export function maskInitials(real: string, enabled: boolean): string {
  return enabled ? "•" : real;
}
