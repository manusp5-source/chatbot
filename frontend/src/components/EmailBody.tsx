import { useMemo, useState } from "react";
import DOMPurify from "dompurify";
import { ImageOff } from "lucide-react";

/**
 * 5d — Render SEGURO del cuerpo de un correo.
 *
 * El HTML de un correo es contenido NO CONFIABLE: nunca se renderiza crudo.
 * Aquí lo saneamos con DOMPurify (lista blanca de etiquetas y atributos) y solo
 * inyectamos la SALIDA de DOMPurify vía dangerouslySetInnerHTML (jamás el crudo).
 *
 * Decisiones de seguridad/privacidad:
 *  - Lista blanca de etiquetas de formato (a, b, strong, listas, tablas, img…).
 *    PROHIBIDO: script, style (tag), iframe, object, embed, form, link, meta y
 *    todos los manejadores on* (DOMPurify los elimina por defecto).
 *  - Enlaces: se fuerzan target="_blank" + rel="noopener noreferrer nofollow".
 *  - Imágenes remotas BLOQUEADAS por defecto (evita píxeles de rastreo): su src
 *    http(s) se mueve a data-src (y srcset→data-srcset) para que NO carguen. Un
 *    banner "Mostrar imágenes" las restaura (re-saneando, esta vez permitiendo
 *    las imágenes). Igual con url(...) remoto en estilos inline (se neutraliza).
 *  - Si NO hay HTML, se renderiza el texto plano con whitespace-pre-wrap y se
 *    autolinkifican las URLs ESCAPANDO el resto del texto (sin dangerouslySet…
 *    sobre texto sin escapar).
 *
 * Limitaciones conocidas (v1): no resolvemos imágenes inline cid: (las del
 * propio correo embebidas como adjunto) — se tratan como remotas/bloqueadas y
 * no se muestran. El contenido se aísla visualmente en un contenedor con
 * overflow controlado e imágenes a max-width:100%.
 */

// Lista blanca de etiquetas permitidas (solo formato/estructura, nada activo).
const ALLOWED_TAGS = [
  "a", "b", "strong", "i", "em", "u", "s", "p", "br", "hr",
  "ul", "ol", "li", "blockquote",
  "h1", "h2", "h3", "h4", "h5", "h6",
  "span", "div", "table", "thead", "tbody", "tr", "td", "th",
  "pre", "code", "img",
];

// Atributos seguros. NADA de on* (DOMPurify ya los quita) ni src/href con
// esquemas peligrosos (DOMPurify filtra javascript:, data: en src, etc.).
const ALLOWED_ATTR = [
  "href", "title", "alt", "src", "srcset", "width", "height",
  "style", "colspan", "rowspan", "align", "target", "rel",
];

// Detecta una URL remota http(s) o protocol-relative (//host/...).
const REMOTE_SRC_RE = /^\s*(https?:)?\/\//i;
// url(...) remoto dentro de un atributo style.
const REMOTE_URL_IN_STYLE_RE = /url\(\s*['"]?\s*(https?:)?\/\//i;

// Estado de bloqueo de imágenes para el hook (DOMPurify usa hooks globales y el
// render es síncrono: fijamos el flag justo antes de sanitize y lo reseteamos
// después). `blockedImages` lo activa el hook si encontró alguna img remota.
let _blockImages = true;
let _blockedImages = false;

let _hookInstalled = false;
function installHookOnce(): void {
  if (_hookInstalled) return;
  _hookInstalled = true;

  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    const el = node as Element;
    const tag = el.tagName ? el.tagName.toLowerCase() : "";

    // Enlaces: abrir fuera y cortar la relación con la pestaña/origen.
    if (tag === "a" && el.getAttribute("href")) {
      el.setAttribute("target", "_blank");
      el.setAttribute("rel", "noopener noreferrer nofollow");
    }

    // Imágenes remotas: si toca bloquear, mover src/srcset a data-* para que
    // el navegador NO haga la petición (anti-tracking). Si NO toca bloquear
    // (el usuario pulsó "Mostrar imágenes"), las dejamos cargar tal cual.
    if (tag === "img") {
      const src = el.getAttribute("src") || "";
      const srcset = el.getAttribute("srcset") || "";
      const isRemote = REMOTE_SRC_RE.test(src) || REMOTE_SRC_RE.test(srcset);
      if (isRemote) {
        if (_blockImages) {
          _blockedImages = true;
          if (src) {
            el.setAttribute("data-src", src);
            el.removeAttribute("src");
          }
          if (srcset) {
            el.setAttribute("data-srcset", srcset);
            el.removeAttribute("srcset");
          }
        }
      } else if (src && !src.startsWith("data:")) {
        // src no remoto y no data: (p. ej. cid: de imagen inline que no
        // soportamos en v1) → lo quitamos para no dejar un roto/petición rara.
        el.removeAttribute("src");
      }
    }

    // Estilos inline con url(...) remoto: neutralizar para no filtrar peticiones
    // (background-image con pixel de rastreo). Si hay imágenes mostradas, lo
    // dejamos solo cuando el usuario decidió mostrarlas.
    const style = el.getAttribute("style");
    if (style && REMOTE_URL_IN_STYLE_RE.test(style)) {
      if (_blockImages) {
        _blockedImages = true;
        el.setAttribute("style", style.replace(/url\([^)]*\)/gi, "none"));
      }
    }
  });
}

