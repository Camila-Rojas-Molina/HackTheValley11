# C2: file formats

Every file the pipeline writes and the browser reads. Galit reviews this file in M1. Only S0 changes it, as a contract commit.

## Grids and coordinates
- **CT crop:** `.nii.gz`, **int16 HU**, **1.0 mm isotropic**, a **10 mm margin** around the level's vertebra mask, cropped from the conventional (`*_konv`, 120 kVp) series.
- **Affine:** voxel → NIfTI world mm (RAS+ world, as nibabel writes it). The Myel_018 Day-273 L1 crop has `[[-1,0,0,62.875],[0,-1,0,-122.744],[0,0,1,-568.673],[0,0,0,1]]`, shape 95×109×80.
- **Every volume of one scan shares the CT crop's shape and affine** (masks, strain volumes). A test checks `affine` and `shape` equality for every volume in a manifest.
- **Masks are resampled onto the CT grid by physical coordinates, never by index** (the SEGs span fewer slices than the CT; data.md §5).
- **Surfaces** (`.mz3`, `.vtp`, `.stl`) have vertices in **NIfTI world mm**, so they overlay the CT without a transform.

## File names inside one scan (`demo/<scan_id>/` and `/api/v1/scans/<scan_id>/assets/<name>`)
| Name | Kind | Content |
|---|---|---|
| `ct.nii.gz` | `ct` | int16 HU |
| `mask_vertebra.<source>.nii.gz` | `mask_vertebra` | uint8 {0,1}; whole vertebra incl. lesion |
| `mask_body.<source>.nii.gz` | `mask_body` | uint8 {0,1}; vertebral body only. Split method recorded in the manifest (`vertebrae_body` or `geometric_cut`) |
| `mask_lesion.expert.nii.gz` | `mask_lesion` | uint8 {0,1}; **expert only** (no lesion segmenter exists for ordinary CT) |
| `<field>.<variant>.nii.gz` | `strain_volume` | float32 on the CT grid; slice overlay and 3D cut-away |
| `<field>.<variant>.mz3` | `strain_surface` | mesh + one float32 scalar per vertex (NiiVue mesh layer) |
| `context_T11-L5.mz3` | `context_mesh` | low-poly T11–L5 surfaces, no scalars |
| `surface.<variant>.vtp` | `surface_vtp` | XML PolyData, every point array (vtk.js fallback, ParaView) |
| `fields.<variant>.vti` | `volume_vti` | XML ImageData, cell arrays |
| `vertebra.<source>.stl` | `stl` | binary STL, outward normals, watertight, positive volume |
| `metrics.json` | `metrics_json` | the run's metrics + calibration block (schema below) |

- `<source>` ∈ `expert`, `ai`. `<variant>` ∈ `expert`, `ai` (mask source the FE ran on), or `mock` (phantom only).
- `<field>` ∈:
  - `strain_min_principal_ue`: compressive (minimum) principal strain in µε, **the default display, shown as magnitude**;
  - `strain_max_principal_ue`: µε;
  - `vonmises_mpa`: MPa, optional layer, display only;
  - `rigidity_ratio`: surface only (CTRA fallback painting, PLAN M5 fallback).
- Strain volumes and surfaces are written for lesion model `weak_bone` (the HU chain, science.md §4.2). The `void` model feeds metrics only.
- **No `.vtu` is ever served.** Names match `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`; the asset route returns 404 for any other name (no `../`).

## Media types (the asset route and the M3 smoke test)
| Extension | Content-Type |
|---|---|
| `.nii.gz` | `application/gzip` |
| `.mz3` | `application/octet-stream` |
| `.vtp`, `.vti` | `application/xml` |
| `.stl` | `model/stl` |
| `.json` | `application/json` |

## `.mz3` (what NiiVue 0.69.0 loads; viewer.md §7, checked in the spike)
gzip of: header `<HHIII`: magic 23117, attr = 1 (faces) | 2 (vertices) | 8 (scalars), n_faces, n_vertices, n_skip = 0; then faces `<i4` (n_faces×3), vertices `<f4` (n_vertices×3, world mm), scalars `<f4` (n_vertices). Context meshes use attr = 3 (no scalars).

