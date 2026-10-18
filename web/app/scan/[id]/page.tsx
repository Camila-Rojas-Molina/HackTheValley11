// /scan/[id]: Step 1 of Galit's prompt (PLAN M3). Centre: NiiVue 2x2 views (Day 0 and the latest scan side by
// side, not registered until M5). Right: the metrics panel and the AI slots. Bottom: the timeline.
// All data is read on the server from the API, or from the golden fixtures when the API is down.
import Link from "next/link";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { ScanViewer, type ScanViewerProps, type ViewerLayer } from "@/components/viewer";
import { StrainLegend } from "@/components/viewer/StrainLegend";
import { MetricsPanel } from "@/components/metrics/MetricsPanel";
import { Badge } from "@/components/metrics/Badge";
import { Timeline } from "@/components/timeline/Timeline";
import { SummaryCard } from "@/components/ai/SummaryCard";
import { CopilotDrawer } from "@/components/ai/CopilotDrawer";
import { getJson, type Fetched } from "@/lib/api";
import { SCAN_ID_PATTERN, assetHref } from "@/lib/assets";
import { basisBadge, lesionVolumeLine, provenanceBadge } from "@/lib/format";
import { labels } from "@/lib/labels";
import type { Asset, ColorScale, Generation, ScanList, ScanManifest, ScanRef, Series } from "@/lib/types";

const STRAIN_FIELD = "strain_min_principal_ue"; // the default display (formats.md)

function layerFor(m: ScanManifest, asset: Asset | undefined, scaleKey: string, kind: ViewerLayer["kind"]): ViewerLayer | null {
  const scale: ColorScale | undefined = m.color_scales.find((c) => c.layer === scaleKey);
  if (!asset || !scale) return null;
  return {
    kind,
    url: assetHref(m.scan_id, asset.name),
    colormap: scale.colormap,
    cal_min: scale.cal_min,
    cal_max: scale.cal_max,
    opacity: scale.opacity,
  };
}

function viewerProps(
  m: ScanManifest,
): Omit<ScanViewerProps, "height"> & { strainScale: ColorScale | null; strainAsset: Asset | null } {
  const ready = m.assets.filter((a) => a.status === "ready");
  const byKind = (kind: string) => ready.find((a) => a.kind === kind && a.variant !== "ai") ?? ready.find((a) => a.kind === kind);
  const ct = byKind("ct");
  if (!ct) {
    const pendingCt = m.assets.find((a) => a.kind === "ct");
    return {
      scanId: m.scan_id,
      layers: [],
      strain: null,
      pending: basisBadge("pending", pendingCt?.available_in),
      strainScale: null,
      strainAsset: null,
    };
  }
  const strainAsset = ready.find((a) => a.kind === "strain_volume" && a.field === STRAIN_FIELD) ?? null;
  const strainMesh = ready.find((a) => a.kind === "strain_surface" && a.field === STRAIN_FIELD) ?? null;
  const strainScale = m.color_scales.find((c) => c.layer === STRAIN_FIELD) ?? null;
  const layers = [
    layerFor(m, ct, "ct", "ct"),
    layerFor(m, byKind("mask_vertebra"), "mask_vertebra", "mask_vertebra"),
    layerFor(m, byKind("mask_lesion"), "mask_lesion", "mask_lesion"),
    layerFor(m, strainAsset ?? undefined, STRAIN_FIELD, "strain"),
  ].filter((l): l is ViewerLayer => l !== null);
  const hasStrain = layers.some((l) => l.kind === "strain");
  return {
    scanId: m.scan_id,
    layers,
    strain:
      hasStrain && strainScale
        ? {
            colormap: strainScale.colormap,
            cal_min: strainScale.cal_min,
            cal_max: strainScale.cal_max,
            meshUrl: strainMesh ? assetHref(m.scan_id, strainMesh.name) : null,
          }
        : null,
    pending: null,
    strainScale: hasStrain ? strainScale : null,
    strainAsset: hasStrain ? strainAsset : null,
  };
}

function ViewerPanel({ m, height }: { m: ScanManifest; height: number }) {
  const v = viewerProps(m);
  const sim = provenanceBadge(m.provenance);
  const hasLesion = v.layers.some((l) => l.kind === "mask_lesion");
  const volume = lesionVolumeLine(m.metrics);
  return (
    <div data-panel={m.scan_id} className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">{m.label}</h2>
        {sim ? <Badge tone="simulated">{sim}</Badge> : null}
        {m.registration.status === "not_registered" && m.registration.available_in ? (
          <Badge tone="pending" testId="not-registered">
            {labels.viewer.not_registered}
          </Badge>
        ) : null}
      </div>
      {m.provenance !== "real_scan" ? (
        <p data-testid="phantom-label" className="text-sm font-medium">
          {labels.viewer.phantom_label}
        </p>
      ) : null}
      {volume ? (
        <p data-testid="panel-lesion-volume" className="text-sm tabular-nums">
          {volume}
        </p>
      ) : null}
      <ScanViewer scanId={v.scanId} layers={v.layers} strain={v.strain} pending={v.pending} height={height} />
      {hasLesion ? (
        <p data-testid="lesion-caption" className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="inline-block size-2.5 rounded-sm bg-red-600" aria-hidden />
          {labels.viewer.lesion_outline_caption}
        </p>
      ) : null}
      {v.strainScale && v.strainAsset ? (
        <StrainLegend
          calMin={v.strainScale.cal_min}
          calMax={v.strainScale.cal_max}
          unit={v.strainScale.unit}
          basis={v.strainAsset.basis}
        />
      ) : null}
    </div>
  );
}

