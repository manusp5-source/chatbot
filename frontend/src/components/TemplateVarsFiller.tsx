import { Fragment } from "react";
import type { TemplateVar } from "@/services/admin";

/**
 * Relleno "huecos en la frase" + preview para variables de plantilla WhatsApp.
 *
 * CONTRATO: el envío real recibe siempre un array ORDENADO `variables` donde
 * `variables[idx-1]` -> `{{idx}}`. Estos componentes solo mejoran cómo se
 * RECOGEN esos valores (etiquetas amigables, huecos incrustados en la frase).
 * El array de strings nunca cambia de forma.
 */

// Divide el body por los marcadores {{n}} conservando el texto entre ellos.
// Devuelve segmentos de texto y, entre cada par, el número de variable (1-based)
// del hueco. `placeholders` siempre tiene length = (text.length - 1).
export interface ParsedBody {
  segments: string[];
  // índices 1-based de variable, en orden de aparición en el body
  slotVarNumbers: number[];
}

const SLOT_RE = /\{\{\s*(\d+)\s*\}\}/g;

export function parseTemplateBody(body: string | null | undefined): ParsedBody {
  const segments: string[] = [];
  const slotVarNumbers: number[] = [];
  if (!body) return { segments: [""], slotVarNumbers: [] };
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  SLOT_RE.lastIndex = 0;
  while ((m = SLOT_RE.exec(body)) !== null) {
    segments.push(body.slice(lastIndex, m.index));
    slotVarNumbers.push(Number(m[1]));
    lastIndex = m.index + m[0].length;
  }
  segments.push(body.slice(lastIndex));
  return { segments, slotVarNumbers };
}

// Etiqueta amigable de una variable: nombre configurado o fallback "Variable n".
export function varLabel(varNumber: number, vars: TemplateVar[]): string {
  const cfg = vars.find((v) => v.idx === varNumber);
  const nombre = cfg?.nombre?.trim();
  return nombre || `Variable ${varNumber}`;
}

interface InlineFillProps {
  body: string | null | undefined;
  // valores actuales por idx (1-based) -> values[idx-1]
  values: string[];
  vars: TemplateVar[];
  onChange: (varNumber: number, value: string) => void;
  // tamaño compacto (OutboundPage usa text-xs)
  compact?: boolean;
}

/**
 * Renderiza el cuerpo de la plantilla con un input incrustado en cada hueco
 * {{n}}, en orden. La etiqueta/placeholder de cada input = nombre amigable.
 * Si el body no tiene huecos, cae a una lista de inputs etiquetados (fallback).
 */
export function InlineTemplateFill({
  body,
  values,
  vars,
  onChange,
  compact,
}: InlineFillProps) {
  const { segments, slotVarNumbers } = parseTemplateBody(body);
  const inputCls = compact
    ? "inline-block min-w-[6rem] max-w-full align-baseline px-1.5 py-0.5 mx-0.5 rounded-coro-sm border border-brand-ink/40 bg-paper text-xs text-ink focus:outline-none focus:border-brand-ink"
    : "inline-block min-w-[6.5rem] max-w-full align-baseline px-2 py-0.5 mx-0.5 rounded-coro-sm border border-brand-ink/40 bg-paper text-sm text-ink focus:outline-none focus:border-brand-ink";

  // Body sin huecos: no hay nada inline. Devolvemos el texto tal cual (el caller
  // decide si además muestra inputs de fallback).
  if (slotVarNumbers.length === 0) {
    return (
      <p className={compact ? "text-xs text-ink2 whitespace-pre-wrap" : "text-sm text-ink2 whitespace-pre-wrap"}>
        {segments.join("")}
      </p>
    );
  }

  return (
    <p
      className={
        compact
          ? "text-xs text-ink2 leading-7 whitespace-pre-wrap"
          : "text-sm text-ink2 leading-8 whitespace-pre-wrap"
      }
    >
      {segments.map((seg, i) => (
        <Fragment key={i}>
          {seg}
          {i < slotVarNumbers.length && (
            <input
              type="text"
              value={values[slotVarNumbers[i] - 1] ?? ""}
              onChange={(e) => onChange(slotVarNumbers[i], e.target.value)}
              placeholder={varLabel(slotVarNumbers[i], vars)}
              aria-label={varLabel(slotVarNumbers[i], vars)}
              className={inputCls}
            />
          )}
        </Fragment>
      ))}
    </p>
  );
}

interface PreviewProps {
  body: string | null | undefined;
  values: string[];
  vars: TemplateVar[];
  compact?: boolean;
}

/**
 * Preview en vivo: el mensaje final con los valores sustituidos. Los huecos
 * vacíos se resaltan como «▢ {nombre}». Todo se renderiza como texto (React
 * escapa; no usamos dangerouslySetInnerHTML).
 */
export function TemplatePreview({ body, values, vars, compact }: PreviewProps) {
  const { segments, slotVarNumbers } = parseTemplateBody(body);
  if (slotVarNumbers.length === 0) {
    return (
      <div
        className={
          compact
            ? "rounded-coro-sm bg-paper2 p-2.5 text-xs text-ink2 whitespace-pre-wrap"
            : "rounded-coro-sm bg-paper2 p-3 text-sm text-ink2 whitespace-pre-wrap"
        }
      >
        {segments.join("")}
      </div>
    );
  }
  return (
    <div
      className={
        compact
          ? "rounded-coro-sm bg-paper2 p-2.5 text-xs text-ink2 whitespace-pre-wrap leading-relaxed"
          : "rounded-coro-sm bg-paper2 p-3 text-sm text-ink2 whitespace-pre-wrap leading-relaxed"
      }
    >
      {segments.map((seg, i) => (
        <Fragment key={i}>
          {seg}
          {i < slotVarNumbers.length &&
            (() => {
              const val = (values[slotVarNumbers[i] - 1] ?? "").trim();
              if (val) {
                return <span className="font-medium text-ink">{val}</span>;
              }
              return (
                <span className="text-state-warn font-medium">
                  ▢ {`{${varLabel(slotVarNumbers[i], vars)}}`}
                </span>
              );
            })()}
        </Fragment>
      ))}
    </div>
  );
}

// Helper de validación: índices (1-based) de variables con hueco vacío.
// Considera SOLO las variables hasta `count` (las que la plantilla declara).
export function emptyVarNumbers(values: string[], count: number): number[] {
  const out: number[] = [];
  for (let n = 1; n <= count; n++) {
    if (!(values[n - 1] ?? "").trim()) out.push(n);
  }
  return out;
}
