// Asset names, media types and the browser-side URL of every scan asset (contracts/formats.md).
// The browser always loads assets from this app's own route, /assets/scans/<scan_id>/<name>, which asks the API
// first and serves the demo bundle when the API cannot (offline, or before the API serves assets).

export const ASSET_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;
export const SCAN_ID_PATTERN = /^[a-z0-9][a-zA-Z0-9-]{0,62}$/;

const MEDIA_TYPES: [string, string][] = [
  [".nii.gz", "application/gzip"],
  [".mz3", "application/octet-stream"],
  [".vtp", "application/xml"],
  [".vti", "application/xml"],
  [".stl", "model/stl"],
  [".json", "application/json"],
];

/** The formats.md media type for an asset name, or null if the extension is not a served format. */
export function mediaType(name: string): string | null {
  for (const [ext, type] of MEDIA_TYPES) if (name.endsWith(ext)) return type;
  return null;
}

export function isValidAssetRef(scanId: string, name: string): boolean {
  return SCAN_ID_PATTERN.test(scanId) && ASSET_NAME_PATTERN.test(name) && mediaType(name) !== null;
}

export function assetHref(scanId: string, name: string): string {
  return `/assets/scans/${encodeURIComponent(scanId)}/${encodeURIComponent(name)}`;
}
