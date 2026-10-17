# C4: the AI provider interface

Code: `api/app/ai/` (S5). The API models (`Facts`, `Generation`, `Explanation`, `Attempt`, `CopilotAnswer`) are in `api/app/schemas.py` (S0). Wording lists live in `contracts/labels.json` under `ai` and `copilot`; the code reads them, it never copies them.

**Rule:** AI never computes a stiffness, strain or risk number. It words numbers our code computed, and the checker proves every number it used.

## Provider (`api/app/ai/providers/base.py`)
```python
class Result(NamedTuple):
    text: str | None            # raw text (JSON string when schema is given)
    json: dict | None           # parsed JSON when schema is given
    tool_calls: list[dict]      # [{"name": str, "args": dict}]
    finish_reason: str | None   # "stop" | "length" | "tool_calls" | provider value
    ms: float
    tokens_in: int | None
    tokens_out: int | None

class Provider(Protocol):
    name: Literal["gemini", "groq", "vultr", "local", "template"]
    model: str | None
    def status(self) -> dict: ...   # {"name", "model", "configured": bool, "key": "<shape only>"}; never the key
    async def generate(self, messages: list[dict], schema: dict | None = None, tools: list[dict] | None = None,
                       image: bytes | None = None, timeout: float = 10.0) -> Result: ...
```
- Raises `ProviderUnavailable` (no key, cooldown, offline) or `ProviderError(status, reason)`; never returns partial text.
- Adapters:
  - `gemini.py`: `google-genai` 2.31.0, `client.aio.models.generate_content`, `GenerateContentConfig(response_mime_type="application/json", response_json_schema=...)`, thinking level "low" (`minimal` errors on 3.8 Flash), automatic function calling **off**. The documented `response_format` config is rejected by 2.31.0 (ai.md §4).
  - `openai_compat.py`: one httpx adapter for Groq (`https://api.groq.com/openai/v1`, `response_format.json_schema.strict=true`), Vultr (the moved `providers/vultr.py` transport) and local `mlx_lm.server`.
  - `template`: always available, 0 ms, correct by construction.

