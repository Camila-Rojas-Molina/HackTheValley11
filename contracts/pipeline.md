# C7: pipeline functions

Package `pipeline/rb_pipeline/` (Python 3.12, no web or DB imports). In Push 1 every function below exists with exactly this signature and raises `NotImplementedError("<function>: lands after G1 (M5)")`: **no segmentation, FE or physics compute exists before Galit's gate (G1).** S0 writes the stubs in M1 (ownership.json `bootstrap_exceptions`); S2 and S3 own the packages afterwards and keep the signatures. A repo test snapshots `inspect.signature` of every public function against this file.

## Boundaries (tests in `tests/repo/`)
- `pipeline/` never imports `fastapi`, `psycopg`, `app` or `app.ai`.
- `rb_pipeline.physics` and `rb_pipeline.export` never import anything AI.
- Large inputs come from `RB_DATA_DIR` (outside the repo); outputs follow `contracts/formats.md`.

## Types (`rb_pipeline/__init__.py`, S0)
```python
Level = Literal["T11", "T12", "L1", "L2", "L3", "L4", "L5"]

@dataclass(frozen=True)
class ScanFiles:            # one CT acquisition on disk
    acquisition_id: str
    ct_path: Path           # full-scan NIfTI (int16 HU), RB_DATA_DIR
    seg_vertebrae_path: Path | None   # expert label map on the CT grid (from DICOM-SEG, combine_segments=True)
    seg_lesions_path: Path | None
    series_instance_uid: str | None
    spacing_mm: tuple[float, float, float]
    kvp: float | None
    sha256: dict[str, str]  # path name -> sha256

@dataclass(frozen=True)
class Calibration:          # the calibration block of formats.md
    method: Literal["phantomless_fat_air", "phantomless_air_fat_muscle"]
    slope: float; intercept: float; slope_sd: float
    peaks_hu: dict[str, float | None]; reference: dict[str, float]; reference_unit: str
    roi: str; fit_r2: float | None; label: str

@dataclass(frozen=True)
class Masks:
    vertebra: Path          # uint8 on the CT grid
    body: Path | None
    source: Literal["expert", "ai"]
    body_split_method: Literal["vertebrae_body", "geometric_cut"] | None

@dataclass(frozen=True)
class Crop:                 # one level, 1 mm, 10 mm margin
    scan_id: str; level: Level
    ct: Path; masks: Masks; lesion: Path | None
    calibration: Calibration | None
    affine: tuple[tuple[float, ...], ...]; shape: tuple[int, int, int]

@dataclass(frozen=True)
class Transform:            # rigid, moving -> fixed, world mm
    matrix: tuple[tuple[float, ...], ...]   # 4x4
    fixed_scan_id: str; moving_scan_id: str; metric: float | None

@dataclass(frozen=True)
class PhysicsParams:
    load_n: float = 800.0
    voxel_mm: float = 1.0
    lesion_models: tuple[Literal["weak_bone", "void"], ...] = ("weak_bone", "void")
    statistic: Literal["central80_mean", "weakest_5mm"] = "central80_mean"
    exclusion_band_mm: float = 3.0
    max_dof: int = 1_200_000

@dataclass(frozen=True)
class RunResult:
    metrics: list[dict]     # Metric dicts (schemas.py Metric fields)
    volumes: dict[str, Path]    # strain volume name -> file (formats.md names)
    surfaces: dict[str, Path]   # .mz3 / .vtp / .stl name -> file
    calibration: Calibration
    dof: int; seconds: float

class CalibrationMissingError(ValueError): ...
class ImplantRegionError(ValueError): ...    # Myel_018 T1-T6 (metal C6-T6)
class TooLargeError(ValueError): ...         # over PhysicsParams.max_dof
```

## Functions
| Module (owner) | Signature | Notes |
|---|---|---|
| `io` (S2) | `ingest(series_uids: list[str], out_dir: Path) -> ScanFiles` | IDC via `idc-index` (no login); DICOM → NIfTI with SimpleITK; SEG → label maps with highdicom `combine_segments=True` |
| `physics` (S3) | `calibrate(full_ct: Path, body_region: Path \| None = None) -> Calibration` | **Full scan, never the crop** (the L1 crop has no outside air). Fat–air preferred, muscle peak logged; slope_sd from peak-fit residuals |
| `segment` (S2) | `segment(ct: Path, levels: list[Level], device: Literal["cpu", "mps", "cuda"] = "cpu") -> dict[Level, Masks]` | TotalSegmentator `total --roi_subset vertebrae_*` + `vertebrae_body`, 1.5 mm. Expert masks come from `ScanFiles.seg_*` |
| `io` (S2) | `crop(ct: Path, masks: Masks, level: Level, calibration: Calibration \| None, lesion: Path \| None = None) -> Crop` | 1 mm, 10 mm margin, masks resampled by physical coordinates. Raises `ImplantRegionError` for Myel_018 T1–T6 |
| `longitudinal` (S2) | `register(moving: Crop, fixed: Crop) -> Transform` | rigid, driven by the vertebra masks |
| `physics` (S3) | `refill_lesion(crop_b: Crop, crop_a_registered: Crop) -> Crop` | lesion voxels of B get registered A HU; gives the lesion share Y |
| `physics` (S3) | `run_physics(crop: Crop, baseline: Crop \| None = None, params: PhysicsParams = PhysicsParams()) -> RunResult` | **Raises `CalibrationMissingError` if `crop.calibration is None`.** CTRA on the body mask (X, Y, background), voxel FE, p99 compressive strain with the mm exclusion band |
| `export` (S3) | `export(result: RunResult, crop: Crop, out_dir: Path) -> dict[str, Path]` | formats.md names; STL normals fixed (the spike's surface had negative volume) |
| `longitudinal` (S2) | `synth_series(scan_a: Crop, scan_b: Crop, days: list[int], volume_curve: Literal["linear"] = "linear", seed: int = 0, generator_version: str = "insilico/1") -> list[Crop]` | lesion = core of B's SDF sized to the curve; bone outside the lesion interpolated A → B; no projection rows; every output is `synthetic_insilico` |

`export` and `PhysicsParams`/`TooLargeError` are additions to the PLAN §3.3 list, needed so `run_physics` has typed params and the files of formats.md have one writer.
