// The copilot slot (M8 makes it live). M3 shows the drawer with the five suggested questions from labels.json,
// all disabled, so the layout is final and nothing is sent anywhere.
import { labels } from "@/lib/labels";

export function CopilotDrawer() {
  return (
    <details data-testid="copilot-drawer" data-disabled="true" className="rounded-md border border-dashed border-border p-3">
      <summary className="cursor-pointer text-sm font-semibold">{labels.copilot.drawer_label}</summary>
      <ul className="mt-2 flex flex-col gap-1.5">
        {labels.copilot.suggested_questions.map((q) => (
          <li key={q}>
            <button
              type="button"
              disabled
              className="w-full cursor-not-allowed rounded-sm border border-border px-2 py-1 text-left text-xs text-muted-foreground opacity-70"
            >
              {q}
            </button>
          </li>
        ))}
      </ul>
      <input
        disabled
        aria-label={labels.copilot.drawer_label}
        placeholder={labels.copilot.drawer_label}
        className="mt-2 w-full cursor-not-allowed rounded-sm border border-border bg-muted px-2 py-1 text-xs"
      />
    </details>
  );
}
