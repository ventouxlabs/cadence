# Deploying Cadence to VM-201

Cadence runs as one container on VM-201 (`192.168.1.21`), published on host port **8090**,
fronted by Nginx Proxy Manager at `https://cadence.grepon.cc`, reachable over Tailscale only.

> **These deployment facts are operator knowledge, not repo facts.** VM-201, Nginx Proxy
> Manager, Tailscale and `*.grepon.cc` appear nowhere in the VitalForge tree, whose sample
> nginx config uses `yourdomain.com` placeholders. Everything below comes from the brief's
> "Known environment" block. Confirm it on the first deploy rather than trusting it.

| | |
|---|---|
| Container listens on | `0.0.0.0:8000` (uvicorn, one worker) |
| Host publishes | `8090` → `8000` |
| Container user | uid **10001** (`cadence`), non-root |
| Database | `./data/cadence.db` on the host, `/app/data/cadence.db` in the container |
| Backups | `./data/backups/cadence-YYYYmmdd-HHMM.db`, newest 30 kept |
| Public name | `cadence.grepon.cc` → `192.168.1.21:8090` |

---

## 1. Prerequisites

On **VM-201**:

- Docker Engine with the Compose plugin (`docker compose version`).
- `sqlite3` is optional. The backup script prefers the CLI and falls back to Python's
  `sqlite3` module, which is how it runs inside the container, where there is no CLI. Both
  call the same SQLite Online Backup API.
- Tailscale up and the host reachable on its Tailscale address.
- Nginx Proxy Manager already running (it fronts VitalForge on the same host).
- A directory for the install, writable by the deploying user, **created before the first
  deploy** — `scripts/deploy.sh` refuses a path that does not exist rather than creating it
  (D-281). `/opt/cadence` by default; `/home/user/docker/cadence` on VM-201 (D-257a), named
  with `CADENCE_DEPLOY_PATH`.

On the **workstation**:

- An ssh host alias `vm-201` in `~/.ssh/config` with key auth. `scripts/deploy.sh` checks this
  first with `ssh -o BatchMode=yes` and refuses to start if it fails. Override the alias with
  `CADENCE_DEPLOY_HOST`, and the remote directory with `CADENCE_DEPLOY_PATH`.
- **Passwordless sudo for that user on VM-201**, for the one `chown -R 10001:10001 data` the
  deploy runs over ssh. A sudo password prompt on a non-interactive ssh hangs the deploy
  halfway through, which is exactly the failure the BatchMode check exists to prevent. If
  passwordless sudo is not wanted, chown `data/` once by hand and run
  `scripts/deploy.sh --no-build` past it, or make the deploying user own `data/`.
- `rsync`, and `jq` for `make smoke`.
- Podman and `podman-compose` for `make dev-docker` (D-004: this workstation aliases
  `docker` → `podman`; VM-201 runs real Docker Compose).

---

## 2. First deploy

> **As actually deployed on VM-201, 2026-09-07 (D-257).** The live install is at
> **`/home/user/docker/cadence`**, not `/opt/cadence` — VitalForge lives at
> `~/docker/vitalforge`, and matching the host's own convention beat matching this document.
> It was deployed by `git clone` of the public repo rather than by `make deploy`, because the
> workstation had nothing to rsync that GitHub did not already have. **The runnable commands
> in this section now use that path directly** rather than asking you to substitute it — D-280
> and D-281 are both what "substitute throughout" costs in practice. Everything else is accurate.
>
> **A manual clone skips `scripts/deploy.sh`, and therefore skips the chown.** That is the one
> step a `git clone && docker compose up` path silently misses, and it fails exactly as
> described below: `unable to open database file`, container in `Restarting`, healthy-looking
> compose output right up until it isn't. Run the chown before the first `up`:
>
> ```bash
> mkdir -p ~/docker/cadence/data/backups
> sudo chown -R 10001:10001 ~/docker/cadence/data
> ```

