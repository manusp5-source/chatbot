/* Widget de chat embebible (F4)
 *
 * Uso:
 *   <script src="https://CHATBOT/widget.js"
 *           data-api-key="..."
 *           data-api-base="https://CHATBOT"
 *           data-title="Soporte"
 *           data-greeting="Hola, ¿en qué te ayudo?"
 *           data-brand="#DBE09E"
 *           data-subtitle="Asistente virtual · respuestas automáticas"
 *           data-privacy-url="https://tuweb.com/privacidad"></script>
 *
 * Estado del visitor: visitor_id + session_token en localStorage con prefijo
 * `chatbot_chat_` (ver STORAGE_PREFIX). El visitor_id NO se borra nunca desde
 * aquí: es la
 * identidad del contacto. El token sí (caduca, o el servidor lo rechaza si se
 * regeneró la clave del canal), y se pide uno nuevo con el mismo visitor_id.
 *
 * El hilo NO vive en el DOM: se pide al servidor (`/webchat/history`) al abrir,
 * al reconectar y al volver a la pestaña. La entrega en vivo es pub/sub y lo
 * que se publica sin nadie escuchando se pierde; el historial es la verdad.
 */
(function () {
  "use strict";

  // ---------------- Config ----------------
  var script = document.currentScript || (function () {
    var all = document.getElementsByTagName("script");
    return all[all.length - 1];
  })();
  var API_KEY = script.getAttribute("data-api-key");
  var API_BASE = (script.getAttribute("data-api-base") || "").replace(/\/$/, "");
  var BRAND = script.getAttribute("data-brand") || "#DBE09E";
  var GREETING =
    script.getAttribute("data-greeting") ||
    "Hola, ¿en qué te puedo ayudar?";
  var TITLE = script.getAttribute("data-title") || "Chat";
  // Divulgación de IA (AI Act): aviso proactivo de que atiende un asistente
  // virtual. Personalizable con data-subtitle; enlace de privacidad opcional.
  var SUBTITLE = script.getAttribute("data-subtitle") || "Asistente virtual · respuestas automáticas";
  var PRIVACY_URL = script.getAttribute("data-privacy-url") || "";
  var TYPING_TEXT = script.getAttribute("data-typing-text") || "Escribiendo…";
  var CLOSED_TEXT =
    script.getAttribute("data-closed-text") ||
    "Esta conversación se ha cerrado. Escribe otra vez para abrir una nueva.";

  if (!API_KEY || !API_BASE) {
    console.error("[chat-widget] widget.js: falta data-api-key o data-api-base");
    return;
  }

  // ---------------- Storage ----------------
  var STORAGE_PREFIX = "chatbot_chat_" + API_KEY.slice(0, 8) + "_";
  function lsGet(k) {
    try { return localStorage.getItem(STORAGE_PREFIX + k); } catch (e) { return null; }
  }
  function lsSet(k, v) {
    try { localStorage.setItem(STORAGE_PREFIX + k, v); } catch (e) { /* ignore */ }
  }
  function lsDel(k) {
    try { localStorage.removeItem(STORAGE_PREFIX + k); } catch (e) { /* ignore */ }
  }
  // Resto de instalaciones anteriores: la conversación ya no se guarda aquí,
  // la manda el servidor. Se limpia para no dejar basura en el navegador.
  lsDel("conversation_id");

  // ---------------- UI ----------------
  var root = document.createElement("div");
  root.id = "cbw-chat-root";
  root.style.cssText =
    "position:fixed;bottom:24px;right:24px;z-index:2147483647;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;";

  var style = document.createElement("style");
  style.textContent =
    "#cbw-fab{width:56px;height:56px;border-radius:50%;background:" + BRAND +
    ";border:none;cursor:pointer;box-shadow:0 6px 24px rgba(0,0,0,.18);display:flex;align-items:center;justify-content:center;color:#1a1612;transition:transform .15s}" +
    "#cbw-fab:hover{transform:scale(1.05)}" +
    "#cbw-fab svg{width:24px;height:24px}" +
    "#cbw-panel{position:absolute;bottom:72px;right:0;width:360px;max-width:calc(100vw - 48px);height:520px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;box-shadow:0 16px 48px rgba(0,0,0,.2);display:none;flex-direction:column;overflow:hidden}" +
    "#cbw-panel.open{display:flex}" +
    "#cbw-header{background:" + BRAND + ";color:#1a1612;padding:10px 16px;font-weight:600;display:flex;align-items:center;justify-content:space-between}" +
    "#cbw-header .cbw-htitle{display:flex;flex-direction:column;line-height:1.2}" +
    "#cbw-header .cbw-hsub{font-weight:400;font-size:11px;opacity:.75}" +
    "#cbw-header .cbw-hsub a{color:inherit;text-decoration:underline}" +
    "#cbw-header button{background:transparent;border:none;color:#1a1612;cursor:pointer;font-size:18px;line-height:1}" +
    "#cbw-msgs{flex:1;overflow-y:auto;padding:12px;background:#F7F2E9;display:flex;flex-direction:column;gap:8px}" +
    ".cbw-bubble{max-width:80%;padding:8px 12px;border-radius:14px;font-size:14px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}" +
    ".cbw-bubble.bot{background:#fff;color:#1a1612;align-self:flex-start;border:1px solid rgba(0,0,0,.08)}" +
    ".cbw-bubble.user{background:" + BRAND + ";color:#1a1612;align-self:flex-end}" +
    ".cbw-bubble.typing{font-style:italic;color:#6a604f;background:transparent;border:none}" +
    ".cbw-bubble.note{align-self:center;max-width:100%;text-align:center;font-size:12px;color:#6a604f;background:transparent;border:none}" +
    "#cbw-form{display:flex;gap:8px;padding:10px;border-top:1px solid rgba(0,0,0,.08);background:#fff}" +
    "#cbw-input{flex:1;border:1px solid rgba(0,0,0,.15);border-radius:10px;padding:8px 12px;font-size:14px;font-family:inherit;outline:none}" +
    "#cbw-input:focus{border-color:" + BRAND + "}" +
    "#cbw-send{background:" + BRAND + ";color:#1a1612;border:none;border-radius:10px;padding:8px 14px;cursor:pointer;font-weight:600}" +
    "#cbw-send:disabled{opacity:.5;cursor:not-allowed}";
  document.head.appendChild(style);

  var fab = document.createElement("button");
  fab.id = "cbw-fab";
  fab.setAttribute("aria-label", "Abrir chat");
  fab.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  root.appendChild(fab);

  var panel = document.createElement("div");
  panel.id = "cbw-panel";
  var subHtml = escapeHtml(SUBTITLE);
  // Solo http(s): evita esquemas peligrosos (javascript:) en el href.
  if (/^https?:\/\//i.test(PRIVACY_URL)) {
    subHtml +=
      ' · <a href="' + escapeHtml(PRIVACY_URL) + '" target="_blank" rel="noopener">Privacidad</a>';
  }
  panel.innerHTML =
    '<div id="cbw-header"><span class="cbw-htitle"><span>' + escapeHtml(TITLE) +
    '</span><span class="cbw-hsub">' + subHtml + '</span></span>' +
    '<button id="cbw-close" aria-label="Cerrar">×</button></div>' +
    '<div id="cbw-msgs"></div>' +
    '<form id="cbw-form" autocomplete="off"><input id="cbw-input" placeholder="Escribe…" autocomplete="off" /><button id="cbw-send" type="submit">Enviar</button></form>';
  root.appendChild(panel);

  // Inyección defensiva: si DOM no está ready (script en <head> o body
  // open), esperar a DOMContentLoaded. Y tras inyectar, re-verificar a los
  // 800ms por si algún tema/plugin reemplaza el DOM tras nuestro mount.
  function attach() {
    if (!document.body) {
      document.addEventListener("DOMContentLoaded", attach);
      return;
    }
    if (!document.body.contains(root)) {
      document.body.appendChild(root);
    }
  }
  attach();
  // Re-attach si algún tema agresivo lo elimina (Elementor, builder, etc.).
  setTimeout(function () {
    if (!document.body || !document.body.contains(root)) attach();
  }, 800);
  setTimeout(function () {
    if (!document.body || !document.body.contains(root)) attach();
  }, 2500);

  var msgsEl = panel.querySelector("#cbw-msgs");
  var formEl = panel.querySelector("#cbw-form");
  var inputEl = panel.querySelector("#cbw-input");
  var sendEl = panel.querySelector("#cbw-send");
  var closeEl = panel.querySelector("#cbw-close");

  function escapeHtml(s) {
    return (s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
  function appendBubble(text, who, opts) {
    opts = opts || {};
    var b = document.createElement("div");
    b.className = "cbw-bubble " + who + (opts.typing ? " typing" : "") + (opts.note ? " note" : "");
    b.textContent = text;
    if (opts.id) b.dataset.id = opts.id;
    msgsEl.appendChild(b);
    msgsEl.scrollTop = msgsEl.scrollHeight;
    return b;
  }

  // ---------------- Session / WS / send ----------------
  var session = null;   // {visitor_id, session_token}
  var ws = null;
  var wsPath = null;    // ruta del WS que da el servidor (lleva conversación)
  var convId = null;
  var lastMsgId = null; // último id del historial ya pintado (cursor)
  var wsAttempts = 0;      // reintentos seguidos sin conseguir conectar
  var wsRetryTimer = null; // reintento programado (para poder cancelarlo)

  /* WS-BACKOFF-BEGIN — probado desde backend/tests/test_widget_ws_backoff.py */
  // Reconexión del WebSocket: espera exponencial con jitter y tope.
  //
  // Antes era `setTimeout(connectWs, 2000)` a secas: sin backoff ni límite. Con
  // el widget montado en webs de clientes, una caída del backend se convertía
  // en cada navegador abierto golpeando cada 2 segundos indefinidamente — la
  // reconexión amplificaba la caída y no dejaba levantarse al servidor. El
  // jitter evita además el efecto rebaño (todas las pestañas del mundo
  // reintentando en el mismo instante).
  var WS_RECONNECT_BASE_MS = 1000;
  var WS_RECONNECT_MAX_MS = 30000;
  var WS_MAX_RECONNECT_ATTEMPTS = 8;

  function wsBackoffDelay(attempt) {
    var exp = WS_RECONNECT_BASE_MS * Math.pow(2, attempt);
    var capped = Math.min(WS_RECONNECT_MAX_MS, exp);
    // Jitter "full-ish": entre el 50% y el 100% de la espera calculada.
    return Math.round(capped * (0.5 + Math.random() * 0.5));
  }
  /* WS-BACKOFF-END */

  /* DEDUPE-BEGIN — probado desde backend/tests/test_widget_history.py */
  // El historial es la verdad, pero algunas burbujas ya están en pantalla
  // porque las pintamos en local (la del propio visitante) o llegaron por el
  // WebSocket. Los ids del WS no son los de la base de datos, así que la
  // coincidencia se hace por (rol + texto) con CONTADOR, no con un simple
  // "ya lo he visto": si el visitante escribe "hola" dos veces, el historial
  // trae dos y solo debe saltarse tantas como haya pintadas.
  function msgKey(role, text) {
    return role + "\u0001" + text;
  }
  function markPainted(painted, role, text) {
    var k = msgKey(role, text);
    painted[k] = (painted[k] || 0) + 1;
  }
  function consumePainted(painted, role, text) {
    var k = msgKey(role, text);
    if (painted[k] > 0) {
      painted[k]--;
      return true; // ya está en pantalla: no repintar
    }
    return false;
  }
  /* DEDUPE-END */

  var painted = Object.create(null);

  // ---- indicador "escribiendo" ----
  var typingEl = null;
  var typingTimer = null;
  var TYPING_MAX_MS = 90000;

  function showTyping() {
    if (!panel.classList.contains("open")) return;
    if (typingEl && typingEl.parentNode) {
      msgsEl.appendChild(typingEl); // mantenerlo al final del hilo
    } else {
      typingEl = appendBubble(TYPING_TEXT, "bot", { typing: true });
    }
    msgsEl.scrollTop = msgsEl.scrollHeight;
    if (typingTimer) clearTimeout(typingTimer);
    typingTimer = setTimeout(hideTyping, TYPING_MAX_MS);
  }
  function hideTyping() {
    if (typingTimer) { clearTimeout(typingTimer); typingTimer = null; }
    if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl);
    typingEl = null;
  }

  function resetSession(keepIdentity) {
    // keepIdentity === false solo si algún día hace falta un borrado real.
    session = null;
    wsPath = null;
    convId = null;
    lastMsgId = null;
    lsDel("session_token");
    lsDel("expires_at");
    if (keepIdentity === false) lsDel("visitor_id");
    try { ws && ws.close(); } catch (e) {}
    ws = null;
  }

  async function ensureSession() {
    if (session) return session;
    var visitorId = lsGet("visitor_id");
    var storedToken = lsGet("session_token");
    var expiresAt = parseInt(lsGet("expires_at") || "0", 10) || 0;
    if (visitorId && storedToken && (!expiresAt || expiresAt * 1000 > Date.now())) {
      session = { visitor_id: visitorId, session_token: storedToken };
      return session;
    }
    var res = await fetch(API_BASE + "/api/v1/webchat/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: API_KEY,
        // El visitor_id se CONSERVA aunque el token haya caducado o el
        // servidor lo rechace: es el mismo contacto, no una persona nueva.
        visitor_id: visitorId || null,
        referrer: document.referrer || null,
      }),
    });
    if (!res.ok) throw new Error("session " + res.status);
    var data = await res.json();
    session = { visitor_id: data.visitor_id, session_token: data.session_token };
    convId = data.conversation_id || null;
    wsPath = data.ws_path || null;
    lsSet("visitor_id", data.visitor_id);
    lsSet("session_token", data.session_token);
    lsSet("expires_at", String(data.expires_at || 0));
    return session;
  }

  // incremental=false → foto completa y repintado; true → solo lo nuevo.
  async function loadHistory(incremental, reintentado) {
    if (!session) return;
    var url =
      API_BASE + "/api/v1/webchat/history?visitor_id=" +
      encodeURIComponent(session.visitor_id) +
      "&token=" + encodeURIComponent(session.session_token);
    if (incremental && lastMsgId) url += "&after=" + encodeURIComponent(lastMsgId);
    var res = await fetch(url, { method: "GET" });
    if (res.status === 401 || res.status === 403) {
      // Token caducado (12 h) o clave del canal regenerada. Se suelta el token
      // —NO la identidad— y se reintenta UNA vez con sesión nueva. Sin este
      // reintento, al visitante le saldría el chat en blanco hasta que
      // escribiera otra vez, que es justo lo que estamos arreglando.
      if (reintentado) return;
      resetSession();
      await ensureSession();
      return loadHistory(incremental, true);
    }
    if (!res.ok) throw new Error("history " + res.status);
    var data = await res.json();
    convId = data.conversation_id || null;
    wsPath = data.ws_path || wsPath;
    var msgs = data.messages || [];
    if (!incremental) {
      hideTyping();
      msgsEl.textContent = "";
      painted = Object.create(null);
      lastMsgId = null;
    }
    for (var i = 0; i < msgs.length; i++) {
      var m = msgs[i];
      if (!consumePainted(painted, m.role, m.text)) {
        appendBubble(m.text, m.role === "user" ? "user" : "bot");
      }
      lastMsgId = m.id;
    }
    if (typingEl) msgsEl.appendChild(typingEl); // el "escribiendo" siempre al final
    if (!incremental) {
      if (!msgsEl.children.length) appendBubble(GREETING, "bot");
      if (data.status === "cerrada") appendBubble(CLOSED_TEXT, "bot", { note: true });
    }
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  function connectWs() {
    if (!session || !wsPath) return;
    if (wsRetryTimer) { clearTimeout(wsRetryTimer); wsRetryTimer = null; }
    var wsUrl = (API_BASE.replace(/^http/, "ws")) + wsPath;
    try { ws && ws.close(); } catch (e) {}
    var sock = new WebSocket(wsUrl);
    ws = sock;
    sock.onopen = function () {
      // Conexión buena: se reinicia la cuenta de reintentos.
      wsAttempts = 0;
      // Cierra el hueco entre la foto del historial y la suscripción: lo que
      // se haya publicado mientras tanto está en la base de datos.
      loadHistory(true).catch(function () {});
    };
    sock.onmessage = function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg.type === "message.out" && msg.payload && msg.payload.text) {
          hideTyping();
          appendBubble(msg.payload.text, "bot");
          markPainted(painted, "bot", msg.payload.text);
        } else if (msg.type === "typing") {
          showTyping();
        }
        // "ping" y cualquier otro tipo se ignoran a propósito.
      } catch (e) { /* ignore */ }
    };
    sock.onclose = function () {
      // Solo reconecta el socket vigente (un close() nuestro al reconectar no
      // debe programar otro reintento encima).
      if (ws !== sock) return;
      if (!panel.classList.contains("open")) return;
      if (wsAttempts >= WS_MAX_RECONNECT_ATTEMPTS) {
        // Se deja de insistir. Al reabrir el panel se vuelve a intentar desde
        // cero: es el usuario quien decide reintentar, no un bucle infinito.
        console.warn("[chat-widget] WebSocket: reintentos agotados");
        return;
      }
      var delay = wsBackoffDelay(wsAttempts);
      wsAttempts++;
      wsRetryTimer = setTimeout(function () {
        wsRetryTimer = null;
        if (panel.classList.contains("open")) connectWs();
      }, delay);
    };
  }

  async function openPanel() {
    panel.classList.add("open");
    inputEl.focus();
    if (!msgsEl.children.length) appendBubble(GREETING, "bot");
    try {
      await ensureSession();
      await loadHistory(false);
      connectWs();
    } catch (e) {
      appendBubble("No se pudo iniciar el chat. Recarga la página o avísanos.", "bot");
      console.error("[chat-widget]", e);
    }
  }
  function closePanel() {
    panel.classList.remove("open");
    // Cancela un reintento en vuelo y reinicia la cuenta: al volver a abrir,
    // el usuario merece un intento inmediato, no la espera acumulada.
    if (wsRetryTimer) { clearTimeout(wsRetryTimer); wsRetryTimer = null; }
    wsAttempts = 0;
    hideTyping();
    try { ws && ws.close(); } catch (e) {}
    ws = null;
  }

  fab.addEventListener("click", function () {
    if (panel.classList.contains("open")) closePanel();
    else openPanel();
  });
  closeEl.addEventListener("click", closePanel);

  // Volver a la pestaña: el socket puede llevar rato muerto (los navegadores
  // congelan las pestañas de fondo). Se recupera lo perdido y se reconecta.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    if (!panel.classList.contains("open")) return;
    loadHistory(true).catch(function () {});
    if (!ws || ws.readyState === 2 || ws.readyState === 3) {
      wsAttempts = 0;
      connectWs();
    }
  });

  formEl.addEventListener("submit", async function (e) {
    e.preventDefault();
    var text = (inputEl.value || "").trim();
    if (!text) return;
    sendEl.disabled = true;
    hideTyping();
    appendBubble(text, "user");
    markPainted(painted, "user", text);
    inputEl.value = "";
    async function trySend() {
      await ensureSession();
      return fetch(API_BASE + "/api/v1/webchat/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          visitor_id: session.visitor_id,
          token: session.session_token,
          text: text,
        }),
      });
    }
    try {
      var res = await trySend();
      if (res.status === 401) {
        // Token caducado o clave del canal regenerada. Sesión nueva CON EL
        // MISMO visitor_id: el contacto no se duplica y no se pierde ni el
        // nombre ni el email que hubiera dejado.
        resetSession();
        res = await trySend();
      }
      if (!res.ok) throw new Error("send " + res.status);
      var data = await res.json();
      if (data.conversation_id) {
        var changed = convId !== data.conversation_id;
        convId = data.conversation_id;
        if (data.ws_path) wsPath = data.ws_path;
        if (changed || !ws || ws.readyState === 2 || ws.readyState === 3) {
          wsAttempts = 0;
          connectWs();
        }
      }
      // Feedback inmediato: entre el buffer del agente y la latencia del
      // modelo pasan 15-20 s. Sin señal, el visitante lee "está roto".
      showTyping();
    } catch (err) {
      hideTyping();
      appendBubble("No se pudo enviar el mensaje. Reintenta en un momento.", "bot");
      console.error("[chat-widget]", err);
    } finally {
      sendEl.disabled = false;
      inputEl.focus();
    }
  });
})();
