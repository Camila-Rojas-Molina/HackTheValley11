"use client";
// NiiVue 0.69.0 (viewer.md): CT in a bone window, the masks as label overlays, and the strain volume as a Turbo
// overlay on a fixed scale from the manifest. The default is one draggable 3D bone (drag to rotate, scroll to
// zoom); "Slices" switches to the 2x2 view (axial, coronal, sagittal, 3D). "Cut-away" slices the 3D bone open at
// its centre, facing the camera, with a clip plane that also clips the overlays, so the strain inside shows.
// Loaded only through ./index.tsx (next/dynamic, ssr:false).
import { useEffect, useRef, useState } from "react";
import { Niivue, SLICE_TYPE } from "@niivue/niivue";
import { labels } from "@/lib/labels";
import type { ScanViewerProps } from "./types";

type NVMeshLayerKey = Parameters<Niivue["setMeshLayerProperty"]>[2];

// The clip plane faces the camera (same azimuth/elevation), through the centre of the volume (depth 0), so the
// near half is cut away; its colour is transparent so only the cut face shows. Tuned on the phantom and Day 273.
const NO_CLIP = [2, 0, 0]; // NiiVue: a depth above 1 turns the clip plane off
const RENDER_AZIMUTH_ELEVATION: [number, number] = [340, 20];
const FOCUS_FRACTION = 0.8; // strain focus: voxels at or above 80% of the volume's maximum

type NVImageLike = { img?: ArrayLike<number> | null; hdr?: { dims?: number[]; affine?: number[][] } | null };

/** Centroid, in world mm, of the voxels at or above `threshold` (null if none). Overlays share the CT grid. */
function centroidMM(v: NVImageLike, threshold: (max: number) => number): number[] | null {
  const img = v.img;
  const dims = v.hdr?.dims;
  const A = v.hdr?.affine;
  if (!img || !dims || !A) return null;
  const [, nx, ny, nz] = dims;
  let max = -Infinity;
  for (let i = 0; i < img.length; i++) if (img[i] > max) max = img[i];
  if (!(max > 0)) return null;
  const t = threshold(max);
  let si = 0, sj = 0, sk = 0, n = 0;
  for (let k = 0; k < nz; k++)
    for (let j = 0; j < ny; j++)
      for (let i = 0; i < nx; i++)
        if (img[i + nx * (j + ny * k)] >= t) {
          si += i;
          sj += j;
          sk += k;
          n++;
        }
  if (!n) return null;
  const vox = [si / n, sj / n, sk / n];
  return [0, 1, 2].map((r) => A[r][0] * vox[0] + A[r][1] * vox[1] + A[r][2] * vox[2] + A[r][3]);
}

