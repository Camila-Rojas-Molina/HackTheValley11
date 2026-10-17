"""RainbowBone API contract models (C1). Lands in Push 1 as api/app/schemas.py (owned by S0).

Every model forbids extra fields, so drift between a route, a fixture and the web client fails a test
instead of passing silently. contracts/openapi.json is generated from these models plus ROUTES below;
contracts/fixtures/*.json are golden payloads that each parse with the model named in
contracts/fixtures/index.json.

Two provenance ideas are kept apart on purpose:
- `provenance` (data origin, also a DB column): real_scan | synthetic_insilico | synthetic_metric_interp.
  Any synthetic_* value renders the SIMULATED badge. There is no projection or forecast value.
- `basis` (how one number was obtained, the tile badge): measured | simulation | mock | pending.
  measured = computed by our code from real voxels and masks; simulation = output of the FE model;
  mock = placeholder for the UI, never computed; pending = not computed yet (value null, available_in set).

Routes: all under /api/v1. ROUTES is the route table; route_kwargs(operation_id) gives the exact decorator
arguments, so the skeleton routes and the generated OpenAPI cannot disagree.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "1.0.0"
API_PREFIX = "/api/v1"
API_TITLE = "RainbowBone API"

# ----------------------------------------------------------------------------------------- enums
Provenance = Literal["real_scan", "synthetic_insilico", "synthetic_metric_interp"]
Basis = Literal["measured", "simulation", "mock", "pending"]
Milestone = Literal["M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9-1", "M9-2"]
Level = Literal["T11", "T12", "L1", "L2", "L3", "L4", "L5"]
Site = Literal["T11", "T12", "L1", "L2", "L3", "L4", "L5", "left_femur", "right_femur"]
MaskKind = Literal["mask_vertebra", "mask_body", "mask_lesion"]
MaskSource = Literal["expert", "ai"]
LesionModel = Literal["weak_bone", "void"]
VolumeGrid = Literal["original", "crop_1mm"]
SliceStatistic = Literal["central80_mean", "weakest_5mm"]
BodySplitMethod = Literal["vertebrae_body", "geometric_cut"]
StrainField = Literal["strain_min_principal_ue", "strain_max_principal_ue", "vonmises_mpa", "rigidity_ratio"]
AssetKind = Literal[
    "ct", "mask_vertebra", "mask_body", "mask_lesion",
    "strain_volume", "strain_surface", "context_mesh", "surface_vtp", "volume_vti", "stl", "metrics_json",
]
AssetVariant = Literal["expert", "ai", "mock"]
Unit = Literal["pct", "pts", "cm3", "gpa", "mpa", "ue", "ratio", "dice"]
MetricKey = Literal[
    "stiffness_change_axial_pct",    # X: L1 total change since the baseline scan
    "stiffness_change_bending_pct",  # shown only if R1 finds bending stable
    "lesion_share_pts",              # Y: points of X the lesion accounts for (refill counterfactual)
    "background_change_pct",         # X after the lesion is refilled with registered baseline HU
    "strain_ratio",                  # lesion-region p99 compressive strain vs the same region at baseline
    "strain_p99_lesion_ue",
    "vonmises_p99_mpa",              # optional layer, display only
    "lesion_volume_cm3",
    "vertebra_volume_cm3",           # includes the lesion
    "body_volume_cm3",
    "lesion_pct_of_body",
    "lesion_pct_of_vertebra",
    "modulus_lesion_gpa",
    "modulus_ref_gpa",
    "dice_ai_vs_expert",
]
SeriesMetric = Literal[
    "lesion_volume_cm3", "vertebra_volume_cm3", "body_volume_cm3", "stiffness_change_axial_pct",
    "stiffness_change_bending_pct", "lesion_share_pts", "background_change_pct", "strain_ratio",
    "modulus_lesion_gpa",
]
AISource = Literal["gemini", "groq", "vultr", "local", "template"]
AttemptOutcome = Literal["accepted", "rejected", "error", "unavailable", "cooldown", "skipped"]
AITask = Literal["care_team_summary", "patient_explanation", "copilot"]
JobKind = Literal["segment", "physics", "summarize", "synth_series"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
FeedbackKind = Literal["useful", "not_useful", "snooze"]
FlagStatus = Literal["flagged", "not_flagged", "pending"]
FlagConditionKey = Literal["lesion_new_or_grew", "lesion_share_at_least", "share_exceeds_calibration_range"]
CopilotTool = Literal["list_scans", "get_metric_series", "compare_scans", "get_monthly_trend", "define_term"]
AuditAction = Literal[
    "seed_demo", "upload", "ingest", "calibrate", "register", "segment", "physics", "store",
    "summarize", "regenerate", "feedback", "synth_series",
]
Colormap = Literal["gray", "red", "green", "blue", "turbo", "viridis"]

ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"            # patient_id and alert_id
SCAN_ID_PATTERN = r"^[a-z0-9][a-zA-Z0-9-]{0,62}$"     # e.g. myel018-L1-d273, phantom
ASSET_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------------------- shared
class Range(Model):
    low: float
    high: float
    kind: Literal["calibration", "lesion_model", "statistic"] = Field(
        description="calibration: slope perturbed by its measured uncertainty; lesion_model: weak bone vs "
                    "void; statistic: central-80% mean vs weakest 5 mm window")

    @model_validator(mode="after")
    def _ordered(self) -> "Range":
        if self.low > self.high:
            raise ValueError("range low > high")
        return self


class Metric(Model):
    key: MetricKey
    value: Optional[float] = Field(description="null exactly when basis is pending")
    unit: Unit
    basis: Basis
    provenance: Provenance
    available_in: Optional[Milestone] = Field(default=None, description="required when basis is pending")
    range: Optional[Range] = None
    grid: Optional[VolumeGrid] = Field(default=None, description="required for every volume metric")
    mask_source: Optional[MaskSource] = None
    lesion_model: Optional[LesionModel] = None
    statistic: Optional[SliceStatistic] = None
    compared_to_scan_id: Optional[str] = Field(default=None, description="baseline scan for a change metric")
    note: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "Metric":
        if self.basis == "pending":
            if self.value is not None or self.available_in is None:
                raise ValueError("pending metric: value must be null and available_in set")
        elif self.value is None:
            raise ValueError(f"{self.basis} metric needs a value")
        if self.key.endswith("_volume_cm3") and self.grid is None:
            raise ValueError("volume metric must name its grid")
        return self


class ErrorDetail(Model):
    detail: str


class NotAvailable(Model):
    detail: str
    available_in: Milestone


class Unavailable(Model):
    """503 body from the database exception handlers kept from StillHere."""
    ok: Literal[False] = False
    error: str


class QueryTiming(Model):
    raw_ms: Optional[float] = None
    aggregate_ms: Optional[float] = None


# ----------------------------------------------------------------------------------------- health
class Health(Model):
    status: Literal["ok", "down"]
    db: Literal["ok", "down"]
    version: str = Field(description="CONTRACT_VERSION")
    labels_version: str
    server_time: Optional[datetime] = None
    timescaledb: Optional[bool] = Field(default=None, description="null when the DB is down")
    ai_chain: list[AISource]
    queue: Optional[dict[str, int]] = Field(default=None, description="job counts; X-Admin-Key only")


# ----------------------------------------------------------------------------------------- patients
class FlagCondition(Model):
    key: FlagConditionKey
    met: Optional[bool] = Field(description="null = not evaluated yet")
    value: Optional[float] = None
    threshold: Optional[float] = None
    unit: Optional[Unit] = None
    basis: Basis
    available_in: Optional[Milestone] = None
    detail: Optional[str] = None


class DataQuality(Model):
    mask_source: Optional[MaskSource]
    lesion_source: Optional[Literal["expert"]]
    calibration_range: Optional[Range]
    provenance: Provenance
    simulated: bool


class Flag(Model):
    status: FlagStatus
    rule_version: str
    level: Optional[Level]
    scan_id: Optional[str]
    alert_id: Optional[str] = Field(description="set only when status is flagged")
    conditions: list[FlagCondition]
    line: str = Field(description="deterministic 'Flagged because' / pending / not flagged line, "
                                  "rendered by code from labels.json flag.templates")
    protocol_line: str
    data_quality: DataQuality


class PatientListItem(Model):
    patient_id: str = Field(pattern=ID_PATTERN)
    label: str
    dataset: str
    dataset_case: str
    latest_scan_id: Optional[str]
    latest_day: Optional[int]
    scan_count: int
    flag: Flag


class PatientList(Model):
    patients: list[PatientListItem]


class ScanRef(Model):
    scan_id: str = Field(pattern=SCAN_ID_PATTERN)
    patient_id: Optional[str]
    level: Optional[Site]
    day: Optional[int] = Field(description="days since the patient's baseline scan")
    time: Optional[datetime] = Field(description="demo calendar; Day 0 = 2026-01-17T00:00:00Z")
    label: str
    provenance: Provenance
    simulated: bool
    status: Literal["ready", "pending"]
    has_strain: bool


class PatientDetail(PatientListItem):
    scans: list[ScanRef]
    sites: list[Site]


class ScanList(Model):
    patient_id: str
    scans: list[ScanRef]


class LevelRow(Model):
    level: Level
    baseline: list[Metric] = Field(description="values on the baseline scan (Day 0)")
    followup: list[Metric] = Field(description="values on the follow-up scan")
    change: list[Metric] = Field(description="X, Y and background for this level")
    excluded: bool = False
    exclusion_reason: Optional[str] = None


class LevelLadder(Model):
    patient_id: str
    baseline_scan_day: int
    followup_scan_day: int
    statistic: SliceStatistic
    levels: list[LevelRow]
    source: Literal["raw", "aggregate", "fixture"]
    query_ms: QueryTiming


class SeriesPoint(Model):
    time: datetime
    day: int
    value: Optional[float]
    basis: Basis
    provenance: Provenance
    filled: bool = Field(description="gap-filled at query time (time_bucket_gapfill + locf)")
    scan_id: Optional[str]
    mask_source: Optional[MaskSource] = None
    lesion_model: Optional[LesionModel] = None
    available_in: Optional[Milestone] = None


class Series(Model):
    patient_id: str
    site: Site
    metric: SeriesMetric
    unit: Unit
    points: list[SeriesPoint]
    contains_synthetic: bool
    source: Literal["raw", "aggregate", "fixture"]
    query_ms: Optional[float]


# ----------------------------------------------------------------------------------------- scans
class Calibration(Model):
    method: Literal["phantomless_fat_air", "phantomless_air_fat_muscle"]
    slope: float
    intercept: float
    slope_sd: float
    peaks_hu: dict[Literal["air", "fat", "muscle"], Optional[float]]
    reference: dict[Literal["air", "fat", "muscle"], float] = Field(
        description="reference density values the peaks are fitted to, with units in `reference_unit`")
    reference_unit: str
    roi: str = Field(description="where on the full scan the peaks were taken")
    fit_r2: Optional[float] = None
    label: str = Field(description="labels.json calibration.label")


class Registration(Model):
    status: Literal["not_registered", "registered"]
    fixed_scan_id: Optional[str] = None
    method: Optional[str] = None
    available_in: Optional[Milestone] = None


class Acquisition(Model):
    acquisition_id: str
    dataset_case: Optional[str]
    series_instance_uid: Optional[str]
    series_description: Optional[str]
    modality: Literal["CT"] = "CT"
    kvp: Optional[float]
    source_shape: Optional[list[int]] = None
    source_spacing_mm: Optional[list[float]] = None


class Grid(Model):
    shape: list[int] = Field(min_length=3, max_length=3)
    spacing_mm: list[float] = Field(min_length=3, max_length=3)
    affine: list[list[float]] = Field(description="4x4 voxel-to-NIfTI-world-mm, row-major")
    margin_mm: Optional[float]
    dtype: Literal["int16"]


class Asset(Model):
    name: str = Field(pattern=ASSET_NAME_PATTERN)
    kind: AssetKind
    url: str = Field(description="/api/v1/scans/{scan_id}/assets/{name}")
    media_type: str
    variant: Optional[AssetVariant] = Field(default=None, description="mask source, or mock")
    field: Optional[StrainField] = None
    basis: Basis
    provenance: Provenance
    status: Literal["ready", "pending"]
    available_in: Optional[Milestone] = None
    bytes: Optional[int] = None
    sha256: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)


class ColorScale(Model):
    layer: Literal["ct", "mask_vertebra", "mask_body", "mask_lesion",
                   "strain_min_principal_ue", "strain_max_principal_ue", "vonmises_mpa", "rigidity_ratio"]
    colormap: Colormap
    cal_min: float
    cal_max: float
    unit: Optional[Unit]
    display: Literal["window", "label", "magnitude", "signed"]
    opacity: float = Field(ge=0, le=1)
    scope: Literal["scan", "patient_level"] = Field(
        description="patient_level: identical for every timepoint of this patient and level")
    basis: Basis


class MaskSources(Model):
    vertebra: Optional[MaskSource]
    body: Optional[MaskSource]
    lesion: Optional[Literal["expert"]]
    body_split_method: Optional[BodySplitMethod]
    caption: str


class ScanManifest(Model):
    schema_: Literal["rb.manifest/1"] = Field(alias="schema", default="rb.manifest/1")
    scan_id: str = Field(pattern=SCAN_ID_PATTERN)
    patient_id: Optional[str]
    label: str
    level: Optional[Site]
    day: Optional[int]
    time: Optional[datetime]
    provenance: Provenance
    simulated: bool
    generator_version: Optional[str] = None
    anchor_scan_ids: list[str] = []
    seed: Optional[int] = None
    acquisition: Acquisition
    grid: Optional[Grid] = Field(description="null until the crop exists")
    registration: Registration
    calibration: Optional[Calibration]
    calibration_available_in: Optional[Milestone] = None
    mask_sources: MaskSources
    assets: list[Asset]
    color_scales: list[ColorScale]
    metrics: list[Metric]
    labels_version: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _rules(self) -> "ScanManifest":
        if self.provenance != "real_scan" and not self.generator_version:
            raise ValueError("synthetic scan needs generator_version")
        if self.simulated != (self.provenance != "real_scan"):
            raise ValueError("simulated must equal provenance != real_scan")
        if self.provenance == "real_scan" and any(a.basis == "mock" and a.kind.startswith("strain")
                                                  for a in self.assets):
            raise ValueError("real anatomy never carries a mock strain field")
        if self.provenance == "real_scan" and self.calibration is None and self.calibration_available_in is None:
            raise ValueError("real scan without calibration: set calibration_available_in")
        if self.grid is None and any(a.status == "ready" for a in self.assets):
            raise ValueError("ready assets need a grid")
        names = [a.name for a in self.assets]
        if len(names) != len(set(names)):
            raise ValueError("duplicate asset name")
        return self


class ScanUploadAccepted(Model):
    scan_id: str
    patient_id: str
    status: Literal["pending"]
    jobs: list["JobRef"]


class RunRequest(Model):
    steps: list[Literal["segment", "physics", "summarize"]] = Field(min_length=1)
    levels: list[Level] = Field(default_factory=lambda: ["L1"])
    mask_source: MaskSource = "expert"
    lesion_models: list[LesionModel] = Field(default_factory=lambda: ["weak_bone", "void"])
    baseline_scan_id: Optional[str] = None
    load_n: float = Field(default=800.0, gt=0)
    voxel_mm: Literal[1.0, 1.5] = 1.0


class JobRef(Model):
    job_id: str
    kind: JobKind
    status: JobStatus


class RunAccepted(Model):
    run_id: str
    scan_id: str
    jobs: list[JobRef]


class CostBreakdown(Model):
    compute_seconds: Optional[float]
    compute_usd_per_hour: Optional[float]
    gpu_seconds: Optional[float]
    gpu_usd_per_hour: Optional[float]
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    token_usd: Optional[float]
    note: Optional[str] = None


class Job(Model):
    job_id: str
    kind: JobKind
    status: JobStatus
    scan_id: Optional[str]
    patient_id: Optional[str]
    run_id: Optional[str]
    params: dict[str, Any]
    result: Optional[dict[str, Any]]
    error: Optional[str]
    attempts: int
    runner: Optional[str] = Field(description="e.g. vultr-vhf-4c-16gb-yto, vultr-a16, mac-dev")
    device: Optional[Literal["cpu", "mps", "cuda"]]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    seconds: Optional[float]
    cost_usd: Optional[float]
    cost: Optional[CostBreakdown]


# ----------------------------------------------------------------------------------------- AI
class Facts(Model):
    """The only input any model sees: numbers our code computed. No names, DICOM tags or free text."""
    facts_version: Literal["1"] = "1"
    scan_id: str
    level: Level
    day_a: int
    day_b: int
    interval_days: int
    lesion_volume_a_cm3: float
    lesion_volume_b_cm3: float
    lesion_volume_change_cm3: float
    lesion_grid: VolumeGrid
    vertebra_volume_b_cm3: float
    lesion_pct_of_vertebra_b: float
    stiffness_change_axial_pct: Optional[float]
    lesion_share_pts: Optional[float]
    background_change_pct: Optional[float]
    calib_low_pct: Optional[float]
    calib_high_pct: Optional[float]
    strain_ratio: Optional[float]
    mask_source: MaskSource
    lesion_source: Literal["expert"]
    provenance: Provenance
    simulated: bool


class GeneratedText(Model):
    headline: str
    detail: str
    numbers_used: list[float]


class Attempt(Model):
    provider: AISource
    model: Optional[str]
    ms: float
    outcome: AttemptOutcome
    reason: Optional[str] = None


class Generation(Model):
    """Chain.run(...) result as served: GET summary and the regenerate drill."""
    scan_id: str
    task: Literal["care_team_summary", "patient_explanation"]
    text: GeneratedText
    footer: str
    source: AISource
    model: Optional[str]
    verified: bool
    numbers_matched: int
    numbers_total: int
    badge: str = Field(description="labels.json ai.badge rendered, e.g. 'template · 3/3 numbers match computed values'")
    attempts: list[Attempt]
    facts_sha256: str = Field(pattern=SHA256_PATTERN)
    prompt_version: str
    generated_at: datetime
    cache_hit: bool
    chain_override: bool
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    cost_usd: Optional[float]


class Explanation(Generation):
    reading_grade: float = Field(description="Flesch-Kincaid grade of headline + detail, computed by code")
    reading_grade_formula: Literal["flesch_kincaid_grade/1"]
    target_grade_max: float
    review_note: str


class RegenerateRequest(Model):
    chain: list[AISource] = Field(min_length=1, description="e.g. ['groq','vultr','template']; template is "
                                                            "appended if missing")
    bypass_cache: bool = True
    task: Literal["care_team_summary", "patient_explanation"] = "care_team_summary"


class CopilotRequest(Model):
    question: str = Field(min_length=1, max_length=500)
    patient_id: str = Field(pattern=ID_PATTERN, description="from the UI session; never chosen by a model")


class ToolCall(Model):
    tool: CopilotTool
    args: dict[str, Any]
    rows: int


class CopilotAnswer(Model):
    answer: str
    refused: bool
    tool_calls: list[ToolCall]
    mentions_simulated: bool
    source: AISource
    model: Optional[str]
    verified: bool
    numbers_matched: int
    numbers_total: int
    footer: str
    attempts: list[Attempt]


# ----------------------------------------------------------------------------------------- alerts
class AlertFeedbackRequest(Model):
    feedback: FeedbackKind
    snooze_hours: Optional[int] = Field(default=None, ge=1, le=168, description="snooze only; default 24")
    note: Optional[str] = Field(default=None, max_length=280)


class AlertFeedbackAck(Model):
    feedback_id: int
    alert_id: str
    feedback: FeedbackKind
    snooze_until: Optional[datetime]
    recorded_at: datetime


# ----------------------------------------------------------------------------------------- tiger
class HypertableInfo(Model):
    name: str
    chunk_interval: Optional[str]
    chunks: Optional[int]
    compressed_chunks: Optional[int]
    rows_estimate: Optional[int]
    before_bytes: Optional[int]
    after_bytes: Optional[int]


class CaggInfo(Model):
    name: str
    hypertable: str
    bucket: str
    group_by: list[str]
    materialized_only: Optional[bool]


class TigerInfo(Model):
    backend: Literal["timescaledb", "postgres"]
    timescaledb_version: Optional[str]
    hypertables: list[HypertableInfo]
    continuous_aggregates: list[CaggInfo]
    db_size_bytes: Optional[int]
    db_size_pretty: Optional[str]
    budget_bytes: int = Field(description="contracts/db.md total budget")
    query_ms: QueryTiming
    source: Literal["live", "fixture"]
    measured_at: datetime


# ----------------------------------------------------------------------------------------- audit
class AuditEntry(Model):
    seq: int
    at: datetime
    actor: str
    action: AuditAction
    patient_id: Optional[str]
    scan_id: Optional[str]
    run_id: Optional[str]
    detail: dict[str, Any]
    prev_hash: str
    hash: str = Field(pattern=SHA256_PATTERN)


class AuditTrail(Model):
    scan_id: str
    chain_key: str
    chain_ok: bool
    head: str
    entries: list[AuditEntry]


ScanUploadAccepted.model_rebuild()


# ----------------------------------------------------------------------------------------- route table
class RouteSpec(Model):
    operation_id: str
    method: Literal["GET", "POST"]
    path: str = Field(description="relative to API_PREFIX")
    summary: str
    tag: str
    response_model: str
    status_code: int = 200
    admin: bool = False
    request_model: Optional[str] = None
    raw_body_media_type: Optional[str] = None
    path_params: list[str] = []
    query_params: list[str] = []
    m1_behaviour: Literal["live", "fixture", "501"]
    available_in: Optional[Milestone] = Field(
        default=None, description="milestone that makes the route live (501 body until then)")


ROUTES: list[RouteSpec] = [
    RouteSpec(operation_id="get_health", method="GET", path="/health", summary="Service and database status",
              tag="health", response_model="Health", m1_behaviour="live"),
    RouteSpec(operation_id="list_patients", method="GET", path="/patients",
              summary="Care-team list with the deterministic flag", tag="patients",
              response_model="PatientList", m1_behaviour="fixture", available_in="M2"),
    RouteSpec(operation_id="get_patient", method="GET", path="/patients/{patient_id}", summary="One patient",
              tag="patients", response_model="PatientDetail", path_params=["patient_id"],
              m1_behaviour="fixture", available_in="M2"),
    RouteSpec(operation_id="list_patient_scans", method="GET", path="/patients/{patient_id}/scans",
              summary="Scans of one patient, real and simulated", tag="patients", response_model="ScanList",
              path_params=["patient_id"], m1_behaviour="fixture", available_in="M2"),
    RouteSpec(operation_id="get_patient_levels", method="GET", path="/patients/{patient_id}/levels",
              summary="T11-L5 ladder with raw and aggregate query_ms", tag="patients",
              response_model="LevelLadder", path_params=["patient_id"], m1_behaviour="fixture",
              available_in="M2"),
    RouteSpec(operation_id="get_patient_series", method="GET", path="/patients/{patient_id}/series",
              summary="One metric over time; points carry provenance and filled", tag="patients",
              response_model="Series", path_params=["patient_id"], query_params=["site", "metric"],
              m1_behaviour="fixture", available_in="M2"),
    RouteSpec(operation_id="upload_scan", method="POST", path="/scans", summary="Upload a CT (.nii.gz), admin",
              tag="scans", response_model="ScanUploadAccepted", status_code=202, admin=True,
              raw_body_media_type="application/gzip", query_params=["patient_id", "day", "label"],
              m1_behaviour="501", available_in="M5"),
    RouteSpec(operation_id="get_scan", method="GET", path="/scans/{scan_id}",
              summary="Scan manifest: assets, colour scales, metrics, calibration, mask sources", tag="scans",
              response_model="ScanManifest", path_params=["scan_id"], m1_behaviour="fixture",
              available_in="M2"),
    RouteSpec(operation_id="get_scan_asset", method="GET", path="/scans/{scan_id}/assets/{name}",
              summary="One asset file named in the manifest", tag="scans", response_model="bytes",
              path_params=["scan_id", "name"], m1_behaviour="501", available_in="M3"),
    RouteSpec(operation_id="start_scan_run", method="POST", path="/scans/{scan_id}/runs",
              summary="Queue segment/physics/summarize jobs, admin", tag="scans", response_model="RunAccepted",
              status_code=202, admin=True, request_model="RunRequest", path_params=["scan_id"],
              m1_behaviour="501", available_in="M5"),
    RouteSpec(operation_id="get_job", method="GET", path="/jobs/{job_id}", summary="Job status, seconds, cost",
              tag="jobs", response_model="Job", path_params=["job_id"], m1_behaviour="501",
              available_in="M5"),
    RouteSpec(operation_id="get_scan_summary", method="GET", path="/scans/{scan_id}/summary",
              summary="Care-team summary with provider badge", tag="ai", response_model="Generation",
              path_params=["scan_id"], m1_behaviour="fixture", available_in="M2"),
    RouteSpec(operation_id="regenerate_scan_summary", method="POST", path="/scans/{scan_id}/summary/regenerate",
              summary="Outage drill: rerun the chain with an override, admin", tag="ai",
              response_model="Generation", admin=True, request_model="RegenerateRequest",
              path_params=["scan_id"], m1_behaviour="501", available_in="M6"),
    RouteSpec(operation_id="get_scan_explanation", method="GET", path="/scans/{scan_id}/explanation",
              summary="Patient explanation with reading grade", tag="ai", response_model="Explanation",
              path_params=["scan_id"], m1_behaviour="501", available_in="M6"),
    RouteSpec(operation_id="post_alert_feedback", method="POST", path="/alerts/{alert_id}/feedback",
              summary="Useful / Not useful / Snooze", tag="alerts", response_model="AlertFeedbackAck",
              status_code=201, request_model="AlertFeedbackRequest", path_params=["alert_id"],
              m1_behaviour="501", available_in="M2"),
    RouteSpec(operation_id="post_copilot", method="POST", path="/copilot",
              summary="Care-Team Copilot over read-only tools", tag="ai", response_model="CopilotAnswer",
              request_model="CopilotRequest", m1_behaviour="501", available_in="M8"),
    RouteSpec(operation_id="get_tiger_info", method="GET", path="/tiger/info",
              summary="Hypertables, continuous aggregates, columnstore bytes, db size", tag="tiger",
              response_model="TigerInfo", m1_behaviour="fixture", available_in="M2"),
    RouteSpec(operation_id="get_audit_trail", method="GET", path="/audit/{scan_id}",
              summary="Hash-chained audit trail of one scan", tag="audit", response_model="AuditTrail",
              path_params=["scan_id"], m1_behaviour="fixture", available_in="M2"),
]

ROUTES_BY_ID = {r.operation_id: r for r in ROUTES}

# Query parameter types (exact names, order and types the routes declare).
QUERY_PARAM_TYPES: dict[str, dict[str, str]] = {
    "get_patient_series": {"site": "Site (required)", "metric": "SeriesMetric (required)"},
    "upload_scan": {"patient_id": "str (required)", "day": "int (required, >= 0)",
                    "label": "Optional[str] = None"},
}

ADMIN_RESPONSES: dict[int, dict[str, Any]] = {
    401: {"model": ErrorDetail, "description": "X-Admin-Key missing or wrong"},
    503: {"model": Unavailable, "description": "ADMIN_KEY unset, or the database is down"},
}


def _model(name: str) -> Any:
    return globals()[name]


def route_kwargs(operation_id: str) -> dict[str, Any]:
    """Exact keyword arguments for the FastAPI route decorator of `operation_id`."""
    spec = ROUTES_BY_ID[operation_id]
    responses: dict[int, dict[str, Any]] = {
        503: {"model": Health, "description": "database down (same body shape)"} if operation_id == "get_health"
        else {"model": Unavailable, "description": "database unavailable"},
    }
    if spec.path_params:
        responses[404] = {"model": ErrorDetail, "description": "unknown id"}
    if spec.available_in is not None and spec.m1_behaviour == "501":
        responses[501] = {"model": NotAvailable, "description": "lands in a later milestone"}
    if spec.admin:
        responses.update(ADMIN_RESPONSES)
    if spec.raw_body_media_type:
        responses[413] = {"model": ErrorDetail, "description": "over the upload size cap"}
    kwargs: dict[str, Any] = {
        "operation_id": spec.operation_id,
        "summary": spec.summary,
        "tags": [spec.tag],
        "status_code": spec.status_code,
        "responses": dict(sorted(responses.items())),
    }
    if spec.response_model == "bytes":
        from fastapi.responses import FileResponse  # local import: the models above need only pydantic
        kwargs["response_class"] = FileResponse
        kwargs["response_model"] = None
        kwargs["responses"][200] = {"description": "file bytes; media type per contracts/formats.md",
                                    "content": {"application/octet-stream": {}}}
        kwargs["responses"] = dict(sorted(kwargs["responses"].items()))
    else:
        kwargs["response_model"] = _model(spec.response_model)
    extra: dict[str, Any] = {}
    if spec.admin:
        extra["x-admin"] = True
    if spec.raw_body_media_type:
        extra["requestBody"] = {"required": True, "content": {
            spec.raw_body_media_type: {"schema": {"type": "string", "format": "binary"}}}}
    if extra:
        kwargs["openapi_extra"] = extra
    return kwargs
