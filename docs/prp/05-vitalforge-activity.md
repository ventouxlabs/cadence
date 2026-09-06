# PRP-05 — VitalForge `/api/activity`

## 1. Goal

Extend **VitalForge** (not Cadence) with `POST /p/{slug}/api/activity`, which stores a completed strength session and, when asked, creates a `strength_training` activity in Garmin Connect. Idempotent on `session_id`, safe under concurrent double-POST, and hard-guarded against filing one person's session under another person's Garmin account. Ships with the guaranteed `create_manual_activity` path only; the per-exercise `exerciseSets` upload is behind a default-off feature flag until JD's live probe settles its payload shape.

This is the write half of the Cadence↔VitalForge contract. `docs/vitalforge-contract.md` §4 is the design; this PRP is that design made buildable.

## 2. Scope

**In** — all inside the VitalForge clone at `/var/home/user/Documents/vibe-code/vitalforge`:

- `strength_sessions` table in `shared/database.py`'s `init_db()` DDL block.
- `ActivityIn` / `ActivityExerciseIn` request models in `vitalforge-weight/app.py`.
- `POST /p/{slug}/api/activity` (202 / 200 / 409 / 422).
- `GET /p/{slug}/api/activity/{session_id}` and `GET /p/{slug}/api/strength-sessions`.
- `push_activity` helper in `shared/garmin_client.py` (+ optional `push_activity_sets`).
- `FakeGarminClient` extension and app-module name patching in `tests/conftest.py`.
- New tests (§8). One `TZ`/flag line in `.env.example`; one paragraph in `CLAUDE.md`.

**Out**

| Not this PRP | Where |
|---|---|
| Any Cadence-side code, client, payload builder, retry queue | PRP-06 |
| A migration marker in `_KNOWN_MIGRATIONS` | nowhere — see §5.1, adding one is harmful |
| Rebuilding the existing `activities` table | rejected, contract §4.2 |
| Any change to `main` in the VitalForge repo, or any `git push` | forbidden, D-005 |
| Per-person Garmin credentials | VitalForge "Phase 3", does not exist |
| Real-Garmin verification | deferred to JD, §10.2 |

**Branch discipline.** Create `cadence/activity-endpoint` **from `fix/a6-review-followups`**, not from `main`. Every line citation in `docs/vitalforge-contract.md` points at that tree, and `main` does not yet contain `should_attempt_garmin_push` (verified: 0 occurrences on `main`, 3 on the branch; the branch is 2 commits ahead and unmerged). Commit there, never push, never check out `main`. If a6 squash-merges upstream later, rebase this branch onto the new `main` and re-verify the citations.

## 3. Data model

New table, created **unguarded** in `init_db()`'s DDL block alongside the other 19 tables (`shared/database.py:143-432`). DDL is contract §4.2 verbatim, plus two additive nullable columns marked below.

```sql
CREATE TABLE IF NOT EXISTS strength_sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id          INTEGER NOT NULL,
    session_id         TEXT NOT NULL,
    session_label      TEXT,                       -- ADDED: Garmin activity name suffix
    start_time_utc     TEXT NOT NULL,
    duration_seconds   INTEGER NOT NULL,
    exercises_json     TEXT NOT NULL,
    notes              TEXT,
    source             TEXT,
    garmin_status      TEXT NOT NULL DEFAULT 'skipped'
                       CHECK (garmin_status IN ('skipped','pending','synced','failed')),
    garmin_activity_id TEXT,
    garmin_error       TEXT,
    garmin_target      TEXT,                       -- ADDED: 'credential_person' when D-015 override used
    garmin_sets_status TEXT NOT NULL DEFAULT 'not_attempted'
                       CHECK (garmin_sets_status IN ('not_attempted','synced','failed')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (person_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_strength_sessions_person_start
    ON strength_sessions(person_id, start_time_utc);
```

Why the two additions: `session_label` is referenced by contract §4.6's `f"Cadence — {session_label}"` but defined nowhere, and D-015's name prefixing needs it; `garmin_target` makes the D-015 override auditable after the fact rather than log-only. Both nullable, both additive.

`UNIQUE (person_id, session_id)` is a **plain** unique constraint, not the partial index used for `client_id` — `session_id` is required, so there are no NULLs to exclude (contract §4.2).

