"use client";
// The synthetic femur (lib/femur.ts) in one draggable 3D view: drag to rotate, scroll to zoom. The rainbow is the
// MOCK strain field on NiiVue's Turbo scale; "Cut-away" slices the bone open at the centre, facing the camera, so
// the stress inside the cortex shows on the cut face. Loaded only through ./index.tsx (next/dynamic, ssr:false).
import { useEffect, useRef, useState } from "react";
import { Niivue, SLICE_TYPE } from "@niivue/niivue";
import { buildFemur } from "@/lib/femur";

const HOME: [number, number] = [20, 12]; // azimuth, elevation: the femur upright, seen from the front
const NO_CLIP = [2, 0, 0]; // NiiVue: a depth above 1 turns the clip plane off

export default function FemurViewer({ height }: { height: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const nvRef = useRef<Niivue | null>(null);
  const cutRef = useRef(false);
  const [status, setStatus] = useState("loading");
  const [stress, setStress] = useState(true);
  const [cut, setCut] = useState(false);

  const aimCut = () => {
    const nv = nvRef.current;
    if (!nv) return;
    nv.setClipPlane(cutRef.current ? [0, nv.scene.renderAzimuth, nv.scene.renderElevation] : NO_CLIP);
  };

  useEffect(() => {
    if (!canvas.current) return;
    let cancelled = false;
    let revoke = () => {};
    const nv = new Niivue({
      backColor: [0.04, 0.05, 0.08, 1],
      isColorbar: false,
      show3Dcrosshair: false,
      isClipAllVolumes: true,
      isOrientCube: true,
    });
    nvRef.current = nv;
    (async () => {
      await nv.attachToCanvas(canvas.current!);
      if (cancelled) return; // a StrictMode remount: build the volumes only for the mount that stays
      const files = buildFemur();
      revoke = files.revoke;
      await nv.loadVolumes([
        { url: files.ctUrl, name: "femur_ct.nii", colormap: "gray", cal_min: -500, cal_max: 1500, opacity: 1 },
        { url: files.strainUrl, name: "femur_strain_mock.nii", colormap: "turbo", cal_min: 60, cal_max: 1500, opacity: 0.95, alphaThreshold: true },
      ] as never);
      if (cancelled) return;
      nv.setSliceType(SLICE_TYPE.RENDER);
      nv.scene.volScaleMultiplier = 0.82; // zoom out so the whole femur fits
      nv.setRenderAzimuthElevation(...HOME);
      nv.setClipPlaneColor([0, 0, 0, 0]);
      nv.setClipPlane(NO_CLIP);
      nv.onMouseUp = () => cutRef.current && aimCut(); // the cut keeps facing the camera after a drag
      void nv.setVolumeRenderIllumination(0.5);
      nv.updateGLVolume();
      setStatus("ready");
    })().catch((e: unknown) => {
      if (cancelled) return;
      wrap.current?.setAttribute("data-error", String(e).slice(0, 300));
      setStatus("error");
    });
    return () => {
      cancelled = true;
      nvRef.current = null;
      nv.cleanup();
      revoke();
    };
  }, []);

  const toggleStress = (on: boolean) => {
    setStress(on);
    nvRef.current?.setOpacity(1, on ? 0.95 : 0);
  };
  const toggleCut = (on: boolean) => {
    setCut(on);
    cutRef.current = on;
    aimCut();
  };
  const reset = () => {
    const nv = nvRef.current;
    if (!nv) return;
    nv.setRenderAzimuthElevation(...HOME);
    aimCut();
  };

  return (
    <div className="flex flex-col gap-2">
      <div
        ref={wrap}
        data-viewer="femur"
        data-status={status}
        style={{ height }}
        className="relative w-full overflow-hidden rounded-md border border-border bg-[#0a0d14]"
      >
        <canvas ref={canvas} className="cursor-grab active:cursor-grabbing" aria-label="3D femur: drag to rotate" />
        {status === "loading" ? <p className="pointer-events-none absolute left-3 top-3 text-xs text-slate-300">…</p> : null}
        {status === "error" ? (
          <p className="absolute inset-0 grid place-items-center px-4 text-center text-sm text-slate-300">
            The viewer could not start (WebGL 2 is required).
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={stress} onChange={(e) => toggleStress(e.target.checked)} /> Stress colours
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={cut} onChange={(e) => toggleCut(e.target.checked)} /> Cut-away
        </label>
        <button type="button" onClick={reset} className="underline underline-offset-4 hover:text-foreground">
          Reset view
        </button>
        <span>Drag to rotate · scroll to zoom</span>
      </div>
    </div>
  );
}
