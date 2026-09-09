<!-- Generated: 2026-09-08 | Files scanned: 121 py + 33 templates + 22 library yaml | Token estimate: ~700 -->

# Architecture

One FastAPI process serves both the HTML UI (Jinja + HTMX, no build step) and a JSON API.
SQLite via SQLModel. No background workers: one in-process 5-minute task drains the sync
queue and refreshes the metrics cache.

## Boundaries

```
                 browser / PWA (offline queue in IndexedDB)
                            |
      web/routers/*  (HTML + HTMX partials)   api/*  (JSON)
                            |                    |
        seance  historique  bilan  profils  programme  bibliotheque
                            |
              schema/  ->  validateur/   (the single gate)
                            |
                          db.py (SQLite)
                            |
        vitalforge/ (reads metrics, writes activity)   ia/ (OmniRoute)
```

## Domain packages (French names, English identifiers)

| Package | Owns |
|---|---|
| `schema/` | Pydantic v2 library DSL — Exercise, Workout, Progression, YouthRuleSet |
| `validateur/` | The gate. Every seed, import and generation passes through it |
| `bibliotheque/` | YAML library loader, seed, import/adoption of untrusted docs |
| `programme/` | 4-week plan builder, materialise rows, autoregulation, load ladder |
| `profils/` | Profiles, settings, age bands, rebuild, visibility (solo mode) |
| `seance/` | Today resolution, ticks, adjust, Done |
| `historique/` | History, weekly scorecard, streaks, badges, sparkline |
| `bilan/` | Assessments, gap detection, challenges |
| `vitalforge/` | Metrics read + cache, activity write-back, leased retry queue |
| `ia/` | OmniRoute client, prompt template, generation |

## Invariants

- **The validator is the only way in.** Seed, `/api/import` and `/api/generate/accept` all
  call `validate_workout`; nothing reaches the DB or a screen otherwise.
- **Youth rules are data, enforced in code.** Band caps live in `library/youth_rules.yaml`;
  `validateur/youth_rules.py` applies them. A youth profile never sees body composition.
- **Today makes no network call.** Metrics come from `metrics_cache`, refreshed out of band.
- **Writes are never dropped by a display setting.** Solo mode hides screens, never the work.
- **No auth** (D-009): reachability is the access control, hence the Tailscale bind.
