#!/usr/bin/env bash
# Deploy Cadence to VM-201.
#
#   scripts/deploy.sh [--mode rsync|git] [--host HOST] [--path PATH] [--no-build]
#
# rsync (default) ships the working tree, including an unpushed branch - which matters because
# PRP-05's VitalForge branch is deliberately never pushed (D-005). `--mode git` pulls on the VM
# instead, so the VM's state is an auditable git ref; it needs the commit pushed first.
#
#   CADENCE_DEPLOY_HOST  ssh host alias   (default: vm-201, from ~/.ssh/config)
#   CADENCE_DEPLOY_PATH  remote directory (default: /opt/cadence)

set -euo pipefail

MODE="rsync"
HOST="${CADENCE_DEPLOY_HOST:-vm-201}"
REMOTE_PATH="${CADENCE_DEPLOY_PATH:-/opt/cadence}"
BUILD="--build"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
APP_UID=10001

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --path) REMOTE_PATH="${2:-}"; shift 2 ;;
    --no-build) BUILD=""; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "deploy: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" != "rsync" && "$MODE" != "git" ]]; then
  echo "deploy: unknown mode '$MODE' (expected rsync or git)" >&2
  exit 2
fi

# ------------------------------------------------------------------ the host and the path
#
# Both reach a remote shell: the path is interpolated into `ssh HOST "cd '$REMOTE_PATH' && ..."`
# and the host is handed to ssh and rsync as their target. Single-quoting the path in those
# strings is not enough on its own, because a path containing a quote closes it and everything
# after runs on VM-201 as the deploying user, which has passwordless sudo (D-204). A host is
# worse than a string: ssh reads a leading `-` as an option, so `-oProxyCommand=...` executes
# on *this* machine before a connection is opened.
#
# So both are validated against what they are actually allowed to be, before either is used.
# These are checked ahead of the reachability probe on purpose - the probe is the first thing
# that hands $HOST to ssh.
if [[ ! "$HOST" =~ ^([A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9_]([A-Za-z0-9._-]*[A-Za-z0-9])?$ ]]; then
  echo "deploy: refusing host '$HOST'." >&2
  echo "        Expected an ssh alias, hostname or IP - letters, digits, dot, dash, underscore," >&2
  echo "        with an optional 'user@' - and never a leading '-', which ssh reads as an option." >&2
  exit 2
fi
# Absolute, and no `..`: the deploy runs `rsync --delete` into this directory and chowns it.
if [[ ! "$REMOTE_PATH" =~ ^/[A-Za-z0-9._/-]*$ ]] || [[ "$REMOTE_PATH" == *".."* ]]; then
  echo "deploy: refusing remote path '$REMOTE_PATH'." >&2
  echo "        Expected an absolute path of letters, digits, dot, dash, slash and underscore," >&2
  echo "        with no '..'. This path is interpolated into a remote shell command." >&2
  exit 2
fi

# Reachability first, so a missing ssh key fails here with one clear line rather than halfway
# through an rsync that has already deleted things on the far end.
echo "deploy: checking ssh to '$HOST'"
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$HOST" true; then
  echo "deploy: cannot reach '$HOST' over ssh without a password." >&2
  echo "        Add a host alias to ~/.ssh/config, or set CADENCE_DEPLOY_HOST." >&2
  exit 3
fi

# The remote directory has to exist already, and this script must not create it (D-281).
#
# `mkdir -p` here is what turned a wrong `CADENCE_DEPLOY_PATH` into a *second* install beside the
# running one. The default is `/opt/cadence` (D-161) and VM-201's install is at
# `/home/user/docker/cadence` (D-257a), so a bare `make deploy` created the wrong directory,
# rsynced into it, and stopped at `up` with a missing `.env` - which `docs/deploy.md` section 2
# describes as the *expected* stop on a first deploy. The operator reads a normal message and
# concludes the deploy worked as documented, while the running app was never touched.
#
# Requiring the directory costs nothing: section 2 already tells the operator to create it once.
echo "deploy: checking $HOST:$REMOTE_PATH"
REMOTE_STATE="$(ssh -- "$HOST" "
  if [ ! -d '$REMOTE_PATH' ]; then echo missing
  elif [ -d '$REMOTE_PATH/.git' ]; then echo clone
  elif [ -f '$REMOTE_PATH/.env' ] || [ -f '$REMOTE_PATH/docker-compose.yml' ]; then echo install
  else echo bare
  fi")"

# Fails closed on anything unexpected. An empty answer means the probe did not run as written,
# and "carry on and hope" is the behaviour this whole block exists to remove.
case "$REMOTE_STATE" in
  missing|clone|install|bare) ;;
  *)
    echo "deploy: could not tell what '$REMOTE_PATH' is on $HOST (got '$REMOTE_STATE')." >&2
    echo "        Refusing rather than guessing. Check ssh to '$HOST' by hand." >&2
    exit 4
    ;;
