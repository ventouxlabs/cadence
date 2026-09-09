<!-- Generated: 2026-09-08 | Files scanned: 121 py | Token estimate: ~900 -->

# Backend

## JSON API — `cadence/api/`

```
GET  /api/health                        health.py            -> db ping + env/mode (never a token)
GET  /api/today?profile=                today.py             -> seance.today.resolve_today
POST /api/sessions/{id}/rows/{position} sessions.py          -> seance.ticks.apply_patch      (idempotent; offline replay target)
POST /api/sessions/{id}/done            sessions.py          -> seance.done.finish            (idempotent)
GET  /api/sessions | /api/scorecard     history.py           -> historique.queries / scorecard
GET  /api/activities                    history.py           -> historique.queries
GET/PUT /api/settings                   settings.py          -> profils.services.update_settings
GET/PUT /api/profiles/{id}              profiles.py          -> profils.services
PUT  /api/profiles/{id}/kind            profiles.py          -> explicit, confirmed adult/youth switch (D-099)
GET  /api/metrics?profile=              metrics.py           -> vitalforge.metrics.read_cached (cache only)
POST /api/sync/retry                    sync.py              -> vitalforge.sync.drain         (throttled, 429)
GET/POST /api/assessments               assessments.py       -> bilan.service
POST /api/import                        import_.py           -> bibliotheque.import_service   (untrusted)
POST /api/generate | /generate/accept   generate.py          -> ia.generate -> same validator
GET  /api/_mock/activities              mock_inspect.py      -> mock mode only, never prod (D-169)
```

## HTML — `cadence/web/routers/`

```
GET  /                                   -> /setup until setup_complete, else /today
GET  /today?profile=me|son|together      today.py    -> checklist, readiness nudge, assessment card
POST /today/{sid}/rows/{pos}/tick|adjust today.py    -> HTMX row partial
POST /today/{sid}/felt | /done           today.py    -> finalise, then /done/{sid}
GET  /done/{sid}                         today.py    -> 3-line summary + sync status
POST /done/{sid}/retry                   today.py    -> re-drain one job (throttled)
GET  /history[?profile=]                 history.py  -> list, scorecard, badges, trend (adult)
GET  /history/sessions/{sid}             history.py  -> expand-in-place fragment
GET  /history/badges/{pid}/{badge_id}    history.py  -> caption fragment
GET/POST /setup | /settings              settings.py -> first-run gate and editable settings
POST /settings/import | /generate*       settings_ai.py -> the two untrusted-doc cards
GET/POST /assess | /assess/skip          assess.py   -> baseline and 4-weekly retest
GET  /manifest.json | /sw.js             pwa.py      -> PWA at the root scope
```

## Middleware and cross-cutting

- `GZipMiddleware` — the only middleware (D-025), for the 60 KB `/today` budget.
- `require_setup` on `GATED_ROUTERS` — HTML screens bounce to `/setup` until set up.
- `require_visible_profile` on `TABBED_ROUTERS` — solo mode; **GET only**, so writes and the
  offline replay are never refused (D-264).
- `RequestRefused` / `ProfileHidden` / `SetupRequired` — exception handlers, not inline checks.

## Key files

```
cadence/validateur/youth_rules.py   411   band caps, V1-V14
cadence/programme/materialise.py    428   template -> concrete rows, load ladder, trim
cadence/vitalforge/sync.py          410   leased retry queue, 5 job states
cadence/bibliotheque/import_service.py 466 untrusted YAML/JSON -> validated workout
cadence/profils/services.py         431   settings, rebuild triggers, age bands
```