interface SanitizeResult {
  html: string;
  blocked: boolean;
}

function sanitizeEmailHtml(raw: string, blockImages: boolean): SanitizeResult {
  installHookOnce();
  _blockImages = blockImages;
  _blockedImages = false;
  const clean = DOMPurify.sanitize(raw, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // Refuerzo explícito (DOMPurify ya los prohíbe, pero lo dejamos por claridad
    // y por si cambia un default en futuras versiones).
    FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "link", "meta", "base"],
    FORBID_ATTR: ["onerror", "onload", "onclick"],
    // No dejamos pasar data-* del HTML entrante. Nuestros data-src/data-srcset
    // los AÑADE el hook (afterSanitizeAttributes), que corre DESPUÉS del saneado,
    // así que sobreviven aunque aquí esté en false (más seguro: no filtramos
    // data-* arbitrarios del correo).
    ALLOW_DATA_ATTR: false,
  });
  const result = { html: clean, blocked: _blockImages && _blockedImages };
  _blockImages = true; // restaura el default seguro tras cada sanitize.
  return result;
}

// Escapa texto plano para inyectarlo seguro en HTML.
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Autolinkifica URLs (http/https/www) y mailto sobre texto ya ESCAPADO. Devuelve
// HTML seguro: el texto base va escapado y solo añadimos anclas <a> controladas.
const URL_OR_MAIL_RE =
  /((?:https?:\/\/|www\.)[^\s<]+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})/gi;

function autolinkPlainText(text: string): string {
  // Importante: escapamos PRIMERO; luego buscamos URLs sobre el texto escapado.
  // Las URLs no contienen caracteres que cambien al escapar salvo '&' en query;
  // por eso reconstruimos el href des-escapando solo '&amp;'→'&'.
  const escaped = escapeHtml(text);
  return escaped.replace(URL_OR_MAIL_RE, (match) => {
    const isEmail = match.includes("@") && !/^https?:|^www\./i.test(match);
    const hrefRaw = isEmail
      ? `mailto:${match}`
      : match.toLowerCase().startsWith("www.")
        ? `https://${match}`
        : match;
    // Des-escapamos &amp; en el href (queda dentro de un atributo entre comillas
    // dobles, así que escapamos comillas dobles por seguridad).
    const href = hrefRaw.replace(/&amp;/g, "&").replace(/"/g, "%22");
    const rel = isEmail ? "" : ' rel="noopener noreferrer nofollow"';
    const target = isEmail ? "" : ' target="_blank"';
    return `<a href="${escapeHtml(href)}"${target}${rel}>${match}</a>`;
  });
}

export function EmailBody({ html, text }: { html?: string | null; text: string }) {
  const [showImages, setShowImages] = useState(false);

  const hasHtml = !!(html && html.trim());

  // Saneado del HTML (memoizado por html + showImages). Si no hay HTML, este
  // bloque no se usa (renderizamos el texto plano autolinkificado).
  const sanitized = useMemo<SanitizeResult>(() => {
    if (!hasHtml) return { html: "", blocked: false };
    return sanitizeEmailHtml(html as string, !showImages);
  }, [html, hasHtml, showImages]);

  // Texto plano autolinkificado (memoizado). Solo se usa si no hay HTML.
  const plainHtml = useMemo(
    () => (hasHtml ? "" : autolinkPlainText(text || "")),
    [hasHtml, text],
  );

  if (hasHtml) {
    return (
      <div className="email-body-wrap">
        {sanitized.blocked && !showImages && (
          <button
            type="button"
            onClick={() => setShowImages(true)}
            className="mb-2 inline-flex items-center gap-1.5 rounded-coro-sm border border-line bg-paper2 px-2.5 py-1 text-[11px] text-ink2 hover:bg-paper3 transition-colors"
            title="Las imágenes remotas están bloqueadas por privacidad (evita píxeles de rastreo)"
          >
            <ImageOff className="w-3 h-3" />
            Imágenes bloqueadas · Mostrar imágenes
          </button>
        )}
        {/* SOLO la salida de DOMPurify llega aquí, nunca el HTML crudo. */}
        <div
          className="email-html-content max-w-full overflow-x-auto break-words [&_img]:max-w-full [&_img]:h-auto [&_a]:text-brand-ink [&_a]:underline [&_table]:max-w-full [&_table]:border-collapse [&_a]:break-all"
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: sanitized.html }}
        />
      </div>
    );
  }

  // Sin HTML: texto plano con saltos respetados + autolink (texto escapado).
  return (
    <div
      className="whitespace-pre-wrap break-words [&_a]:text-brand-ink [&_a]:underline"
      // plainHtml está construido sobre texto ESCAPADO + anclas controladas.
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{ __html: plainHtml }}
    />
  );
}