**Do not** add to `_KNOWN_MIGRATIONS` (`shared/migrations.py:46`), `_REBUILD_TABLES`, or `tests/test_migrations.py`. The DDL block runs before any `run_migration` (`shared/database.py:453-468`), so a `003` apply-function would be a no-op on every path — and `assert_schema_understood()` boot-loops any older image that sees an unknown marker (`shared/migrations.py:367-372`), which would make a rollback refuse to start.

## 4. API surface

### 4.1 `POST /p/{slug}/api/activity`

`require_person("manage")` — matches every existing write route (`vitalforge-weight/app.py:361`). **The path carries the person; there is no `profile` field in the body.** `require_person` raises `RuntimeError` on a path with no `{slug}` placeholder precisely to kill body/query person addressing (`shared/auth.py:443-448`).

```python
@app.post("/p/{slug}/api/activity", status_code=202)
async def post_activity(data: ActivityIn, response: Response,
                        person_id: int = Depends(require_person("manage"))):
```

> **Mechanics gotcha:** `status_code=202` on the decorator applies to *every* return. The dedup branch must override it explicitly (`response.status_code = 200`, or return a `JSONResponse(status_code=200, …)`), or Cadence cannot tell "created" from "already had it" by status alone.

**Request model — `ActivityIn`, `ConfigDict(extra="forbid")`.** This table is duplicated verbatim in PRP-06 §4; the two must stay identical, because `extra="forbid"` turns any drift into a 422 that only the end-to-end test catches.

| Field | Type | Rule |
|---|---|---|
| `session_id` | `str` | required, `min_length=1, max_length=128` (same bounds as `client_id`, `vitalforge-weight/app.py:126`) |
| `session_label` | `str \| None` | optional, `max_length=60`, default `None` |
| `start` | `datetime` | required, **must carry a UTC offset**, ≤ 60 s in the future |
| `duration_min` | `int` | required, `ge=1, le=600` |
| `exercises` | `list[ActivityExerciseIn]` | required, `min_length=1, max_length=50` |
| `notes` | `str \| None` | `max_length=1000` |
| `source` | `Literal["cadence","pwa","manual"] \| None` | default `None` |
| `push_to_garmin` | `bool` | default **`False`** |
| `garmin_target` | `Literal["credential_person"] \| None` | default `None` |

`ActivityExerciseIn`, `extra="forbid"`:

| Field | Type | Rule |
|---|---|---|
| `name` | `str` | required, `max_length=100` |
| `garmin_category` | `str \| None` | optional; validated against `garminconnect.exercises.CATEGORIES` at the model layer → 422 on an unknown value |
| `garmin_exercise` | `str \| None` | optional sub-category; `None` always accepted under a known parent |
| `sets` | `int` | required, `ge=1, le=100` |
| `reps` | `int` | required, `ge=1, le=100` |
| `seconds` | `int \| None` | optional, `ge=1, le=3600` |
| `weight_kg` | `float \| None` | optional, `ge=0, le=500` |
| `rest_s` | `int \| None` | optional, `ge=0, le=3600` |

Three field-level rules the implementer must not skip:

- **`_reject_bool` on every numeric field**, copied from `vitalforge-weight/app.py:136-146`. Python's `bool` is an `int` subclass and Pydantic lax mode silently coerces `true` → `1`. This bit VitalForge before.
- **`start` validation is `_validate_captured_at` copied verbatim** (`vitalforge-weight/app.py:158-174`), reusing the existing `CAPTURED_AT_FUTURE_TOLERANCE_SECONDS = 60` constant. Naive datetimes are rejected.
- **`seconds` is new relative to contract §4.3** and exists because `reps` is required `ge=1` while planks, dead hangs, carries and the whole mobility prelude are time- or distance-measured. Time rows arrive as `reps=1, seconds=<hold>`. `create_manual_activity` ignores it; it feeds the `exerciseSets` `duration` when the flag is on. PRP-06 sends it under exactly this name.

**Responses.** Mirrors `post_weight`'s flat `{"success": true, …}` shape (`vitalforge-weight/app.py:702-745`), which is the only existing write route with a Garmin side effect. VitalForge has no single envelope convention (contract §4.4).

