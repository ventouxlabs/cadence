# PRP-06 — VitalForge client (Cadence side)

## 1. Goal

Build `cadence/vitalforge/` — the read half (metrics cache + readiness nudge) and the write half (activity payload + bounded retry queue) of the VitalForge integration. On Done, a session becomes a `sync_job`, is POSTed once inline, and is retried on a bounded backoff by an in-process periodic task until VitalForge accepts it. Every network call is mocked in tests; the app degrades gracefully when VitalForge is down, unconfigured, or has not yet deployed PRP-05's endpoint.

## 2. Scope

**In**

- `cadence/vitalforge/client.py`, `metrics.py`, `payload.py`, `sync.py`, `errors.py`, `__init__.py`.
- `metrics_cache` and `sync_job` tables (`docs/architecture.md` §3).
- `GET /api/metrics?profile=` and `POST /api/sync/retry` in `cadence/api/`.
- The lifespan periodic drain task in `cadence/main.py` (5 min).
- The sync-status line on the Done screen (`cadence/web/templates/done.html`).
- `tests/test_vitalforge_client.py`, `test_metrics.py`, `test_payload.py`, `test_sync.py`, `tests/e2e/test_session_to_activity.py`.

**Out**

| Not this PRP | Where |
|---|---|
| Any VitalForge-side code, the `/api/activity` route itself | PRP-05 |
| The Done screen's layout, the 3-line summary, the felt toggle | PRP-02 |
| The History trend line's rendering | PRP-04 (this PRP supplies the cached data) |
| OmniRoute / AI | PRP-08 |
| Person-slug editing UI | PRP-03 (this PRP reads `profile.vitalforge_person`) |
| Upgrading `/api/health` to probe VitalForge | **forbidden** — see §5.7 |

## 3. Data model

Two tables from `docs/architecture.md` §3, created here.

`metrics_cache`

| Column | Type | Notes |
|---|---|---|
| `profile_id` | TEXT PK | `me` / `son` |
| `fetched_at` | TEXT | UTC ISO with offset |
| `payload_json` | TEXT | the `ProfileMetrics` dump (§4.1) |
| `stale` | BOOL | `True` once older than 6 h, or when the last refresh failed |

One row per profile, replaced wholesale on each refresh. **A failed refresh never deletes or blanks the row** — it flips `stale` and keeps the last good payload.

`sync_job`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | uuid4 |
| `session_id` | TEXT | **unique** — the DB backstop for get-or-create |
| `target` | TEXT | `vitalforge` |
| `status` | TEXT | `pending` / `sent` / `failed` / `skipped` |
| `attempts` | INT | default 0 |
| `next_attempt_at` | TEXT \| NULL | **`NULL` means terminal, never retry again** |
| `last_error` | TEXT \| NULL | operator-readable, never contains the token |
| `payload_json` | TEXT | the exact `ActivityIn` body that was sent |
| `remote_ref` | TEXT \| NULL | VitalForge's row `id` from the response |

Status semantics, stated once so the UI and the drainer agree:

| `status` | `next_attempt_at` | Meaning |
|---|---|---|
| `pending` | set | created or awaiting the next attempt |
| `sent` | NULL | VitalForge returned 202 or 200 |
| `failed` | set | last attempt failed, will retry |
| `failed` | NULL | terminal: 409, 422, or attempts exhausted |
| `skipped` | NULL | no `VITALFORGE_TOKEN` configured; stored locally only |

`ADD UNIQUE INDEX ux_sync_job_session_id ON sync_job(session_id)`.

## 4. API / UI surface

### 4.1 `GET /api/metrics?profile=me|son`

Reads `metrics_cache` only. **No network call on this route** — the periodic task refreshes; `docs/architecture.md` §5 makes Today the hot path.

