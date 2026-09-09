<!-- Generated: 2026-09-08 | Tables: 10 | Token estimate: ~700 -->

# Data

SQLite at `data/cadence.db` (WAL). SQLModel classes; `init_db` creates everything, with a
small `schema_version` marker. Library YAML is loaded *into* these tables by `make seed` —
the files are the source, the tables are the runtime copy.

## Tables

| Table | Module | Key columns |
|---|---|---|
| `profile` | `profils/tables.py` | `id` (`me`/`son`), `kind` adult/youth, `age_years`, `age_band`, `bodyweight_kg`, `has_overhead_anchor`, `vitalforge_person` |
| `setting` | `profils/tables.py` | `key` pk, `value_json` — 10 keys incl. `son_enabled`, `setup_complete`, `display_unit` |
| `exercise` / `workout` | `programme/tables.py` | `id` slug, `doc_json`, `source` seed/import/generated |
| `program` | `programme/tables.py` | one active per profile, `start_date` preserved across rebuilds |
| `planned_session` | `programme/tables.py` | `week` 1-4, `day_index`, `day_type`, `rows_json`, `status` planned/done/skipped |
| `session` | `seance/tables.py` | uuid4 = the VitalForge `session_id`, `felt`, `together_group_id` |
| `session_row` | `seance/tables.py` | `*_planned` vs `*_done`, `done`, `is_challenge` |
| `sync_job` | `vitalforge/tables.py` | idempotent on `session_id`; `target_slug` frozen at enqueue; `garmin_claimed_at` lease |
| `assessment` / `challenge` | `bilan/tables.py` | six tests; <=3 active challenges, `due_on` = baseline + 28d |
| `metrics_cache` | `vitalforge/tables.py` | `profile_id` pk, `payload_json`, `stale` |

## Flows

```
library/*.yaml --validator--> exercise/workout --build_program--> program + planned_session
planned_session --resolve_today--> session + session_row --Done--> sync_job --> VitalForge
VitalForge metrics --refresh--> metrics_cache --> readiness nudge (Today) + trend (History)
assessment --gaps--> challenge --weave--> planned_session.rows_json (is_challenge)
```

## Rules that bite

- **`metrics_cache` units.** VitalForge returns weight and muscle in **grams**; the cache
  stores kg. `muscle_pct` is *derived* (mass / bodyweight), paired by day, never interpolated.
- **Youth profiles cache readiness only** — no body composition is ever stored for them.
- **Rebuild triggers** are `days_per_week, session_minutes, equipment, weights_available` and
  profile `age_years, bodyweight_kg, has_overhead_anchor`. Nothing else re-plans a block;
  `son_enabled` deliberately does not.
- A rebuild replaces `planned` rows only. `done` and `skipped` survive, as does `start_date`.

## Backups

`scripts/backup.sh` -> `data/backups/cadence-YYYYmmdd-HHMM.db` via sqlite3 `.backup` (or a
Python fallback in-container), `umask 077`, keeps 30, nightly cron at 03:17.
