# PRP-00 — Foundation

## 1. Goal

Stand up the Cadence repository skeleton so every later PRP has a running app, a validated schema, and a green CI to build against. This PRP delivers the uv project, the `cadence` package with `create_app()`, typed settings, the SQLite layer, the **complete Pydantic v2 library DSL**, the **validator** that is the single gate for seed / import / generate, `GET /api/health`, the response envelope, the pytest harness, lint/pre-commit, and GitHub Actions CI. No UI, no library content, no integrations.

Domain *values* (enum members, youth numbers, seed data) come from `docs/exercise-principles.md`. This PRP builds the *structure* that holds them and proves the gate cannot be bypassed.

## 2. Scope

**In**

- `pyproject.toml` (uv), `.python-version`, `Makefile`, `.env.example`, `ruff.toml` config in `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`.
- `cadence/__init__.py`, `main.py` (`create_app()`, lifespan), `config.py`, `db.py`.
- `cadence/schema/` — every model and enum listed in §4.
- `cadence/validateur/` — `validate_exercise`, `validate_workout`, error-code table.
- `cadence/api/health.py` + the `{"ok","data","error","meta"}` envelope helper.
- Empty-but-importable packages: `bibliotheque/`, `programme/`, `profils/`, `seance/`, `historique/`, `bilan/`, `vitalforge/`, `ia/`, `web/`, `api/`.
- `tests/conftest.py` with an isolated temp-DB app fixture, `tests/test_health.py`, `tests/test_schema.py`, `tests/test_validator.py`, `tests/test_schema_bypass.py`.

**Out (owned by later PRPs)**

| Owned by | Thing |
|---|---|
| PRP-01 | `library/*.yaml` content, `youth_rules.yaml` values, `make seed` loader body, program builder, **the `profile` / `setting` tables and `age_band()`** |
| PRP-02 | Jinja templates, HTMX, service worker, Today, **the `session` / `session_row` tables** |
| PRP-03 | Profiles and settings **services and screens** (Setup, Settings, youth rules wired end to end) |
| PRP-04 | History queries, weekly scorecard, streak |
| PRP-06 | `cadence/vitalforge/` bodies, `metrics_cache`, `sync_job` |
| PRP-08 | `cadence/ia/`, `/api/import`, `/api/generate` |
| PRP-09 | Dockerfile, compose, deploy |

Two of those rows follow build order rather than subject matter. `make seed` (PRP-01) creates the two demo profiles and a program for each, and the program builder needs `age_band()`, `bodyweight_kg` and `has_overhead_anchor` for the cap arithmetic and anchor filtering — so PRP-01 owns the `profile` and `setting` tables and PRP-03 owns the services and screens over them. Likewise PRP-02 creates a `session` on the first render of Today and writes `session_row` on every tick, so it owns both tables and PRP-04 only queries them.

`make seed` exists as a target and exits 0 with "no library yet"; PRP-01 fills it.

## 3. Data model

Only two tables are created in this PRP. Every other table in `docs/architecture.md` §3 is created by its owning PRP; `init_db()` must call `SQLModel.metadata.create_all` so later PRPs only add model modules.

| Table | Columns |
|---|---|
| `schema_version` | `id` INTEGER PK (always 1), `version` INTEGER NOT NULL, `applied_at` TEXT NOT NULL |
| `exercise` | `id` TEXT PK, `doc_json` TEXT NOT NULL, `source` TEXT NOT NULL, `created_at` TEXT NOT NULL |
| `workout` | `id` TEXT PK, `doc_json` TEXT NOT NULL, `source` TEXT NOT NULL, `target_profile_kind` TEXT NOT NULL, `created_at` TEXT NOT NULL |

`source ∈ {seed, import, generated}` (D-010: `import`/`generated` never write back to `library/`).

**`db.py` requirements**

- Engine from `settings.db_path`, `check_same_thread=False`, `SQLModel` metadata.
- On connect: `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000` (SQLAlchemy `connect` event listener, not a one-shot).
- `init_db()` — create parent dir, `create_all`, then `_ensure_schema_version(EXPECTED_SCHEMA_VERSION = 1)`: insert row 1 if absent; if `version > EXPECTED`, raise `RuntimeError` naming both numbers (a downgrade must fail loudly, not silently corrupt).
- `get_session()` FastAPI dependency yielding a `Session`.
- Immutability rule (`docs/architecture.md` §3): services build new model instances; a helper `update_model(obj, **fields) -> Model` returns a copy. No in-place attribute assignment outside it.

## 4. Schema — `cadence/schema/`

