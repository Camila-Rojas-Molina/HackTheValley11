// Title and fixed range for the strain overlay. NiiVue's own colorbar (inside the canvas) has no title, so the
// title, unit and the MOCK suffix live here, all from labels.json; the range is the manifest's cal_min/cal_max.
import { labels } from "@/lib/labels";
import { basisBadge, unitLabel } from "@/lib/format";
import type { Basis, Unit } from "@/lib/types";

// Turbo, linearly interpolated from NiiVue 0.69.0's src/cmaps/turbo.json at 0, 0.25, 0.5, 0.75 and 1.
const TURBO = "linear-gradient(90deg, #30123b, #30b7e2, #9ff944, #f48324, #7a0403)";

export function StrainLegend({
  calMin,
  calMax,
  unit,
  basis,
}: {
  calMin: number;
  calMax: number;
  unit: Unit | null;
  basis: Basis;
}) {
  const title = labels.viewer.strain_legend + (basis === "mock" ? labels.viewer.mock_suffix : "");
  return (
    <figure data-testid="strain-legend" className="flex flex-col gap-1 text-xs">
      <figcaption className="font-medium">{title}</figcaption>
      <div className="h-2 w-full rounded-sm" style={{ background: TURBO }} aria-hidden />
      <div className="flex justify-between tabular-nums text-muted-foreground">
        <span>
          {calMin} {unitLabel(unit)}
        </span>
        <span>{basis === "mock" ? basisBadge("mock") : null}</span>
        <span>
          {calMax} {unitLabel(unit)}
        </span>
      </div>
      {/* the caption describes a simulated load; a mock field is a fixed formula (demo/ATTRIBUTION.md) */}
      {basis === "mock" ? null : <p className="text-muted-foreground">{labels.viewer.heatmap_caption}</p>}
    </figure>
  );
}
