# PRP-08 — Import and AI generation

Branch `prp/08-ai-ingestion`. Depends on PRP-00 (schema, `validateur`, config), PRP-01 (`LibraryBundle`, `exercise`/`workout` tables), PRP-03 (Settings page containers and partial names). Gets a Codex adversarial review on top of the usual three roles.

## Goal

Two ways to get a workout that is not in the seed library: paste one, or ask a model for one. Both pass through the same validator as the seed load, against the target profile's kind, band and equipment. Nothing that fails reaches the database or a screen, and nothing about the family's health leaves the homelab beyond the fields the prompt needs.

## Scope in

- `POST /api/import` — JSON text or a file upload, validated, stored as `source=import`.
- `POST /api/generate` and `POST /api/generate/accept` via OmniRoute, stored as `source=generated`.
- `cadence/ia/` — client, prompt template, generation service.
- The Settings UI content for Import and Generate, rendered into PRP-03's containers.
- Prompt-injection and untrusted-content hardening.

## Scope out

- The validator itself — **owned by PRP-00**. The library loader — **PRP-01**. The Settings form and its containers — **PRP-03**.
- Scheduling a generated workout into a program. An accepted workout is stored and selectable; wiring it into `build_program` is out of scope and stated in HANDOFF (PRP-10).
- Any write to `library/` — forbidden by D-010.

## Data model

No new tables. `workout` rows gain `source = "import" | "generated"` and `target_profile_kind` from the request. Generated previews are **not** stored; only an accepted, re-validated document is.

## API surface

### `POST /api/import`

Two content types.

```
POST /api/import
Content-Type: application/json
{"text": "id: my-upper\nname: My upper\n...", "profile": "me"}
```

```
POST /api/import
Content-Type: multipart/form-data
file=<upload>, profile=son
```

Success:

```json
{"ok": true, "error": null, "meta": {"source": "import"},
 "data": {"workout_id": "my-upper", "name": "My upper", "rows": 5,
          "target_profile_kind": "adult", "warnings": []}}
```

Failure — always 422, never 500, and always the full list:

```json
{"ok": false, "error": "validation_failed", "meta": {},
 "data": {"errors": [
   {"code": "equipment_not_whitelisted", "row": 2, "message": "barbell-back-squat uses load_type 'barbell', which is not on the equipment whitelist."},
   {"code": "youth_rep_floor", "row": 3, "message": "Loaded rows need at least 8 reps at age band age_10_13."}]}}
```

Error codes come from two places and must not be invented twice. **Pipeline codes**, owned here, cover the gates before the validator: `parse_error`, `not_a_mapping`, `too_large`, `too_many_exercises`, `string_too_long`, `suspicious_content`, `id_collision`, `validation_failed`. **Validator codes** pass through verbatim from PRP-00's `codes.py` — `schema`, `unknown_exercise`, `equipment_not_whitelisted`, `equipment_not_available`, `measure_mismatch`, `measure_conflict`, `youth_load_type_not_allowed`, `youth_load_exceeded`, `youth_rep_floor`, `youth_rest_floor`, `youth_exercise_count`, `youth_set_count`, `youth_banned_tag`, `youth_session_length`, `youth_amrap_not_allowed`, `youth_banned_goal`, `requires_anchor_unavailable`, `youth_exercise_not_allowed` (V14, the §9 per-band allowlist). Never remap or rename one.

Rules:

| Rule | Value |
|---|---|
| Body / file size | ≤ 256 KB, enforced **before** parsing, on `Content-Length` and again on the read bytes |
| Parser | `yaml.safe_load` only. JSON is a subset of YAML, so one parser serves both. Never `yaml.load`, never `pickle`, never `eval` |
| Documents | exactly one; a multi-document stream (`---`) is `parse_error` |
| Top level | must be a mapping; unknown top-level keys are dropped silently after being counted in `meta.ignored_keys` |
| Exercises | ≤ 30 rows |
| Strings | PRP-00's model bounds are the limit: `name` ≤ 100, `cue` / `cue_override` ≤ 120, `Workout.notes` ≤ 500. Anything longer is `string_too_long` |
| Nesting | depth ≤ 6; deeper is `parse_error` |
| Unknown exercise ids | rejected, **unless** the document also defines them inline under `exercises:` and each passes `validate_exercise` |
| Target | `profile` field, default `me`; validation runs with that profile's `kind`, `age_band` and the current `equipment` setting |
| Filesystem | never touched. No filename is used for anything, not even logging. D-010 |

