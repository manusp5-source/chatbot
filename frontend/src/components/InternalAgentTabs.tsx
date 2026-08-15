import { NavLink } from "react-router-dom";

// Pestañas del Agente interno: unifica Ajustes + Prompt en una sola sección.
const TABS = [
  { to: "/admin/internal-agent", label: "Ajustes", end: true },
  { to: "/admin/internal-agent/prompt", label: "Prompt" },
];

export function InternalAgentTabs() {
  return (
    <div className="px-4 md:px-6 border-b border-line bg-paper2 flex gap-1">
      {TABS.map((t) => (
        <NavLink
          key={t.to}
          to={t.to}
          end={t.end}
          className={({ isActive }) =>
            "px-3 py-2.5 text-sm border-b-2 -mb-px transition-colors " +
            (isActive
              ? "border-brand text-ink font-medium"
              : "border-transparent text-ink3 hover:text-ink")
          }
        >
          {t.label}
        </NavLink>
      ))}
    </div>
  );
}
