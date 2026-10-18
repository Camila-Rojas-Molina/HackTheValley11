// Turns manifest values into the strings labels.json defines. No wording lives here: every template, unit and
// badge is read from contracts/labels.json (C6).
import { labels } from "./labels";
import type { Basis, Metric, Provenance, Unit } from "./types";

/** Not-yet-computed placeholder inside a filled template. */
export const MISSING = "—";

/** Replace each {name} in a labels.json template; a missing value becomes MISSING. */
export function fill(template: string, values: Record<string, string | number | null | undefined>): string {
  return template.replace(/\{(\w+)\}/g, (_m, key: string) => {
    const v = values[key];
    return v === null || v === undefined ? MISSING : String(v);
  });
}

/** Two decimals for every measured number on screen (3.24 cm³, 0.00 cm³, 4.73%). */
export function num(value: number | null | undefined, digits = 2): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function unitLabel(unit: Unit | null | undefined): string {
  if (!unit) return "";
  return (labels.units as Record<string, string>)[unit] ?? unit;
}

/** "3.24 cm³", "4.73%", "1.20×", or null when the value is pending. */
export function valueWithUnit(m: Pick<Metric, "value" | "unit">): string | null {
  const v = num(m.value);
  if (v === null) return null;
  const u = unitLabel(m.unit);
  if (m.unit === "dice") return `${u} ${v}`;
  return m.unit === "pct" || m.unit === "ratio" ? `${v}${u}` : `${v} ${u}`;
}

/** The basis badge: "measured", "simulation", "MOCK" or "pending (M5)". */
export function basisBadge(basis: Basis, availableIn?: string | null): string {
  const t = (labels.basis_badges as Record<string, string>)[basis] ?? basis;
  return fill(t, { available_in: availableIn ?? "" }).replace(/\s*\(\)$/, "");
}

/** "SIMULATED" for synthetic or gap-filled data, null for a real scan. */
export function provenanceBadge(provenance: Provenance, filled = false): string | null {
  const b = labels.provenance_badges as Record<string, string | null>;
  return filled ? b.filled : (b[provenance] ?? null);
}

export function gridLabel(grid: string | null | undefined): string | null {
  return grid ? ((labels.grid_labels as Record<string, string>)[grid] ?? grid) : null;
}

export function maskLabel(source: string | null | undefined): string | null {
  return source ? ((labels.mask_labels as Record<string, string>)[source] ?? source) : null;
}

export function lesionModelLabel(model: string | null | undefined): string | null {
  return model ? ((labels.lesion_model_labels as Record<string, string>)[model] ?? model) : null;
}

export function statisticLabel(stat: string | null | undefined): string | null {
  return stat ? ((labels.statistic_labels as Record<string, string>)[stat] ?? stat) : null;
}

export function dayLabel(day: number | null | undefined): string {
  return fill(labels.viewer.day_label, { day: day ?? null });
}

/** The lesion-volume line for a scan, on the original grid when it exists: "3.24 cm³ (original scan grid; …)". */
export function lesionVolumeLine(metrics: Metric[]): string | null {
  const tile = labels.tiles.find((t) => t.key === "lesion_volume");
  const vols = metrics.filter((m) => m.key === "lesion_volume_cm3" && m.value !== null);
  const m = vols.find((v) => v.grid === "original") ?? vols[0];
  if (!tile || !m || !("value_label" in tile)) return null;
  return `${tile.label} ${fill(tile.value_label as string, {
    value: num(m.value),
    grid_label: gridLabel(m.grid),
    mask_label: maskLabel(m.mask_source),
  })}`;
}