export default function ScanViewer({ scanId, layers, strain, pending, height }: ScanViewerProps) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const nvRef = useRef<Niivue | null>(null);
  const meshId = useRef<string | null>(null);
  const [status, setStatus] = useState<string>(pending ? "pending" : "loading");
  const [surface, setSurface] = useState(false);
  const [view, setView] = useState<"3d" | "slices">("3d");
  // A lesion sits inside the vertebra, so a scan with a lesion mask starts cut open (3D view only).
  const hasLesion = layers.some((l) => l.kind === "mask_lesion");
  const [cut, setCut] = useState(hasLesion);
  const cutRef = useRef(hasLesion);
  const layerKey = JSON.stringify(layers);
  const hasStrain = strain !== null;

  useEffect(() => {
    if (pending || !canvas.current) return;
    const t0 = performance.now();
    const nv = new Niivue({
      backColor: [0.04, 0.05, 0.08, 1],
      isColorbar: hasStrain,
      show3Dcrosshair: true,
      isClipAllVolumes: true,
      crosshairColor: [1, 1, 1, 0.5],
    });
    nvRef.current = nv;
    meshId.current = null;
    let cancelled = false;
    const ls: ScanViewerProps["layers"] = JSON.parse(layerKey);
    (async () => {
      await nv.attachToCanvas(canvas.current!);
      await nv.loadVolumes(
        ls.map((l) => ({ url: l.url, colormap: l.colormap, cal_min: l.cal_min, cal_max: l.cal_max, opacity: l.opacity })),
      );
      if (cancelled) return;
      nv.volumes.forEach((v, i) => {
        v.colorbarVisible = ls[i]?.kind === "strain";
      });
      // Centre the slices on the lesion (or the strain hotspot) rather than on the middle of the crop.
      const lesion = ls.findIndex((l) => l.kind === "mask_lesion");
      const hot = ls.findIndex((l) => l.kind === "strain");
      const focus =
        (lesion >= 0 ? centroidMM(nv.volumes[lesion] as NVImageLike, () => 0.5) : null) ??
        (hot >= 0 ? centroidMM(nv.volumes[hot] as NVImageLike, (max) => FOCUS_FRACTION * max) : null);
      if (focus) nv.scene.crosshairPos = nv.mm2frac(focus);
      nv.setMultiplanarLayout(2); // 2x2: axial, coronal, sagittal + 3D render
      nv.setRenderAzimuthElevation(...RENDER_AZIMUTH_ELEVATION);
      nv.setClipPlaneColor([0, 0, 0, 0]);
      nv.setSliceType(SLICE_TYPE.RENDER);
      aimCut(nv);
      nv.onMouseUp = () => cutRef.current && aimCut(nv); // the cut keeps facing the camera after a drag
      nv.updateGLVolume();
      const ms = Math.round(performance.now() - t0);
      wrap.current?.setAttribute("data-ms", String(ms));
      wrap.current?.setAttribute("data-layers", ls.map((l) => l.kind).join(","));
      setStatus("ready");
    })().catch((e: unknown) => {
      if (cancelled) return;
      wrap.current?.setAttribute("data-error", String(e).slice(0, 300));
      setStatus("error");
      console.error("viewer", scanId, e);
    });
    return () => {
      cancelled = true;
      nvRef.current = null;
      // Not WEBGL_lose_context: the context belongs to the <canvas>, which React reuses on the StrictMode remount.
      nv.cleanup();
    };
  }, [scanId, layerKey, pending, hasStrain]);

  function aimCut(nv: Niivue) {
    nv.setClipPlane(cutRef.current ? [0, nv.scene.renderAzimuth, nv.scene.renderElevation] : NO_CLIP);
  }

  // One draggable 3D bone by default; "Slices" is the 2x2 view. The 3D crosshair only helps in the slice view.
  useEffect(() => {
    const nv = nvRef.current;
    if (!nv || status !== "ready") return;
    nv.opts.show3Dcrosshair = view === "slices";
    nv.setSliceType(view === "3d" ? SLICE_TYPE.RENDER : SLICE_TYPE.MULTIPLANAR);
    nv.updateGLVolume();
  }, [view, status]);

  function toggleCut(on: boolean) {
    setCut(on);
    cutRef.current = on;
    if (nvRef.current) aimCut(nvRef.current);
  }

  async function toggleSurface(on: boolean) {
    setSurface(on);
    const nv = nvRef.current;
    if (!nv || !strain?.meshUrl) return;
    if (meshId.current === null) {
      if (!on) return;
      await nv.loadMeshes([{ url: strain.meshUrl }]);
      const id = nv.meshes[0].id;
      meshId.current = id;
      // .mz3 scalars arrive as layer 0; keys and values are typed narrower than NiiVue accepts at runtime.
      await nv.setMeshLayerProperty(id, 0, "colormap" as NVMeshLayerKey, strain.colormap as unknown as number);
      await nv.setMeshLayerProperty(id, 0, "cal_min" as NVMeshLayerKey, strain.cal_min);
      await nv.setMeshLayerProperty(id, 0, "cal_max" as NVMeshLayerKey, strain.cal_max);
      return;
    }
    // setMeshProperty is typed with a numeric id; NVMesh ids are strings and getMeshIndexByID takes either.
    nv.setMeshProperty(meshId.current as unknown as number, "visible", on);
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        ref={wrap}
        data-viewer={scanId}
        data-status={status}
        style={{ height }}
        className="relative w-full overflow-hidden rounded-md border border-border bg-[#0a0d14]"
      >
        {pending ? (
          <p className="absolute inset-0 grid place-items-center px-4 text-center text-sm text-slate-300">{pending}</p>
        ) : (
          <canvas ref={canvas} />
        )}
        {status === "loading" && !pending ? (
          <p className="pointer-events-none absolute left-3 top-3 text-xs text-slate-300">…</p>
        ) : null}
      </div>
      {status === "ready" ? (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted-foreground">
          <span role="group" aria-label="View" className="flex overflow-hidden rounded-md border border-border">
            {(["3d", "slices"] as const).map((v) => (
              <button
                key={v}
                type="button"
                aria-pressed={view === v}
                onClick={() => setView(v)}
                className={`px-2.5 py-1 ${view === v ? "bg-foreground text-background" : "hover:text-foreground"}`}
              >
                {v === "3d" ? "3D" : "Slices"}
              </button>
            ))}
          </span>
          {view === "3d" ? (
            <>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={cut} onChange={(e) => toggleCut(e.target.checked)} /> Cut-away
              </label>
              <span>Drag to rotate · scroll to zoom</span>
            </>
          ) : null}
        </div>
      ) : null}
      {strain?.meshUrl && status === "ready" ? (
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <input type="checkbox" checked={surface} onChange={(e) => void toggleSurface(e.target.checked)} />
          <span>{labels.viewer.surface_toggle}</span>
        </label>
      ) : null}
    </div>
  );
}