```bash
# On VM-201, once. `scripts/deploy.sh` refuses a path that does not exist rather than
# creating one (D-281), so this step is deliberate and not a convenience.
sudo mkdir -p /home/user/docker/cadence && sudo chown "$USER" /home/user/docker/cadence
```

On a host with no convention of its own, `/opt/cadence` is the script's default and needs no
`CADENCE_DEPLOY_PATH`. VM-201 has one (D-257a), so every command below names the real path.

Ship the code, then create the secrets file **by hand**. `.env` is never rsynced, never
committed, and never baked into the image. **Order matters:** `env_file: .env` makes Compose
refuse to start when the file is absent, so the first `make deploy` gets as far as the rsync
and then fails at `up`. That is expected on a first deploy, and it is why `.env` comes between
the two:

```bash
# 1. Workstation - ships the tree, then stops at `up` with a missing .env
CADENCE_DEPLOY_PATH=/home/user/docker/cadence make deploy

# 2. VM-201, once: create .env from the committed template and fill in the real tokens
cd /home/user/docker/cadence
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env                # VITALFORGE_TOKEN, OMNIROUTE_KEY, the two person slugs

# 3. Workstation - now it completes
CADENCE_DEPLOY_PATH=/home/user/docker/cadence make deploy
```

`scripts/deploy.sh` creates `data/` and `data/backups/` and chowns them to uid 10001 before
bringing the stack up. Doing it by hand instead:

```bash
mkdir -p /home/user/docker/cadence/data/backups
sudo chown -R 10001:10001 /home/user/docker/cadence/data
```

**Why 10001.** The container runs non-root, and a bind mount that compose created is owned by
root, so the app fails at startup with `unable to open database file`. VitalForge solves the
same problem with an entrypoint that `chown`s `/app/data` as root and then drops privileges.
Cadence does not, deliberately: a bind mount the operator owns is an ordinary directory to back
up, copy and restore, and the ownership is a one-line first-deploy step rather than a root
capability the container keeps forever.

Seed the library and the two profiles on the first boot:

```bash
docker compose exec cadence python -m cadence.bibliotheque.seed
```

Then confirm, before touching Nginx Proxy Manager:

```bash
docker compose ps                            # STATUS must say (healthy), not just Up
docker exec cadence id -u                    # 10001
docker compose port cadence 8000             # the address:port the publish actually landed on
curl -s "http://$(docker compose port cadence 8000)/api/health" | jq .
```

> **Ask the publish where it is rather than assuming the LAN.** The port follows
> `CADENCE_BIND_ADDR` (§5), and on VM-201 that is the Tailscale address, so a hardcoded
> `http://192.168.1.21:8090/api/health` **is refused** — which reads like a container that never
> came up, at exactly the moment you are trying to prove one did. `docker compose port` answers
> with whatever the mapping really is, on a wide bind and a narrow one alike.

### Local dry run first

```bash
make dev-docker      # podman-compose with the dev overlay, on http://localhost:8090
make smoke           # eight steps against localhost
```

`make dev-docker` layers `docker-compose.dev.yml`, which sets `CADENCE_ENV=dev` and
`CADENCE_VITALFORGE_MODE=mock`. The base file states production's values (`CADENCE_ENV=prod`)
and deliberately leaves the mode to the VM's `.env`, because a compose `environment:` entry
overrides `env_file:` and would silently ignore what the operator wrote there.

`make dev-docker` runs `podman unshare chown -R 10001:10001 data` before starting. Rootless
podman maps container uid 10001 to a high subuid on the host, so the plain `chown` VM-201 uses
does not apply on the workstation and the container would hit the same `unable to open database
file`.

---

## 3. Routine deploy

```bash
# VM-201's install is not at the script's default path, so name it (D-257a, D-281).
CADENCE_DEPLOY_PATH=/home/user/docker/cadence make deploy

# Same, in git mode - `git pull --ff-only` on the VM rather than shipping the working tree.
CADENCE_DEPLOY_PATH=/home/user/docker/cadence make deploy DEPLOY_MODE=git
```