```json
{"ok": true,
 "data": {"profile": "me", "fetched_at": "2026-09-06T07:31:00+00:00", "stale": false,
          "weight_kg": 84.1, "body_fat": 18.2, "muscle_mass_kg": 34.5,
          "resting_hr": 52, "sleep_score": 76, "body_battery": 61,
          "readiness": {"score": 72, "status": "ok"},
          "nudge": "Good day to push"},
 "error": null, "meta": {"source": "cache"}}
```

Youth profile — **body composition is never present, not even as `null`**, so a template bug cannot render it:

```json
{"ok": true,
 "data": {"profile": "son", "fetched_at": "...", "stale": false,
          "readiness": {"score": null, "status": "insufficient_data"},
          "nudge": "Readiness not available"},
 "error": null, "meta": {"source": "cache"}}
```

Unknown profile → 404 with the envelope's `error` set. Cache miss → 200 with `data.stale = true` and every metric absent.

### 4.2 `POST /api/sync/retry`

Drains the queue now. Body optional `{"session_id": "..."}` to drain one job. Returns `{"ok": true, "data": {"drained": 3, "sent": 2, "failed": 1, "skipped": 0}, ...}`. Ignores `next_attempt_at` when a `session_id` is given (an explicit user retry beats the backoff); respects it otherwise.

### 4.3 Done screen sync line

`GET /done/{session_id}` renders one line under the 3-line summary, from the session's `sync_job`:

| Condition | Line |
|---|---|
| `sent` | `synced ✓` |
| `pending`, or `failed` with `attempts == 0` (the 404 / 401 cases, which do not burn attempts) | `will sync` |
| `failed`, `next_attempt_at` set, `attempts >= 1` | `sync failed (retrying)` |
| `failed`, `next_attempt_at IS NULL` | `sync failed` + a **Retry** button posting to `/api/sync/retry` |
| `skipped` | `stored locally — VitalForge not configured` |

No spinner, no polling loop. The line is static HTML; the Retry button is one HTMX post swapping the same line.

> This is **not** PRP-02's offline banner. That banner ("Saved on this phone — will sync") reports the browser's IndexedDB queue of un-POSTed ticks; this line reports the server-side `sync_job` to VitalForge. Both can be visible at once and they mean different things. Keep the two strings distinct.

### 4.4 Wire contract — `ActivityIn`

Reproduced **verbatim** from PRP-05 §4.1 rather than cross-referenced, because the VitalForge model is `extra="forbid"` and any drift between the two implementers becomes a 422 that only the end-to-end test catches. If these two tables ever disagree, PRP-05 is the server and wins.

| Field | Type | Rule |
|---|---|---|
| `session_id` | `str` | required, `min_length=1, max_length=128` |
| `session_label` | `str \| None` | optional, `max_length=60` |
| `start` | `datetime` | required, **must carry a UTC offset**, ≤ 60 s in the future |
| `duration_min` | `int` | required, `ge=1, le=600` |
| `exercises` | `list[ActivityExerciseIn]` | required, `min_length=1, max_length=50` |
| `notes` | `str \| None` | `max_length=1000` |
| `source` | `Literal["cadence","pwa","manual"] \| None` | Cadence always sends `"cadence"` |
| `push_to_garmin` | `bool` | default `False` |
| `garmin_target` | `Literal["credential_person"] \| None` | default `None` (D-015) |

| `exercises[]` field | Type | Rule |
|---|---|---|
| `name` | `str` | required, `max_length=100` |
| `garmin_category` | `str \| None` | validated server-side against `garminconnect.exercises.CATEGORIES`; **no `UNKNOWN` member exists** |
| `garmin_exercise` | `str \| None` | optional sub-category |
| `sets` | `int` | required, `ge=1, le=100` |
| `reps` | `int` | required, `ge=1, le=100` |
| `seconds` | `int \| None` | optional, `ge=1, le=3600` |
| `weight_kg` | `float \| None` | optional, `ge=0, le=500` |
| `rest_s` | `int \| None` | optional, `ge=0, le=3600` |

Booleans are rejected for every numeric field server-side (`_reject_bool`), so `"sets": true` is a 422, not a silent `1`. Omit an optional key rather than sending `null` wherever "absent" is meant.

