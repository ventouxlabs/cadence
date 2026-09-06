# VitalForge integration contract for Cadence

Phase 0 discovery. Source of truth: the VitalForge working tree at
`/var/home/user/Documents/vibe-code/vitalforge` (read-only; nothing was modified).
All paths below are relative to the VitalForge repo root. `docs/CODEMAPS/*` was
**not** used as evidence — every claim here was read out of code or tests.

`garminconnect==0.3.11` was installed into a throwaway venv at
`…/scratchpad/gc-venv` and its installed source read directly.

---

## 1. Verified facts

### 1.1 Services and layout

| Fact | Evidence |
|---|---|
| Two FastAPI apps share one SQLite file. Weight on 8085, dashboard on 8086. | `docker-compose.yml:6-9,18-21`; `vitalforge-weight/Dockerfile:19` |
| The DB is a shared Docker volume `vitalforge-data` mounted at `/app/data` in both. `DB_PATH` defaults to `/app/data/fitness.db`. | `docker-compose.yml:8-9,20-21`; `shared/database.py:10` |
| Both services call `init_db()` **concurrently** at startup with no ordering. Every schema helper is written to be race-safe under that. | `vitalforge-weight/app.py:50`; `vitalforge-dashboard/app.py:107`; `shared/database.py:63-81` |
| Python 3.12+, ruff line-length 120, rules `E,F,W,I`, `E501` ignored. | `pyproject.toml:9,33-41` |
| pytest: `pythonpath=["."]`, `testpaths=["tests"]`, `asyncio_mode="auto"`, Playwright tests excluded by default via `addopts`. | `pyproject.toml:18-31` |
| `shared/` is the only installable package. The two `vitalforge-*` dirs are hyphenated and loaded via `importlib.import_module`. | `pyproject.toml:11-16`; `tests/conftest.py:181-186` |

**Route ownership.** Weight service owns weight write/read and person landing.
Dashboard owns metrics, readiness, sync, FIT import, activities, goals, correlations,
export. Both register `add_auth_routes(app)` and `add_person_routes(app)` so one login
covers both (`vitalforge-weight/app.py:72-77`, `vitalforge-dashboard/app.py:133-138`).

### 1.2 Person model

- `persons(id, slug UNIQUE, display_name, created_at, archived_at, is_primary)`,
  with a partial unique index allowing exactly one primary — `shared/database.py:286-299`.
- `person_grants(person_id, user_id, access CHECK IN ('view','manage','own'), …)`,
  PK `(person_id, user_id)` — `shared/database.py:301-311`.
- Slug rule: `^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$`, plus a reserved set
  (`api`, `auth`, `static`, `health`, `p`, `new`, `admin`, `persons`, `anonymous`,
  `api-token`) — `shared/slugs.py:13-20`.
- **Cadence's two profiles are two `persons` rows.** Create them with
  `POST /api/persons {display_name, slug?}` (admin only); the creating admin gets an
  automatic `own` grant — `shared/persons_admin.py:250-289`.
- Access is ranked `view < manage < own` — `shared/auth.py:336`.
- `require_person(level)` is the **only** sanctioned way a request path obtains a
  `person_id`, and it raises `RuntimeError` if mounted on a path with no `{slug}`
  placeholder, specifically to kill query-string person addressing —
  `shared/auth.py:397-475`, especially `:443-448`.
- Missing grant returns **404, never 403** — `shared/auth.py:409-414,471-472`.
- A structural test asserts over the AST that no request path calls
  `get_primary_person_id()` — `tests/test_no_unscoped_person_access.py:65-70`,
  allow-list at `:38-42`.

### 1.3 Auth scheme

**Header format, exactly:** `Authorization: Bearer <raw-token>`. Scheme match is
case-insensitive; the value is stripped — `shared/auth.py:622-630`.

- Tokens are minted at `POST /auth/tokens` with body `{label, current_password}`
  (password step-up required). `secrets.token_urlsafe(32)`; only the SHA-256 hash is
  stored. The raw value is returned **once**, with `Cache-Control: no-store` —
  `shared/auth.py:1291-1327`.
- Storage: `api_tokens(id, user_id, label, token_hash UNIQUE, created_at, last_used_at)`
  — `shared/database.py:313-323`.
- **Tokens are per-USER, not per-person.** A bearer token resolves to its owning user
  account, and that user's `person_grants` rows then decide which persons it can reach —
  `shared/auth.py:634-652`. There is no per-person token scoping.
- Precedence: bearer is checked **before** the session cookie —
  `shared/auth.py:205-207`.
- Admin bypass: `identity.role == "admin"` skips the grant check inside
  `require_person` — `shared/auth.py:459-462`. Admin-only routes use `_require_admin`,
  which 403s on a non-admin — `shared/auth.py:254-277`.
- Middleware: `auth_middleware` skips `/auth/*`, `/health`, `/static/*`. Otherwise an
  unauthenticated request to an API path gets `401 {"detail":"Not authenticated"}` with
  `WWW-Authenticate: Bearer`; a non-API path gets a `302` to `/auth/login` —
  `shared/auth.py:1498-1518`. "API path" means `/api/…` **or** `/p/{slug}/api/…` —
  `shared/auth.py:74-89`.
- **Open-access mode:** with an empty `users` table, `_get_current_identity` returns the
  sentinel `_Identity("anonymous", None, None, None)` and everything is open —
  `shared/auth.py:203-204`, `:455-458`.
- **CSRF:** the session cookie is `httponly`, `samesite="lax"`, `secure` when behind
  HTTPS — `shared/auth.py:1224-1231`. There is no CSRF token. A bearer-token client is
  unaffected. **Cadence should authenticate with a bearer token, never by borrowing the
  cookie.**

### 1.4 `/api/weight` and the `client_id` idempotency pattern

Route: `POST /p/{slug}/api/weight`, `require_person("manage")` —
`vitalforge-weight/app.py:360-361`.

Request model `WeightIn` (`extra="forbid"`) — `vitalforge-weight/app.py:114-134`:

```json
{
  "weight": 185.4,
  "unit": "lbs",
  "body_fat_pct": 18.2,
  "body_water_pct": 55.0,
  "muscle_pct": 41.0,
  "bone_mass_kg": 3.2,
  "source": "pwa",
  "client_id": "reading-1",
  "captured_at": "2026-09-06T07:31:00+00:00"
}
```

Bounds: `body_fat_pct` 3–75, `body_water_pct` 30–80, `muscle_pct` 10–90,
`bone_mass_kg` 0.5–10, weight 2–500 kg after unit conversion. `client_id` is 1–128 chars.
Booleans are explicitly rejected for every numeric field
(`vitalforge-weight/app.py:136-146`). `captured_at` must carry a UTC offset and may not
be more than 60 s in the future (`vitalforge-weight/app.py:158-174`).

Fresh-insert response (200):

```json
{"success": true, "weight_lbs": 185.4, "weight_kg": 84.1,
 "timestamp": "2026-09-06T07:31:00+00:00", "synced_to_garmin": true,
 "body_fat_pct": 18.2, "source": "pwa", "client_id": "reading-1"}
```

Dedup response (200):

```json
{"success": true, "deduplicated": true, "id": 42, "weight_lbs": 185.4,
 "weight_kg": 84.1, "timestamp": "...", "synced_to_garmin": true,
 "enriched": true, "conflict": true, "conflict_fields": ["weight"],
 "client_id": "reading-1"}
```

`garmin_error` is added to either shape when the push failed —
`vitalforge-weight/app.py:702-745`.

**The idempotency mechanism Cadence should mirror** (`vitalforge-weight/app.py:399-586`):

1. Everything from the duplicate lookup through the insert/update runs inside one
   `BEGIN IMMEDIATE`, so two concurrent requests can never both see "no duplicate".
2. **Primary match** is an exact `(person_id, client_id)` lookup, which short-circuits
   the time window entirely — a replay months later still matches.
3. Fallback is a `±60 s`, `±50 g` window, with a 1-second-wider sargable
   `timestamp >= ?` prefilter so the index can prune.
4. Enrichment is **first-write-wins**: a NULL field is filled, a differing field is
   reported in `conflict_fields` and *not* overwritten.
5. The Garmin push happens **after COMMIT, outside the lock**, because it is
   synchronous and unbounded (`vitalforge-weight/app.py:399-403`).
6. **Retry is client-driven, not a background worker.** `should_attempt_garmin_push`
   re-attempts only when the row matched by `client_id` and `synced_to_garmin` is false —
   `vitalforge-weight/app.py:631-633`.
7. DB-level backstop: `CREATE UNIQUE INDEX idx_weight_log_person_client_id ON
   weight_log(person_id, client_id) WHERE client_id IS NOT NULL` — a **partial** index,
   because `client_id` is nullable — `shared/database.py:172-175`.

Timestamps are `datetime.now(timezone.utc).isoformat()` TEXT, normalized to UTC with
`.astimezone(timezone.utc)` before storage — `vitalforge-weight/app.py:396-397`.

### 1.5 Migrations

- Marker table `schema_migrations(name PRIMARY KEY, completed_at)` —
  `shared/migrations.py:23-28`.
- `_KNOWN_MIGRATIONS = ("001-person-id-rebuild", "002-activities-person-id")` —
  `shared/migrations.py:46`. **Any new migration must be added here**, or
  `assert_schema_understood()` treats the DB as being from the future and boot-loops the
  container (`shared/migrations.py:338-372`).
- `run_migration(name, apply)` opens its own connection with `isolation_level=None`,
  sets `busy_timeout=30000`, takes `BEGIN IMMEDIATE`, checks the marker **inside** the
  transaction, applies, and commits the marker in the same transaction —
  `shared/migrations.py:375-443`.
- **Migrations are immutable once written.** 002 exists as its own marker rather than as
  extra work inside 001 for exactly this reason — `shared/migrations.py:41-45`.
- Ordering in `init_db()`: DDL block → `assert_schema_understood()` → snapshot →
  migrations → orphan backfill — `shared/database.py:453-471`.

### 1.6 Garmin client wrapper

`shared/garmin_client.py`, 164 lines. One module-level `_client`.

- `authenticate()` does `Garmin(email=…, password=…)` then
  `client.login(tokenstore=str(GARTH_TOKEN_DIR))`. In 0.3.x this single call both resumes
  from saved tokens and persists fresh ones — `shared/garmin_client.py:34-39`.
- `GARTH_TOKEN_DIR` defaults to `/app/data/.garth`, overridable by env —
  `shared/garmin_client.py:10`.
- **There is no retry or backoff in this wrapper.** Pull helpers each catch `Exception`,
  log a warning and return `None` (`shared/garmin_client.py:95-164`). `push_weight` does
  **not** catch — the caller's `_push_composition` does, returning an error string
  (`vitalforge-weight/app.py:339-357`).
- Only push method today: `push_weight` → `add_body_composition` —
  `shared/garmin_client.py:53-88`.
- Test mocking: `FakeGarminClient` + a `fake_garmin_client` fixture that monkeypatches
  `garmin_client._client` and `garmin_client.authenticate` —
  `tests/conftest.py:49-101`. **Critically**, because app modules use
  `from shared.garmin_client import authenticate, push_weight`, the fixtures *also*
  patch those names **in the app module's own namespace** —
  `tests/conftest.py:295-305`. A new push helper must get the same treatment or tests
  silently reach the real client.

### 1.7 Deploy shape

- Compose services have no healthcheck stanza; the healthcheck lives in the Dockerfile:
  `HEALTHCHECK … CMD python -c "…urlopen('http://localhost:8085/health')"` —
  `vitalforge-weight/Dockerfile:21-22`.
- Container runs as non-root `vitalforge` via an entrypoint that `chown`s `/app/data`
  first — `vitalforge-weight/Dockerfile:19`.
- Env is a single shared `.env` file for both services — `docker-compose.yml:10-11,22-23`.
- Prod pulls prebuilt images `bearyj/vitalforge-{weight,dashboard}:latest` —
  `docker-compose.prod.yml:14,24`.