esac

if [[ "$REMOTE_STATE" == "missing" ]]; then
  echo "deploy: '$REMOTE_PATH' does not exist on $HOST." >&2
  echo "        Deploying there would create a second install beside the running app" >&2
  echo "        rather than update it, and stop at a missing .env that reads like the" >&2
  echo "        expected first-deploy pause." >&2
  echo >&2
  echo "        If this host's install lives elsewhere, name it - absolute, since a leading" >&2
  echo "        '~' is refused by the path check above:" >&2
  echo "          CADENCE_DEPLOY_PATH=/path/to/cadence make deploy" >&2
  echo "        See docs/deploy.md section 3 for this host's value." >&2
  echo >&2
  echo "        If this really is a first deploy, create it first:" >&2
  echo "          ssh $HOST 'mkdir -p $REMOTE_PATH'" >&2
  exit 4
fi

# `--mode git` pulls, so it needs a clone and not merely a directory. Caught here rather than by
# `git pull` failing halfway, because by then the chown and `up` below are still queued.
if [[ "$MODE" == "git" && "$REMOTE_STATE" != "clone" ]]; then
  echo "deploy: '$REMOTE_PATH' on $HOST is not a git clone, and --mode git pulls into one." >&2
  echo "        Either clone it there, or use the default rsync mode, which ships the working" >&2
  echo "        tree and carries unpushed branches (D-005, D-161)." >&2
  exit 4
fi

if [[ "$MODE" == "rsync" ]]; then
  echo "deploy: rsync $REPO_ROOT/ -> $HOST:$REMOTE_PATH"
  # The first three excludes are load-bearing next to --delete. Without --exclude '.env' the VM's
  # real VITALFORGE_TOKEN and OMNIROUTE_KEY are wiped and Cadence comes back with sync skipped;
  # without --exclude 'data' the production database is deleted outright. Never type this by hand.
  #
  # docker-compose.dev.yml is excluded for a different reason: it is not dangerous to overwrite,
  # it is dangerous to *have* there. It sets CADENCE_VITALFORGE_MODE=mock, and a copy-pasted
  # `-f docker-compose.dev.yml` on the VM would put production in mock mode - every session
  # reported synced while nothing reaches Garmin, with the inspection route mounted (D-200).
  rsync -az --delete \
    --exclude '.env' \
    --exclude 'data' \
    --exclude 'docker-compose.dev.yml' \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    --exclude '.omc' \
    --exclude 'node_modules' \
    --exclude '.shots' \
    -- "$REPO_ROOT/" "$HOST:$REMOTE_PATH/"
else
  echo "deploy: git pull on $HOST:$REMOTE_PATH"
  ssh -- "$HOST" "cd '$REMOTE_PATH' && git pull --ff-only"
fi

# uid 10001 is the container user. A bind mount created by compose is root-owned, and the app
# then fails with `unable to open database file` - the classic first-deploy failure.
#
# All three under sudo, and the chown before the chmod (D-285). The old line was
# `mkdir -p data/backups && chmod 700 data/backups && sudo chown ...`, with only the chown
# elevated. On any host that has run once, `data/` already belongs to uid 10001, so the
# deploying user cannot chmod into it - and `chmod` refuses even when the mode is already
# correct, because it is not the owner. Under `set -e` that killed the deploy *after* the rsync
# and *before* `compose up`: new code on disk, old container still serving, and a message that
# reads like a permissions warning rather than "your deploy did not happen". That is D-277's
# failure exactly, in the script D-277 did not look at.
ssh -- "$HOST" "cd '$REMOTE_PATH' && sudo mkdir -p data/backups && sudo chown -R $APP_UID:$APP_UID data && sudo chmod 700 data/backups"
ssh -- "$HOST" "cd '$REMOTE_PATH' && $COMPOSE up -d $BUILD"
ssh -- "$HOST" "cd '$REMOTE_PATH' && $COMPOSE ps && $COMPOSE logs --tail 30 cadence"

echo "deploy: done. Smoke it with: make smoke BASE=https://cadence.grepon.cc"