## Chain (`api/app/ai/chain.py`)
```python
class Chain:
    async def run(self, task: Literal["care_team_summary", "patient_explanation"], facts: Facts,
                  override: list[str] | None = None, bypass_cache: bool = False) -> Generation: ...
def check(output: GeneratedText, facts: Facts) -> Verdict      # Verdict{ok, reason, numbers_matched, numbers_total}
def template(task, facts: Facts) -> GeneratedText              # labels.json ai.templates; passes check() for any facts
```
- **Order** from `AI_CHAIN` (comma list of `gemini,groq,vultr,local,template`). An unknown name **fails at startup**. `template` is appended if missing. **M1–M5: `AI_CHAIN=template`** (Galit's gate).
- `AI_OFFLINE=1` → the chain is `local,template`.
- Per provider: a timeout (defaults S5 may tune: Gemini 10 s, Groq 8 s, local 15 s; Vultr keeps `VULTR_TIMEOUT_S`, default 6 s); one retry on 429/5xx; after a failure a **60 s cooldown** (outcome `cooldown`, the provider is skipped). A Gemini 429 retries once on `gemini-3.5-flash-lite` before leaving Gemini.
- **Cache** keyed on `(task, prompt_version, facts_sha256)`; in memory, backed by `ai_generation`. `bypass_cache=True` skips the read, never the write.
- `override` (the outage drill) replaces the order for this call, sets `chain_override=true`, and is always logged.
- Every attempt is recorded: `Attempt{provider, model, ms, outcome, reason}`, outcome ∈ `accepted | rejected | error | unavailable | cooldown | skipped`.
- The chain returns a `Generation` (schemas.py) with `badge` = labels.json `ai.badge` (`"{provider} · {model} · {n}/{m} numbers match computed values"`), or `ai.badge_template` for the template.

## Facts (`api/app/ai/facts.py`)
- `Facts` (schemas.py, `facts_version "1"`) is the only model input: numbers our code computed, rounded to display precision (2 decimals), plus enums. **No names, DICOM tags, dates or free text.** Derived values (`interval_days`, `lesion_volume_change_cm3`, `lesion_pct_of_vertebra_b`) are computed here, because a model given two days wrote "over four months" for 94 days (ai.md §14).
- `facts_sha256` = sha256 of `json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
- Golden input: `contracts/fixtures/ai/facts.myel018-L1-d273.json`.

## Output and the checker (`api/app/ai/contract.py`)
- Output schema `{headline: str, detail: str, numbers_used: [number]}`, sent as `response_json_schema` (Gemini) or strict `json_schema` (Groq/Vultr/local), and validated again locally.
- `check()` rejects, in this order:
  1. `finish_reason == "length"`, or headline + detail over `ai.max_chars` (600);
  2. a number not in the facts. Numbers are tokens matching `(?<![A-Za-z0-9.])[-−]?\d+(?:\.\d+)?`, so `L1`, `T11`, `cm3` and `M5` are not numbers, while a number written against a unit (`15mm`, `2x`) still counts; values compare as floats against every numeric fact;
  3. a spelled-out quantity (`ai.number_words`, whole words, case-insensitive);
  4. blocked wording (`ai.blocked_output_patterns`, case-insensitive regex search). It still blocks "risk" and "predict" (also in the moved Vultr filter), allows "fall"/"fell", and handles `cm³`/`cm3`.
- `numbers_total` = distinct numeric values in headline + detail; `numbers_matched` = those found in the facts. The footer is added by code after the check (`ai.footer`) and is not checked.
- M2 filter tests: "rigidity fell 12%" passes (12 in facts); "Fracture Risk Index rose" fails; "indicating a positive response to therapy" fails; "42 cm³" passes when 42 is in the facts.

## Patient explanation
- Same facts, same checker, `task="patient_explanation"`, plus `reading_grade` computed by code, target ≤ `ai.reading_grade.target_max` (8):
  - **`flesch_kincaid_grade/1`** = 0.39 × (words / sentences) + 11.8 × (syllables / words) − 15.59, rounded to 0.1.
  - Sentences: split on `[.!?]+` followed by whitespace or end. Words: `[A-Za-z0-9]+(?:[.'’][A-Za-z0-9]+)*`, **excluding tokens that contain a digit**. Syllables per word: vowel groups `[aeiouy]+` in the lowercased word, minus 1 for a final `e` not preceded by `l` when the count is above 1, minimum 1.
  - The fixture's template text grades 2.0 under this rule. Prucker 2025's benchmark was 13.69 → 8.89 (ai.md §8).
- `review_note` = `ai.patient_review_note`.

## Copilot (M8; `api/app/ai/copilot.py`, `tools.py`)
- `POST /api/v1/copilot` with `{question, patient_id}`; `patient_id` comes from the UI session and is bound by the server; **the model never chooses it**.
- Tools (fixed parameterised SQL, enum-validated args): `list_scans()`, `get_metric_series(site: Site, metric: SeriesMetric)`, `compare_scans(scan_a, scan_b)` (returns every level, so "and L2?" works), `get_monthly_trend(site, metric)` (the monthly continuous aggregate with gapfill/locf), `define_term(term)` with `term` ∈ `copilot.terms`.
- Our own loop: at most `copilot.max_rounds` (3) rounds and `copilot.max_output_tokens` (400). Every number must appear in this turn's tool results; citing a simulated or filled point requires the word "simulated".
- Out of scope (surgery, prognosis, diagnosis) → `copilot.refusal`, `refused=true`, no tools.
- Fallback with every provider down: the 5 `copilot.suggested_questions` map to the same tools plus `copilot.template_answers`.

## Environment (names are fixed; values only via `/env-key`)
| Name | Use |
|---|---|
| `AI_CHAIN` | order, e.g. `gemini,groq,vultr,local,template`; `template` until M6 |
| `AI_OFFLINE` | `1` → `local,template` |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | default `gemini-3.8-flash`. **Do not also set `GOOGLE_API_KEY`**: the SDK prefers it |
| `GROQ_API_KEY`, `GROQ_MODEL` | default `openai/gpt-oss-120b` |
| `VULTR_API_KEY` (or `VULTR_INFERENCE_API_KEY`), `VULTR_BASE_URL`, `VULTR_MODEL`, `VULTR_TIMEOUT_S`, `VULTR_MAX_TOKENS`, `VULTR_REASONING_EFFORT` | the moved StillHere client; `VULTR_MODEL` default `qwen3.8-flash-next` |
| `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL` | `mlx_lm.server` serving Qwen3-4B-Instruct-2507 (4-bit MLX); the base URL ai.md used is `http://127.0.0.1:8089/v1`. No default model id: read it from `GET {base}/models` |

- Status reports key **shape only** (configured yes/no, length, prefix, masked suffix), like `VultrConfig.status()`.

## Logging (`api/app/ai/provenance.py`)
- One `ai_generation` row per `Chain.run` / copilot answer (C3): provider, model, prompt_version, facts_sha256, output_sha256, verdict, attempts, ms, tokens, cost, `chain_override`, `cache_hit`, linked into the audit chain of the scan (`action: "summarize"` or `"regenerate"`).
- Cost per call: tokens × list price (jobs.md price table).

## Tests that pin this contract (M1, S5)
- With every stub in place, the chain returns `source:"template"` and lists every other provider as `unavailable`.
- An unknown `AI_CHAIN` entry fails at startup.
- `template(facts fixture)` passes `check()` and contains no blocked wording.
- The moved `providers/vultr.py` imports cleanly and reports `unavailable` without a key (`sample_card()` removed: it imports `care`).
- `api/app/ai/**` is never imported by `pipeline/` (physics never imports AI).