202, fresh insert:

```json
{"success": true, "id": 7, "session_id": "cadence-2026-09-06-a3f9", "person_id": 3,
 "start_time_utc": "2026-09-06T08:00:00+00:00", "duration_min": 42,
 "garmin_status": "synced", "garmin_activity_id": "19283746501",
 "garmin_sets_status": "not_attempted"}
```

200, idempotent repeat:

```json
{"success": true, "deduplicated": true, "id": 7,
 "session_id": "cadence-2026-09-06-a3f9", "garmin_status": "synced",
 "garmin_activity_id": "19283746501", "garmin_sets_status": "not_attempted"}
```

`garmin_error` is added to either shape whenever `garmin_status == "failed"`, carrying the caught exception's string (same treatment as `vitalforge-weight/app.py:742-743`). `garmin_target` is echoed when the D-015 override was applied.

| Code | Meaning |
|---|---|
| 202 | stored, fresh insert (Garmin outcome may still be `pending`/`failed`) |
| 200 | idempotent repeat, nothing created |
| 401 | no/invalid credential, from `auth_middleware`, `WWW-Authenticate: Bearer` |
| 404 | unknown slug **or** no grant — never distinguish them (`shared/auth.py:409-414`) |
| 409 | `push_to_garmin: true` for a non-credential person **without** `garmin_target` |
| 422 | Pydantic validation, including an unknown `garmin_category` |

202 is a new precedent in this codebase (no existing route returns it) and is deliberate — contract §4.4, open question 5.

### 4.2 Read routes

`GET /p/{slug}/api/activity/{session_id}` — `require_person("view")`. Returns the stored row with `exercises` parsed out of `exercises_json`, plus `garmin_status`, `garmin_activity_id`, `garmin_sets_status`, `garmin_error`. **404 when the `session_id` does not exist *for this person*** — scope the `WHERE` on `person_id` as well as the key (`vitalforge-dashboard/app.py:807-817`).

`GET /p/{slug}/api/strength-sessions?since=&limit=` — `require_person("view")`, returns `{"count": n, "sessions": [...]}` following `list_activities` (`vitalforge-dashboard/app.py:797`). `limit = Query(default=50, ge=1, le=200)`; `since` an ISO date compared against `start_time_utc`.

> The name is deliberate: `/api/activities` is already the FIT-import list route, and `?person=` is exactly the query-string addressing `require_person` raises `RuntimeError` to prevent (contract §4.7).

## 5. Implementation notes

### 5.1 Files touched

| File | Change |
|---|---|
| `shared/database.py` | `strength_sessions` DDL + index in the `init_db()` DDL block |
| `vitalforge-weight/app.py` | `ActivityIn`, `ActivityExerciseIn`, three routes, `_push_activity` helper, new import of `garmin_credential_person_id` from `shared.database` (contract §4.1 — not currently imported) |
| `shared/garmin_client.py` | `push_activity()`, and `push_activity_sets()` behind the flag |
| `tests/conftest.py` | `FakeGarminClient.create_manual_activity` / `.set_activity_exercise_sets`; patch both names in the app module |
| `.env.example` | two commented lines: `TZ` (already present, uncomment guidance) and `VITALFORGE_GARMIN_EXERCISE_SETS=0` |
| `CLAUDE.md` | one paragraph, §5.7 |

House style: ruff line-length 120, rules `E,F,W,I`, `E501` ignored, Python 3.12 (contract §1.1).

### 5.2 Idempotency algorithm

Directly mirrors `post_weight` (`vitalforge-weight/app.py:399-586`):

1. Open **`BEGIN IMMEDIATE`**. Everything through the insert happens inside it, so two concurrent requests can never both see "no duplicate".
2. `SELECT … FROM strength_sessions WHERE person_id = ? AND session_id = ?`.
3. **Hit** → do not insert, do not modify the stored payload. First-write-wins, like `ENRICHABLE_FIELDS` (`:515-524`). If the incoming body differs materially, **log a warning naming the fields; do not 409.** Optionally echo `"conflict": true, "conflict_fields": [...]`.
4. **Miss** → insert with `garmin_status = 'pending'` when `push_to_garmin` else `'skipped'`.
5. **`COMMIT`. Then** attempt Garmin, outside the transaction — the call is synchronous with no timeout mechanism (`:399-403`).
6. Record the outcome with a second `UPDATE … SET garmin_status, garmin_activity_id, garmin_error, garmin_sets_status, updated_at WHERE id = ?`, wrapped in `try/except` so a failure here never turns already-committed data into a 500 (`:694-698`).

