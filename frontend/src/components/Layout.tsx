import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Inbox,
  Home,
  Users,
  BookOpen,
  BarChart3,
  ScrollText,
  Activity,
  LogOut,
  Bot,
  ShieldOff,
  Terminal,
  Menu,
  X,
  Sun,
  Moon,
  Settings,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  DatabaseBackup,
  Workflow,
  Megaphone,
  Phone,
  GraduationCap,
  Eye,
  EyeOff,
} from "lucide-react";
import { useAuth } from "@/store/auth";
import { useTheme } from "@/store/theme";
import { usePrivacy } from "@/store/privacy";
import clsx from "clsx";
import { InternalAgentWidget } from "@/components/InternalAgentWidget";
import { PushButton } from "@/components/PushButton";
import { listConversations } from "@/services/conversations";
import { appNameParts } from "@/lib/appName";
import { BrandLogo } from "@/components/BrandLogo";

type NavItem = {
  to: string;
  label: string;
  icon: typeof Inbox;
  badge?: number;
  end?: boolean;
};

const adminHomeNav: NavItem[] = [{ to: "/admin", label: "Home", icon: Home, end: true }];

const clientNav: NavItem[] = [
  { to: "/inbox", label: "Inbox", icon: Inbox },
  // Registro de llamadas del canal de voz (Retell), solo lectura. Justo debajo
  // de Inbox, tanto para operador como para admin (que hereda clientNav).
  //
  // La entrada se pinta SIEMPRE, también con Retell sin conectar. Se valoró
  // ocultarla consultando GET /voice/status desde aquí y se descartó:
  //   1. La página ya distingue las dos situaciones — pinta "Retell no está
  //      conectado" (con dónde conectarlo) en vez de "aún no hay llamadas".
  //      Ese era el problema real, y está resuelto en su sitio.
  //   2. Sondear el estado desde el menú añade una petición en cada carga y
  //      hace que la entrada aparezca/desaparezca a mitad del render.
  //   3. Ocultarla esconde justo el sitio donde se explica que falta conectar
  //      Retell: quien no lo ha conectado nunca descubriría que existe.
  { to: "/calls", label: "Calls", icon: Phone },
  { to: "/contacts", label: "Contacts", icon: Users },
];

// Menú del OPERADOR (no admin): lo básico + la Base de Conocimiento en
// lectura (la subida/borrado sigue siendo admin-only dentro de la página).
const operatorNav: NavItem[] = [
  ...clientNav,
  { to: "/knowledge-base", label: "Knowledge Base", icon: BookOpen },
];

// Operativa para admin = lo del operador + envío masivo.
const adminOperativaNav: NavItem[] = [
  ...clientNav,
  { to: "/admin/outbound", label: "Outbound messaging", icon: Megaphone },
];

const adminUsersNav: NavItem[] = [
  { to: "/admin/users", label: "Users", icon: Users },
  { to: "/admin/connections", label: "Connections", icon: Plug },
  // Bloqueados va justo debajo de Conexiones: se gestiona junto a los canales.
  { to: "/admin/blocklist", label: "Blocked contacts", icon: ShieldOff },
  // Ajustes generales de la app (zona horaria, etc.).
  { to: "/admin/settings", label: "Settings", icon: Settings },
];

const adminAgentNav: NavItem[] = [
  { to: "/admin/agent/dashboard", label: "Dashboard", icon: BarChart3 },
  { to: "/admin/agent/flow", label: "Live flow", icon: Workflow },
  // La antigua "Configuración modelo" es la pestaña "Tarifas y límites"
  // dentro de Agentes (la ruta vieja /admin/agent/config redirige allí).
  { to: "/admin/agent/agents", label: "Agents", icon: Bot },
  { to: "/knowledge-base", label: "Knowledge Base", icon: BookOpen },
  { to: "/admin/agent/learning", label: "Learning", icon: GraduationCap },
];

