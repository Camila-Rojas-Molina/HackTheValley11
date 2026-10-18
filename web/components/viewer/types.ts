// Props the server page computes from a scan manifest for one viewer (plain data, so it crosses the
// server/client boundary).

export type LayerKind = "ct" | "mask_vertebra" | "mask_body" | "mask_lesion" | "strain";

export interface ViewerLayer {
  kind: LayerKind;
  url: string;
  colormap: string;
  cal_min: number;
  cal_max: number;
  opacity: number;
}

export interface ViewerStrain {
  colormap: string;
  cal_min: number;
  cal_max: number;
  meshUrl: string | null;
}

export interface ScanViewerProps {
  scanId: string;
  layers: ViewerLayer[];
  strain: ViewerStrain | null;
  /** Shown instead of the canvas when the CT itself is not ready (e.g. "pending (M5)"). */
  pending: string | null;
  /** Viewer height in px: NiiVue sizes its canvas from the parent box (viewer.md §3). */
  height: number;
}
