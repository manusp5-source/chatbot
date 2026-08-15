import { PageHeader } from "@/components/PageHeader";

interface Props {
  title: string;
  description?: string;
  milestone?: string;
}

export function Placeholder({ title, description, milestone }: Props) {
  return (
    <div className="h-full flex flex-col">
      <PageHeader title={title} description={description} />
      <div className="flex-1 flex items-center justify-center text-ink3">
        {milestone ? `Pendiente — ${milestone}` : "Próximamente"}
      </div>
    </div>
  );
}