## 5. Implementation notes

### 5.1 `client.py`

```python
class VitalForgeClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None)
    async def get_recent_weight(self, slug: str) -> list[dict]
    async def get_metric(self, slug: str, name: str, days: int = 30) -> dict
    async def get_readiness(self, slug: str) -> dict
    async def post_activity(self, slug: str, payload: dict) -> ActivityResult
```

| Rule | Detail |
|---|---|
| Base URLs | `get_recent_weight` and `post_activity` → `settings.vitalforge_weight_url` (:8085). `get_metric` and `get_readiness` → `settings.vitalforge_dashboard_url` (:8086). They are **separate settings** (`docs/architecture.md` §1) |
| Paths | `/p/{slug}/api/weight/recent`, `/p/{slug}/api/metrics/{name}?days=`, `/p/{slug}/api/readiness`, `/p/{slug}/api/activity` |
| Auth | `Authorization: Bearer <VITALFORGE_TOKEN>`, scheme match is case-insensitive server-side. **Never borrow the session cookie** (contract §1.3) |
| Timeout | `httpx.Timeout(5.0)` on every call |
| Token safety | The header is built inside the request call. **Never log the header, the client's `headers` dict, or a `repr` of the request.** `settings.vitalforge_token` is a `SecretStr`; call `.get_secret_value()` at the single point of use |
| Errors | Never raises `httpx` out of the module. Wrap into `VitalForgeUnavailable` (transport/timeout), `VitalForgeHTTPError(status_code, body_excerpt)` (4xx/5xx), `VitalForgeNotConfigured` (blank token or blank slug) — `errors.py` |
| Body excerpt | Truncate any response body to 500 chars before it reaches `last_error`, and strip anything matching `Bearer\s+\S+` |
| Mock mode | `settings.cadence_vitalforge_mode == "mock"` → the client returns canned fixtures and makes no socket call. PRP-09's smoke script relies on this |
| Slug | From `profile.vitalforge_person`, seeded from `VITALFORGE_PERSON_ME` / `VITALFORGE_PERSON_SON` (D-017). Blank slug → `VitalForgeNotConfigured`, never a request to `/p//api/...` |

```python
@dataclass(frozen=True)
class ActivityResult:
    status_code: int
    deduplicated: bool          # response body's "deduplicated" is truthy
    garmin_status: str | None   # skipped | pending | synced | failed
    remote_id: str | None       # response "id"
    error: str | None           # detail/message on a non-2xx, sanitised
```

### 5.2 `metrics.py`

`async def refresh_metrics(profile: Profile, client: VitalForgeClient) -> ProfileMetrics` — fetches, maps, writes `metrics_cache`, returns the new payload. Never raises: on failure it flips `stale = True`, logs at WARNING and returns the cached payload (or an empty one).

| Field | Source | Conversion |
|---|---|---|
| `weight_kg` | `metrics/weight` latest `value` | **÷ 1000** — `weight_history.weight_grams` |
| `body_fat` | `metrics/body_fat` latest `value` | none, already a percentage |
| `muscle_mass_kg` | `metrics/muscle_mass` latest `value` | **÷ 1000** — `weight_history.muscle_mass_g` |
| `resting_hr` | `metrics/resting_hr` latest `value` | none |
| `sleep_score` | `metrics/sleep_score` latest `value` | none |
| `body_battery` | `metrics/body_battery` latest `value` | none |
| `readiness` | `readiness` | `{score, status}` only; drop `components` |

> **Unit trap, contract §2.2.** `weight_log` (what a POST writes) carries `bone_mass_kg` and `muscle_pct`; `weight_history` (what `metrics/{name}` reads) carries `weight_grams`, `bone_mass_g` and `muscle_mass_g`. The `_g`/`_kg`/`_pct` suffix is load-bearing. **Both `weight` and `muscle_mass` need the ÷1000.** The brief asks for "muscle %"; this endpoint only offers mass, so Cadence stores `muscle_mass_kg` and, if a percentage is ever displayed, derives it as `muscle_mass_kg / weight_kg`. Stated assumption; do not invent a `muscle_pct` read.
>
> Do **not** use `weight/recent` for body composition — it returns only `weight_lbs`, `weight_kg`, `timestamp`, `synced_to_garmin` (contract §2.1). It is available for a "logged today" check and nothing more.

