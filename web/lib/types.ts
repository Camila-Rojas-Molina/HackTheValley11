// The subset of the C1 API models (api/app/schemas.py, contracts/openapi.json) that the web app reads.

export type Provenance = "real_scan" | "synthetic_insilico" | "synthetic_metric_interp";
export type Basis = "measured" | "simulation" | "mock" | "pending";
export type Unit = "pct" | "pts" | "cm3" | "gpa" | "mpa" | "ue" | "ratio" | "dice";

export interface Metric {
  key: string;
  value: number | null;
  unit: Unit;
  basis: Basis;
  provenance: Provenance;
  available_in: string | null;
  range: { low: number; high: number } | null;
  grid: "original" | "crop_1mm" | null;
  mask_source: "expert" | "ai" | null;
  lesion_model: "weak_bone" | "void" | null;
  statistic: "central80_mean" | "weakest_5mm" | null;
  compared_to_scan_id: string | null;
  note: string | null;
}

export interface Asset {
  name: string;
  kind: string;
  url: string;
  media_type: string;
  variant: string | null;
  field: string | null;
  basis: Basis;
  provenance: Provenance;
  status: "ready" | "pending";
  available_in: string | null;
  bytes: number | null;
  sha256: string | null;
}

export interface ColorScale {
  layer: string;
  colormap: string;
  cal_min: number;
  cal_max: number;
  unit: Unit | null;
  display: "window" | "label" | "magnitude" | "signed";
  opacity: number;
  scope: "scan" | "patient_level";
  basis: Basis;
}

export interface ScanManifest {
  schema: "rb.manifest/1";
  scan_id: string;
  patient_id: string | null;
  label: string;
  level: string | null;
  day: number | null;
  provenance: Provenance;
  simulated: boolean;
  registration: { status: string; fixed_scan_id: string | null; method: string | null; available_in: string | null };
  mask_sources: {
    vertebra: string | null;
    body: string | null;
    lesion: string | null;
    body_split_method: string | null;
    caption: string;
  };
  grid: { shape: number[]; spacing_mm: number[]; affine: number[][] } | null;
  assets: Asset[];
  color_scales: ColorScale[];
  metrics: Metric[];
  labels_version: string;
}

export interface ScanRef {
  scan_id: string;
  patient_id: string | null;
  level: string | null;
  day: number | null;
  label: string;
  provenance: Provenance;
  simulated: boolean;
  status: string;
}

export interface ScanList {
  patient_id: string;
  scans: ScanRef[];
}

export interface SeriesPoint {
  day: number;
  value: number | null;
  basis: Basis;
  provenance: Provenance;
  filled: boolean;
  scan_id: string | null;
  available_in: string | null;
}

export interface Series {
  patient_id: string;
  site: string;
  metric: string;
  unit: Unit;
  points: SeriesPoint[];
  contains_synthetic: boolean;
}

export interface Generation {
  scan_id: string;
  task: string;
  text: { headline: string; detail: string };
  footer: string;
  source: string;
  model: string | null;
  verified: boolean;
  badge: string;
}