const adminSystemNav: NavItem[] = [
  { to: "/admin/system/logs", label: "Live logs", icon: Terminal },
  { to: "/admin/audit", label: "Audit log", icon: ScrollText },
  { to: "/admin/health", label: "System health", icon: Activity },
  // Copias vive en Sistema (junto a salud/retención), no en Admin.
  { to: "/admin/backups", label: "Backups", icon: DatabaseBackup },
];

// Avatar del sidebar — color fijo de la paleta de marca, con las iniciales en
// tinta oscura. Es fijo a propósito: no cambia con el tema ni con el usuario.
const AVATAR_BG = "#DBE09E";
// Tinta oscura sobre el pistacho fijo del avatar. `--brand-on` es el token de
// "tinta sobre superficie de marca": vale #1A1612 en claro y oscuro, así que se
// mantiene legible en ambos temas (el fondo del avatar no cambia con el tema).
const AVATAR_INK = "var(--brand-on)";

function initialsOf(name: string | null | undefined, email: string | null | undefined): string {
  const source = (name || email || "?").trim();
  if (!source) return "?";
  const parts = source.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return source.slice(0, 2).toUpperCase();
  const first = parts[0]?.[0] ?? "";
  const second = parts.length > 1 ? parts[1]?.[0] ?? "" : parts[0]?.[1] ?? "";
  return (first + second).toUpperCase();
}

