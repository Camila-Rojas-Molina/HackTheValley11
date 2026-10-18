// The right-hand metrics panel: one tile per labels.json `tiles` entry that this scan's manifest has metrics for.
// Every value carries its basis badge (measured, simulation, MOCK, pending (Mx)) and, for synthetic data,
// the SIMULATED badge. Labels, units and sub-labels all come from labels.json.
import { labels } from "@/lib/labels";
import {
  MISSING,
  basisBadge,
  fill,
  gridLabel,
  lesionModelLabel,
  maskLabel,
  num,
  provenanceBadge,
  statisticLabel,
  valueWithUnit,
} from "@/lib/format";
import type { Metric, ScanManifest } from "@/lib/types";
import { Badge, BasisBadge } from "./Badge";

type Tile = (typeof labels.tiles)[number];

function pick(ms: Metric[], key: string, mask?: string): Metric | undefined {
  return ms.find((m) => m.key === key && (mask === undefined || m.mask_source === mask));
}

function tileTitle(tile: Tile, ms: Metric[], level: string | null, dayA: number | null): string {
  const t = tile as Tile & { pending_label?: string };
  switch (tile.key) {
    case "headline": {
      const x = pick(ms, "stiffness_change_axial_pct", "expert");
      const y = pick(ms, "lesion_share_pts", "expert");
      if ((x?.value ?? null) === null && t.pending_label) {
        return fill(t.pending_label, { level, day_a: dayA, available_in: x?.available_in ?? y?.available_in });
      }
      return fill(tile.label, {
        level,
        day_a: dayA,
        x: num(x?.value),
        a: num(x?.range?.low),
        b: num(x?.range?.high),
        y: num(y?.value),
      });
    }
    case "ai_vs_expert":
      return fill(tile.label, {
        ai: num(pick(ms, "stiffness_change_axial_pct", "ai")?.value),
        expert: num(pick(ms, "stiffness_change_axial_pct", "expert")?.value),
      });
    case "strain":
      return fill(tile.label, { ratio: num(pick(ms, "strain_ratio")?.value), day_a: dayA });
    default:
      return fill(tile.label, {});
  }
}

function rowText(tile: Tile, m: Metric): { primary: string; qualifiers: string[] } {
  const t = tile as Tile & { value_label?: string; alt_label?: string };
  const value = valueWithUnit(m);
  if (t.value_label && m.value !== null) {
    return {
      primary: fill(t.value_label, { value: num(m.value), grid_label: gridLabel(m.grid), mask_label: maskLabel(m.mask_source) }),
      qualifiers: [],
    };
  }
  const qualifiers = [
    tile.key === "lesion_pct" ? (m.key === "lesion_pct_of_vertebra" ? t.alt_label : tile.label) : null,
    gridLabel(m.grid),
    tile.key === "ai_vs_expert" || tile.key === "headline" ? maskLabel(m.mask_source) : null,
    lesionModelLabel(m.lesion_model),
    statisticLabel(m.statistic),
  ].filter((q): q is string => Boolean(q));
  return { primary: value ?? MISSING, qualifiers };
}

export function MetricsPanel({ manifest, dayA }: { manifest: ScanManifest; dayA: number | null }) {
  const tiles = labels.tiles.filter(
    (t) => !("milestone" in t && t.milestone) && (t.metrics as string[]).some((k) => manifest.metrics.some((m) => m.key === k)),
  );
  const shown = new Set<Metric>();
  return (
    <section data-testid="metrics-panel" className="flex flex-col gap-3">
      {tiles.length ? (
        <p className="text-xs text-muted-foreground">{labels.headline_statement}</p>
      ) : (
        <p className="text-sm text-muted-foreground">{manifest.simulated ? labels.viewer.phantom_label : MISSING}</p>
      )}
      {tiles.map((tile) => {
        const ms = manifest.metrics.filter((m) => (tile.metrics as string[]).includes(m.key) && !shown.has(m));
        // the AI-vs-expert tile reuses the headline's stiffness metric; every other metric shows once
        if (tile.key !== "headline") ms.forEach((m) => shown.add(m));
        const rows = tile.key === "headline" ? ms.filter((m) => m.mask_source !== "ai") : ms;
        return (
          <article
            key={tile.key}
            data-tile={tile.key}
            className="rounded-md border border-border bg-card p-3 text-card-foreground shadow-xs"
          >
            <h3 className="text-sm font-semibold leading-snug">{tileTitle(tile, manifest.metrics, manifest.level, dayA)}</h3>
            <ul className="mt-2 flex flex-col gap-1.5">
              {rows.map((m, i) => {
                const { primary, qualifiers } = rowText(tile, m);
                const sim = provenanceBadge(m.provenance);
                return (
                  <li key={`${m.key}-${i}`} data-metric={m.key} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                    <span className="font-medium tabular-nums">{primary}</span>
                    {qualifiers.length ? (
                      <span className="text-xs text-muted-foreground">{qualifiers.join(" · ")}</span>
                    ) : null}
                    <BasisBadge basis={m.basis} text={basisBadge(m.basis, m.available_in)} />
                    {sim ? (
                      <Badge tone="simulated" data-provenance={m.provenance}>
                        {sim}
                      </Badge>
                    ) : null}
                    {m.note ? <span className="w-full text-xs text-muted-foreground">{m.note}</span> : null}
                  </li>
                );
              })}
            </ul>
            {"sub_label" in tile && tile.sub_label ? (
              <p className="mt-2 text-xs text-muted-foreground">{tile.sub_label}</p>
            ) : null}
          </article>
        );
      })}
    </section>
  );
}