**A repeat POST never creates a second Garmin activity.** The push is attempted only when the row was freshly inserted, or when the row matched **and** `garmin_status in ('pending','failed')` **and** `push_to_garmin` is true — the exact shape of `should_attempt_garmin_push` (`:631-633`).

**Do not build a retry worker.** VitalForge has none, for anything; `sync_status.backoff_until` is for *pulls*. Retry is client-driven: Cadence re-POSTs the same `session_id` and the push is re-attempted iff `garmin_status != 'synced'` (contract §4.5). Cadence bounds its own retries per D-016.

The `UNIQUE (person_id, session_id)` constraint is the DB-level backstop should a future write path bypass the transaction.

### 5.3 Garmin path — guaranteed

```python
client.create_manual_activity(
    start_datetime=start_local.strftime("%Y-%m-%dT%H:%M:%S.000"),  # LOCAL wall clock, no offset
    time_zone=os.environ.get("TZ", "UTC"),                         # IANA name
    type_key="strength_training",                                  # see §10.2 probe 3
    distance_km=0.0,
    duration_min=data.duration_min,
    activity_name=activity_name,
)
```

> **Timezone is the gotcha.** VitalForge stores UTC ISO strings everywhere (`vitalforge-weight/app.py:396-397`), but `create_manual_activity` wants a **local wall-clock string with no offset** plus a separate IANA zone (`__init__.py:2429-2430`). Store `start` as UTC; convert to the zone named by `TZ` **only at the moment of the call**; format with `"%Y-%m-%dT%H:%M:%S.000"`. Passing a UTC string with the local zone name, or a string carrying an offset, silently misfiles the activity by the offset amount — the same class of bug `push_weight`'s `strftime` without `%z` already caused (`:391-395`). If `TZ` is unset, fall back to `"UTC"` and **log it**. Do not guess the host zone.
>
> **VitalForge's `TZ` is the authoritative one for this conversion.** Cadence has its own `TZ` for display and always sends UTC with an explicit offset. Nobody adjusts the offset on the Cadence side.

**Activity name composition** (D-015, exact):

```python
label = data.session_label or "Strength"
if override_applied:                       # D-015 cross-person push
    activity_name = f"Cadence ({display_name}) — {label}"
else:
    activity_name = f"Cadence — {label}"
```

`display_name` is read from `persons.display_name` for the **target** `person_id` (never from the request body). The em dash is U+2014 with a single space either side. Example, verbatim from D-015: `Cadence (Son) — Lower A`.

### 5.4 Cross-person guard, with the D-015 override

The deployment holds **one** Garmin credential belonging to the primary person, and whatever it accepts is that one human's data regardless of which `person_id` the caller names (`shared/database.py:535-559`). `require_person` authorizes a caller *for a target person*; it cannot authorize them *for a data source*.

```python
override_applied = False
if data.push_to_garmin:
    source_person_id = await garmin_credential_person_id()
    if person_id != source_person_id:
        if data.garmin_target != "credential_person":
            raise HTTPException(status_code=409, detail=(
                "This person has no Garmin account of their own. The deployment holds one "
                "set of Garmin credentials, which belong to a different person, and pushing "
                "would file this session under theirs. Per-person Garmin linking arrives in "
                "Phase 3. Send garmin_target=\"credential_person\" to file it under the "
                "credential owner's account with this person's name in the activity title."))
        override_applied = True
        logger.warning(
            "D-015 override: session %s for person_id=%s filed under Garmin credential "
            "person_id=%s; activity name prefixed with display_name=%r",
            data.session_id, person_id, source_person_id, display_name)
```

Rules that follow from it:

- **409, not 404** — the caller demonstrably holds `manage` on this person, so naming the reason leaks nothing (`vitalforge-dashboard/app.py:309-311`).
- **Never silently downgrade to store-only.** An explicit `push_to_garmin: true` that quietly does nothing is worse than an error.
- `garmin_target` on its own does nothing: it is only consulted when `push_to_garmin` is true and the person differs. `garmin_target` with `push_to_garmin: false` is accepted and ignored (still stored, `garmin_status='skipped'`).
- The **row keeps the target `person_id`**. Only the Garmin filing goes under the credential owner. Set `garmin_target = 'credential_person'` on the row.
- `push_to_garmin: false` (the default) stores with `garmin_status = 'skipped'` and is the normal path for the kid profile.

### 5.5 Garmin path — enhancement, feature-flagged off

```python
_SETS_FLAG = os.environ.get("VITALFORGE_GARMIN_EXERCISE_SETS", "0").strip().lower() in {"1", "true", "yes", "on"}
```

> Parse it as a **string**. `bool(os.environ.get(...))` is `True` for `"0"`.

Default `0`, so **the shipped path is `create_manual_activity` only**. When the flag is on and the activity call returned an id, attempt `client.set_activity_exercise_sets(activity_id, {"exerciseSets": [...]})`, one entry per set, `exercises: [{"category": …, "name": … or None}]`. Omit `garmin_category` → **skip that exercise's sets rather than guessing a category**.

The payload's exact JSON keys are **unverified** — a grep for `repetitionCount` / `setType` / `exerciseSets` across the whole installed `garminconnect` 0.3.11 tree returns only the two method definitions (contract §3.3 item 1). The believed shape:

```json
{"exerciseSets": [{"setType": "ACTIVE", "startTime": "2026-09-06T08:00:00.0",
  "duration": 45.0, "repetitionCount": 10, "weight": 20000.0,
  "exercises": [{"category": "BENCH_PRESS", "name": null}]}]}
```

Weight is *believed* grams (`kg * 1000.0`, matching `workout.py:494`) and `duration` seconds. Keep the builder in one function so JD's probe result changes one place. Do not enable the flag in any committed file.

Also unverified: whether Garmin accepts exercise sets on a *manually created* activity at all (contract §3.3 item 2). That is why `garmin_sets_status` is a column, not an architectural assumption.

### 5.6 Failure handling

Wrap both calls in a helper mirroring `_push_composition` (`vitalforge-weight/app.py:339-357`): **never raises, returns an error string or `None`.**

| Failure | Effect |
|---|---|
| `create_manual_activity` raises | `garmin_status='failed'`, message in `garmin_error`, response still 202 |
| returns no usable activity id | `garmin_status='synced'`, `garmin_activity_id=None`, sets step skipped. `get_last_activity()` is racy — document it as a fallback, do not make it the path (contract §3.3 item 5) |
| `set_activity_exercise_sets` raises | `garmin_sets_status='failed'` only; `garmin_status` stays `'synced'`, because the activity really is on Garmin |
| the post-commit `UPDATE` raises | logged, swallowed; the request still succeeds |

`garminconnect` raises `GarminConnectTooManyRequestsError` on 429 with **no** automatic backoff, and a tight retry loop can get the shared credential IP-blocked — which breaks weight logging too, same module-level client (contract §5.9). VitalForge does not retry; Cadence bounds it (D-016).

### 5.7 Docs

- `.env.example`: add `VITALFORGE_GARMIN_EXERCISE_SETS=0` with a one-line comment, and make the `TZ` line's role explicit ("also used for the Garmin activity wall-clock conversion"). `tests/test_docs_drift.py` does **not** require a README change, so **do not touch `README.md`** unless a new assertion is added; if one is, add `test_env_example_documents_exercise_sets_flag` asserting the flag name is present.
- `CLAUDE.md`: one paragraph — "Two activity concepts share one database: `activities` (FIT imports, read by the dashboard) and `strength_sessions` (Cadence sessions, written by the weight service). They are deliberately separate. Do not unify them." (contract §5.10.)

### 5.8 Mocking — the easy thing to get wrong

`vitalforge-weight/app.py` imports Garmin helpers **by name** (`from shared.garmin_client import authenticate, push_weight`, `:30`), so patching `shared.garmin_client` alone does **not** reach them. `tests/conftest.py:295-305` says so explicitly and patches the names in the app module's own namespace. **Any new `push_activity` helper needs the same treatment or the tests silently reach the real Garmin client.**