Each metric is a separate request. One failing metric must not blank the others: gather with `return_exceptions=True`, keep whatever came back, mark `stale` only if **all** requests failed.

**Staleness.** `stale = (now - fetched_at) > 6 h`, evaluated on read as well as on write, so a cache that ages between refreshes reports honestly.

**Youth profile.** `refresh_metrics` for `profile.kind == "youth"` calls **only** `get_readiness`. It never calls `metrics/weight`, `body_fat`, or `muscle_mass`, and `ProfileMetrics` for a youth profile has those fields **absent**, not `None` (`docs/architecture.md` §5: the youth profile never sees weight, body-fat, muscle %, or a trend line). Enforce it in `metrics.py`, not only in the template.

**Readiness nudge** — one line, from `readiness.score`:

| Condition | Nudge |
|---|---|
| `score >= 70` | `Good day to push` |
| `50 <= score < 70` | `Steady day` |
| `score < 50` | `Easy day — hold loads` |
| `score is None`, `status == "insufficient_data"`, or the call failed | `Readiness not available` |

> The kid's readiness will be `null` **permanently** — one Garmin credential, one person, and `MIN_BASELINE_DAYS = 5` of trailing data never arrives for the second person (contract §2.3). Render "not available". **Never coerce `None` to 0**, never crash. A 500 from that route is a genuine failure and is logged as one.

### 5.3 `payload.py`

`def build_activity_payload(session, rows, profile, settings, exercises) -> dict`

| Field | Value |
|---|---|
| `session_id` | `session.id` (uuid4, also the VitalForge key) |
| `session_label` | the planned session's `day_type` title-cased, e.g. `"Lower A"`; ≤60 chars |
| `start` | `session.started_at` as UTC ISO **with the `+00:00` offset** |
| `duration_min` | `max(1, round((finished_at - started_at).total_seconds() / 60))` |
| `exercises` | from `rows` (below) |
| `notes` | `felt` label + the progression engine's "next time" note, joined by `" · "`, ≤1000 |
| `source` | `"cadence"` |
| `push_to_garmin` | **`me` → `profile.push_to_garmin`. `son` → the `push_son_to_garmin` setting, full stop.** Architecture §5 makes the setting a *replacement* for the youth profile, not an extra `and`: reading it as a conjunction would let a seeded `profile.push_to_garmin = False` pin the son off while the setting says on |
| `garmin_target` | `"credential_person"` **only** when `profile.kind == "youth"` **and** `push_to_garmin` is true (D-015); otherwise the key is **omitted**. Cadence does **not** look up who owns the Garmin credential — `garmin_credential_person_id()` is server-side only and has no route. The youth profile is the one that is never the credential owner, and that is the whole condition |

Row → exercise entry. **Include a row only if `row.done` or `row.sets_done > 0`.**

| Key | Value |
|---|---|
| `name` | the exercise doc's `name` |
| `garmin_category` | the exercise doc's `garmin_category`; **omit the key when it is `None`** (D-018 — there is no `UNKNOWN` category and emitting one earns a Garmin 400) |
| `garmin_exercise` | the doc's `garmin_exercise` if set, else omitted |
| `sets` | `sets_done or sets_planned`, clamped to 1–100 |
| `reps` | `reps_done or reps_planned or 1` — time and distance rows send `reps: 1` |
| `seconds` | `seconds_done or seconds_planned`, omitted when absent |
| `weight_kg` | `load_done_kg` if not `None`, else `load_planned_kg`; omitted when both are `None` |
| `rest_s` | the planned `rest_s`, omitted when `None` |

