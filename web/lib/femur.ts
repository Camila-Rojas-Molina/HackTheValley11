// A synthetic femur, built in the browser: head, neck, shaft with a hollow canal, greater trochanter and condyles.
// It is not patient data and not a scan. The strain field is MOCK: a fixed formula that puts the highest values
// where a load through the head is usually thought to concentrate (inferomedial neck, medial shaft), so the
// viewer's rainbow can be shown before the physics exists. The page labels it with viewer.phantom_label and the
// legend adds viewer.mock_suffix; a mock field is never shown on real anatomy (contracts/formats.md).
export const FEMUR_SPACING_MM = 1.5;
export const FEMUR_ORIGIN_MM = [-60, -48, -235] as const;
export const FEMUR_SHAPE = [108, 64, 308] as const; // x medial(+), y anterior(+), z proximal(+)

type Vec = readonly [number, number, number];

const capsule = (p: Vec, a: Vec, b: Vec, r: number): number => {
  const ab = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
  const ap = [p[0] - a[0], p[1] - a[1], p[2] - a[2]];
  const t = Math.max(0, Math.min(1, (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / (ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2)));
  return Math.hypot(ap[0] - t * ab[0], ap[1] - t * ab[1], ap[2] - t * ab[2]) - r;
};
const sphere = (p: Vec, c: Vec, r: number) => Math.hypot(p[0] - c[0], p[1] - c[1], p[2] - c[2]) - r;
const ellipsoid = (p: Vec, c: Vec, r: Vec) =>
  (Math.hypot((p[0] - c[0]) / r[0], (p[1] - c[1]) / r[1], (p[2] - c[2]) / r[2]) - 1) * Math.min(...r);
// smooth union, so the neck, trochanter and condyles blend into the shaft instead of meeting at creases
const smin = (a: number, b: number, k = 7) => {
  const h = Math.max(k - Math.abs(a - b), 0) / k;
  return Math.min(a, b) - h * h * k * 0.25;
};

// Signed distance to the outer bone surface in mm (negative inside).
export function femurSdf(p: Vec): number {
  let d = capsule(p, [0, 0, -140], [0, 7, 0], 14);
  d = smin(d, capsule(p, [0, 7, 0], [0, 0, 130], 14));
  d = smin(d, capsule(p, [0, 0, -140], [0, 0, -178], 19)); // distal flare
  d = smin(d, capsule(p, [3, 0, 126], [48, 0, 176], 11.5)); // neck, about 133 degrees to the shaft
  d = smin(d, sphere(p, [62, 0, 190], 24)); // head
  d = smin(d, ellipsoid(p, [-15, 0, 140], [14, 17, 19])); // greater trochanter
  d = smin(d, sphere(p, [7, -11, 100], 7)); // lesser trochanter
  d = smin(d, ellipsoid(p, [-21, 0, -202], [22, 28, 27])); // lateral condyle
  d = smin(d, ellipsoid(p, [21, 0, -202], [22, 28, 27])); // medial condyle
  return d;
}

const blob = (p: Vec, c: Vec, sigma: number, amp: number) =>
  amp * Math.exp(-((p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 + (p[2] - c[2]) ** 2) / (2 * sigma * sigma));

// MOCK compressive-strain magnitude in microstrain, 0 outside the bone.
function mockStrain(p: Vec, sdf: number): number {
  const cortex = sdf > -4.5 ? 1 : 0.6; // the thin shell carries more than the core
  const v =
    230 +
    blob(p, [30, 0, 148], 15, 1350) + // inferomedial neck
    blob(p, [10, 0, 62], 26, 950) + // medial shaft
    blob(p, [-13, 0, 112], 18, 640) + // below the greater trochanter
    blob(p, [22, 0, -195], 20, 420) + // medial condyle
    blob(p, [-22, 0, -195], 20, 330);
  return Math.min(1500, v * cortex);
}

function niftiBlob(data: Int16Array | Float32Array, datatype: 4 | 16): Blob {
  const [nx, ny, nz] = FEMUR_SHAPE;
  const header = new ArrayBuffer(352);
  const v = new DataView(header);
  v.setInt32(0, 348, true);
  [3, nx, ny, nz, 1, 1, 1, 1].forEach((d, i) => v.setInt16(40 + 2 * i, d, true));
  v.setInt16(70, datatype, true);
  v.setInt16(72, datatype === 4 ? 16 : 32, true);
  [1, FEMUR_SPACING_MM, FEMUR_SPACING_MM, FEMUR_SPACING_MM, 1, 1, 1, 1].forEach((d, i) => v.setFloat32(76 + 4 * i, d, true));
  v.setFloat32(108, 352, true);
  v.setFloat32(112, 1, true);
  v.setUint8(123, 2); // millimetres
  v.setInt16(254, 1, true); // sform
  const s = FEMUR_SPACING_MM;
  [s, 0, 0, FEMUR_ORIGIN_MM[0]].forEach((d, i) => v.setFloat32(280 + 4 * i, d, true));
  [0, s, 0, FEMUR_ORIGIN_MM[1]].forEach((d, i) => v.setFloat32(296 + 4 * i, d, true));
  [0, 0, s, FEMUR_ORIGIN_MM[2]].forEach((d, i) => v.setFloat32(312 + 4 * i, d, true));
  new Uint8Array(header).set([0x6e, 0x2b, 0x31, 0], 344); // "n+1"
  return new Blob([header, data.buffer as ArrayBuffer], { type: "application/octet-stream" });
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface FemurFiles {
  ctUrl: string;
  strainUrl: string;
  revoke: () => void;
}

export function buildFemur(): FemurFiles {
  const [nx, ny, nz] = FEMUR_SHAPE;
  const rand = mulberry32(0);
  const ct = new Int16Array(nx * ny * nz);
  const strain = new Float32Array(nx * ny * nz);
  const s = FEMUR_SPACING_MM;
  for (let k = 0; k < nz; k++) {
    const z = FEMUR_ORIGIN_MM[2] + k * s;
    for (let j = 0; j < ny; j++) {
      const y = FEMUR_ORIGIN_MM[1] + j * s;
      for (let i = 0; i < nx; i++) {
        const p: Vec = [FEMUR_ORIGIN_MM[0] + i * s, y, z];
        const idx = i + nx * (j + ny * k);
        const d = femurSdf(p);
        if (d > 0) {
          ct[idx] = -1000; // air: only the bone is drawn
          continue;
        }
        const shaft = z > -125 && z < 105; // the canal is hollow only along the shaft
        let hu = 1250; // cortex
        if (d < -4.5) hu = shaft ? 60 : 340; // marrow in the canal, trabecular bone at the ends
        ct[idx] = Math.round(hu + (rand() - 0.5) * 50);
        strain[idx] = mockStrain(p, d);
      }
    }
  }
  const ctUrl = URL.createObjectURL(niftiBlob(ct, 4));
  const strainUrl = URL.createObjectURL(niftiBlob(strain, 16));
  return { ctUrl, strainUrl, revoke: () => { URL.revokeObjectURL(ctUrl); URL.revokeObjectURL(strainUrl); } };
}
