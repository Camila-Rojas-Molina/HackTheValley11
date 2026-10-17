import { labels } from "@/lib/labels";

// M1 placeholder. The Step 1 dashboard and 3D viewer replace it in Push 3 (PLAN M3).
export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col justify-center gap-4 px-4 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">RainbowBone</h1>
      <p className="max-w-2xl text-muted-foreground">{labels.headline_statement}</p>
    </main>
  );
}
