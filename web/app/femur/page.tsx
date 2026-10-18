// /femur: a synthetic femur in one draggable 3D view with the MOCK stress rainbow. Not a scan and not patient
// data: it exists so the stress display can be shown before the physics (M5) produces real fields.
import Link from "next/link";
import { FemurViewer } from "@/components/viewer";
import { StrainLegend } from "@/components/viewer/StrainLegend";
import { Badge } from "@/components/metrics/Badge";
import { labels } from "@/lib/labels";

export default function FemurPage() {
  return (
    <main data-scan="femur" className="mx-auto flex w-full max-w-4xl flex-col gap-3 px-4 py-4">
      <header className="flex flex-wrap items-center gap-2">
        <Link href="/" className="text-lg font-semibold tracking-tight">
          RainbowBone
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-lg font-semibold">Femur</h1>
        <Badge tone="simulated">{labels.viewer.simulated_badge}</Badge>
        <Badge tone="mock">{labels.basis_badges.mock}</Badge>
      </header>
      <p data-testid="phantom-label" className="text-sm font-medium">
        {labels.viewer.phantom_label}
      </p>
      <FemurViewer height={560} />
      <StrainLegend calMin={0} calMax={1500} unit="ue" basis="mock" />
    </main>
  );
}