A bare `make deploy` uses the script's own default, which is the right thing on a host that has
no convention of its own and the **wrong** thing here — that is the whole of D-281.

> **The path is absolute or nothing.** `scripts/deploy.sh` refuses anything not matching
> `^/[A-Za-z0-9._/-]*$`, because it is interpolated into a remote shell command — so
> `/home/user/docker/cadence`, not `~/docker/cadence`. An unquoted `~` in the assignment above
> happens to work because bash expands it first, which is a coincidence and not a guarantee.
>
> A deploy into a directory that **does not exist** is refused outright (exit 4) rather than
> created. That `mkdir -p` is what used to turn a wrong path into a second install beside the
> running one (D-281); §8 has the symptom.

| | rsync (default) | git |
|---|---|---|
| Ships | the working tree, unpushed branches included | whatever `origin` has |
| Pro | deploys exactly what is on the workstation | the VM's state is an auditable git ref |
| Con | the VM's state is not a commit | needs the commit pushed first |

rsync is the default because PRP-05's VitalForge branch is deliberately never pushed (D-005)
and there will be things to try before pushing. `--mode git` needs a **clone** at the deploy path, with a
remote and a checked-out branch; a directory that is not one is refused (exit 4) rather than
left to fail on `git pull` with the chown and `up` still queued behind it.

The rsync carries `--delete` and therefore two load-bearing excludes:

- `--exclude '.env'` — without it, the VM's real tokens are wiped and Done starts reporting
  `stored locally — VitalForge not configured`.
- `--exclude 'data'` — without it, `--delete` removes the production database.

Both live in `scripts/deploy.sh` so they are never typed by hand. `make deploy` twice in a row
must leave `data/` and `.env` untouched; that is one of the manual checks in §10.

### Upgrading

An upgrade is a routine deploy: `git pull` on the workstation, `make deploy`, then
`make smoke BASE=https://cadence.grepon.cc`. The image is rebuilt on the VM by
`docker compose up -d --build`; there is no registry and no image push. Take a backup first if
the release touches the schema:

```bash
ssh vm-201 'cd /home/user/docker/cadence && ./scripts/backup.sh'
```

### Changing a secret on a running install

**`docker compose restart` does not re-read `.env`.** Compose bakes `env_file` into the
container when it is *created*; `restart` restarts the process inside the existing container
with the environment it already had. Editing `VITALFORGE_TOKEN` or `OMNIROUTE_KEY` and then
restarting therefore appears to succeed and changes nothing — `/api/health` still reports
`"configured": false`, and the only visible symptom is that the thing you just configured
still behaves as unconfigured. Recreate instead:

```bash
cd ~/docker/cadence
${EDITOR:-nano} .env
docker compose up -d          # recreates the container; restart would not
curl -s http://100.74.76.39:8090/api/health   # expect "configured": true
```

This bites only on the *later* edit. The first-deploy flow in §2 writes `.env` before the
container exists, and `scripts/deploy.sh` uses `up -d`, so neither path hits it (D-258).

### Log locations

| What | Where |
|---|---|
| Application log | `docker compose logs -f cadence` on VM-201 |
| Rotated files | `/var/lib/docker/containers/<id>/<id>-json.log`, 10 MB × 3 (prod overlay) |
| Backup cron log | `/var/log/cadence-backup.log` |
| Health state | `docker inspect --format '{{json .State.Health}}' cadence \| jq .` — keep it scoped like this; see the warning at the end of the troubleshooting table below. |

---

## 4. Nginx Proxy Manager — `cadence.grepon.cc`

New Proxy Host:

| Field | Value |
|---|---|
| Domain Names | `cadence.grepon.cc` |
| Scheme | `http` |
| Forward Hostname / IP | **whatever the publish is bound to** — the host half of `docker compose port cadence 8000`. `192.168.1.21` on a default `0.0.0.0` bind; VM-201's Tailscale address once §5 narrows it |
| Forward Port | `8090` (the **host** port, not the container's 8000) |
| Cache Assets | **off** |
| Block Common Exploits | **on** |
| Websockets Support | **off** |
| SSL | request a Let's Encrypt certificate, Force SSL on, HTTP/2 on |
| Access List | optional, §5 |

**The forward address has to be one the publish is bound to.** §5 narrows the bind from
`0.0.0.0` to a Tailscale address, and **VM-201 is in that narrowed state** — D-257c records
`CADENCE_BIND_ADDR=100.74.76.39`, verified there on 2026-09-07, with `192.168.1.21:8090`
refusing. So this field is not the LAN IP on that host. Setting `CADENCE_BIND_ADDR` and leaving
this field on the LAN IP is the one ordering that turns a working proxy into a 502 with a
perfectly healthy container behind it. Do not copy an address from this document into NPM — read
the live one, which is authoritative in a way a document cannot be:

```bash
docker compose port cadence 8000     # e.g. 100.74.76.39:8090 - exactly what NPM needs
```

Advanced tab, one directive:

```nginx
client_max_body_size 5m;
```

`/api/import` accepts a pasted or uploaded YAML/JSON workout. NPM's default limit is 1 MB and a
larger paste fails as a 413 that reads like an app bug. 5 MB, not 25 MB — a workout document is
kilobytes, and a wider limit is needless upload surface. This follows VitalForge's own
precedent of raising the limit only for its import path.

**Cache Assets is off** because the service worker already caches Today; a proxy cache on top
of it serves a stale checklist after a deploy, from a layer neither the app nor the browser can
invalidate. **Websockets is off** because Cadence is HTMX over plain HTTP and has none.

---

## 5. Tailscale and access

**Cadence has no login of its own (D-009).** A login screen makes Today slower for a kid on the
floor, and the brief's security boundary is the homelab. The consequence is plain: anything
that can reach `cadence.grepon.cc` can read both profiles and write sessions.

Guards, in order of preference:

1. **Do not expose 8090 beyond the LAN; reach the host over Tailscale.** This is the baseline
   and is already true — there is no port forward from the internet.
2. **Add an NPM Access List** on the proxy host allowing the Tailscale CGNAT range
   `100.64.0.0/10` and denying everything else. This is the recommended addition and costs one
   form. **Then verify a denial:** request from a non-Tailscale LAN address and confirm a 403.
   An allow-list that never denies anything looks configured and guards nothing.
3. Only if Cadence ever has to be reachable from outside Tailscale, add the access-token
   middleware sketched in §9. Do not build it now.

> The access list applies **at NPM**. A client on the LAN hitting `192.168.1.21:8090` directly
> bypasses it entirely.

**Closing that gap** is one variable, no file edit — and on VM-201 it is **already closed**:
D-257c records `CADENCE_BIND_ADDR=100.74.76.39` set and verified there on 2026-09-07, with
`192.168.1.21:8090` refusing and `100.74.76.39:8090` answering. What follows is how it was
closed, and what a fresh install still has to do. `docker-compose.yml` publishes
`${CADENCE_BIND_ADDR:-0.0.0.0}:8090:8000`, so adding this to `.env` and running `make deploy`
binds the port to the Tailscale interface only:

```dotenv
CADENCE_BIND_ADDR=100.x.y.z    # VM-201's Tailscale address, from `tailscale ip -4`
```

Then verify from a non-Tailscale LAN address that `192.168.1.21:8090` is refused, the same way
§5's step 2 says to verify the access list actually denies. **The default is `0.0.0.0`**, which
is what a deploy publishes until somebody sets this — the gap is closed by the operator, not by
the repo.

Two things not to do. **Do not use `127.0.0.1`**: NPM runs on this host but is containerised, so
it reaches Cadence over a host interface and never over the host's loopback — a loopback-only
bind 502s the proxy. Whatever address you do bind, put that same one in NPM's Forward Hostname
field (§4). And **do not add a
`ports` entry to `docker-compose.prod.yml`**: compose *appends* `ports` across `-f` files rather
than overriding them, so the base mapping stays published alongside the new one and the two
collide on host 8090 (D-206). The address belongs in the base file's single entry or nowhere.

---

## 6. Backups and restore

`scripts/backup.sh` runs on the host, against the live database, with the app running.

Run it **inside the container**:

```bash
cd /home/user/docker/cadence && docker compose exec -T cadence ./scripts/backup.sh
# backup: /app/data/backups/cadence-20260907-0317.db (via python)
```

The snapshot lands in `./data/backups/` on the host either way, because `data/` is a bind
mount. Inside is the default because `data/` is owned by uid 10001: a host cron running as the
deploying user cannot write into `data/backups/` and the run fails with `unable to open
database file` on the *output* file, which reads exactly like the input-side failure in §8 and
is not the same problem at all. Running it on the host works when the caller is root or owns
`data/`:

```bash
sudo -u '#10001' env CADENCE_DB_PATH=/home/user/docker/cadence/data/cadence.db /home/user/docker/cadence/scripts/backup.sh
```

It uses `sqlite3 "$DB" ".backup"`, never `cp`. The database runs in WAL mode, so the `.db` file
alone can be missing the newest committed transactions; `.backup` takes a consistent snapshot
of a live database. It then runs `PRAGMA integrity_check` **on the copy** — a snapshot that
cannot be opened is worse than none, and this catches it while the original is still there. It
prunes to the newest 30 only after a successful, verified backup, so a failed run never eats
the good history.

The script sets `umask 077`, so each snapshot is written `0600`, and then `chmod 700` on
`data/backups/` itself. Both are needed: the umask only governs what this script creates, and
on VM-201 it does not create that directory — `scripts/deploy.sh` runs `mkdir -p data/backups`
on every deploy, so it already exists at `0755` and the files alone would have tightened. A
snapshot is the entire database — both profiles' training history, bodyweights and body-fat
readings — and VM-201 is shared with VitalForge, while the `.env` beside it is `600` (D-201).
Snapshots taken before this was added keep their old modes: run
`chmod 600 /home/user/docker/cadence/data/backups/*.db` once, if any exist.

Cron, installed by hand on VM-201 (`crontab -e`):

```cron
17 3 * * * cd /home/user/docker/cadence && docker compose exec -T cadence ./scripts/backup.sh >> /var/log/cadence-backup.log 2>&1
```

`-T` matters: cron has no TTY, and without it `docker compose exec` fails every night with
`the input device is not a TTY` and the log fills with it instead of with backups.

03:17 rather than 03:00, to miss whatever else on this host runs on the hour.

### Restore

```bash
cd /home/user/docker/cadence
docker compose down

# All three, not just the .db. A stale -wal beside a restored database is replayed over it by
# SQLite on the next open, which silently undoes the restore. This is the step everyone forgets.
mkdir -p data/broken
mv data/cadence.db data/cadence.db-wal data/cadence.db-shm data/broken/ 2>/dev/null || true

cp data/backups/cadence-20260907-0317.db data/cadence.db
sudo chown 10001:10001 data/cadence.db
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
make smoke BASE=https://cadence.grepon.cc
```

---

## 7. Smoke test

```bash
make smoke                                     # against http://localhost:8090
make smoke BASE=https://cadence.grepon.cc      # against the VM
```

Eight steps, exiting non-zero at the first failure with the step number and the response body:

| Step | Check |
|---|---|
| 1 | `/api/health` is 200 **and** `ok: true` **and** `data.db == "ok"`, and the server booted in `mock` mode |
| 2 | `/today?profile=me` renders at least one checklist row |
| 3 | `/api/today?profile=me` yields a `session_id` and a first row |
| 4 | ticking that row returns `done: true` |
| 5 | ticking it again still returns `done: true` — the offline replay path |
| 6 | `/api/sessions/{id}/done` is 200 |
| 7 | `/done/{id}` reports `synced ✓`, **and** the mock recorded exactly one activity for that session |
| 8 | `/api/metrics?profile=me` returns an intact envelope with `ok: true` |

Step 1 refuses to continue unless the server reports `CADENCE_VITALFORGE_MODE=mock`, so the
smoke test never writes a real activity into Garmin. `SMOKE_ALLOW_LIVE=1` overrides that, and
means what it says. Note that the run **finishes a real session**, consuming one planned day.

Step 7 checks both halves of one question. The Done screen's line renders a `sync_job` row, so
it reads the same whether the client handed VitalForge one activity, none, or two; the count
comes from `GET /api/_mock/activities`, which exists **only** when the mode is `mock` and
`CADENCE_ENV` is not `prod`. On the VM that route is absent and answers 404, so a smoke run
against production needs `SMOKE_ALLOW_LIVE=1` and step 7 skips the recorder count, keeping only
the `#sync-status` assertion.

What that assertion accepts depends on the mode, because the two are asking different
questions. Under `mock` nothing can fail at the network, so the job must reach `sent` and read
`synced ✓`. Against a live VitalForge the smoke test's job is to prove **Cadence** works, not
that VitalForge is reachable, so it accepts any state that means the session reached the queue:

| `data-sync` | Line | Live | Why |
|---|---|---|---|
| `sent` | `synced ✓` | pass | VitalForge accepted the activity. |
| `pending` | `will sync` | pass | **The state on VM-201 today.** A 404 from the not-yet-deployed activity endpoint is saved without spending an attempt (D-137), and reads as "will sync" — not as a failure. |
| `failed` | `sync failed (retrying)` | pass | Transport trouble; the queue will retry on its own. Matched on the full sentence, since `sync failed` alone is the *terminal* line. |
| `failed` | `sync failed` | **fail** | Terminal: a 409 or 422 that has given up. |
| `skipped` | `stored locally — VitalForge not configured` | **fail** | No token reached the container — a misconfigured deploy that otherwise looks healthy. Check `VITALFORGE_TOKEN` in the VM's `.env`. |

So a smoke run against VM-201 passes today, before the VitalForge `cadence/activity-endpoint`
branch is deployed (D-005), and reports `will sync` while it does.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `unable to open database file` at startup | `./data` is not owned by uid 10001. `sudo chown -R 10001:10001 /home/user/docker/cadence/data`. On the workstation under rootless podman: `podman unshare chown -R 10001:10001 data`. |
| The same message on the workstation *after* the chown | SELinux. The compose file mounts `./data:/app/data:Z` for this; if a container is started by hand, pass `:Z` too. Not a VM-201 problem. |
| `unable to open database file` from `backup.sh`, app running fine | The *output* path, not the input: the caller cannot write `data/backups/`, which uid 10001 owns. Run the backup inside the container (§6). |
| `make dev-docker` shows no health status | podman-compose builds an OCI image and OCI has no healthcheck field. The target builds with `--format docker` first for this reason; a hand-run `podman build` needs the same flag. |
| `sync failed`, `last_error` names the `cadence/activity-endpoint` branch | PRP-05 is not deployed on VitalForge yet. Expected until that branch is merged and VitalForge redeployed. |
| Done shows `stored locally — VitalForge not configured` | `VITALFORGE_TOKEN` is blank in the VM's `.env`, most likely rsynced over. Restore `.env` and check `--exclude '.env'` is still in `scripts/deploy.sh`. |
| Container restart loop | The process is **exiting**, and `restart: unless-stopped` is putting it back. Read `docker compose logs --tail 50 cadence` first, not the healthcheck. The usual cause is a `.env` that raises at startup — `CADENCE_ENV=prod` with any `*_MODE=mock` is refused by design (D-139) and loops forever, since the settings never get a chance to be wrong differently. |
| Container reports `unhealthy` but keeps running | The opposite case, and the one `restart:` does **not** cover: Docker's restart policy acts on exit, never on a failing healthcheck, so an unhealthy container stays up and NPM keeps sending it traffic. `docker inspect --format '{{json .State.Health}}' cadence \| jq .` shows the last outputs; a 200 with `ok: false` means the database check failed, so check `./data` ownership. |
| 502 from NPM | Container down, or NPM is pointed at container port 8000 instead of host port 8090. |
| — | **Keep `docker inspect` scoped, as the rows above do.** A bare `docker inspect cadence` prints `Config.Env`, which is every variable the container was started with — including `VITALFORGE_TOKEN` and `OMNIROUTE_KEY` in full. That output routinely gets pasted into a chat or an issue. Always pass `--format`, e.g. `--format '{{json .State.Health}}'`. |
| `make deploy` succeeds but nothing changes, or stops at a missing `.env` on a host that has one | It deployed to the **wrong directory**. `CADENCE_DEPLOY_PATH` defaults to `/opt/cadence` and VM-201's install is `/home/user/docker/cadence` (D-257a). `scripts/deploy.sh` now refuses a path that does not exist (exit 4) instead of creating one, but a *different* prepared directory is still a valid target — check the `deploy: rsync ... -> host:path` line it prints (D-281). |
| 413 on Import | `client_max_body_size 5m` missing from the proxy host's Advanced tab (§4). |
| Disk full on VM-201 | Unrotated container logs. The prod overlay exists to prevent this — confirm the deploy used `-f docker-compose.prod.yml`. |
| `/today` is stale after a deploy | NPM Cache Assets is on, or the phone's service worker is holding the old shell. Turn caching off and hard-reload once. |

---

## 9. If auth is ever needed (not built)

D-009 says Cadence has no login. If it ever must be reachable from outside Tailscale, the
smallest thing that works is a single middleware reading `CADENCE_ACCESS_TOKEN` from `.env` and
comparing it with `hmac.compare_digest` against either an `Authorization: Bearer` header or a
long-lived cookie set by a one-field `/unlock` page, skipping `/api/health` and `/static/*`. A
blank token means disabled, so the default stays no-auth. Roughly 40 lines. It is documented
here and deliberately not implemented.

---

## 10. Rollback

```bash
git checkout prp-08          # tags are prp-NN
make deploy
```

The database is forward-compatible within a schema version, and Cadence's `schema_version`
check fails loudly at startup rather than corrupting data if it is not. If the rollback crosses
a schema change, restore the backup taken before the upgrade (§6) as well.

---

## 11. Installing on a phone (D-117)

Cadence is a PWA served by this container. There is no native Android app.

1. Open `https://cadence.grepon.cc/today` in Chrome on the phone, on Tailscale.
2. Menu (⋮) → **Add to Home screen** → Install.
3. It opens standalone, with Today cached offline and the tick queue in IndexedDB.

On iOS: Share → **Add to Home Screen**. A Trusted Web Activity wrapper (PWABuilder/Bubblewrap
→ a sideloadable APK, needing an `assetlinks.json` on the domain) is an optional follow-up, not
part of this build.

---

## 12. First-deploy checklist

Tick and date these on the first real deploy.

- [ ] `make deploy` from a clean workstation checkout succeeds end to end.  _date:_
- [ ] `https://cadence.grepon.cc/today` loads over Tailscale, and does **not** load from a
      non-Tailscale address once the access list is on.  _date:_
- [ ] `docker compose ps` shows `healthy`, not just `running`.  _date:_
- [ ] `./scripts/backup.sh` produces a file and the cron line is installed.  _date:_
- [ ] `make smoke BASE=https://cadence.grepon.cc` exits 0.  _date:_