**Suspicious content** — reject the whole document with `suspicious_content` when the raw text contains any of:

- `http://` or `https://`
- `{{`, `{%`, `{#` (Jinja delimiters)
- `&` immediately followed by a word character at a YAML node position, or `*` likewise — YAML anchors and aliases. `safe_load` blocks object construction but not alias expansion, which is a billion-laughs vector. No legitimate workout uses them.
- `!!` (YAML tags), `<script`, `javascript:`, `data:text/html`

The check runs on the raw text before parsing, and again on every string field after parsing. Message names the offending token, never echoes the whole document.

### `POST /api/generate`

```json
{"profile": "me", "goal": "posture", "gap": "wall_angel_reach"}
```

`goal` is a `GoalType` from principles §11: `strength | posture | movement_quality | consistency | weight | body_fat | appearance`. `weight`, `body_fat` and `appearance` are rejected with 422 for a youth profile (§3.5). The Settings UI labels them in plain language — "Get stronger", "Better posture", "Move better and play", "Keep the habit", "Look better (adult only)" — so the kid-facing wording never says appearance while the enum stays the one PRP-00 owns.

Response is a preview, not a stored row:

```json
{"ok": true, "error": null, "meta": {"model": "code-plan", "attempts": 1},
 "data": {"workout": {"id": "generated-posture-1", "name": "Posture focus", "rows": [...]},
          "errors": []}}
```

`POST /api/generate/accept` takes `{"profile": "me", "workout": {...}}` and **re-validates the posted document from scratch**. The client is never trusted: the preview is not cached server-side and the accept path repeats every check the import path runs, including the suspicious-content scan. Stored with `source=generated`.

### OmniRoute call

