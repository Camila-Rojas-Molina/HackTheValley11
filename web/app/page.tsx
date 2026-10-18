import Link from "next/link";
import { labels } from "@/lib/labels";

// Entry page until the care-team list (/patients) lands. The three scans are the M3 demo bundle (formats.md).
const SCANS = [
  { id: "myel018-L1-d273", text: labels.viewer.demo_case_label },
  { id: "phantom", text: labels.viewer.phantom_label },
];

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col justify-center gap-4 px-4 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">RainbowBone</h1>
      <p className="max-w-2xl text-muted-foreground">{labels.headline_statement}</p>
      <ul className="flex flex-col gap-2">
        {SCANS.map((s) => (
          <li key={s.id}>
            <Link href={`/scan/${s.id}`} className="text-sm font-medium underline underline-offset-4">
              {s.text}
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
