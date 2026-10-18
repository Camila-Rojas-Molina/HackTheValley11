// The right-hand metrics panel: a headline card, then big-number tiles. Every value carries its basis badge
// (measured, simulation, MOCK, pending (Mx)) and, for synthetic data, the SIMULATED badge. Titles, units and
// sub-labels all come from labels.json.
//
// Each tile is an <article> whose first child is its <h3> title; `data-tile` is set only where that title is a
// labels.json tile template (web/tests/scan.test.mjs checks it), `data-part` marks the other cards.
import { labels } from "@/lib/labels";
import { MISSING, basisBadge, fill, gridLabel, maskLabel, num, provenanceBadge, valueWithUnit } from "@/lib/format";
import type { Metric, ScanManifest } from "@/lib/types";
import { Badge, BasisBadge } from "./Badge";

// The tiles differ in which strings they carry (label, sub_label, value_label, ...), so read them by name.
const tile = (key: string) => (labels.tiles.find((t) => t.key === key) ?? { key }) as unknown as Record<string, string>;

function find(ms: Metric[], key: string, grid?: string): Metric | undefined {
  return ms.find((m) => m.key === key && (grid === undefined || m.grid === grid));
}

// "3.24 cm³ (original scan grid; ...)" -> the part in brackets
const paren = (text: string) => text.replace(/^[^(]*\(/, "").replace(/\)\s*$/, "");

function Badges({ metric }: { metric?: Metric }) {
  if (!metric) return <span />;
  const sim = provenanceBadge(metric.provenance);
  return (
    <span className="flex flex-wrap justify-end gap-1">
      <BasisBadge basis={metric.basis} text={basisBadge(metric.basis, metric.available_in)} />
      {sim ? (
        <Badge tone="simulated" data-provenance={metric.provenance}>
          {sim}
        </Badge>
      ) : null}
    </span>
  );
}

function Stat({
  title, tileKey, part, metric, detail, sub, big = false,
}: {
  title: string; tileKey?: string; part?: string; metric?: Metric; detail?: string | null; sub?: string; big?: boolean;
}) {
  const value = metric ? valueWithUnit(metric) : null;
  return (
    <article
      data-tile={tileKey}
      data-part={part}
      className="grid grid-cols-[1fr_auto] items-start gap-x-2 rounded-lg border border-border bg-card/60 p-3 text-card-foreground"
    >
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      <Badges metric={metric} />
      <p
        data-metric={metric?.key}
        className={`col-span-2 mt-1 font-semibold tabular-nums ${big ? "text-3xl" : "text-2xl"} ${value ? "" : "text-muted-foreground"}`}
      >
        {value ?? MISSING}
      </p>
      {detail && value ? <p className="col-span-2 mt-0.5 text-[11px] text-muted-foreground">{detail}</p> : null}
      {sub ? <p className="col-span-2 mt-1 text-[11px] leading-4 text-muted-foreground/80">{sub}</p> : null}
    </article>
  );
}

export function MetricsPanel({ manifest, dayA }: { manifest: ScanManifest; dayA: number | null }) {
  const m = manifest.metrics;
  if (m.length === 0) {
    return (
      <section data-testid="metrics-panel" className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">{manifest.simulated ? labels.viewer.phantom_label : MISSING}</p>
      </section>
    );
  }

  const original = (key: string) => find(m, key, "original") ?? find(m, key);
  const stiff = find(m, "stiffness_change_axial_pct");
  const share = find(m, "lesion_share_pts");
  const head = tile("headline");
  const headline =
    stiff?.value != null && share?.value != null
      ? fill(head.label, {
          level: manifest.level, day_a: dayA, x: num(stiff.value, 1), y: num(share.value, 1),
          a: num(stiff.range?.low, 1), b: num(stiff.range?.high, 1),
        })
      : fill(head.pending_label, { level: manifest.level, day_a: dayA, available_in: stiff?.available_in ?? share?.available_in });

  const lesion = original("lesion_volume_cm3");
  const vert = original("vertebra_volume_cm3");
  const lesionTile = tile("lesion_volume");
  const lesionDetail = lesion?.value != null
    ? paren(fill(lesionTile.value_label, { value: "", grid_label: gridLabel(lesion.grid), mask_label: maskLabel(lesion.mask_source) }))
    : null;

  return (
    <section data-testid="metrics-panel" className="flex flex-col gap-3">
      <article
        data-tile="headline"
        className="grid grid-cols-[1fr_auto] items-start gap-x-2 rounded-lg border border-border bg-gradient-to-br from-card to-card/40 p-3.5 text-card-foreground"
      >
        <h3 className="text-sm font-medium leading-5">{headline}</h3>
        <Badges metric={stiff} />
        <p className="col-span-2 mt-2 text-[11px] leading-4 text-muted-foreground">{head.sub_label}</p>
      </article>

      <Stat tileKey="lesion_volume" title={lesionTile.label} metric={lesion} detail={lesionDetail} sub={lesionTile.sub_label} big />

      <div className="grid grid-cols-2 gap-3">
        <Stat tileKey="vertebra_volume" title={tile("vertebra_volume").label} metric={vert} detail={gridLabel(vert?.grid)} />
        <Stat part="lesion_pct_of_vertebra" title={tile("lesion_pct").alt_label} metric={find(m, "lesion_pct_of_vertebra")} />
        <Stat tileKey="lesion_pct" title={tile("lesion_pct").label} metric={find(m, "lesion_pct_of_body")} />
        <Stat
          part="strain_ratio"
          title={tile("strain").label.split(":")[0]}
          metric={find(m, "strain_ratio")}
          sub={tile("strain").sub_label}
        />
      </div>

      <Stat tileKey="background" title={tile("background").label} metric={find(m, "background_change_pct")} sub={tile("background").sub_label} />
      <Stat tileKey="modulus" title={tile("modulus").label} metric={find(m, "modulus_lesion_gpa")} sub={tile("modulus").sub_label} />
      <Stat tileKey="vonmises" title={tile("vonmises").label} metric={find(m, "vonmises_p99_mpa")} sub={tile("vonmises").sub_label} />
    </section>
  );
}
