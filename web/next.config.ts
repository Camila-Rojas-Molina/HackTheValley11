import path from "node:path";
import type { NextConfig } from "next";

// The repo root, so the app can import the shared contracts (../contracts/labels.json):
// Turbopack does not resolve files outside its root.
const repoRoot = path.join(__dirname, "..");

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  outputFileTracingRoot: repoRoot,
  turbopack: {
    root: repoRoot,
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