async function loadPair(primary: ScanManifest): Promise<{ manifests: ScanManifest[]; sources: Fetched<unknown>["source"][] }> {
  if (!primary.patient_id || primary.provenance !== "real_scan") return { manifests: [primary], sources: [] };
  const list = await getJson<ScanList>(`/patients/${primary.patient_id}/scans`);
  if (!list.ok) return { manifests: [primary], sources: [list.source] };
  const real = list.data.scans
    .filter((s): s is ScanRef & { day: number } => s.provenance === "real_scan" && s.level === primary.level && s.day !== null)
    .sort((a, b) => a.day - b.day);
  if (real.length < 2) return { manifests: [primary], sources: [list.source] };
  const first = real[0].scan_id;
  const last = real[real.length - 1].scan_id;
  const pairIds = primary.scan_id === first ? [first, last] : [first, primary.scan_id];
  const otherId = pairIds.find((id) => id !== primary.scan_id)!;
  const other = await getJson<ScanManifest>(`/scans/${otherId}`);
  if (!other.ok) return { manifests: [primary], sources: [list.source] };
  const manifests = [primary, other.data].sort((a, b) => (a.day ?? 0) - (b.day ?? 0));
  return { manifests, sources: [list.source, other.source] };
}

async function ScanPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!SCAN_ID_PATTERN.test(id)) notFound();
  const scan = await getJson<ScanManifest>(`/scans/${id}`);
  if (!scan.ok) notFound();
  const m = scan.data;

  const [pair, series] = await Promise.all([
    loadPair(m),
    m.patient_id && m.level
      ? getJson<Series>(`/patients/${m.patient_id}/series?site=${m.level}&metric=lesion_volume_cm3`)
      : Promise.resolve(null),
  ]);
  const latest = pair.manifests[pair.manifests.length - 1];
  const summary = latest.provenance === "real_scan" ? await getJson<Generation>(`/scans/${latest.scan_id}/summary`) : null;
  const dayA = pair.manifests.length > 1 ? pair.manifests[0].day : null;

  const sources = [scan.source, ...pair.sources, series?.source, summary?.source];
  const offline = sources.includes("fixtures");
  const lesionTitle = labels.tiles.find((t) => t.key === "lesion_volume")?.label ?? "";

  return (
    <main data-scan={m.scan_id} data-source={offline ? "fixtures" : "api"} className="mx-auto flex w-full max-w-[1500px] flex-col gap-4 px-4 py-4">
      <header className="flex flex-wrap items-center gap-2">
        <Link href="/" className="text-lg font-semibold tracking-tight">
          RainbowBone
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-lg font-semibold">
          {m.level ? `${m.level} · ` : ""}
          {m.label}
        </h1>
        {m.patient_id ? <span className="text-sm text-muted-foreground">{m.patient_id}</span> : null}
        <span className="flex-1" />
        {offline ? (
          <Badge tone="offline" testId="offline-badge">
            {labels.viewer.offline_badge}
          </Badge>
        ) : null}
        {m.provenance === "real_scan" ? (
          <Badge tone="neutral" testId="segmentation-badge">
            {labels.viewer.segmentation_badge}
          </Badge>
        ) : null}
      </header>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex min-w-0 flex-col gap-4">
          <div className={pair.manifests.length > 1 ? "grid gap-4 md:grid-cols-2" : "grid max-w-3xl gap-4"}>
            {pair.manifests.map((pm) => (
              <ViewerPanel key={pm.scan_id} m={pm} height={pair.manifests.length > 1 ? 440 : 600} />
            ))}
          </div>
          {series?.ok ? <Timeline series={series.data} currentScanId={m.scan_id} title={lesionTitle} /> : null}
        </div>
        <aside className="flex flex-col gap-4">
          <MetricsPanel manifest={m} dayA={dayA} />
          {m.provenance === "real_scan" ? (
            <SummaryCard summary={summary?.ok ? summary.data : null} pendingIn="M6" />
          ) : null}
          <CopilotDrawer />
        </aside>
      </div>
    </main>
  );
}

export default function Page({ params }: PageProps<"/scan/[id]">) {
  return (
    <Suspense fallback={<main className="flex-1 p-4 text-sm text-muted-foreground" data-status="loading" />}>
      <ScanPage params={params} />
    </Suspense>
  );
}