- The repo's nginx sample proxies by subdomain and raises `client_max_body_size` to 25m
  **only** for `/api/import/` — `nginx/nginx.conf:39-46`.

> **Not in the repo:** there is no mention of VM-201, Nginx Proxy Manager, or the
> `*.grepon.cc` hostnames anywhere in the tree. The only Tailscale reference is one line
> of prose in `docs/prp/vitalforge-token-auth-pr.md:28`. Treat the VM-201/NPM/Tailscale
> deployment details as operator knowledge to be confirmed with the user, not as
> something this repo specifies. **Any new route with a large body would need its own
> `client_max_body_size` at the proxy, following the `/api/import/` precedent** —
> `/api/activity` bodies are small JSON, so the 1 MB default is fine.

---

## 2. Read endpoints Cadence will use

All are `require_person("view")` and live under `/p/{slug}/`. Send
`Authorization: Bearer <token>`.

### 2.1 Latest weight — weight service, port 8085

`GET /p/{slug}/api/weight/recent` → last 10 rows, newest first —
`vitalforge-weight/app.py:748-770`.

```json
[{"id": 42, "weight_lbs": 185.4, "weight_kg": 84.1,
  "timestamp": "2026-09-06T07:31:00+00:00", "synced_to_garmin": true}]
```

> **Trap:** this endpoint returns **no body-composition fields at all** — only
> `weight_lbs`, `weight_kg`, `timestamp`, `synced_to_garmin`
> (`vitalforge-weight/app.py:753`). For body composition, use the dashboard metrics
> endpoints in §2.3.

`GET /p/{slug}/api/weight/trend` → last 30 days, oldest first, for a trend line —
`vitalforge-weight/app.py:773-790`.

```json
[{"weight_lbs": 185.4, "weight_kg": 84.1, "timestamp": "..."}]
```

### 2.2 Body-composition time series — dashboard, port 8086

`GET /p/{slug}/api/metrics/{metric_name}?days=30` (`days` 1–365, default 30) —
`vitalforge-dashboard/app.py:390-433`.

```json
{"metric": "body_fat", "days": 30, "count": 28,
 "data": [{"date": "2026-09-05", "value": 18.2, "moving_avg_7d": 18.4}]}
```

Unknown metric → 400 naming the valid set. Rows with a NULL value are dropped before the
moving average is computed.

Valid metric names — `vitalforge-dashboard/app.py:84-101`:
`sleep_duration`, `sleep_score`, `resting_hr`, `hrv`, `body_battery`,
`body_battery_low`, `stress`, `vo2max`, `weight`, `body_fat`, `body_water`,
`bone_mass`, `muscle_mass`, `training_load`, `steps`, `active_calories`.

> **Unit trap.** Body composition lives in two tables with **different units**:
> `weight_log` (what Cadence POSTs) carries `bone_mass_kg` and `muscle_pct`, while
> `weight_history` (what `METRIC_TABLES` reads) carries `bone_mass_g` and
> `muscle_mass_g`, and `weight` there is `weight_grams` —
> `shared/database.py:23-37` vs `:44-48,251-263`. The `_g`/`_kg`/`_pct` suffix is
> deliberate and load-bearing (`shared/database.py:39-43`). Cadence must not mix them.
> For a trend line use `metrics/body_fat` and `metrics/weight`; divide `weight` by 1000
> for kg.

`weight_history` is populated by the **Garmin pull**, not by `POST /api/weight`, so a
freshly logged weight appears in `weight/recent` immediately but in `metrics/weight`
only after the next sync (`vitalforge-dashboard/sync.py:202`).

### 2.3 Readiness nudge — dashboard, port 8086

`GET /p/{slug}/api/readiness` — `vitalforge-dashboard/app.py:436-443`. No query params.

```json
{"score": 72, "components": {"hrv": 68, "rhr": 74, "sleep_score": 76}, "status": "ok"}
```

`status` is one of `ok` (all three components present), `partial_data` (weights
renormalized across whichever components have ≥5 trailing days), or
`insufficient_data`. Weights are HRV 0.40, RHR 0.30, sleep score 0.30 —
`vitalforge-dashboard/readiness.py:37,111-135`.

> **Trap for the kid profile.** When no component has `MIN_BASELINE_DAYS = 5` of
> trailing data, the response is `{"score": null, "components": {"hrv": null, "rhr":
> null, "sleep_score": null}, "status": "insufficient_data"}` —
> `readiness.py:29,128-129`. Because the deployment holds **one** Garmin credential
> belonging to the primary person (§3.3), the kid's readiness will be `null`
> **permanently**. Cadence must render the nudge as "not available" on
> `score is None`, never crash and never coerce to 0. Any 500 from this route is a
> genuine failure — `vitalforge-dashboard/app.py:441-443`.

### 2.4 Supporting reads

