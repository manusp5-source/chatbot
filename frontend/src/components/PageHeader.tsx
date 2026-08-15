import { ReactNode } from "react";

interface Props {
  title: ReactNode;
  eyebrow?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}

export function PageHeader({ title, eyebrow, description, actions }: Props) {
  return (
    <div className="px-4 md:px-6 py-4 border-b border-line bg-paper2 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1 className="text-xl font-semibold text-ink truncate">{title}</h1>
        {description && <div className="text-sm text-ink3 mt-0.5">{description}</div>}
      </div>
      {actions && <div className="flex items-center gap-2 flex-wrap sm:shrink-0">{actions}</div>}
    </div>
  );
}
