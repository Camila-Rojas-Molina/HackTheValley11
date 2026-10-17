import { labels } from "@/lib/labels";

// Rendered by the root layout, so every page carries the status line and the data attribution.
export function SiteFooter() {
  return (
    <footer className="border-t border-border px-4 py-4 text-xs text-muted-foreground">
      <div className="mx-auto flex max-w-5xl flex-col gap-1">
        <p data-testid="status-line" className="font-medium text-foreground">
          {labels.status_line}
        </p>
        <p data-testid="attribution">{labels.attribution.short}</p>
      </div>
    </footer>
  );
}