| Endpoint | Purpose | Evidence |
|---|---|---|
| `GET /p/{slug}/api/sync/status` | last sync time/result, `syncing` bool | `vitalforge-dashboard/app.py:360-387` |
| `GET /p/{slug}/api/activities?limit=50` | **existing FIT-import activities** (not Cadence's) | `vitalforge-dashboard/app.py:779-797` |
| `GET /api/persons` | list persons + `grant_count`; **admin only** | `shared/persons_admin.py:226-248` |
| `GET /health` | unauthenticated liveness | `vitalforge-weight/app.py:177-179` |

---

## 3. `garminconnect` 0.3.11 capabilities

Read from the installed source at
`…/scratchpad/gc-venv/lib/python3.12/site-packages/garminconnect/`.
Line citations in this section are into that installed package.

### 3.1 Verified — read from source

| Item | Detail |
|---|---|
| `Garmin.create_manual_activity` | **Exists.** `(start_datetime: str, time_zone: str, type_key: str, distance_km: float, duration_min: int, activity_name: str)` — `__init__.py:2417-2449` |
| Its payload | `{"activityTypeDTO":{"typeKey":…}, "accessControlRuleDTO":{"typeId":2,"typeKey":"private"}, "timeZoneUnitDTO":{"unitKey":…}, "activityName":…, "metadataDTO":{"autoCalcCalories":true}, "summaryDTO":{"startTimeLocal":…, "distance": km*1000, "duration": min*60}}` — `__init__.py:2435-2448` |
| `start_datetime` format | `"2023-12-02T10:00:00.000"` — **local wall-clock, no offset** — `__init__.py:2429` |
| `time_zone` | IANA name, e.g. `'Europe/Paris'` — `__init__.py:2430` |
| `create_manual_activity_from_json(payload)` | **Exists.** POSTs an arbitrary payload; the escape hatch when the six fixed args are too narrow — `__init__.py:2412-2415` |
| `get_activity_exercise_sets(activity_id)` | **Exists.** `GET /activity/{id}/exerciseSets` — `__init__.py:2970-2976` |
| `set_activity_exercise_sets(activity_id, payload)` | **Exists.** `PUT /activity/{id}/exerciseSets`. **Replace-all semantics** — the existing `exerciseSets` array is overwritten — `__init__.py:2978-2994` |
| Garmin-side validation | The docstring states Garmin validates `exercises[].category` (parent) and `exercises[].name` (sub-category) against its FIT enum and returns **400 "Invalid Sub-Category Passed"** for unknown values; `name=None` is always accepted under a known parent — `__init__.py:2984-2988` |
| `upload_strength_workout(workout)` | **Exists** — `__init__.py:3339` |
| `upload_workout(workout_json)` | **Exists** — `__init__.py:3148` |
| `add_workout` | **Does not exist** (verified by `hasattr`) |
| Strength sport type | `sportTypeId=5`, `sportTypeKey="strength_training"` — `workout.py:293-299`, and in the METADATA example at line 460 |
| Exercise catalog | `garminconnect.exercises`: **1527 exercises across 47 categories**, with `EXERCISES`, `BY_NAME`, `CATEGORIES`, `resolve(name)`, `find(term)` — `exercises.py:1-10,2567-2635` |
| `set_activity_name` / `set_activity_type` / `set_activity_description` | All exist — `__init__.py:2376,2384,2404` |
| Rate limiting | The library raises `GarminConnectTooManyRequestsError` on 429 but has **no automatic retry/backoff** for data calls — `client.py:669-671` and ~15 other 429 sites |

**Category names confirmed present in `exercises.CATEGORIES`** (26 of the 27 asked):
`PUSH_UP`, `SQUAT`, `DEADLIFT`, `ROW`, `PLANK`, `CARRY`, `LUNGE`, `HIP_RAISE`, `CORE`,
`SHOULDER_PRESS`, `BENCH_PRESS`, `PULL_UP`, `CURL`, `TRICEPS_EXTENSION`, `TOTAL_BODY`,
`WARM_UP`, `FLYE`, `LATERAL_RAISE`, `SHRUG`, `SIT_UP`, `LEG_RAISE`, `CALF_RAISE`,
`HYPEREXTENSION`, `OLYMPIC_LIFT`, `PLYO`, `CHOP`.

**`UNKNOWN` is NOT a category** in this catalog. Cadence must not emit it. The
correct "I don't know the specific variant" encoding is a **known category with
`name = None`**, which the `set_activity_exercise_sets` docstring says is always accepted.

Also present and usable as fallbacks: `CARDIO`, `BANDED_EXERCISES`, `SUSPENSION`,
`HIP_STABILITY`, `SHOULDER_STABILITY`, `LEG_CURL`, `CRUNCH`, `SLED`, `LADDER`,
`BATTLE_ROPE`, `SANDBAG`, `TIRE`, `SLEDGE_HAMMER`, `FLOOR_CLIMB`, `HIP_SWING`,
plus cardio-machine categories — full list at `exercises.py:2576-2624`.

### 3.2 Critical distinction: workout ≠ activity

Two different Garmin services, and conflating them builds the wrong feature.

- **`upload_strength_workout` / `create_strength_set` build a *planned workout
  template*** in workout-service — the thing you schedule and push to a watch. Its
  builders emit `stepType`/`endCondition`/`targetType` structures and a
  `sportType {sportTypeId:5, sportTypeKey:"strength_training"}` —
  `workout.py:283-299,473-573`. `create_strength_exercise_step` encodes weight as
  `weightValue = kg * 1000.0` (grams) with an explicit `weightUnit` —
  `workout.py:492-495`.
- **`create_manual_activity` + `set_activity_exercise_sets` record a *completed
  activity***. This is what Cadence needs.

Cadence's Garmin path is the **activity** path. The workout builders are noted only so a
later implementer does not reach for them by mistake.

### 3.3 Believed, unverified

Everything below is inference. None of it was confirmed against a live Garmin account,
and none of it appears in the library source or its METADATA.

1. **The `exerciseSets` payload's field names.** The method exists and its semantics are
   documented, but the actual JSON keys are **nowhere in the package** — a grep for
   `repetitionCount`, `setType`, and `exerciseSets` across the whole installed tree
   returns only the two method definitions. The shape below is a guess based on the
   docstring's mention of `exercises[].category` / `exercises[].name` and on Garmin's
   public FIT `set` message; **it must be confirmed empirically** (see Open Question 1):

   ```json
   {"exerciseSets": [
     {"setType": "ACTIVE", "startTime": "2026-09-06T08:00:00.0",
      "duration": 45.0, "repetitionCount": 10, "weight": 20000.0,
      "exercises": [{"category": "BENCH_PRESS", "name": null}]}]}
   ```
   Weight is *believed* to be grams (matching `workout.py:494`'s `kg * 1000.0`), and
   `duration` seconds. Both unverified.

2. **Whether `set_activity_exercise_sets` works on a *manually created* activity at
   all.** Garmin may only accept exercise sets on activities that arrived with FIT set
   data. This is the single biggest risk to the enhancement path.

3. **`type_key` for strength.** `"strength_training"` is confirmed as the *workout*
   `sportTypeKey` (`workout.py:296`) but was **not** found as an *activity* `typeKey`
   anywhere in the package. `create_manual_activity`'s docstring points at Garmin's
   external `activity_types.properties` list rather than bundling one
   (`__init__.py:2427-2428`). `Garmin.get_activity_types()` exists
   (`__init__.py:2713`) and can confirm this against a live account in one call.

4. **Whether Garmin accepts a large backdate** on a manual activity. VitalForge already
   flags the same unknown for backdated weight pushes —
   `vitalforge-weight/app.py:654-659`.

5. **Whether `create_manual_activity` returns the new `activityId`** and under which
   key. It returns whatever the POST returns, unwrapped (`__init__.py:2415`). The
   two-step design in §4 depends on getting an id back; if it does not, fall back to
   `get_last_activity()` (`__init__.py:2451-2461`) — which is racy and should be a
   documented fallback, not the primary path.

---

## 4. `/api/activity` extension design

### 4.1 Host service: **vitalforge-weight** (port 8085)

It already owns the **only** Garmin *write* path in the codebase (`push_weight` →
`add_body_composition`, `shared/garmin_client.py:53-88`, called from
`vitalforge-weight/app.py:339-357`); the dashboard only ever *pulls*. It is also the
service that already implements the store-then-push-outside-the-transaction pattern this
design copies (`vitalforge-weight/app.py:399-403`).

**Cost, stated honestly:** the FIT-activity read routes live in the dashboard
(`vitalforge-dashboard/app.py:779-821`), so activity reads end up split across two
services. Acceptable — the two tables are different concepts (§4.2) and both services
share one DB and one login. The alternative puts a synchronous, unbounded Garmin write
onto the service running the background sync loop
(`vitalforge-dashboard/app.py:63-82,125`). That trade is worse.

`vitalforge-weight/app.py` does **not** currently import `garmin_credential_person_id`;
that import is new (§4.6).

### 4.2 Storage: a NEW table, not the existing `activities`

`activities` already exists and is **not** reusable —
`shared/database.py:410-432`:

- `source_format TEXT NOT NULL CHECK (source_format IN ('fit'))` — a Cadence session is
  not a FIT file, and **SQLite cannot alter a CHECK constraint**, so reuse forces a full
  table rebuild.
- `file_sha256 TEXT NOT NULL` with `UNIQUE (person_id, file_sha256)` — a manual session
  has no file, and that key has nothing to do with `session_id`.
- Two read routes already serve it (`vitalforge-dashboard/app.py:779,800`); a rebuild
  would change their response shape.

**Decision: a new table `strength_sessions`, created in `init_db()`'s DDL block with no
migration marker** (see the wiring note below for why no marker). A pure additive
`CREATE TABLE` is far cheaper to review and test than rebuilding a table two routes
already serve. The rejected alternative — rebuild `activities` to allow `'manual'`, make
`file_sha256` nullable, and add the new columns — is recorded here so the choice is not
re-litigated silently.

```sql
CREATE TABLE IF NOT EXISTS strength_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id         INTEGER NOT NULL,
    session_id        TEXT NOT NULL,
    start_time_utc    TEXT NOT NULL,
    duration_seconds  INTEGER NOT NULL,
    exercises_json    TEXT NOT NULL,
    notes             TEXT,
    source            TEXT,
    garmin_status     TEXT NOT NULL DEFAULT 'skipped'
                      CHECK (garmin_status IN ('skipped','pending','synced','failed')),
    garmin_activity_id TEXT,
    garmin_error      TEXT,
    garmin_sets_status TEXT NOT NULL DEFAULT 'not_attempted'
                      CHECK (garmin_sets_status IN ('not_attempted','synced','failed')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (person_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_strength_sessions_person_start
    ON strength_sessions(person_id, start_time_utc);
```

Notes on the shape:

- **`UNIQUE (person_id, session_id)` is a plain unique constraint, not a partial index.**
  This deliberately differs from `idx_weight_log_person_client_id`
  (`shared/database.py:172-175`), which is partial *only because* `client_id` is
  nullable. `session_id` is required here, so there are no NULLs to exclude.
- `exercises_json` is a JSON TEXT blob, following `activities.raw_summary_json`
  (`shared/database.py:425`). Per-exercise rows are not needed for v1 and would triple
  the migration's surface.
- `garmin_sets_status` is separate from `garmin_status` because the exercise-set upload
  is a distinct, riskier second call (§3.3 item 2) that may fail while the activity
  itself succeeded.
- `person_id` is `NOT NULL` from the start, which the pre-existing tables could not be
  (`shared/database.py:480-483`).

**Wiring: no migration is needed. Do not add one.**

> **Correcting a premise in the task.** The task asked for "a migration number following
> `shared/migrations.py` conventions." Following those conventions correctly means
> **not** adding a migration here.

1. Add the `CREATE TABLE`/`CREATE INDEX` to `init_db()`'s DDL block, **unguarded** —
   `person_id` is in the `CREATE TABLE` itself, so the guard that
   `idx_activities_person_start_time` needs does not apply here
   (`shared/database.py:436-450`). That is the whole change.
2. **Leave `_KNOWN_MIGRATIONS` (`shared/migrations.py:46`) untouched.**

Why. The DDL block runs *before* any `run_migration` call
(`shared/database.py:453-468`), and `CREATE TABLE IF NOT EXISTS` is correct on both a
fresh database and an upgrade — so a `003` apply-function would be a no-op on every
path. All **19** tables in this schema are created in that DDL block with no marker,
including every additive one (`goals`, `training_load`, `steps`, `active_calories`,
`sync_status`, `api_tokens`, `activities`) — `shared/database.py:143-432`. Only 001 and
002 have markers, and **both are rebuilds of pre-existing tables**, not new ones. The
rule is stated directly at `shared/database.py:16-22`: the runner is for a genuine
schema change, not for something a metadata-only operation already handles.

Adding a marker anyway is not merely redundant, it is **harmful**.
`assert_schema_understood()` boot-loops any image that sees a marker outside its own
`_KNOWN_MIGRATIONS` — `shared/migrations.py:367-372`. A rollback to a pre-Cadence image
would then **refuse to start**, where a bare extra table it does not know about is
ignored harmlessly.

**Do not add the table to `_REBUILD_TABLES`** (`shared/migrations.py:55-58`) — that list
is for the date-keyed metric tables 001 rebuilt, and this table already carries
`person_id`.

### 4.3 `POST /p/{slug}/api/activity`

**The path carries the person. There is no `profile` field in the body.**
`require_person` raises `RuntimeError` if mounted on a path without `{slug}`, precisely
to prevent body/query person addressing — `shared/auth.py:443-448`. Cadence's parent and
kid profiles are two slugs.

```python
@app.post("/p/{slug}/api/activity", status_code=202)
async def post_activity(data: ActivityIn,
                        person_id: int = Depends(require_person("manage"))):
```

**Mechanics gotcha:** `status_code=202` on the decorator applies to *every* return, so
the idempotent-repeat branch must override it explicitly — return
`JSONResponse(status_code=200, content=result)` on the dedup path (or take
`response: Response` and set `response.status_code = 200`). Without that, a repeat POST
also answers 202 and Cadence cannot tell "created" from "already had it" by status code
alone.

`require_person("manage")` matches every existing write route —
`vitalforge-weight/app.py:361`, `vitalforge-dashboard/app.py:674`.

Request model (`ConfigDict(extra="forbid")`, matching `WeightIn`):

```json
{
  "session_id": "cadence-2026-09-06-a3f9",
  "start": "2026-09-06T08:00:00+00:00",
  "duration_min": 42,
  "exercises": [
    {"name": "Bench Press", "garmin_category": "BENCH_PRESS",
     "garmin_exercise": "BARBELL_BENCH_PRESS",
     "sets": 3, "reps": 10, "weight_kg": 40.0, "rest_s": 90}
  ],
  "notes": "felt strong",
  "source": "cadence",
  "push_to_garmin": true
}
```

Field rules:

| Field | Rule |
|---|---|
| `session_id` | required, `min_length=1, max_length=128` — same bounds as `client_id` (`vitalforge-weight/app.py:126`) |
| `start` | required `datetime`, **must carry a UTC offset**, and may not be more than 60 s in the future — copy `_validate_captured_at` verbatim (`vitalforge-weight/app.py:158-174`), including the `CAPTURED_AT_FUTURE_TOLERANCE_SECONDS` constant |
| `duration_min` | required int, `ge=1, le=600` |
| `exercises` | required list, `min_length=1, max_length=50` |
| `exercises[].name` | required free text, `max_length=100` |
| `exercises[].garmin_category` | optional; **validated against `garminconnect.exercises.CATEGORIES`** at the model layer, 422 on an unknown value. Do this locally rather than discovering it as a Garmin 400 |
| `exercises[].garmin_exercise` | optional sub-category; `None` is always accepted under a known parent (`__init__.py:2988`) |
| `sets`, `reps` | required ints, `ge=1, le=100` |
| `weight_kg` | optional float, `ge=0, le=500` |
| `rest_s` | optional int, `ge=0, le=3600` |
| `notes` | optional, `max_length=1000` |
| `source` | optional `Literal["cadence", "pwa", "manual"]`, mirroring `WeightIn.source` |
| `push_to_garmin` | optional bool, **default `False`** |

Every numeric field needs the same `_reject_bool` validator `WeightIn` uses —
`vitalforge-weight/app.py:136-146`. Python's `bool` is an `int` subclass and Pydantic
lax mode silently coerces `true` to `1`; this bit VitalForge before.

`push_to_garmin` defaults to **`False`** deliberately: the safe default is to store, and
the destructive-side-effect default should never be implicit.

### 4.4 Response envelope

> **Correcting a premise in the task.** VitalForge has **no single response envelope
> convention.** Three routes, three shapes: `post_weight` returns a flat dict with
> `"success": true` (`vitalforge-weight/app.py:702-745`); `import_activity` returns the
> bare row plus optional `duplicate`/`duplicate_reason` and **no** `success` key
> (`vitalforge-dashboard/app.py:772-776`); `list_activities` returns
> `{count, activities}` (`vitalforge-dashboard/app.py:797`).

Mirror `post_weight`, since this is a write with a Garmin side effect and its
`deduplicated` / `garmin_error` keys are exactly the signals Cadence needs.

**202 on a fresh insert:**

```json
{"success": true, "id": 7, "session_id": "cadence-2026-09-06-a3f9",
 "person_id": 3, "start_time_utc": "2026-09-06T08:00:00+00:00",
 "duration_min": 42, "garmin_status": "synced",
 "garmin_activity_id": "19283746501", "garmin_sets_status": "failed"}
```

**200 on an idempotent repeat:**

```json
{"success": true, "deduplicated": true, "id": 7,
 "session_id": "cadence-2026-09-06-a3f9", "garmin_status": "synced",
 "garmin_activity_id": "19283746501", "garmin_sets_status": "synced"}
```

`garmin_error` is added whenever `garmin_status == "failed"`, carrying the string from
the caught exception — same treatment as `vitalforge-weight/app.py:742-743`.

Status codes:

| Code | Meaning |
|---|---|
| 202 | session stored (fresh insert). 202 rather than 200 because the Garmin outcome may still be `pending`/`failed` |
| 200 | idempotent repeat; nothing new was created |
| 401 | no/invalid credential (from `auth_middleware`, with `WWW-Authenticate: Bearer`) |
| 404 | unknown slug **or** no grant — never distinguish them (`shared/auth.py:409-414`) |
| 409 | `push_to_garmin: true` for a person who is not the Garmin-credential person (§4.6) |
| 422 | Pydantic validation, including an unknown `garmin_category` |

> **Divergence flagged:** no existing VitalForge route returns 202 — `post_weight` is 200
> and `trigger_sync` returns 200 with `{"status": "started"}`
> (`vitalforge-dashboard/app.py:357`). The 202 here is the task's explicit request and is
> defensible for a route with an async-ish side effect, but it is a new precedent. See
> Open Question 5.

### 4.5 Idempotency on `session_id`

Directly mirrors `post_weight` (`vitalforge-weight/app.py:399-586`):

1. Open `BEGIN IMMEDIATE`. Everything through the insert happens inside it.
2. `SELECT … FROM strength_sessions WHERE person_id = ? AND session_id = ?`.
3. **Hit** → do not insert, do not modify the stored payload. First-write-wins, exactly
   like `ENRICHABLE_FIELDS` (`vitalforge-weight/app.py:515-524`). If the incoming body
   differs materially, log a warning naming the fields — do **not** 409 — matching the
   `conflicts` treatment at `vitalforge-weight/app.py:583-584`. Optionally echo
   `"conflict": true, "conflict_fields": [...]`.
4. **Miss** → insert with `garmin_status = 'pending'` if `push_to_garmin` else
   `'skipped'`.
5. `COMMIT`. **Then** attempt Garmin, outside the transaction, for the same reason
   `post_weight` does: the call is synchronous with no timeout mechanism
   (`vitalforge-weight/app.py:399-403`).
6. Record the outcome with a second `UPDATE … SET garmin_status = ?, garmin_activity_id
   = ?, garmin_error = ?, updated_at = ? WHERE id = ?`, wrapped in `try/except` so a
   failure here never turns already-committed data into a 500 —
   `vitalforge-weight/app.py:694-698`.

**A repeat POST never creates a second Garmin activity.** The push is attempted only
when the row was freshly inserted, or when the row matched and
`garmin_status in ('pending','failed')` and `push_to_garmin` is true. This is the exact
shape of `should_attempt_garmin_push` — `vitalforge-weight/app.py:631-633`.

**Retry story.** There is **no** background retry worker in VitalForge, for weight or
anything else; `sync_status.backoff_until` is for *pulls*
(`vitalforge-dashboard/sync.py:292-294`). Retry is client-driven: **Cadence re-POSTs the
same `session_id`, and the push is re-attempted iff `garmin_status != 'synced'`.** Same
mechanism, no new machinery. Say this in the PRP so nobody builds a queue.

The `UNIQUE (person_id, session_id)` constraint is the DB-level backstop for the same
invariant, in case a future write path bypasses the transaction — the rationale at
`shared/database.py:162-171`.

### 4.6 Garmin path

**Guaranteed path** (do this first, ship it alone if need be):

```python
client.create_manual_activity(
    start_datetime=start_local.strftime("%Y-%m-%dT%H:%M:%S.000"),  # LOCAL wall clock
    time_zone=os.environ.get("TZ", "UTC"),                         # IANA name
    type_key="strength_training",                                  # see OQ 3
    distance_km=0.0,
    duration_min=data.duration_min,
    activity_name=f"Cadence — {session_label}",
)
```

**Timezone conversion is the gotcha.** VitalForge stores UTC ISO strings everywhere
(`vitalforge-weight/app.py:396-397`), but `create_manual_activity` wants a **local
wall-clock string with no offset** plus a separate IANA zone
(`__init__.py:2429-2430`). So: store `start` as UTC; convert to the zone named by `TZ`
(`.env.example:21-22`) only at the moment of the call; format with
`"%Y-%m-%dT%H:%M:%S.000"`. Passing a UTC string with the local zone name, or a string
carrying an offset, silently misfiles the activity by the offset amount — the same class
of bug `push_weight`'s `strftime` without `%z` already caused
(`vitalforge-weight/app.py:391-395`).

If `TZ` is unset, fall back to `"UTC"` and log it. Do not guess the host zone.

**Enhancement path** (attempt after the activity exists; failure must not fail the
request):

```python
client.set_activity_exercise_sets(activity_id, {"exerciseSets": [...]})
```

Build one entry per set, `exercises: [{"category": …, "name": … or None}]`. Omit
`garmin_category` → skip that exercise's sets rather than guessing a category.
**The payload's exact keys are unverified (§3.3 item 1) — the PRP must open with the
`get_activity_exercise_sets` probe (Open Question 1) before writing the builder.**

**Failure handling.** Wrap both calls in a helper that mirrors `_push_composition`
(`vitalforge-weight/app.py:339-357`): never raises, returns an error string or `None`.
The activity call failing sets `garmin_status = 'failed'` and stores the message; the
exercise-set call failing sets only `garmin_sets_status = 'failed'`, leaving
`garmin_status = 'synced'`, because the activity really is on Garmin.

**Cross-person guard — this is what protects the kid's data.** The deployment holds
**one** Garmin credential, belonging to the primary person, and whatever it returns or
accepts is that one human's data regardless of which `person_id` the caller names —
`shared/database.py:535-559`. `require_person` authorizes a caller *for a target
person*; it cannot authorize them *for a data source*.

So, before any Garmin call, copy `trigger_sync`'s guard verbatim in shape
(`vitalforge-dashboard/app.py:299-322`):

```python
if data.push_to_garmin:
    source_person_id = await garmin_credential_person_id()
    if person_id != source_person_id:
        raise HTTPException(status_code=409, detail=(
            "This person has no Garmin account of their own. The deployment holds one "
            "set of Garmin credentials, which belong to a different person, and pushing "
            "would file this session under theirs. Per-person Garmin linking arrives in "
            "Phase 3."))
```

**409, not 404**, because the caller demonstrably holds `manage` on this person, so
naming the reason leaks nothing — the reasoning at
`vitalforge-dashboard/app.py:309-311`. **Never silently downgrade to store-only**: an
explicit `push_to_garmin: true` that quietly does nothing is worse than an error.
`push_to_garmin: false` (the default) stores with `garmin_status = 'skipped'` and is the
normal path for the kid profile.

This requires a new import of `garmin_credential_person_id` from `shared.database` into
`vitalforge-weight/app.py`.

### 4.7 Read routes

`GET /p/{slug}/api/activity/{session_id}` — status polling,
`require_person("view")`. Returns the stored row including `exercises` (parsed from
`exercises_json`), `garmin_status`, `garmin_activity_id`, `garmin_sets_status`, and
`garmin_error`. 404 when the `session_id` does not exist **for this person** — scope the
`WHERE` on `person_id` as well as the key, following
`vitalforge-dashboard/app.py:807-817`.

`GET /p/{slug}/api/strength-sessions?since=&limit=` — optional list route,
`require_person("view")`, `{"count": n, "sessions": [...]}` following
`list_activities` (`vitalforge-dashboard/app.py:797`). `limit` as
`Query(default=50, ge=1, le=200)`; `since` an ISO date compared against
`start_time_utc`.

> **Naming, deliberately.** The task proposed `GET /api/activities?person=…&since=…`.
> That path is **already taken** by the FIT-import list route
> (`vitalforge-dashboard/app.py:779`), and `?person=` is exactly the query-string person
> addressing `require_person` raises `RuntimeError` to prevent
> (`shared/auth.py:434-448`). Hence `/p/{slug}/api/strength-sessions`.

### 4.8 Tests to model on

| New test | Model on |
|---|---|
| `session_id` idempotency matrix | `tests/test_client_id_idempotency.py` — especially `test_client_id_match_retries_a_previously_failed_garmin_push:195` and `test_unique_index_rejects_duplicate_client_id_for_same_person:462` |
| Concurrent double-POST inserts once, pushes once | `tests/test_dedup_concurrency.py`, notably `test_two_concurrent_identical_retries_push_to_garmin_once` |
| garminconnect signature guards for the two new methods | `tests/test_garmin_client_api.py` — the whole file is 41 lines of `inspect.signature` assertions against the real library, and exists precisely to catch a version bump changing this shape |
| Auth matrix (401/404/403, cross-person isolation) | `tests/test_require_person.py`, `tests/test_no_unscoped_person_access.py`, `tests/test_idor_by_row_id.py` |
| Route-level request/response shapes | `tests/test_weight_api.py`, `tests/test_dashboard_api.py` |
| `UNIQUE (person_id, session_id)` enforced at the DB level | `tests/test_client_id_idempotency.py:462-508` |

No migration test is needed, because there is no migration (§4.2). Do **not** add a case
to `tests/test_migrations.py`.

Harness: an `httpx.AsyncClient` over `ASGITransport(app=module.app)` with the
`weight_app_module` fixture, and `PERSON_PREFIX` from `tests/conftest.py:37` —
see `tests/test_client_id_idempotency.py:26-30`.

**Mocking, the easy thing to get wrong.** `vitalforge-weight/app.py` imports Garmin
helpers by name (`from shared.garmin_client import authenticate, push_weight`,
`:30`), so patching `shared.garmin_client` alone does **not** reach them —
`tests/conftest.py:295-297` says so explicitly and patches the names in the app module.
Any new `push_activity` helper needs the same treatment, and `FakeGarminClient`
(`tests/conftest.py:49-87`) needs `create_manual_activity` and
`set_activity_exercise_sets` methods that record their calls, the way
`add_body_composition` records into `pushed_weights` (`tests/conftest.py:60-62`).

---

## 5. Open questions and risks

1. **[Blocking the enhancement path] The `exerciseSets` payload shape is unverified.**
   Neither the library nor its METADATA documents the JSON keys. **First action in the
   VitalForge-side PRP:** on a real account, find an existing strength activity, call
   `get_activity_exercise_sets(activity_id)`, and record the exact response. The
   `set_…` docstring says the request takes the same shape
   (`__init__.py:2983-2984`), so that one call settles it. Until then, ship the
   `create_manual_activity`-only path.

2. **[High] Does Garmin accept exercise sets on a *manually created* activity?**
   Garmin may only accept them on activities that arrived with FIT set data. If not,
   the fallback is generating a real FIT file and using `upload_activity`
   (`__init__.py:2463`), which is a much larger piece of work. Design the DB so this is
   a `garmin_sets_status` value, not an architectural assumption — done in §4.2.

3. **[High] Is `"strength_training"` a valid activity `typeKey`?** Confirmed only as a
   *workout* `sportTypeKey` (`workout.py:296`). Resolve with one live call to
   `get_activity_types()` (`__init__.py:2713`) and pin the answer in a test.

4. **[Medium] Is `create_manual_activity`'s return value the new activity id, and under
   what key?** The two-step design depends on it. `get_last_activity()` is a racy
   fallback, not a plan.

5. **[Medium] 202 is a new precedent** — no existing VitalForge route returns it. Worth
   confirming with the maintainer before it becomes house style.

6. **[Medium] Weight units on the Garmin side.** `create_strength_exercise_step` encodes
   kg as `kg * 1000.0` grams (`workout.py:494`), but that is the *workout* API. Whether
   the *exerciseSets* API uses grams is unverified. Resolved by Open Question 1.

7. **[Medium] The kid's readiness will be permanently `null`.** One Garmin credential,
   one person (`shared/database.py:535-559`). Cadence's readiness nudge must degrade
   gracefully, not error. Per-person Garmin linking is VitalForge "Phase 3" and does not
   exist yet.

8. **[Medium] Deployment specifics are not in the repo.** VM-201, Nginx Proxy Manager,
   Tailscale, and the `*.grepon.cc` hostnames appear nowhere in the tree — the sample
   nginx config uses `yourdomain.com` placeholders (`nginx/nginx.conf:11,24`). Confirm
   the real topology with the operator before writing deploy steps.

9. **[Medium] Rate limiting.** garminconnect raises on 429 with **no** automatic backoff
   (`client.py:669-671`), and VitalForge's own wrapper adds none
   (`shared/garmin_client.py`). A Cadence client that retries a failed session in a tight
   loop can get the shared Garmin credential IP-blocked — which breaks *weight logging
   too*, since it is the same module-level client. **Cadence must bound its retries**
   (exponential backoff, a cap, and never automatic on a 409).

10. **[Low] Two activity concepts now share one database** — `activities` (FIT imports,
    dashboard) and `strength_sessions` (Cadence, weight service). Deliberate (§4.2);
    note it in VitalForge's `CLAUDE.md` so nobody "unifies" them by accident.

11. **[Low] `docs/CODEMAPS/*` will go stale.** `tests/test_docs_drift.py` guards
    README/`.env.example` content only. If the PRP adds env vars or endpoints, consider
    adding a matching assertion there.

12. **[Low] The single-worker assumption is inherited.** `post_weight`'s post-commit flag
    update is race-free only because the push is synchronous and one uvicorn worker runs
    (`vitalforge-weight/app.py:606-612,634-643`). A second worker would need both routes
    revisited.