`FakeGarminClient` (`tests/conftest.py:49-87`) gains:

```python
def create_manual_activity(self, **kwargs):
    self.created_activities.append(kwargs)
    return {"activityId": 19283746501}

def set_activity_exercise_sets(self, activity_id, payload):
    self.pushed_exercise_sets.append({"activity_id": activity_id, "payload": payload})
    return {"success": True}
```

recording the way `add_body_composition` records into `pushed_weights` (`:60-62`). Harness: `httpx.AsyncClient` over `ASGITransport(app=module.app)` with the `weight_app_module` fixture and `PERSON_PREFIX` (`tests/conftest.py:37`), as in `tests/test_client_id_idempotency.py:26-30`.

## 6. Acceptance tests

`tests/test_activity_api.py` unless noted. Modelled on the files in contract §4.8.

1. `test_post_activity_fresh_returns_202` — 202, `success`, `id`, `session_id`, `person_id`, `garmin_status == "skipped"` with the default `push_to_garmin`.
2. `test_repeat_post_returns_200_deduplicated` — same body twice → 202 then **200** with `deduplicated: true` and the same `id`. **Guards the `status_code=202` decorator trap.**
3. `test_repeat_post_does_not_insert_second_row` — one row in `strength_sessions`.
4. `test_repeat_post_does_not_create_second_garmin_activity` — `len(fake.created_activities) == 1`.
5. `test_repeat_post_retries_a_previously_failed_push` — first call's Garmin raises → `garmin_status='failed'`; second call succeeds → `'synced'`, still one row. Model: `test_client_id_match_retries_a_previously_failed_garmin_push:195`.
6. `test_repeat_post_does_not_repush_when_already_synced` — `garmin_status='synced'` → `len(fake.created_activities) == 1`.
7. `test_first_write_wins_on_differing_body` — second POST with different `duration_min` does not overwrite; a warning naming `duration_min` is logged.
8. `tests/test_activity_concurrency.py::test_two_concurrent_identical_posts_insert_once_push_once` — `asyncio.gather` of two identical POSTs → one row, one Garmin call, one 202 and one 200. Model: `tests/test_dedup_concurrency.py`.
9. `test_unique_constraint_rejects_duplicate_session_id_for_same_person` — raw `INSERT` of a duplicate raises `IntegrityError`. Model: `test_client_id_idempotency.py:462-508`. **Negative.**
10. `test_same_session_id_allowed_for_different_persons` — the constraint is `(person_id, session_id)`, not `session_id`.
11. `test_naive_start_rejected_422` — `start` without an offset. **Negative.**
12. `test_future_start_beyond_tolerance_rejected_422` — `start` +120 s. **Negative.**
13. `test_start_within_60s_future_accepted` — +30 s accepted (boundary).
14. `test_unknown_garmin_category_rejected_422` — `"BURPEE_OF_DOOM"` → 422 before any Garmin call. **Negative.**
15. `test_bool_rejected_for_numeric_fields` — parametrised over `duration_min`, `sets`, `reps`, `seconds`, `weight_kg`, `rest_s` with `true` → 422 each. **Guards `_reject_bool`. Negative.**
16. `test_extra_field_rejected_422` — `{"profile": "me"}` in the body → 422, proving no body-based person addressing. **Negative.**
17. `test_empty_exercises_rejected_422` and `test_51_exercises_rejected_422`. **Negative.**
18. `tests/test_activity_auth.py::test_unauthenticated_returns_401` — with a populated `users` table, no header → 401 + `WWW-Authenticate: Bearer`. **Negative.**
19. `::test_unknown_slug_returns_404` and `::test_no_grant_returns_404` — identical bodies, never 403. Model: `tests/test_require_person.py`. **Negative.**
20. `::test_view_grant_cannot_post` — a `view`-only grant → 404 (not 403). **Negative.**
21. `::test_cross_person_read_isolated` — person A's `session_id` fetched under person B's slug → 404. Model: `tests/test_idor_by_row_id.py`. **Negative.**
22. `tests/test_activity_garmin_guard.py::test_cross_person_push_returns_409` — `push_to_garmin: true`, non-credential person, no `garmin_target` → 409, **no Garmin call made**, and no row's `garmin_status` becomes `synced`. **Negative.**
23. `::test_cross_person_push_with_override_succeeds` — same plus `garmin_target: "credential_person"` → 202, one Garmin call, row `garmin_target == 'credential_person'`.
24. `::test_override_prefixes_activity_name_with_display_name` — asserts the recorded `activity_name == "Cadence (Son) — Lower A"` for `display_name="Son"`, `session_label="Lower A"`. **Exact string.**
25. `::test_override_logs_a_warning` — `caplog` contains `"D-015 override"`, the target and credential `person_id`s.
26. `::test_override_is_ignored_when_push_false` — stored, `garmin_status='skipped'`, no Garmin call.
27. `::test_same_person_push_needs_no_override` — credential person, no `garmin_target` → 202 synced, name `"Cadence — Lower A"`.
28. `::test_no_session_label_defaults_to_strength` — name `"Cadence — Strength"`.
29. `tests/test_activity_garmin_time.py::test_start_converted_to_local_wall_clock` — `TZ=Europe/Paris`, `start=2026-09-06T08:00:00+00:00` → recorded `start_datetime == "2026-09-06T10:00:00.000"` and `time_zone == "Europe/Paris"`. **The single highest-value test in this PRP.**
30. `::test_start_string_carries_no_offset` — the recorded string matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000$`. **Negative.**
31. `::test_tz_unset_falls_back_to_utc_and_logs` — no `TZ` → `time_zone == "UTC"` and a log line says so.
32. `::test_stored_start_time_is_utc` — the DB row keeps the UTC ISO string regardless of `TZ`.
33. `tests/test_activity_failure.py::test_garmin_failure_still_returns_202` — the client raises → 202, `garmin_status='failed'`, `garmin_error` non-empty, row present. **Negative.**
34. `::test_sets_failure_leaves_activity_synced` — flag on, sets call raises → `garmin_status='synced'`, `garmin_sets_status='failed'`. **Negative.**
35. `::test_post_commit_update_failure_does_not_500` — patch the second `UPDATE` to raise → still 202. **Negative.**
36. `tests/test_activity_flag.py::test_sets_not_attempted_by_default` — flag unset → `set_activity_exercise_sets` never called, `garmin_sets_status == 'not_attempted'`.
37. `::test_flag_string_zero_is_false` — parametrised `"0"`, `""`, `"false"`, `"off"` → disabled; `"1"`, `"true"`, `"yes"`, `"on"` → enabled. **Guards `bool("0") is True`. Negative.**
38. `::test_exercise_without_category_is_skipped_in_sets` — flag on, one exercise with `garmin_category=None` → that exercise contributes no set entries, no guessed category. **Negative.**
39. `tests/test_activity_read.py::test_get_activity_by_session_id` — returns the row with `exercises` parsed from JSON.
40. `::test_get_unknown_session_id_404`. **Negative.**
41. `::test_list_strength_sessions_shape` — `{"count", "sessions"}`; `limit=0` and `limit=201` → 422. **Negative.**
42. `tests/test_garmin_client_api.py` (**extend the existing file**) — `inspect.signature` assertions that `create_manual_activity` takes `start_datetime, time_zone, type_key, distance_km, duration_min, activity_name` and `set_activity_exercise_sets` takes `activity_id, payload`, against the real installed library. This is the version-bump tripwire; the file exists for exactly this.
43. `tests/test_no_unscoped_person_access.py` (**existing, must still pass**) — the AST test proves no new route calls `get_primary_person_id()`.
44. `tests/test_migrations.py` (**existing, must still pass unchanged**) — proves no marker was added.

## 7. Devil's-advocate risks

1. **Someone adds a `003` migration marker "for tidiness".** A rollback to a pre-Cadence image then boot-loops (`shared/migrations.py:367-372`). → §3 forbids it; test 44 is the guard.
2. **Timezone misfiling.** A UTC string sent with a local zone name silently shifts the activity by the offset and nobody notices for weeks. → Tests 29–32, and the conversion lives in one function.
3. **Mock patched in the wrong namespace, so tests hit real Garmin.** → §5.8; add an autouse fixture that raises if `shared.garmin_client._client` is a real `Garmin` instance during any test.
4. **The dedup path answers 202 because the decorator's status wins.** Cadence then treats every retry as a fresh create. → Test 2.
5. **`bool` coerced to `1`.** `push_to_garmin` is genuinely bool, but `sets`/`reps` are not. → Test 15.
6. **The D-015 override becomes the default.** A misconfigured Cadence sends it on every session and the parent's Garmin fills with the kid's workouts. → Cadence sends it only when `push_son_to_garmin` is on (PRP-06); server-side it is logged at WARNING and recorded in `garmin_target`; test 26 proves it is inert without `push_to_garmin`.
7. **409 quietly becomes store-only.** → Explicitly forbidden in §5.4; test 22 asserts the 409 and that no Garmin call happened.
8. **The unverified `exerciseSets` payload ships on by default and 400s every session.** → Flag default `0`, tests 36–37.
9. **`create_manual_activity` returns no id and the code assumes a dict key.** → §5.6 row 2; never index blindly, and never fall back to `get_last_activity()` silently.
10. **A large backdate is rejected by Garmin** (contract §3.3 item 4, same unknown VitalForge already flags for weight). → Failure is non-fatal by §5.6; JD's probe 4 settles it.
11. **Concurrent double-POST from the offline replay queue inserts twice.** → `BEGIN IMMEDIATE` plus `UNIQUE (person_id, session_id)`; tests 8 and 9.
12. **Two workers break the post-commit flag update** (contract §5.12). → Inherited single-worker assumption; state it in a code comment next to the update.
13. **A `session_id` collision across persons is treated as a duplicate.** → Test 10.
14. **Someone reuses the `activities` table later.** → `CLAUDE.md` paragraph, §5.7.

## 8. Done when

- [ ] Branch `cadence/activity-endpoint` exists in `../vitalforge`, based on `fix/a6-review-followups`, **never pushed**, `main` untouched (`git log main..HEAD` non-empty, `git log HEAD..main` empty).
- [ ] `strength_sessions` created by `init_db()` on both a fresh DB and an existing one; `_KNOWN_MIGRATIONS` unchanged (`git diff main -- shared/migrations.py` empty).
- [ ] All three routes live, with the 202/200/409/422 matrix in §4.1.
- [ ] `uv run pytest` (or the repo's runner) green — the **whole** suite, not just the new files.
- [ ] `ruff check .` clean at line-length 120.
- [ ] All 44 acceptance tests present and passing.
- [ ] `VITALFORGE_GARMIN_EXERCISE_SETS` absent or `0` in every committed file.
- [ ] No credential, token, or Garmin email appears in the diff; `../vitalforge/.env` never read or opened.
- [ ] `CLAUDE.md` paragraph added.

### 8.1 Deferred to JD — live probe on VM-201

The real Garmin account cannot be reached from the build environment, so these stay open. Contract §5 items 1–4, in the order JD should run them:

- [ ] **Probe 1 — `exerciseSets` payload shape.** On a real account, find an existing strength activity and call `get_activity_exercise_sets(activity_id)`; record the exact response. The `set_…` docstring says the request takes the same shape (`__init__.py:2983-2984`), so this one call settles the field names, the weight unit (grams vs kg) and the duration unit.
- [ ] **Probe 2 — do exercise sets stick on a *manually created* activity?** Create one via `create_manual_activity`, then `set_activity_exercise_sets` on it, then read it back. If Garmin only accepts sets on FIT-sourced activities, the enhancement path needs a real FIT file and `upload_activity` — a much larger piece of work. Leave the flag at `0` if this fails.
- [ ] **Probe 3 — is `"strength_training"` a valid activity `typeKey`?** It is confirmed only as a *workout* `sportTypeKey` (`workout.py:296`). One call to `get_activity_types()` (`__init__.py:2713`) confirms it; pin the answer in a test.
- [ ] **Probe 4 — does Garmin accept a large backdate** on a manual activity? Push one dated a week back and check it lands.

Until probes 1–3 come back, the shipped path is `create_manual_activity` only, and that is a complete, working feature.
