# Cadence

A very simple daily workout app for a parent and a kid.

Open the app, see today's workout as a checklist, tick each exercise as you finish it, hit **Done**, and the completed session shows up in [VitalForge](https://github.com/bearyjd/vitalforge) and, from there, in Garmin Connect as a strength-training activity.

![Today on a phone](docs/screenshots/today-mobile.png)

No video, no social, no nutrition, no chat. Server-rendered Jinja + HTMX, no build step, no CDN: `/today` is under 60 KB over the wire so it stays usable one-handed, on the floor, on a low-end Android phone.

## Quick start

```bash
make seed    # load library/ into the database and build a 4-week block per profile
make dev     # http://localhost:8090
```

Then open `/setup` and fill in the table below. Everything on it is editable later under Settings.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Son's age | *required on first run* | Drives the youth rules by age band: under 10, 10–13, 14–17. Until it is set, the son's profile uses the strictest band. |
| Equipment | bodyweight, dumbbells, kettlebells, adjustable bench | Plus free-text weights available (`DB 5–52.5 lb adj`, `KB 16/24 kg`). Nothing outside the whitelist is ever selectable. |
| Days per week | 4 | 2–6 |
| Session length | 30 min | 15–45 |
| Push son's sessions to Garmin | off | His sessions are always stored in VitalForge; pushing them to the parent's Garmin account is opt-in. |

Youth rules and the equipment whitelist are enforced in the validator and the program engine, not just documented — an imported or AI-generated workout cannot get past either.

## Other targets

| Target | What it does |
|---|---|
| `make test` | Unit and API tests with coverage |
| `make lint` | The CI gate: `ruff check` and `ruff format --check` |
| `make e2e` | Playwright at 390×844 |
| `make perf` | Fails if `/today` breaks the page-weight budget |
| `make screenshots` | Rewrites `docs/screenshots/` from a throwaway seeded database |
| `make deploy` | rsync to VM-201 and bring the stack up — see `docs/deploy.md` |

## Documentation

Start with **[docs/HANDOFF.md](docs/HANDOFF.md)**: what shipped, what was descoped, the decisions left open, how to deploy, and a day-one checklist. `docs/DECISIONS.md` is the long-form record behind it, and `docs/architecture.md` is the module map.

Ventouxlabs · AGPL-3.0
