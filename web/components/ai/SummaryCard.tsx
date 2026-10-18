// The care-team summary slot (M6 makes it live). M3 shows whatever GET /scans/{id}/summary returns, which is the
// deterministic template, with its badge ("template · n/m numbers match computed values") and the fixed footer.
import { labels } from "@/lib/labels";
import { basisBadge } from "@/lib/format";
import type { Generation } from "@/lib/types";
import { Badge } from "@/components/metrics/Badge";

export function SummaryCard({ summary, pendingIn }: { summary: Generation | null; pendingIn: string }) {
  const provider = summary ? ((labels.ai.provider_names as Record<string, string>)[summary.source] ?? summary.source) : null;
  return (
    <section data-testid="summary-card" data-source={summary?.source ?? "none"} className="rounded-md border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        {provider ? (
          <Badge tone="ai" testId="summary-provider">
            {provider}
          </Badge>
        ) : null}
        {summary ? (
          <Badge tone="neutral" testId="summary-badge">
            {summary.badge}
          </Badge>
        ) : (
          <Badge tone="pending">{basisBadge("pending", pendingIn)}</Badge>
        )}
      </div>
      {summary ? (
        <div className="mt-2 flex flex-col gap-1 text-sm">
          <p className="font-medium">{summary.text.headline}</p>
          <p>{summary.text.detail}</p>
          <p className="mt-1 text-xs text-muted-foreground">{summary.footer}</p>
        </div>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground">{labels.ai.footer}</p>
      )}
    </section>
  );
}
