// Bottom time-series placeholder in plain SVG (PLAN M3). Real-scan points are solid; a point that is simulated
// or gap-filled is hollow, and any segment touching one is dashed. The legend already shows both styles.
// Every point links to that scan's view.
import { labels } from "@/lib/labels";
import { basisBadge, dayLabel, num, provenanceBadge, unitLabel } from "@/lib/format";
import type { Series } from "@/lib/types";

const W = 640;
const H = 170;
const PAD = { l: 48, r: 24, t: 16, b: 34 };

const isSimulated = (p: Series["points"][number]) => p.filled || p.provenance !== "real_scan";

export function Timeline({ series, currentScanId, title }: { series: Series; currentScanId: string; title: string }) {
  const pts = series.points.filter((p) => p.value !== null).sort((a, b) => a.day - b.day);
  const days = pts.map((p) => p.day);
  const d0 = Math.min(...days, 0);
  const d1 = Math.max(...days, d0 + 1);
  const vmax = Math.max(...pts.map((p) => p.value as number), 0) || 1;
  const x = (d: number) => PAD.l + ((d - d0) / (d1 - d0)) * (W - PAD.l - PAD.r);
  const y = (v: number) => H - PAD.b - (v / (vmax * 1.15)) * (H - PAD.t - PAD.b);
  const unit = unitLabel(series.unit);
  const simulated = provenanceBadge("synthetic_insilico") ?? labels.viewer.simulated_badge;

  return (
    <figure data-testid="timeline" className="flex flex-col gap-2">
      <figcaption className="text-sm font-semibold">
        {title} ({unit}) · {series.site}
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full max-w-3xl text-foreground" role="img" aria-label={title}>
        <line x1={PAD.l} y1={H - PAD.b} x2={W - PAD.r} y2={H - PAD.b} stroke="currentColor" strokeOpacity={0.3} />
        <line x1={PAD.l} y1={PAD.t} x2={PAD.l} y2={H - PAD.b} stroke="currentColor" strokeOpacity={0.3} />
        <text x={PAD.l - 6} y={y(vmax) + 4} textAnchor="end" fontSize={10} fill="currentColor" fillOpacity={0.6}>
          {num(vmax)}
        </text>
        {pts.slice(1).map((p, i) => {
          const a = pts[i];
          const dashed = isSimulated(a) || isSimulated(p);
          return (
            <line
              key={`seg-${a.day}-${p.day}`}
              data-segment={dashed ? "simulated" : "measured"}
              x1={x(a.day)}
              y1={y(a.value as number)}
              x2={x(p.day)}
              y2={y(p.value as number)}
              stroke="currentColor"
              strokeWidth={2}
              strokeDasharray={dashed ? "5 4" : undefined}
            />
          );
        })}
        {pts.map((p) => {
          const sim = isSimulated(p);
          const current = p.scan_id === currentScanId;
          const dot = (
            <g data-point={p.scan_id ?? ""} data-simulated={sim ? "true" : "false"}>
              <circle
                cx={x(p.day)}
                cy={y(p.value as number)}
                r={current ? 7 : 5.5}
                fill={sim ? "var(--background)" : "currentColor"}
                stroke="currentColor"
                strokeWidth={2}
              />
              <text x={x(p.day)} y={H - PAD.b + 16} textAnchor="middle" fontSize={11} fill="currentColor">
                {dayLabel(p.day)}
              </text>
              <text x={x(p.day)} y={y(p.value as number) - 11} textAnchor="middle" fontSize={11} fill="currentColor">
                {num(p.value)}
              </text>
            </g>
          );
          return p.scan_id ? (
            <a key={`pt-${p.day}`} href={`/scan/${p.scan_id}`} aria-label={`${dayLabel(p.day)} ${num(p.value)} ${unit}`}>
              {dot}
            </a>
          ) : (
            <g key={`pt-${p.day}`}>{dot}</g>
          );
        })}
      </svg>
      <div data-testid="timeline-legend" className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <svg width="30" height="10" aria-hidden>
            <line x1="0" y1="5" x2="30" y2="5" stroke="currentColor" strokeWidth="2" />
            <circle cx="15" cy="5" r="4" fill="currentColor" />
          </svg>
          {basisBadge("measured")}
        </span>
        <span className="flex items-center gap-1.5">
          <svg width="30" height="10" aria-hidden>
            <line x1="0" y1="5" x2="30" y2="5" stroke="currentColor" strokeWidth="2" strokeDasharray="5 4" />
            <circle cx="15" cy="5" r="4" fill="var(--background)" stroke="currentColor" strokeWidth="2" />
          </svg>
          {`${simulated}: ${labels.viewer.timeline_legend}`}
        </span>
      </div>
    </figure>
  );
}