`max_length=50` on `exercises` — if a session somehow has more done rows, truncate to the first 50 and log a WARNING; a 422 would lose the session.

> **The request model is `extra="forbid"` on the VitalForge side.** The field table in PRP-05 §4.1 is the same table; any key not in it is a hard 422. Do not add fields "for debugging".
>
> **Cadence always sends UTC with an offset.** The local wall-clock conversion for Garmin happens server-side in VitalForge using VitalForge's `TZ`. Cadence's own `TZ` is display-only. Do not adjust the offset on this side.

### 5.4 `sync.py`

```python
def enqueue(session_id, payload, *, settings) -> SyncJob   # get-or-create
async def attempt(job, client) -> SyncJob                  # one POST, returns the new job
async def drain(*, limit: int = 20, force_session_id: str | None = None) -> DrainReport
```

**`enqueue` is get-or-create on `session_id`, not an insert.** `POST /api/sessions/{id}/done` is idempotent and is the offline-queue replay target (`docs/architecture.md` §4), so a blind insert duplicates the job. Return the existing job untouched if one exists.

Flow on Done: `enqueue` → **one inline `attempt` with the 5 s timeout** → whatever the result, the response returns immediately. The Done screen never waits on a retry.

**Backoff, D-016** — bounded, client-driven re-POST of the same `session_id`:

```
delay_minutes(attempts) = min(2 ** (attempts - 1), 120)   # 1, 2, 4, 8, 16, 32, 64, 120
MAX_ATTEMPTS = 8
```

Outcome table:

| Result | `status` | `attempts` | `next_attempt_at` |
|---|---|---|---|
| 202 or 200 | `sent` | +1 | NULL; store `remote_ref` |
| 409 | `failed` | +1 | **NULL — terminal.** `last_error` = the 409 detail. Cross-person push needs a settings change, not a retry (D-016) |
| 422 | `failed` | +1 | **NULL — terminal.** The payload is wrong; retrying cannot fix it |
| **404** | `failed` | **unchanged** | now + 2 h. `last_error` = `"VitalForge has no /api/activity yet — deploy the cadence/activity-endpoint branch"` |
| 401/403 | `failed` | unchanged | now + 2 h. `last_error` = `"VitalForge rejected the token"` |
| 5xx, timeout, connection error | `failed` | +1 | now + `delay_minutes(attempts)`, unless `attempts >= MAX_ATTEMPTS` → NULL |
| no token configured | `skipped` | unchanged | NULL |

404 and 401 do **not** burn the 8-attempt budget: neither is the session's fault, and a deploy or a token fix should find the job still alive. Everything else is capped.

**Why bounded matters.** `garminconnect` raises on 429 with **no** automatic backoff and VitalForge's wrapper adds none. A tight retry loop can get the shared Garmin credential IP-blocked, which breaks JD's weight logging too, since it is the same module-level client (contract §5.9). Never retry a 409 automatically.

**No token configured** → `status = "skipped"` and exactly this log line, once per job, at WARNING:

```
VitalForge token not configured — session stored locally only
```

**Periodic drain.** A lifespan `asyncio.Task` calls `drain()` every 5 minutes and also refreshes `metrics_cache` for each profile. It must: catch every exception per iteration and keep looping; skip entirely in `mock` mode is **not** allowed (mock mode still exercises the loop, it just gets canned responses); be cancelled cleanly on shutdown with `contextlib.suppress(asyncio.CancelledError)`. `drain()` selects jobs with `status in ('pending','failed')` and `next_attempt_at <= now`, ordered oldest first, `LIMIT 20`.

### 5.5 Tolerating a VitalForge without PRP-05

`/api/activity` returning **404** is the expected state until JD deploys the `cadence/activity-endpoint` branch. It must not look like a bug: the job survives on the 2-hour schedule, the Done screen says `will sync`, and `last_error` names the branch. A 404 from a *read* endpoint is different and simply leaves the cache stale.

### 5.6 Test doubles

