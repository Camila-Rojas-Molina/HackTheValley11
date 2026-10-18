"use client";
// next/dynamic with ssr:false is only allowed inside a Client Component (viewer.md §3). It keeps NiiVue
// (about 344 KB gzip) off every other route and out of the server render.
import dynamic from "next/dynamic";

export const ScanViewer = dynamic(() => import("./ScanViewer"), {
  ssr: false,
  loading: () => <div className="h-[440px] w-full rounded-md border border-border bg-[#0a0d14]" data-status="loading" />,
});

export type { ScanViewerProps, ViewerLayer, ViewerStrain } from "./types";
