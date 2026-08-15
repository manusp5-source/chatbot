import { Navigate, Outlet, createBrowserRouter } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { useAuth } from "@/store/auth";
import Login from "@/pages/Login";
import NotFound from "@/pages/NotFound";
import ProfilePage from "@/pages/ProfilePage";
import ForgotPasswordPage from "@/pages/ForgotPasswordPage";
import ResetPasswordPage from "@/pages/ResetPasswordPage";
import Inbox from "@/pages/client/Inbox";
import Calls from "@/pages/client/Calls";
import Contacts from "@/pages/client/Contacts";
import ContactDetail from "@/pages/client/ContactDetail";
import KnowledgeBase from "@/pages/client/KnowledgeBase";
import AdminHome from "@/pages/admin/AdminHome";
import UsersPage from "@/pages/admin/UsersPage";
import BackupsPage from "@/pages/admin/BackupsPage";
import ConnectionsPage from "@/pages/admin/ConnectionsPage";
import AgentDashboard from "@/pages/admin/AgentDashboard";
import AgentsPage from "@/pages/admin/AgentsPage";
import FlowDashboard from "@/pages/admin/FlowDashboard";
import OutboundPage from "@/pages/admin/OutboundPage";
import SystemLogs from "@/pages/admin/SystemLogs";
import AuditPage from "@/pages/admin/AuditPage";
import HealthPage from "@/pages/admin/HealthPage";
import BlocklistPage from "@/pages/admin/BlocklistPage";
import SettingsPage from "@/pages/admin/SettingsPage";
import LearningPage from "@/pages/admin/LearningPage";
import InternalAgentPromptPage from "@/pages/admin/InternalAgentPromptPage";

// El admin aterriza en la Home (dashboard); el operador, en el inbox.
function HomeRedirect() {
  const role = useAuth((s) => s.user?.role);
  return <Navigate to={role === "admin" ? "/admin" : "/inbox"} replace />;
}

export const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  { path: "/forgot-password", element: <ForgotPasswordPage /> },
  { path: "/reset-password", element: <ResetPasswordPage /> },
  {
    path: "/",
    element: (
      <ProtectedRoute>
        <Layout />
      </ProtectedRoute>
    ),
    children: [
      { index: true, element: <HomeRedirect /> },
      { path: "inbox", element: <Inbox /> },
      { path: "calls", element: <Calls /> },
      { path: "contacts", element: <Contacts /> },
      { path: "contacts/:id", element: <ContactDetail /> },
      { path: "knowledge-base", element: <KnowledgeBase /> },
      { path: "profile", element: <ProfilePage /> },
      {
        path: "admin",
        element: (
          <ProtectedRoute role="admin">
            <Outlet />
          </ProtectedRoute>
        ),
        children: [
          { index: true, element: <AdminHome /> },
          { path: "users", element: <UsersPage /> },
          // Ya no hay página de credenciales: cada clave se edita dentro de la
          // tarjeta de su servicio, en Conexiones → Servicios. El redirect se
          // queda para no romper marcadores antiguos.
          { path: "credentials", element: <Navigate to="/admin/connections?tab=services" replace /> },
          { path: "connections", element: <ConnectionsPage /> },
          // Ajustes generales de la app (zona horaria, etc.). La zona horaria
          // vivía dentro de Agentes → Modelo; ahora tiene página propia.
          { path: "settings", element: <SettingsPage /> },
          { path: "backups", element: <BackupsPage /> },
          { path: "agent/dashboard", element: <AgentDashboard /> },
          { path: "agent/flow", element: <FlowDashboard /> },
          { path: "agent/agents", element: <AgentsPage /> },
          { path: "agent/classifier", element: <Navigate to="/admin/agent/agents?tab=classifier" replace /> },
          { path: "outbound", element: <OutboundPage /> },
          // Ruta legacy: la gestión del prompt es ahora por-agente (AgentsPage).
          // Mantenemos un redirect para no romper marcadores antiguos.
          { path: "agent/prompt", element: <Navigate to="/admin/agent/agents" replace /> },
          // La antigua "Configuración modelo" es hoy la pestaña "Tarifas y
          // límites" de Agentes (?tab=model por compatibilidad); redirect
          // para no romper enlaces/marcadores antiguos.
          { path: "agent/config", element: <Navigate to="/admin/agent/agents?tab=model" replace /> },
          { path: "agent/learning", element: <LearningPage /> },
          { path: "audit", element: <AuditPage /> },
          { path: "health", element: <HealthPage /> },
          { path: "blocklist", element: <BlocklistPage /> },
          { path: "system/logs", element: <SystemLogs /> },
          { path: "internal-agent", element: <Navigate to="/admin/agent/agents?tab=internal" replace /> },
          { path: "internal-agent/prompt", element: <InternalAgentPromptPage /> },
        ],
      },
    ],
  },
  { path: "*", element: <NotFound /> },
]);