**Use `respx`** (pinned in PRP-00's dev deps as `respx>=0.21,<0.23`), not a hand-rolled `MockTransport`. One choice, both PRPs. `respx.mock(base_url=...)` per service; assert on `route.calls.last.request.content` for payload tests.

An autouse fixture asserts no real socket is opened: `respx.mock(assert_all_mocked=True)` at session scope, so an unmocked host raises instead of hanging on a 5 s timeout in CI.

### 5.7 Forbidden

`GET /api/health` stays a **static config read** — `{"configured": bool(token), "mode": …}` (PRP-00 §6). Do **not** make it probe VitalForge: compose polls it every 30 s (PRP-09) and a 5 s timeout there turns a VitalForge outage into a Cadence restart loop.

## 6. Acceptance tests

1. `tests/test_vitalforge_client.py::test_bearer_header_sent` — the recorded request has `Authorization: Bearer <token>`.
2. `::test_token_never_logged` — `caplog.text` contains neither the token nor the string `Bearer` after a successful call, a 500, and a timeout. **Negative.**
3. `::test_uses_weight_url_for_activity_and_dashboard_url_for_metrics` — two different hosts recorded. **Guards the split-service trap.**
4. `::test_timeout_is_five_seconds` — the client's `timeout.read == 5.0`.
5. `::test_transport_error_raises_vitalforge_unavailable` — `httpx.ConnectError` → `VitalForgeUnavailable`, not an `httpx` exception. **Negative.**
6. `::test_blank_slug_never_issues_request` — `VitalForgeNotConfigured` and `respx` records zero calls. **Negative.**
7. `::test_error_body_is_truncated_and_sanitised` — a 500 body of 5 000 chars containing `Bearer abc123` yields a `last_error` ≤500 chars with no `abc123`. **Negative.**
8. `::test_mock_mode_makes_no_request` — `mode="mock"` → zero recorded calls, canned data returned.
9. `tests/test_metrics.py::test_weight_divided_by_1000` — `metrics/weight` value `84100` → `weight_kg == 84.1`.
10. `::test_muscle_mass_divided_by_1000` — value `34500` → `muscle_mass_kg == 34.5`. **The trap most likely to ship wrong.**
11. `::test_body_fat_not_converted` — `18.2` stays `18.2`.
12. `::test_partial_failure_keeps_other_metrics` — `body_fat` 500s, the rest succeed → those are cached, `stale is False`. **Negative.**
13. `::test_total_failure_marks_stale_and_keeps_last_payload` — all requests fail after a good refresh → previous values intact, `stale is True`. **Negative.**
14. `::test_cache_older_than_six_hours_is_stale` — `fetched_at` 6 h 1 min ago → `stale is True` on read.
15. `::test_youth_refresh_calls_only_readiness` — `respx` records exactly one call, to `/api/readiness`. **Negative.**
16. `::test_youth_payload_omits_body_comp_keys` — `"weight_kg" not in data and "body_fat" not in data and "muscle_mass_kg" not in data`. **Absent, not null. Negative.**
17. `::test_nudge_thresholds` — parametrised `(70, "Good day to push")`, `(85, …)`, `(69, "Steady day")`, `(50, "Steady day")`, `(49, "Easy day — hold loads")`, `(0, "Easy day — hold loads")`. Boundaries included.
18. `::test_null_readiness_renders_not_available` — `{"score": null, "status": "insufficient_data"}` → `"Readiness not available"`, no exception. **Negative.**
19. `::test_readiness_500_is_not_coerced_to_zero` — a 500 → nudge is "not available" and the score is absent, never `0`. **Negative.**
20. `tests/test_payload.py::test_payload_matches_contract_field_set` — the built dict's keys are a subset of the PRP-05 §4.1 table, and `session_id`/`start`/`duration_min`/`exercises` are present. **Guards `extra="forbid"` drift.**
21. `::test_start_has_utc_offset` — the `start` string ends `+00:00` and parses to an aware datetime.
22. `::test_duration_min_never_zero` — a 20-second session → `duration_min == 1`. **Guards a 422 on a fast test session. Negative.**
23. `::test_only_done_rows_included` — three rows, one `done`, one `sets_done=2`, one untouched → two entries.
24. `::test_load_done_beats_planned` — `load_done_kg=22.5`, `load_planned_kg=20` → `weight_kg == 22.5`.
25. `::test_weight_key_omitted_when_no_load` — both loads `None` → `"weight_kg" not in entry`. **Negative.**
26. `::test_garmin_category_key_omitted_when_none` — `"garmin_category" not in entry`, and `"UNKNOWN"` appears nowhere in the payload. **Negative.**
27. `::test_time_row_sends_reps_one_and_seconds` — a 45 s plank → `reps == 1, seconds == 45`.
28. `::test_garmin_target_present_only_when_pushing_son` — parametrised: son + push on → `"credential_person"`; son + push off → key absent; me + push on → key absent. **Negative.**
29. `::test_notes_combines_felt_and_next_time` and `::test_notes_truncated_to_1000`.
30. `::test_more_than_50_rows_truncated_not_rejected` — 60 done rows → 50 entries and a WARNING. **Negative.**
31. `tests/test_sync.py::test_enqueue_is_get_or_create` — two `enqueue` calls for one `session_id` → one row, same `id`. **Negative.**
32. `::test_done_attempts_inline_once` — one recorded POST, and the Done response returns without waiting for a retry.
33. `::test_202_marks_sent_with_remote_ref` — `status == "sent"`, `next_attempt_at is None`, `remote_ref == "7"`.
34. `::test_200_dedup_also_marks_sent` — a 200 with `deduplicated: true` is a success, not a failure.
35. `::test_409_is_terminal` — `status == "failed"`, `next_attempt_at is None`, and a subsequent `drain()` makes **zero** requests. **Negative.**
36. `::test_422_is_terminal` — same shape. **Negative.**
37. `::test_404_sets_the_branch_message_and_slow_retry` — `last_error` is exactly `"VitalForge has no /api/activity yet — deploy the cadence/activity-endpoint branch"`, `attempts` unchanged, `next_attempt_at` ≈ now + 2 h. **Negative.**
38. `::test_401_does_not_burn_attempts` — `attempts` unchanged after three drains. **Negative.**
39. `::test_backoff_schedule` — parametrised `attempts` 1→8 gives delays `1, 2, 4, 8, 16, 32, 64, 120` minutes.
40. `::test_attempts_exhausted_becomes_terminal` — after the 8th 500, `next_attempt_at is None`. **Negative.**
41. `::test_no_token_marks_skipped_and_logs` — `status == "skipped"`, zero requests, and `caplog` contains exactly `"VitalForge token not configured — session stored locally only"`. **Negative.**
42. `::test_drain_respects_next_attempt_at` — a job due in an hour is not attempted.
43. `::test_explicit_retry_ignores_backoff` — `POST /api/sync/retry` with that `session_id` attempts it anyway.
44. `::test_drain_survives_one_failing_job` — three jobs, the middle one raises → the other two are still attempted. **Negative.**
45. `::test_periodic_task_cancels_cleanly` — app shutdown leaves no pending task and logs no `CancelledError` traceback.
46. `tests/test_metrics_api.py::test_get_metrics_makes_no_network_call` — `respx` records zero calls. **Negative.**
47. `::test_get_metrics_unknown_profile_404` and `::test_cache_miss_returns_stale_true`. **Negative.**
48. `tests/test_done_sync_line.py` — parametrised over the five §4.3 states, asserting the exact rendered string and that the Retry button appears only in the terminal state.
49. `tests/test_health_still_static.py::test_health_makes_no_vitalforge_call` — `respx` records zero calls when `/api/health` is hit. **Guards §5.7. Negative.**
50. **`tests/e2e/test_session_to_activity.py::test_full_session_posts_exact_activity_json`** — the brief's finish criterion. Seed a profile and a workout, tick every row through the API, set `felt`, POST Done, and assert the **exact** JSON body received by the mocked `/api/activity`:

```json
{"session_id": "<uuid>", "session_label": "Lower A",
 "start": "2026-09-06T08:00:00+00:00", "duration_min": 42,
 "exercises": [
   {"name": "Goblet Squat", "garmin_category": "SQUAT", "sets": 3, "reps": 10,
    "weight_kg": 20.0, "rest_s": 90},
   {"name": "Plank", "garmin_category": "PLANK", "sets": 3, "reps": 1, "seconds": 45,
    "rest_s": 60}],
 "notes": "felt: right · next time: +2.5 kg on Goblet Squat",
 "source": "cadence", "push_to_garmin": true}
```

Compare with `==` on the parsed body, not a subset match, so an added key fails the test.

## 7. Devil's-advocate risks

1. **Silent unit bug on `muscle_mass`.** Everyone remembers `weight ÷ 1000` and forgets muscle; 34 500 kg of muscle renders without an exception. → Test 10, and one shared `_grams_to_kg` helper used by both.
2. **The token reaches a log via an `httpx` exception `repr`.** → All `httpx` errors are caught and re-raised as own types carrying a sanitised message; test 2 covers success, 500 and timeout.
3. **A 409 retried in a loop** hammers the shared Garmin credential and gets it IP-blocked, breaking weight logging (contract §5.9). → 409 is terminal, test 35 asserts zero subsequent requests.
4. **Blind insert on Done duplicates the job** once the offline queue replays. → `enqueue` is get-or-create with a unique index; test 31.
5. **A 20-second smoke session 422s on `duration_min: 0`.** → `max(1, round(...))`, test 22.
6. **`extra="forbid"` drift between 05 and 06** surfaces only as a 422 in the e2e test. → The field table is duplicated verbatim in both PRPs; tests 20 and 50 pin it from this side.
7. **`garmin_target` sent by default** fills the parent's Garmin with the kid's sessions. → Emitted only when both conditions hold; test 28 covers all three cases.
8. **The youth profile leaks body composition** through a template that renders whatever the dict holds. → Keys are **absent**, not null, enforced in `metrics.py`; tests 15 and 16.
9. **The periodic task dies silently** on the first exception and sync stops forever with no signal. → Per-iteration try/except with a WARNING; test 44, plus the Done screen's terminal state gives the user a Retry button.
10. **A 5 s timeout on Done blocks the UI** on a slow VitalForge. → One inline attempt, hard 5 s, and failure is a normal outcome that still renders Done.
11. **`/api/health` gets "improved" into a live probe** and a VitalForge outage restarts Cadence every 30 s. → §5.7 and test 49.
12. **`GET /api/metrics` grows a refresh call** because the cache looked empty in dev. → Test 46.
13. **The kid's `null` readiness is coerced to 0** and renders "Easy day — hold loads" forever. → Tests 18 and 19.
14. **Unmocked hosts hang CI for 5 s per call.** → `assert_all_mocked=True`, §5.6.

## 8. Done when

- [ ] `cadence/vitalforge/` has all five modules; no `httpx` exception escapes the package.
- [ ] `metrics_cache` and `sync_job` created, `sync_job.session_id` unique.
- [ ] `GET /api/metrics` and `POST /api/sync/retry` return the `{"ok","data","error","meta"}` envelope.
- [ ] The 5-minute drain + refresh task starts with the app and cancels cleanly.
- [ ] The Done screen renders all five sync states, with Retry only on the terminal one.
- [ ] All 50 acceptance tests present and passing; `make test` ≥ 80 % coverage.
- [ ] `respx` records **zero** unmocked requests across the whole suite.
- [ ] `grep -rn "get_secret_value" cadence/` shows the token unwrapped in exactly one place.
- [ ] With a blank `VITALFORGE_TOKEN`, a full session completes, Done renders, and the log carries the one WARNING verbatim.
- [ ] With VitalForge mocked to 404 on `/api/activity`, Done renders `will sync` and `last_error` names the `cadence/activity-endpoint` branch.