## Colour scales
- Sent by the API in the manifest (`color_scales`), never chosen by the browser.
- `scope: "patient_level"`: **identical for every timepoint of one patient and level**, so "redder" means the same thing on Day 0 and Day 273. S3 fixes the strain range from the baseline run in M5 and records it in the scan rows; it does not change per scan.
- CT: `gray`, window −500…1500 HU. Lesion mask: `red` label, opacity 0.6. Vertebra mask: `green` label, opacity 0.15.
- Strain: `turbo`, `display: "magnitude"`, cal_min 0. The phantom's mock field uses 0–1500 µε (S2 generates the mock inside that range).
- **Stored as magnitude.** A `strain_min_principal_ue` volume (`.nii.gz`) or surface (`.mz3`) stores −ε_min ≥ 0 in µε, the magnitude, not the signed value: the viewer passes `cal_min`/`cal_max` to NiiVue as they are, and NiiVue draws nothing below `cal_min`. The database keeps the signed value (negative is compressive, `db.md`). S3's export test asserts min ≥ 0 on every written strain file (M5).

## `metrics.json` and the manifest
- The manifest is `GET /api/v1/scans/{scan_id}` (`ScanManifest` in `api/app/schemas.py`); the pipeline also writes it as `manifest.json`. `schema: "rb.manifest/1"`.
- `metrics.json` (`schema: "rb.metrics/1"`) = `{"schema", "scan_id", "run_id", "solver_version", "params": {load_n, voxel_mm, lesion_models, statistic, exclusion_band_mm}, "calibration": <block>, "metrics": [Metric...], "dof", "seconds"}`. `Metric` is the same model as in the manifest.
- **Calibration block** (both files): `method` (`phantomless_fat_air` preferred, `phantomless_air_fat_muscle`), `slope`, `intercept`, `slope_sd`, `peaks_hu {air, fat, muscle}`, `reference {air, fat, muscle}` + `reference_unit`, `roi` (where on the **full scan**), `fit_r2`, `label` (labels.json `calibration.label`). A crop without this block is refused by `run_physics` (C7).
- **Lesion volumes name their grid:** `original` (the source CT grid, the headline value: 3.24 cm³ for L1 on Day 273) or `crop_1mm` (3,217 voxels = 3.22 cm³).

## Demo bundle (`demo/`, S2 in M3)
- `demo/<scan_id>/<asset name>` for `myel018-L1-d273`, `myel018-L1-d0`, `phantom`; plus `demo/ATTRIBUTION.md` and `demo/PROVENANCE.json`.
- `PROVENANCE.json`: per file `{path, sha256, bytes, source_script, basis: measured|mock, provenance}` and a `calibration` block per scan (null until M5).
- The Day-273 crops are the spike files byte-for-byte, so their sha256 match the fixture: `ct.nii.gz` 4ca90d8b…, `mask_vertebra.expert.nii.gz` b22e99cd…, `mask_lesion.expert.nii.gz` 163103d9…. If S2 regenerates any file, S0 updates `contracts/fixtures/scans/myel018-L1-d273.json` in the same push.
- Phantom: the viewer spike's synthetic femur shaft (96×96×128, 1 mm, origin (−48, −48, −64), seed 0) with a **mock** strain volume and **mock** `.mz3`. Real anatomy never carries a mock strain field (the manifest model refuses it).
- Every NIfTI in `demo/` is 1 mm; CTs are int16 (M3 test).

## Upload (`POST /api/v1/scans`)
- Body: one CT as **`application/gzip` (`.nii.gz`)**, streamed; query `patient_id`, `day`, optional `label`. Over `RB_MAX_UPLOAD_MB` → 413 before the body is fully read. No multipart, so no form parser is needed.
- An uploaded CT gets bone masks from the Bone Finder only; its lesion tiles read "not available: no lesion segmenter".