export function Layout() {
  const user = useAuth((s) => s.user);
  const logout = useAuth((s) => s.logout);
  const navigate = useNavigate();
  const location = useLocation();

  const theme = useTheme((s) => s.theme);
  const toggleTheme = useTheme((s) => s.toggle);

  const privacy = usePrivacy((s) => s.enabled);
  const togglePrivacy = usePrivacy((s) => s.toggle);

  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("chatbot:sidebar-collapsed") === "1"
  );
  const [attention, setAttention] = useState(0);

  useEffect(() => {
    localStorage.setItem("chatbot:sidebar-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);

  // Inbox badge: conversations waiting for human attention, using the same
  // predicate as the inbox's pending tab. Refresh lightly every 30 seconds.
  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const p = await listConversations({ pending_only: true, active_only: true, page_size: 1 });
        if (alive) setAttention(p.total);
      } catch {
        /* ignore */
      }
    }
    void load();
    const t = setInterval(load, 30000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Close the drawer when the route changes.
  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  // Lock body scrolling while the drawer is open.
  useEffect(() => {
    if (mobileOpen) document.body.style.overflow = "hidden";
    else document.body.style.overflow = "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [mobileOpen]);

  const sections = useMemo(
    () =>
      user?.role === "admin"
        ? [
            { title: "", items: adminHomeNav },
            { title: "Operativa", items: adminOperativaNav },
            { title: "Agente IA", items: adminAgentNav },
            { title: "Admin", items: adminUsersNav },
            { title: "Sistema", items: adminSystemNav },
          ]
        : [{ title: "", items: operatorNav }],
    [user?.role]
  );

  async function onLogout() {
    await logout();
    navigate("/login");
  }

  function onNewConversation() {
    navigate("/inbox");
  }

  const initials = initialsOf(user?.nombre, user?.email);

  return (
    <div className="flex h-full bg-paper text-ink">
      {/* Mobile backdrop */}
      {mobileOpen && (
        <div
          className="lg:hidden fixed inset-0 z-40 bg-ink/40 backdrop-blur-[1px]"
          onClick={() => setMobileOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar */}
      <aside
        className={clsx(
          "z-50 bg-paper2 border-r border-line flex flex-col",
          // Mobile: drawer full-screen width 100% (max 320). Desktop: 220px estático.
          "fixed inset-y-0 left-0 w-[86vw] max-w-[320px] transform transition-[transform,width] duration-200",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
          "lg:static lg:translate-x-0 lg:shrink-0",
          collapsed ? "lg:w-[72px]" : "lg:w-[240px]"
        )}
        style={{ padding: "18px 14px" }}
      >
        {/* Header */}
        <div className={clsx("flex items-center mb-4", collapsed ? "lg:justify-center" : "justify-between")}>
          {/* Marca: icono line-art en currentColor (sin tile),
              wordmark serif itálica y subtítulo uppercase atenuado. El nombre
              sale de la config de la instalación (APP_NAME): primera palabra
              en grande y el resto como subtítulo, si lo hay. */}
          <div className={clsx("flex items-center gap-2.5 min-w-0", collapsed && "lg:hidden")}>
            <BrandLogo className="w-7 h-7 shrink-0 text-ink" />
            <div className="leading-none min-w-0">
              <div
                className="font-display italic text-ink"
                style={{ fontSize: 20, letterSpacing: "-0.01em" }}
              >
                {appNameParts().main}
              </div>
              {appNameParts().sub && (
                <div
                  className="text-ink3 mt-0.5"
                  style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.14em" }}
                >
                  {appNameParts().sub}
                </div>
              )}
            </div>
          </div>
          <button
            type="button"
            aria-label="Close menu"
            onClick={() => setMobileOpen(false)}
            className="lg:hidden inline-flex items-center justify-center w-9 h-9 rounded-coro-sm hover:bg-paper3"
          >
            <X className="w-4 h-4 text-ink2" />
          </button>
          <button
            type="button"
            aria-label={collapsed ? "Expand menu" : "Collapse menu"}
            title={collapsed ? "Expand menu" : "Collapse menu"}
            onClick={() => setCollapsed((c) => !c)}
            className="hidden lg:inline-flex items-center justify-center w-9 h-9 rounded-coro-sm hover:bg-paper3 text-ink2 shrink-0"
          >
            {collapsed ? <PanelLeftOpen className="w-4 h-4" /> : <PanelLeftClose className="w-4 h-4" />}
          </button>
        </div>


        {/* Nav */}
        <nav className="flex-1 overflow-auto -mx-1 px-1 space-y-4">
          {sections.map(({ title, items }, idx) => (
            <div key={idx} className="space-y-0.5">
              {title && (
                <div
                  className={clsx("px-2 pt-2 pb-1 text-ink3 font-medium", collapsed && "lg:hidden")}
                  style={{
                    fontSize: 10,
                    textTransform: "uppercase",
                    letterSpacing: "0.08em",
                  }}
                >
                  {title}
                </div>
              )}
              {items.map(({ to, label, icon: Icon, badge, end }) => {
                const badgeVal = to === "/inbox" ? attention : badge;
                return (
                  <NavLink
                    key={to}
                    to={to}
                    end={end}
                    title={collapsed ? label : undefined}
                    className={({ isActive }) =>
                      clsx(
                        "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors",
                        collapsed && "lg:justify-center lg:px-0",
                        isActive
                          ? "bg-brand/15 text-brand-ink font-semibold"
                          : "text-ink2 hover:bg-paper3 font-medium"
                      )
                    }
                  >
                    <Icon className="w-4 h-4 shrink-0" />
                    <span className={clsx("flex-1 truncate", collapsed && "lg:hidden")}>{label}</span>
                    {typeof badgeVal === "number" && badgeVal > 0 && (
                      <span
                        className={clsx("bg-brand text-brand-on rounded-full font-semibold", collapsed && "lg:hidden")}
                        style={{
                          fontSize: 10,
                          padding: "1px 7px",
                          lineHeight: "16px",
                        }}
                      >
                        {badgeVal}
                      </span>
                    )}
                  </NavLink>
                );
              })}
            </div>
          ))}
        </nav>

        {/* Footer en DOS filas: usuario arriba, acciones debajo. En una sola
            fila no caben (avatar + 4 botones de 36px ≈ 190px en un sidebar de
            220px) y los iconos se solapaban con el bloque del usuario. */}
        <div className="pt-3 mt-3 border-t border-line space-y-2">
          <NavLink
            to="/profile"
            title="My profile"
            className={clsx("flex items-center gap-2.5 min-w-0 rounded-lg p-1 -m-1 hover:bg-paper3", collapsed && "lg:justify-center")}
          >
            <div
              className="w-8 h-8 rounded-full flex items-center justify-center text-[12px] font-bold shrink-0"
              style={{ background: AVATAR_BG, color: AVATAR_INK }}
              aria-hidden="true"
            >
              {initials}
            </div>
            <div className={clsx("flex-1 min-w-0 leading-tight", collapsed && "lg:hidden")}>
              <div className="text-[12px] font-semibold text-ink truncate">
                {user?.nombre || user?.email || "—"}
              </div>
              <div className="text-[11px] text-ink3 truncate">{user?.email}</div>
              <div className="text-[10px] text-ink4 uppercase tracking-wider mt-0.5">
                {user?.role}
              </div>
            </div>
          </NavLink>
          {/* Fila de acciones: iconos 16px en botones de 32px (área de click),
              gap uniforme de 10px, alineados a la izquierda con el bloque de
              usuario (-ml-2 compensa el padding interno del primer botón). */}
          <div
            className={clsx(
              "flex items-center gap-2.5 py-2.5 -ml-2",
              collapsed && "lg:flex-col lg:items-center lg:gap-1.5 lg:ml-0"
            )}
          >
            <PushButton />
            <button
              type="button"
              aria-pressed={privacy}
              aria-label={privacy ? "Disable privacy mode" : "Enable privacy mode (hide customer data)"}
              title={privacy ? "Privacy mode ACTIVE — customer data hidden" : "Privacy mode — hide customer data while sharing your screen"}
              onClick={togglePrivacy}
              className={clsx(
                "inline-flex items-center justify-center w-8 h-8 rounded-coro-sm shrink-0 transition-colors",
                privacy ? "bg-brand text-brand-on" : "hover:bg-paper3 text-ink2"
              )}
            >
              {privacy ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
            <button
              type="button"
              aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              onClick={toggleTheme}
              className="inline-flex items-center justify-center w-8 h-8 rounded-coro-sm hover:bg-paper3 text-ink2 shrink-0 transition-colors"
            >
              {theme === "dark" ? (
                <Sun className="w-4 h-4" />
              ) : (
                <Moon className="w-4 h-4" />
              )}
            </button>
            <button
              type="button"
              aria-label="Sign out"
              title="Sign out"
              onClick={onLogout}
              className="inline-flex items-center justify-center w-8 h-8 rounded-coro-sm hover:bg-paper3 text-ink2 shrink-0 transition-colors"
            >
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>
      </aside>

      {/* Columna de contenido: cabecera móvil + main */}
      <div className="flex flex-col flex-1 min-w-0 h-full overflow-hidden">
        {/* Mobile header: brand on the left, menu on the right. */}
        <header className="lg:hidden flex items-center justify-between gap-3 shrink-0 px-4 py-2.5 border-b border-line bg-paper">
          <div className="flex items-center gap-2.5 min-w-0">
            <BrandLogo className="w-6 h-6 shrink-0 text-ink" />
            <span
              className="font-display italic text-ink truncate"
              style={{ fontSize: 19, letterSpacing: "-0.01em" }}
            >
              {appNameParts().main}
            </span>
          </div>
          <button
            type="button"
            aria-label="Open menu"
            onClick={() => setMobileOpen(true)}
            className="inline-flex items-center justify-center w-10 h-10 rounded-coro-sm hover:bg-paper3 shrink-0"
          >
            <Menu className="w-5 h-5 text-ink" />
          </button>
        </header>

        {/* Main */}
        <main className="flex-1 overflow-auto bg-paper">
          <Outlet />
        </main>
      </div>

      {/* Privacy indicator: visible during screen sharing and clickable to
          disable. It stays bottom-left to avoid the internal agent widget. */}
      {privacy && (
        <button
          type="button"
          onClick={togglePrivacy}
          title="Privacy mode active — click to disable"
          className="fixed bottom-3 left-3 z-30 inline-flex items-center gap-1.5 rounded-full bg-brand text-brand-on shadow-coro-1 text-[11px] font-semibold px-3 py-1.5"
        >
          <EyeOff className="w-3.5 h-3.5" /> Privacy mode
        </button>
      )}

      {/* Internal agent: read-only floating chat for admins. */}
      {user?.role === "admin" && <InternalAgentWidget />}
    </div>
  );
}