Files: `enums.py`, `equipment.py`, `exercise.py`, `workout.py`, `progression.py`, `youth.py`, `assessment.py`, `__init__.py` (re-exports all).

All models: `model_config = ConfigDict(extra="forbid", frozen=True)`. `extra="forbid"` is what makes an imported YAML with a stray key fail closed.

### 4.1 Enums — `enums.py`

**Enum member values are exactly `docs/exercise-principles.md` §11.** Do not invent members. Required enums: `AgeBand`, `Pattern`, `Region`, `LoadType`, `LoadUnit`, `Measure`, `Equipment`, `ExerciseTag`, `ProgressionType`, `RegressTrigger`, `AutoregOutcome`, `Felt`, `Readiness`, `SessionStatus`, `GoalType`, `DayType`, `AssessmentId`, `TargetTier`, `GarminCategory`.

One enum is **not** in §11's list: `YouthAllow = yes | no | conditional`, the three states of §9's per-band columns (`Y` / `N` / `Y*`). It is named here because §9 uses symbols rather than identifiers.

All are `str, Enum`.

> **`GarminCategory` has no `UNKNOWN` member.** `garminconnect` 0.3.11 does not define one (`docs/vitalforge-contract.md` §3.1) and emitting it earns a Garmin 400. "Variant unknown" is encoded as `garmin_category: None`, per D-018. Members = the 26 confirmed present in `exercises.CATEGORIES` (contract §3.1).

### 4.2 Equipment whitelist — `equipment.py`

```python
EQUIPMENT_WHITELIST: frozenset[Equipment] = frozenset(Equipment)   # bodyweight, dumbbells, kettlebells, bench
```

The enum **is** the whitelist; membership is the check. Anything not an `Equipment` member fails at parse time with code `equipment_not_whitelisted`. `has_overhead_anchor` is a profile bool, never an equipment id (principles §1.7).

### 4.3 `Exercise`

| Field | Type | Constraint |
|---|---|---|
| `id` | `str` | `pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"`, 1–64 |
| `name` | `str` | 1–100 |
| `pattern` | `Pattern` | required |
| `region` | `Region` | required |
| `load_type` | `LoadType` | required |
| `load_unit` | `LoadUnit` | required (principles §1.4 — youth caps are uncheckable without it) |
| `measure` | `Measure` | required |
| `equipment` | `list[Equipment]` | `min_length=1`, deduped |
| `tags` | `list[ExerciseTag]` | default `[]` |
| `cue` | `str` | 1–120, one line, no newline |
| `garmin_category` | `GarminCategory \| None` | default `None` |
| `garmin_exercise` | `str \| None` | default `None`, ≤64, `A-Z0-9_` |
| `regression_of` | `str \| None` | exercise id |
| `progression_of` | `str \| None` | exercise id |
| `default_progression` | `str \| None` | progression id |
| `est_seconds_per_set` | `int \| None` | 1–600 |
| `youth_ok_by_band` | `dict[AgeBand, YouthAllow]` | default `{}`; see below |

Model validator: `garmin_exercise` set with `garmin_category is None` → error (a sub-category needs its parent, contract §4.3).

