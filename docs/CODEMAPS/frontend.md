<!-- Generated: 2026-09-08 | Templates: 33 | Token estimate: ~700 -->

# Frontend

Server-rendered Jinja + HTMX 2.0.10 (vendored, no CDN) + ~25 KB of vanilla JS. No build step,
no framework. `/today` is ~31 KB gzipped against a 60 KB gate enforced by `tests/test_perf.py`.

## Page tree

```
base.html                        topbar: brand | profile tabs (Me/Son/Both) | ⚙ | History
├── setup.html                   first run; tabs hidden (nothing past the gate)
├── today.html                   readiness nudge, assessment card, checklist, Done
│   └── partials/row.html        one exercise: checkbox, sets×reps, load, cue, timer
│       ├── row_adjust.html      inline ± stepper (<=2 taps)
│       ├── felt.html            easy / just right / hard, once all rows ticked
│       └── exit.html            youth "Good enough — I'm done!" (never labelled "Done")
├── done.html                    3 lines + sync status + new-badge line
├── history.html                 scorecard, sessions, badges, trend (adult only)
│   └── partials/{scorecard,trend,history_list,session_detail,badges}.html
├── assess.html                  six tests, segmented rubrics
└── settings.html                the settings form + import and generate cards
```

## Profile scoping (the part that matters)

- `base.html` sets `data-profile-kind="youth"` on `<body>` for the son's own screens.
- **Together renders two people in one document**, so the youth scope moves to the element that
  belongs to one person: `.hcolumn` / `.summary-card` / the checklist `<section>` — never the
  body (D-233, D-234, D-255).
- Youth screens carry no weight, body-fat, muscle or appearance text. The exercise name
  "Bodyweight squat" is legal; the word-boundary rule in the banned-word test permits it.
- Solo mode (`son_enabled=false`) drops the Son and Both tabs; `profile_tabs` is derived
  per-request, not a frozen global.

## JS — `static/app.js`

```
IndexedDB "cadence-queue": {op, session_id, position, patch, ts, attempts, inflight_at}
  tick / adjust / felt / done  -> queued BEFORE send, dropped on 2xx or 409
  5xx holds and halts the drain; other 4xx parks with a banner
  replay on load and on 'online'; Done navigates optimistically to /done/{id}
timer: per-row, off by default, one tap, pure client-side (zero network)
--footer-h measured so a fixed footer never covers the last row
```

## Service worker — `static/sw.js`

Precache static + the offline Done page; network-first with cache fallback for `/today*` and
`/api/today*`. Served from the root scope so the PWA installs (`manifest.json` +
`sw.js` both at `/`, correct MIME types over HTTPS).
