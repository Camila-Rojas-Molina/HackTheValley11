import { cn } from "@/lib/utils";

// One provenance or basis badge. `tone` only picks the colour; the text always comes from labels.json.
export function Badge({
  children,
  tone = "neutral",
  testId,
  ...data
}: {
  children: React.ReactNode;
  tone?: "neutral" | "measured" | "mock" | "pending" | "simulated" | "offline" | "ai";
  testId?: string;
} & Record<`data-${string}`, string>) {
  return (
    <span
      data-testid={testId}
      {...data}
      className={cn(
        "inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[11px] font-medium leading-none whitespace-nowrap",
        tone === "neutral" && "border-border text-muted-foreground",
        tone === "measured" && "border-emerald-600/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-300",
        tone === "mock" && "border-fuchsia-600/50 bg-fuchsia-500/15 text-fuchsia-800 dark:text-fuchsia-300",
        tone === "pending" && "border-amber-600/40 bg-amber-500/10 text-amber-800 dark:text-amber-300",
        tone === "simulated" && "border-sky-600/50 bg-sky-500/10 text-sky-800 dark:text-sky-300",
        tone === "offline" && "border-orange-600/60 bg-orange-500/15 text-orange-800 dark:text-orange-300",
        tone === "ai" && "border-violet-600/40 bg-violet-500/10 text-violet-800 dark:text-violet-300",
      )}
    >
      {children}
    </span>
  );
}

export function BasisBadge({ basis, text }: { basis: string; text: string }) {
  const tone = basis === "measured" ? "measured" : basis === "mock" ? "mock" : basis === "pending" ? "pending" : "simulated";
  return (
    <Badge tone={tone} data-basis={basis} testId="basis-badge">
      {text}
    </Badge>
  );
}