**`youth_ok_by_band` is the `<10` / `10–13` / `14–17` columns of `docs/exercise-principles.md` §9**, which that section says are checked "in addition to" the load-type and tag rules of §3, stricter winning. `YouthAllow = yes | no | conditional` (§9's `Y` / `N` / `Y*`).

It lives on `Exercise` and is checked by `validate_workout`, not by a library-side helper, because the brief's hard rule requires that **neither an imported nor an AI-generated workout can bypass youth rules**. A helper the import and generate paths must remember to call is exactly that bypass. Without this field, `kb-swing` at 40 kg bodyweight passes V1 and V2 for a 14-year-old whose band allows kettlebells, while §3.6 says it is illegal.

The `adult` band is never consulted. **A youth band absent from the dict is `no`** — the allowlist fails closed, so a seed row that forgets a column denies rather than permits.

### 4.4 `WorkoutRow`

| Field | Type | Constraint |
|---|---|---|
| `exercise_id` | `str` | slug pattern |
| `sets` | `int` | 1–10 |
| `reps` | `int \| None` | 1–100 |
| `seconds` | `int \| None` | 1–3600 |
| `meters` | `float \| None` | 1–1000 |
| `steps` | `int \| None` | 1–2000 |
| `load_kg` | `float \| None` | 0–500 |
| `load_unit` | `LoadUnit` | required |
| `rest_s` | `int` | 0–3600 |
| `rpe_target` | `int \| None` | 1–10 |
| `amrap` | `bool` | default `False` |
| `is_prelude` | `bool` | default `False` |
| `is_challenge` | `bool` | default `False` |
| `cue_override` | `str \| None` | ≤120 |
| `progression_id` | `str \| None` | — |

Model validator: **exactly one** of `reps`/`seconds`/`meters`/`steps` is non-null (`measure_conflict`). Whether it matches the referenced exercise's `measure` is checked in `validate_workout` (the row alone cannot know).

### 4.5 `Workout`

`id` (slug) · `name` (1–100) · `day_type: DayType` · `target_profile_kind: Literal["adult","youth","both"]` · `variant: str | None` · `rows: list[WorkoutRow]` (1–60) · `estimated_minutes: int` (1–120) · `notes: str | None` (≤500).

### 4.6 `Progression`

Field-for-field as `docs/exercise-principles.md` §5.1. One shape serves two lives: it is the default stored on a seed exercise **and** the per-user mutable state copied onto each workout row at generation time. PRP-01 seeds the defaults; PRP-07 reads and rewrites the same field names out of `planned_session.rows_json`. Do not fork it into two models.

| Field | Type | Constraint |
|---|---|---|
| `id` | `str` | slug; **an addition** — architecture §1 gives progressions their own `library/progressions/*.yaml`, which needs a key. Principles §5.1 has no `id` because it only ever shows the embedded copy |
| `type` | `ProgressionType` | required |
| `rep_min` | `int \| None` | 1–100 |
| `rep_max` | `int \| None` | 1–100 |
| `load_step_kg` | `float \| None` | 0–20 |
| `load_step_pct` | `float \| None` | 0–1; used when `load_step_kg` is `None` |
| `time_step_s` | `int \| None` | 0–120 |
| `distance_step_m` | `int \| None` | 0–200 |
| `regress_on` | `list[RegressTrigger]` | subset of `{hard, missed, low_readiness}` |
| `regress_step` | `float` | 0–1, **default `0.10`** — fraction of current load |
| `regress_reps` | `int` | 0–20, **default `2`** — reps removed when load cannot drop |
| `deload_pct` | `float` | 0–1, **default `0.60`** |
| `allow_load_progression` | `bool` | default `True`; **`False` for every youth band** (§5.6) |
| `cap_load_kg` | `float \| None` | injected by the validator from principles §3.3; the injection returns a copy via `model_copy(update=...)`, never an in-place set |

> **`deload_pct` is `0.60`, not `0.40`.** Set arithmetic hides the difference — `max(2, floor(sets × pct))` gives 2 for three and four sets either way — but **carry distance scales by it directly** (§5.5: "Carries: distance × `deload_pct`"), so a wrong default silently shortens every deload-week carry by a third. Test 30 pins the value.

Names are principles' names: `rep_min`/`rep_max` (not `rep_low`/`rep_high`) and `regress_on` (not `regress_triggers`). There is no `bump_after_n_easy` — bump timing is decided by §5.4's ordered decision rules, not by a counter on the progression.

### 4.7 `YouthRuleSet`

Field-for-field as `docs/exercise-principles.md` §3.9. `max_load_pct_bw_*` are `float | None` in `0..1`. `banned_tags: list[ExerciseTag]`, `banned_goal_types: list[GoalType]`, `assessment_caps: dict[AssessmentId, int]`.

`YouthRules = RootModel[dict[AgeBand, YouthRuleSet]]` — the parsed shape of `library/youth_rules.yaml`. PRP-01 supplies the values; this PRP ships a `u10` fixture only.

### 4.8 `AssessmentSpec`

`id: AssessmentId` · `name` · `unit: str` · `measure: Measure` · `self_rated: bool` · `higher_is_better: bool` · `protocol: str` (≤500) · `retest_days: int` (default 28) · `youth_cap: int | None`.

## 5. Validator — `cadence/validateur/`

Files: `result.py`, `codes.py`, `exercise_rules.py`, `youth_rules.py`, `workout.py`, `__init__.py`.

```python
class ValidationError(BaseModel):        # frozen
    path: str                            # "rows[3].load_kg" / "equipment[1]" / "$"
    code: str                            # from codes.py
    message: str                         # human, no jargon, names the offending value
    severity: Literal["error", "warn"] = "error"
    remedy: str | None = None            # principles §3.10 remedy column

class ValidationResult(BaseModel):       # frozen
    ok: bool                             # == no severity == "error"
    errors: list[ValidationError]

def validate_exercise(doc: dict | Exercise) -> ValidationResult: ...

def validate_workout(
    doc: dict | Workout, *,
    profile_kind: Literal["adult", "youth"],
    age_band: AgeBand,
    equipment: list[Equipment],
    bodyweight_kg: float | None = None,
    has_overhead_anchor: bool = False,
    exercises: Mapping[str, Exercise] | None = None,
    youth_rules: Mapping[AgeBand, YouthRuleSet] | None = None,
) -> ValidationResult: ...
```

> **Signature divergence, resolved.** `docs/exercise-principles.md` §3.10 proposes `validate_workout(workout, profile) -> list[Violation]`. `docs/architecture.md` §5 is the fixed backbone and specifies the four arguments above; it wins. The principles' content is preserved: every `Violation` field maps onto `ValidationError` (`rule → code`, `row_index → path`, plus `severity` and `remedy` verbatim). Callers that want the principles' list use `result.errors`.

Never raises on bad input. A `ValidationError` from Pydantic is caught and flattened into one `ValidationError` per Pydantic error, `code="schema"`, `path` = the joined `loc`.

> **Callers gate on `result.ok`, never on `len(result.errors)`.** Exactly two codes are `severity="warn"` — `youth_rep_ceiling` (V4) and `youth_rpe_exceeded` (V12) — so a perfectly acceptable document can carry entries and still be `ok is True`. PRP-01's loader, `/api/import` and `/api/generate` must all branch on `.ok` — treating any non-empty list as failure would reject valid seed workouts. Test 21 pins this.
>
> **`ValidationResult` is a Pydantic model and is therefore always truthy.** `if validate_workout(...): raise` rejects *every* document, valid ones included. PRP-01 currently says "treat any non-empty return as failure", which assumes a list return; the correct call is `if not result.ok: raise`.

### 5.1 Error codes — `codes.py`

| Code | Source | Severity | Trigger |
|---|---|---|---|
| `schema` | Pydantic | error | any parse/constraint failure, incl. `extra="forbid"` |
| `unknown_exercise` | — | error | `row.exercise_id` not in `exercises` |
| `equipment_not_whitelisted` | — | error | an equipment value outside the `Equipment` enum |
| `equipment_not_available` | — | error | exercise needs equipment the profile has not enabled |
| `measure_mismatch` | — | error | row's populated measure field ≠ exercise `measure` |
| `measure_conflict` | schema | error | zero or ≥2 of reps/seconds/meters/steps set |
| `youth_load_type_not_allowed` | V1 | error | `load_type ∉ allowed_load_types` |
| `youth_load_exceeded` | V2 | error | `load_kg > effective_cap` for the row's `load_unit` |
| `youth_rep_floor` | V3 | error | loaded row `reps < rep_min_loaded` |
| `youth_rep_ceiling` | V4 | **warn** | `reps > rep_max_*` |
| `youth_rest_floor` | V5 | error | loaded row `rest_s < min_rest_s_loaded` |
| `youth_exercise_count` | V6 | error | non-prelude rows > `max_exercises_per_session` |
| `youth_set_count` | V7 | error | `sets > max_sets_per_exercise` |
| `youth_banned_tag` | V8 | error | row's exercise carries a tag in `banned_tags` |
| `youth_session_length` | V9 | error | `estimated_minutes > max_session_minutes` |
| `youth_amrap_not_allowed` | V10 | error | `row.amrap` and not `allow_amrap` |
| `youth_banned_goal` | V11 | error | profile goal type in `banned_goal_types` |
| `youth_rpe_exceeded` | V12 | **warn** | `rpe_target > rpe_cap` |
| `requires_anchor_unavailable` | V13 | error | row tagged `requires_anchor` and `has_overhead_anchor is False` |
| `youth_exercise_not_allowed` | **V14** | error | the exercise's `youth_ok_by_band` denies this band (§5.3) |

**V14 is an addition to principles §3.10's V1–V13 table**, sourced from §9's per-band columns. §9 states those columns are checked in addition to §3's rules, so the check exists; §3.10 simply predates it. Remedy: substitute via `regression_of`.

Order of evaluation: schema → exercise resolution → equipment → measure → youth V1–V14. All errors are collected; the validator never short-circuits after the first.

### 5.2 Effective load cap (principles §3.6/§3.4)

```
cap_for(load_unit, rules, bodyweight_kg):
    abs_cap = rules.max_load_kg_per_hand      if load_unit == per_hand
              rules.max_load_kg_per_implement if load_unit == per_implement
              None                            otherwise      # total / bodyweight are not capped directly
    pct     = rules.max_load_pct_bw_per_hand / _per_implement
    if pct is None or bodyweight_kg is None:  return abs_cap
    return min(abs_cap, pct * bodyweight_kg)
```

`bodyweight_kg` defaults to `None` (principles §3.8) — **fall back to the absolute kg cap, never raise, never treat a missing bodyweight as unlimited.**

### 5.3 The per-band allowlist (V14)

For a youth profile, resolve `exercise.youth_ok_by_band.get(age_band, "no")`:

| Value | Result |
|---|---|
| `yes` | permitted; V1–V13 still apply, stricter wins |
| `no`, or the band is absent | `youth_exercise_not_allowed`, remedy "substitute via `regression_of`" |
| `conditional` | permitted **only if `bodyweight_kg` is not `None`**; V1–V13 then decide, with V2 evaluated against the percentage-of-bodyweight cap |

> **`conditional` is the one place a missing `bodyweight_kg` denies rather than falls back.** §5.2's fallback to the absolute kg cap would let a 16 kg kettlebell through for an unweighed 14-year-old, and principles §3.6 says the opposite: "If the bodyweight test fails **or bodyweight is unknown**, the engine substitutes." So V14 rejects a `conditional` row when the bodyweight is unknown, before §5.2's fallback is ever reached. Test 31 pins this interaction, which is the subtlest rule in the validator.

`conditional` needs no per-exercise condition expression: §3.6's real condition (16 kg, two-handed, `0.35 × bodyweight_kg >= 16`) is V2's percentage cap plus V1's `load_unit` check. `conditional` only gates *whether those run against a known bodyweight*.

## 6. API surface

Only one route ships here.

`GET /api/health` → 200

```json
{"ok": true,
 "data": {"status": "ok", "db": "ok", "vitalforge": {"configured": false, "mode": "live"},
          "version": "0.1.0"},
 "error": null, "meta": {}}
```

- `db` is `"ok"` after a real `SELECT 1`, else `"error"` with the route still 200 and `ok: false`.
- **`vitalforge` is a static config read only — `configured = bool(settings.vitalforge_token)`.** It must never make a network call: compose polls this every 30 s (PRP-09) and `docs/architecture.md` §5 requires `configured: true/false`. PRP-06 is forbidden from upgrading it to a live probe.
- Secrets never appear in the body. `version` from `importlib.metadata.version("cadence")`.

Envelope helper — `cadence/api/envelope.py`:

```python
def ok(data, **meta) -> dict
def err(message: str, *, data=None, **meta) -> dict
```

Every `/api/*` route returns this shape (`docs/architecture.md` §4).

## 7. Implementation notes

### 7.1 Project

`.python-version` = `3.12` (D-006). `pyproject.toml`: `requires-python = ">=3.12,<3.13"`, `name = "cadence"`, AGPL-3.0, hatchling backend, packages `["cadence"]`.

| Runtime dep | Pin |
|---|---|
| `fastapi` | `>=0.115,<0.116` |
| `uvicorn[standard]` | `>=0.32,<0.33` |
| `sqlmodel` | `>=0.0.22,<0.1` |
| `pydantic` | `>=2.9,<3` |
| `pydantic-settings` | `>=2.6,<3` |
| `jinja2` | `>=3.1,<4` |
| `python-multipart` | `>=0.0.12,<0.1` |
| `pyyaml` | `>=6.0,<7` |
| `httpx` | `>=0.27,<0.29` |

Dev: `pytest>=8.3,<9`, `pytest-asyncio>=0.24,<0.25`, `pytest-cov>=5,<7`, `ruff>=0.7,<0.10`, `respx>=0.21,<0.23` (PRP-06 uses it; pin it here so both PRPs agree), `playwright>=1.48,<2`, `pytest-playwright>=0.5,<0.7`, `pre-commit>=4,<5`.

`python-dotenv` is **not** a direct dep — `pydantic-settings` reads `.env` itself.

pytest config: `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `addopts = "-q --strict-markers --ignore=tests/e2e"` (e2e runs only via `make e2e`, mirroring VitalForge's split).

### 7.2 `config.py`

`class Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`. Field names are lowercase; env names are the uppercase forms below.

| Env var | Type | Default |
|---|---|---|
| `CADENCE_DB_PATH` | `Path` | `data/cadence.db` |
| `CADENCE_HOST` | `str` | `0.0.0.0` |
| `CADENCE_PORT` | `int` | `8000` |
| `CADENCE_VITALFORGE_MODE` | `Literal["live","mock"]` | `live` |
| `VITALFORGE_WEIGHT_URL` | `AnyHttpUrl` | `https://weight.grepon.cc` |
| `VITALFORGE_DASHBOARD_URL` | `AnyHttpUrl` | `https://health.grepon.cc` |
| `VITALFORGE_TOKEN` | `SecretStr` | `""` |
| `VITALFORGE_PERSON_ME` | `str` | `""` (D-017) |
| `VITALFORGE_PERSON_SON` | `str` | `""` (D-017) |
| `OMNIROUTE_URL` | `AnyHttpUrl` | `https://llm.grepon.cc/v1` |
| `OMNIROUTE_KEY` | `SecretStr` | `""` |
| `OMNIROUTE_MODEL_GENERATE` | `str` | `code-plan` |
| `OMNIROUTE_MODEL_SUMMARY` | `str` | `cheap-think` |
| `TZ` | `str` | `UTC` |

`CADENCE_VITALFORGE_MODE` is defined here and **consumed by PRP-09's smoke script and PRP-06's client**; it belongs to config, not to either consumer.

Gotchas the implementer must build in:

- Secrets are `SecretStr` so a stray `repr(settings)` prints `**********`. Add `__repr__`/`__str__` tests.
- `get_settings()` is `@lru_cache`d; tests override via `app.dependency_overrides` or by clearing the cache, never by mutating the instance (immutability rule).
- Cadence's `TZ` is **display-only**. It is *not* the timezone used for the Garmin wall-clock conversion — that happens server-side in VitalForge using VitalForge's own `TZ` (PRP-05 §Garmin path). Cadence always sends UTC with an explicit offset. Do not "fix" the offset on this side.

### 7.3 `main.py`

```python
def create_app(settings: Settings | None = None) -> FastAPI
```

Lifespan calls `init_db()` on startup. Router registration is a list so later PRPs append one line. `app = create_app()` at module scope for uvicorn. No middleware and no auth (D-009) — with one declared exception: PRP-02 adds `GZipMiddleware`, because the 60 KB page budget is unreachable with vendored HTMX served uncompressed. That is a logged deviation with its own DECISIONS entry, not licence for further middleware here.

### 7.4 Makefile

| Target | Body |
|---|---|
| `dev` | `uv run uvicorn cadence.main:app --reload --host $(HOST) --port $(PORT)` |
| `test` | `uv run pytest --cov=cadence --cov-report=term-missing --cov-fail-under=80` |
| `lint` | `uv run ruff check . && uv run ruff format --check .` |
| `fmt` | `uv run ruff format . && uv run ruff check --fix .` |
| `seed` | `uv run python -m cadence.bibliotheque.seed` (prints "no library yet" and exits 0 until PRP-01) |
| `e2e` | `uv run pytest tests/e2e --browser chromium` |
| `deploy` | placeholder echoing "see docs/deploy.md (PRP-09)" |

### 7.5 Tooling

- ruff: `line-length = 120`, `select = ["E","F","W","I","UP","B","SIM"]`, `target-version = "py312"` (matches VitalForge house style, contract §1.1).
- `.pre-commit-config.yaml`: `ruff` + `ruff-format` (astral-sh/ruff-pre-commit), `end-of-file-fixer`, `trailing-whitespace`, `check-yaml` (pre-commit-hooks v5.0.0).
- `.github/workflows/ci.yml`, `push` + `pull_request`, two jobs:
  - `test`: `astral-sh/setup-uv@v3` → `uv sync --all-extras --dev` → `uv run ruff check .` → `uv run ruff format --check .` → `uv run pytest --cov=cadence --cov-fail-under=80`.
  - `e2e`: needs `test`, `uv run playwright install --with-deps chromium` → `uv run pytest tests/e2e`. Allowed to be a no-op skip until PRP-02 adds tests.
- `.gitignore` already exists — confirm it ignores `.env`, `data/`, `.venv/`; add if missing. **Never commit `.env`.**

### 7.6 `tests/conftest.py`

```python
@pytest.fixture
def db_path(tmp_path) -> Path                 # tmp_path / "cadence.db"

@pytest.fixture
def settings(db_path, monkeypatch) -> Settings # blank tokens, mode="mock"

@pytest.fixture
def app(settings) -> FastAPI                   # create_app(settings); init_db against db_path

@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

Every test gets a fresh file DB under `tmp_path` — never `:memory:` (WAL and the connect-pragma listener behave differently in memory, and PRP-06 needs concurrent connections). `get_settings.cache_clear()` in the `settings` fixture teardown.

## 8. Acceptance tests

Numbered; each is a real test name.

1. `tests/test_health.py::test_health_ok` — `GET /api/health` → 200, `ok is True`, `data.db == "ok"`, `data.status == "ok"`.
2. `::test_health_reports_vitalforge_unconfigured` — with a blank token, `data.vitalforge == {"configured": False, "mode": "mock"}`.
3. `::test_health_never_leaks_secrets` — set `VITALFORGE_TOKEN=sekret`, assert `"sekret" not in response.text` and `data.vitalforge["configured"] is True`.
4. `::test_health_makes_no_network_call` — patch `httpx.AsyncClient.request` to raise; the route still returns 200. **Negative.**
5. `tests/test_config.py::test_settings_defaults` — the eight URL/model defaults in §7.2 land without a `.env`.
6. `::test_secrets_are_masked_in_repr` — `"sekret" not in repr(settings)` and `not in str(settings)`. **Negative.**
7. `tests/test_db.py::test_init_db_is_idempotent` — two `init_db()` calls, one `schema_version` row, `version == 1`.
8. `::test_wal_mode_enabled` — `PRAGMA journal_mode` returns `wal`.
9. `::test_future_schema_version_raises` — write `version = 99`, `init_db()` raises `RuntimeError` naming 99 and 1. **Negative.**
10. `tests/test_schema.py::test_exercise_roundtrip` — a valid `Exercise` dict parses and `model_dump(mode="json")` round-trips.
11. `::test_exercise_rejects_extra_key` — an unknown key raises `ValidationError`. **Negative.**
12. `::test_exercise_rejects_bad_slug` — `"Push Up"` rejected. **Negative.**
13. `::test_garmin_category_has_no_unknown_member` — `"UNKNOWN" not in {c.value for c in GarminCategory}`. **Negative.**
14. `::test_garmin_exercise_requires_category` — `garmin_exercise` set with `garmin_category=None` rejected. **Negative.**
15. `::test_row_requires_exactly_one_measure` — parametrised over zero, two, and three of reps/seconds/meters/steps; all rejected. **Negative.**
16. `tests/test_validator.py::test_valid_adult_workout_passes` — `ok is True`, `errors == []`.
17. `::test_unknown_exercise_id_flagged` — code `unknown_exercise`, `path == "rows[0].exercise_id"`. **Negative.**
18. `::test_measure_mismatch_flagged` — a `seconds` row against a `measure: reps` exercise → `measure_mismatch`. **Negative.**
19. `::test_equipment_not_available_flagged` — a dumbbell exercise with `equipment=[bodyweight]` → `equipment_not_available`. **Negative.**
20. `::test_all_errors_collected_not_short_circuited` — a doc with three distinct faults returns three errors.
21. `::test_warn_only_result_is_ok` — a doc whose sole finding is `youth_rep_ceiling` (warn) has `ok is True` and one error entry.
22. `::test_validator_never_raises` — parametrised over `None`, `{}`, `[]`, a deeply nested dict and a 1 MB string; each returns a `ValidationResult` with `ok is False`. **Negative.**
23. `::test_missing_bodyweight_falls_back_to_absolute_cap` — `bodyweight_kg=None` with a pct cap set: no crash, absolute cap applied. **Negative.**
24. `tests/test_schema_bypass.py::test_off_whitelist_equipment_is_rejected` — parametrised over `"barbell"`, `"machine"`, `"cable"`, `"smith-machine"`, `"resistance-band"`: each fails with `equipment_not_whitelisted` or `schema`. **This case is fixed and must never be relaxed.** **Negative.**
25. `::test_youth_rule_violation_is_rejected` — **parametric**: loads `library/youth_rules.yaml` (falls back to the shipped `u10` fixture until PRP-01), and for each rule field builds a workout that exceeds it by one step; asserts the expected code from §5.1. PRP-01 fills the table by filling the YAML, not by editing this test. **Negative.**
26. `::test_youth_banned_tag_is_rejected` — a row whose exercise carries `max_effort` under `u10` → `youth_banned_tag`. **Negative.**
27. `::test_adult_profile_is_not_youth_checked` — the same over-cap workout passes for `profile_kind="adult"`, proving the youth gate is band-scoped and not global.
28. `tests/test_envelope.py::test_envelope_shape` — `ok()` and `err()` both produce exactly the four keys `ok/data/error/meta`.
29. `tests/test_schema.py::test_progression_field_names_match_principles` — `set(Progression.model_fields) == {id, type, rep_min, rep_max, load_step_kg, load_step_pct, time_step_s, distance_step_m, regress_on, regress_step, regress_reps, deload_pct, allow_load_progression, cap_load_kg}`. Catches a rename back to `rep_low`/`regress_triggers` and any silently dropped field. **Negative.**
30. `::test_progression_defaults` — `deload_pct == 0.60`, `regress_step == 0.10`, `regress_reps == 2`, `allow_load_progression is True`. **`deload_pct` is the one that scales carry distance directly.**
31. `tests/test_schema_bypass.py::test_conditional_denied_when_bodyweight_unknown` — a `conditional` exercise for `age_14_17` with `bodyweight_kg=None` → `youth_exercise_not_allowed`. **The §5.2 fallback must not rescue it. Negative.**
32. `::test_conditional_allowed_when_bodyweight_known` — the same row with `bodyweight_kg=50.0` passes V14 and is then decided by V1/V2.
33. `::test_band_absent_from_allowlist_denies` — an exercise whose `youth_ok_by_band` omits the profile's band → `youth_exercise_not_allowed`. **Fails closed. Negative.**
34. `::test_allowlist_denies_even_when_load_rules_pass` — the `kb-swing` case from principles §3.6: `age_14_17`, bodyweight 40 kg, a load the band's absolute cap permits, `youth_ok_by_band[age_14_17] == "conditional"` → still rejected. **This is the test that proves the allowlist is not redundant with V1/V2. Negative.**
35. `::test_adult_ignores_allowlist` — an exercise marked `no` for every youth band passes for `profile_kind="adult"`.

## 9. Devil's-advocate risks

1. **The validator is bypassable by writing to the DB directly.** → `bibliotheque` (PRP-01) must be the only writer of `exercise`/`workout`, and it calls the validator. This PRP ships no other write path, and test 24 is the standing guard.
2. **`extra="forbid"` off by default on one model silently opens import.** → Assert it structurally: a test iterates every model in `cadence.schema.__all__` and asserts `model_config["extra"] == "forbid"`.
3. **Enum drift against `exercise-principles.md`.** Principles is being written in parallel. → Values live in `enums.py` only; no string literals of enum members anywhere else. A test asserts every `GarminCategory` member is uppercase `A-Z_` so a typo cannot pass as a category.
4. **Hardcoding youth numbers here freezes them before principles lands.** → Test 25 is data-driven off the YAML; only the `u10` fixture ships in this PRP, and only equipment (test 24) is asserted with fixed values.
5. **Coverage gate green with a validator nobody calls.** → Tests 16–27 exercise the public `validate_workout` through its real signature, not internal helpers.
6. **`SecretStr` leaks through `model_dump()`.** `model_dump()` on a `SecretStr` yields the object, but `model_dump(mode="json")` yields `"**********"`. → Never dump settings into a response; test 3 guards `/api/health`.
7. **WAL leaves `-wal`/`-shm` files that break a naive backup.** → Note it in `db.py`'s docstring; PRP-09 uses `sqlite3 .backup`, not `cp`.
8. **The 80 % gate blocks PRP-01 the moment empty packages are added.** → Empty domain packages contain only a docstring and no code, so they add no uncovered lines. Do not add placeholder functions.
9. **`asyncio_mode="auto"` plus a sync test client leads to silently skipped assertions.** → The `client` fixture is async and every route test is `async def`.
10. **A future SQLite downgrade corrupts data silently.** → Test 9 makes the version check a hard failure.
11. **The per-band allowlist drifts out of the validator** into a library helper that `/api/import` and `/api/generate` must remember to call. The brief's hard rule is that neither path can bypass youth rules, so a check reachable only by convention is a bypass. → `youth_ok_by_band` is a field on `Exercise` and V14 runs inside `validate_workout`; tests 31–34.
12. **`conditional` is read as "probably fine".** An unweighed 14-year-old then gets a 16 kg kettlebell, because §5.2's absolute-cap fallback quietly permits it. → §5.3 makes unknown bodyweight a denial for `conditional` only, and test 31 pins the interaction.

## 10. Done when

- [ ] `uv sync` succeeds on Python 3.12; `.python-version` reads `3.12`.
- [ ] **`uv.lock` is committed.** PRP-09's Dockerfile runs `uv sync --frozen`, which fails outright without it.
- [ ] `make dev` boots and `curl localhost:8000/api/health` returns the §6 envelope.
- [ ] `make lint` clean; `make fmt` is a no-op on a fresh checkout.
- [ ] `make test` green with coverage ≥ 80 %.
- [ ] `make seed` exits 0.
- [ ] `pre-commit run --all-files` clean.
- [ ] CI green on both jobs.
- [ ] `.env.example` committed with the two VitalForge URLs, the OmniRoute URL, both model names, and **blank** token values; `.env` is git-ignored and absent from the diff.
- [ ] Every model in `cadence/schema/__all__` is frozen and `extra="forbid"`.
- [ ] All 35 acceptance tests present and passing.
- [ ] `grep -ri "sk-\|Bearer [A-Za-z0-9]" --include="*.py" --include="*.md" .` returns nothing.