- Base URL, key and model from `cadence/config.py`: `OMNIROUTE_BASE_URL` (default `https://llm.grepon.cc/v1`), `OMNIROUTE_KEY`, `OMNIROUTE_MODEL` (default `code-plan`).
- OpenAI-compatible `POST {base}/chat/completions`, `Authorization: Bearer {key}`.
- `temperature` 0.4, `stream` false, timeout 60 s total via `httpx.Timeout`.
- One retry, and only one: if the first response fails validation, re-send the same prompt with the validator's error list appended under a `Previous attempt failed these checks:` heading. A second failure returns `{"ok": false, "error": "generation_failed", "data": {"errors": [...]}}` with the errors, and no stored row.
- No streaming, ever (brief's gateway gotcha).
- Missing `OMNIROUTE_KEY` → 503 with `error: "generation_unavailable"`, and the Settings section renders disabled with one explanatory line. Never a traceback, never the key.

### The prompt — `cadence/ia/prompts/generate.md`

The template may interpolate **only** these values, and the implementer must be able to point at each one:

| Placeholder | Example |
|---|---|
| `profile_kind` | `youth` |
| `age_band` | `age_10_13` |
| `equipment_ids` | `bodyweight, dumbbells, kettlebells, bench` |
| `weight_ranges` | `dumbbells 2.27–23.81 kg step 1.13; kettlebells 16, 24 kg` |
| `days_per_week` | `4` |
| `session_minutes` | `30` |
| `goal` | `posture` |
| `gap_name` | `wall_angel_reach` or empty |
| `allowed_exercises` | `db-rdl (hinge, dumbbell), push-up (push_h, bodyweight), …` |
| `youth_rules` | the band's rule table as a compact list |
| `schema_example` | the YAML skeleton from PRP-01's workout DSL |

**Never sent**: session logs, tick history, assessment values, VitalForge metrics, readiness, body composition, display names, the exact age, `bodyweight_kg`, person slugs, any token, any file path. The band is the only age-derived value that leaves the machine.

The template ends with a hard instruction: reply with one YAML document and nothing else — no prose, no code fence, no second document. The parser strips a leading and trailing ``` fence defensively and then requires a single document.

### Settings UI

Rendered into PRP-03's `#import-result` and `#generate-preview` targets.

```
┌──────────────────────────────────────┐
│ Import workout                       │
│ ┌──────────────────────────────────┐ │
│ │ Paste YAML or JSON               │ │
│ │                                  │ │  textarea, 8 rows, monospace
│ │                                  │ │
│ └──────────────────────────────────┘ │
│ or  [ Choose file ]  (max 256 KB)    │
│ For  [ Me ▾ ]                        │
│ [ Check and import ]                 │
│ ┌── #import-result ────────────────┐ │
│ │ ✕ 2 problems                     │ │
│ │ • row 2 — barbell is not on your │ │
│ │   equipment list                 │ │
│ │ • row 3 — loaded rows need 8+    │ │
│ │   reps at his age                │ │
│ └──────────────────────────────────┘ │
├──────────────────────────────────────┤
│ Generate workout                     │
│ Goal  [ Better posture      ▾ ]      │
│ Chase [ Wall angel reach    ▾ ]      │  ← options = active challenges, or "nothing"
│ [ Generate ]                         │
│ ┌── #generate-preview ─────────────┐ │
│ │ Posture focus · 5 exercises      │ │
│ │ ☐ Wall angel        2 × 10       │ │  read-only checklist, same row markup
│ │ ☐ Prone Y/T/W       2 × 12       │ │  as Today so it looks like the real thing
│ │ ☐ Dead bug          2 × 8/side   │ │
│ │ ☐ Bench hip thrust  3 × 12       │ │
│ │ ☐ Farmer carry      3 × 30 m     │ │
│ │ [ Accept ]        [ Discard ]    │ │
│ └──────────────────────────────────┘ │
└──────────────────────────────────────┘
```

Tap flow for generate: pick a goal, tap Generate, wait with a spinner and a "this takes a few seconds" line, read the preview, tap Accept. Discard clears the target and stores nothing.

Partials owned here: `partials/import_result.html`, `partials/generate_preview.html`, plus the content of `partials/import_section.html` and `partials/generate_section.html`.

## Implementation notes

- Files: `cadence/api/import_.py`, `cadence/api/generate.py`, `cadence/bibliotheque/import_service.py`, `cadence/ia/{client.py,generate.py,prompts/generate.md}`, `cadence/web/routers/settings_ai.py`, the four partials.
- **The §9 per-band allowlist rides inside the validator** as V14 (PRP-00 §5.3), so both write paths get it for free from `validate_workout` — do not add a second check here. Two behaviours to pass `bodyweight_kg` for correctly: a band absent from an exercise's `youth_ok_by_band` is denied, and a `conditional` exercise is denied outright when `bodyweight_kg is None`. So `/api/import` and `/api/generate/accept` must pass the target profile's real `bodyweight_kg` and `has_overhead_anchor` into the call, not the defaults.
- **Validator call.** `validate_workout(doc, *, profile_kind, age_band, equipment, bodyweight_kg, has_overhead_anchor, exercises, youth_rules)` returns a frozen `ValidationResult(ok, errors)` (PRP-00). **Gate on `result.ok`.** The result is a Pydantic model and always truthy, and `youth_rep_ceiling` / `youth_rpe_exceeded` are warnings a valid document may carry — rejecting on a non-empty `errors` list would reject good workouts. Surface warnings in `data.warnings` on a successful import.
- **Jinja autoescape must be on** for every template that renders imported or generated text. Assert it in a test rather than trusting the default. Never use `|safe` on a `name`, `cue` or `notes` field. Never build HTML in Python from imported text.
- **Order of operations on import**: size → suspicious-content scan on raw text → `safe_load` → single-document check → mapping check → depth and count caps → string length caps → drop unknown top-level keys → schema parse → inline exercise validation → `validate_workout` → store. Fail at the first gate that trips, but return **all** validator errors when reaching that stage.
- **`httpx` is the client**, with `MockTransport` in tests (or `respx`, if PRP-06 has already added it — do not add a second mocking library). No SDK dependency. Log the model name, attempt count and latency; never the prompt body, never the key, never the response body at INFO.
- Timeouts: connect 5 s, read 60 s, total 60 s. A timeout is `generation_failed`, not a 500.
- Generated ids are slugified from the name with a numeric suffix on collision; an id that collides with a seed exercise or workout is rejected rather than overwriting (`source=seed` rows are never replaced by import or generation).
- The import service is pure with respect to the filesystem: it takes `text: str`, never a path. The route reads the upload into memory with a hard cap and passes the string.

## Acceptance tests

`tests/test_import.py`, `tests/test_generate.py`, `tests/e2e/test_settings_ai.py`. The OmniRoute fixture is `httpx.MockTransport` in `tests/conftest.py`.

1. `test_import_valid_yaml_stores_workout` — 200, `source == "import"`, row count matches.
2. `test_import_valid_json_same_path` — the same document as JSON stores identically.
3. **Negative** `test_import_barbell_rejected` — a `load_type: barbell` row → 422, code `equipment_not_whitelisted`, nothing stored.
4. **Negative** `test_import_youth_five_rep_sets_rejected` — a loaded 3×5 workout targeting `son` at `age_10_13` → 422, code `youth_rep_floor`.
4b. `test_warning_codes_do_not_reject` — a document whose only issue is `youth_rep_ceiling` imports successfully and the warning appears in `data.warnings`.
5. **Negative** `test_import_youth_kettlebell_rejected` — a kettlebell row targeting `son` at `age_10_13` → 422 `youth_load_type_not_allowed`.
5b. **Negative** `test_import_youth_allowlist_enforced` — `kb-swing` targeting `son` at `age_14_17` with `bodyweight_kg=40` → 422 `youth_exercise_not_allowed` (V14); the same document with `bodyweight_kg=None` is also rejected, proving the route passes the real bodyweight through.
6. **Negative** `test_import_malformed_text` — `"::: not yaml"` → 422 code `parse_error`, no traceback in the body.
7. **Negative** `test_import_multi_document` — two documents separated by `---` → 422 `parse_error`.
8. **Negative** `test_import_too_large` — 300 KB body → 422 `too_large`, and the parser is never reached (assert with a spy).
9. **Negative** `test_import_too_many_exercises` — 31 rows → 422 `too_many_exercises`.
10. **Negative** `test_import_yaml_alias_bomb` — a document using `&a`/`*a` → 422 `suspicious_content`, and the process memory does not balloon.
11. **Negative** `test_import_yaml_tag_rejected` — `!!python/object/apply:os.system` → 422, and `os.system` is never called (patch and assert).
12. **Negative** `test_import_url_in_cue` — a cue containing `https://evil.example` → 422 `suspicious_content`.
13. **Negative** `test_import_jinja_delimiters` — `{{ 7*7 }}` in a name → 422 `suspicious_content`.
14. `test_import_unknown_exercise_rejected` — a row naming `not-an-exercise` → 422 `unknown_exercise`.
15. `test_import_inline_exercise_accepted` — the same document defining that exercise inline and passing `validate_exercise` → 200.
16. `test_import_unknown_top_level_keys_dropped` — extra keys are ignored and listed in `meta.ignored_keys`.
17. **Negative** `test_import_cannot_overwrite_seed` — an import using the id `upper-a` → 422 code `id_collision`, and the seed row is unchanged.
18. `test_import_never_touches_filesystem` — patch `builtins.open`, `pathlib.Path.write_text` and `os.makedirs` to raise; the import still succeeds.
19. `test_injection_payload_renders_inert` — import a cue containing `<script>alert(1)</script>`, render the workout preview, and assert the served HTML contains `&lt;script&gt;` and no executable tag; a Playwright companion asserts no dialog fires.
20. `test_autoescape_is_enabled` — assert on the Jinja environment directly.
21. `test_generate_valid_yaml` — MockTransport returns a good YAML → 200 preview, nothing stored yet.
22. **Negative** `test_generate_barbell_rejected` — MockTransport returns a barbell workout → `ok: false` with the equipment error, nothing stored.
23. **Negative** `test_generate_youth_five_rep_rejected` — a youth workout with 3×5 loaded sets → rejected.
24. **Negative** `test_generate_malformed_text` — MockTransport returns prose → one retry fires, then `generation_failed`.
25. `test_generate_retry_includes_errors` — capture both requests; the second body contains the first attempt's error strings.
26. `test_generate_only_one_retry` — a MockTransport that always fails is called exactly twice.
27. **Negative** `test_generate_injection_in_cue_escaped` — a generated cue with `<script>` is escaped in the preview HTML.
28. `test_prompt_contains_no_secrets_or_logs` — capture the outgoing request body and assert it contains **none** of: the exact `age_years` integer, the profile `display_name`, any `vitalforge_person` slug, any `metrics_cache` value, any session id, any assessment value, the strings `OMNIROUTE`, `VITALFORGE`, `Bearer`. Assert positively that it **does** contain the band string, so the test is not vacuous.
29. `test_prompt_contains_band_not_age` — a profile with `age_years=12` produces a prompt containing `age_10_13` and not `12` as a standalone token.
30. **Negative** `test_generate_appearance_goal_rejected_for_youth` — 422.
31. **Negative** `test_generate_without_key` — unset `OMNIROUTE_KEY` → 503 `generation_unavailable`, and the key name never appears in the body.
32. `test_accept_revalidates` — post an accept body whose rows were tampered with after the preview (a barbell row swapped in) → 422, nothing stored.
33. `test_accept_stores_generated` — a clean accept stores `source == "generated"`.
34. `test_no_streaming_requested` — the captured request body has `"stream": false`.
35. `e2e_import_shows_errors_inline` — 390×844: paste a barbell workout, tap Check and import, two error lines appear in `#import-result` without navigation.
36. `e2e_generate_preview_and_accept` — with the mock gateway: tap Generate, the preview renders as a read-only checklist, tap Accept, a success line appears.
37. `e2e_generate_discard_stores_nothing` — tap Discard, then assert via `GET /api/sessions`-adjacent state that no `generated` workout exists.

## Devil's-advocate risks

1. **`yaml.load` instead of `safe_load`** — arbitrary object construction. Mitigation: test 11 patches `os.system`; a grep test asserts `yaml.load(` appears nowhere in `cadence/`.
2. **Alias expansion (billion laughs)** survives `safe_load`. Mitigation: the raw-text anchor/alias rejection and test 10.
3. **Size checked after reading the whole upload** into memory. Mitigation: check `Content-Length` first, then cap the read; test 8 asserts the parser is never reached.
4. **Prompt injection through an imported cue**, later fed to the model as context. Mitigation: imported text is never included in any prompt — the template's placeholder list is closed, and test 28 asserts what the body contains.
5. **Prompt injection through the model's own output** rendered as HTML. Mitigation: generated documents are treated exactly like imports; autoescape asserted; tests 19, 27.
6. **The accept path trusting the preview.** The obvious optimisation — cache the preview and store it on accept — lets a tampered client body through if anyone later switches to trusting it. Mitigation: no server-side preview cache at all, full re-validation, test 32.
7. **A generated workout overwriting a seed id.** Mitigation: test 17 for import, same guard for generate.
8. **Path traversal on the multipart filename.** Mitigation: the filename is read and discarded; the service takes a string; test 18.
9. **Secrets in logs.** A debug `logger.info(response.text)` leaks the prompt and possibly the key on an auth error. Mitigation: log model, attempts and latency only; a test asserts `Bearer` never appears in captured log records.
10. **Calling the validator with default `bodyweight_kg` / `has_overhead_anchor`.** Both default in the signature, so an incomplete call compiles and silently weakens V2, V13 and V14 — a `conditional` kettlebell row would be judged on the absolute cap alone. Mitigation: test 5b's second half, which rejects the same document under an unknown bodyweight.
11. **Youth validation run against the wrong profile.** Defaulting `profile` to `me` while the UI targets `son` would let a barbell workout onto the kid's plan. Mitigation: the target profile select is required in the UI, the API default is `me`, and tests 4, 5 and 23 all pass an explicit `son`.
12. **A 60 s hang blocking the event loop.** Mitigation: `httpx.AsyncClient`, awaited, with the timeout above; never `requests`.
13. **Unbounded retry** on a flapping gateway. Mitigation: exactly one retry, test 26.
14. **`suspicious_content` false positives** on a legitimate cue. None of the seed cues contain a URL, a Jinja delimiter or a YAML anchor — assert that once over the whole seed library so the rule is proven compatible with real content.

## Done when

- [ ] `POST /api/import` accepts YAML and JSON, text and file, and rejects every case in the test list with a code and a readable message.
- [ ] `safe_load` is the only parser; no anchor, alias, tag, URL or Jinja delimiter survives; nothing touches the filesystem.
- [ ] `POST /api/generate` calls OmniRoute non-streaming with one retry, validates the reply, and stores nothing until `accept` re-validates it.
- [ ] The outgoing prompt carries the band and never the age, name, slug, metric, log or token — proven by an assertion on the captured request.
- [ ] Imported and generated text renders escaped everywhere.
- [ ] Import and Generate sections work inside PRP-03's Settings containers.
- [ ] All 39 acceptance tests pass; `make lint && make test && make e2e` green; the Codex adversarial review has no unresolved Highs.
