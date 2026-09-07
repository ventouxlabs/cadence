#!/usr/bin/env bash
# End-to-end smoke test against a RUNNING Cadence, locally or on VM-201.
#
#   BASE=http://localhost:8090 scripts/smoke.sh
#   make smoke BASE=https://cadence.grepon.cc
#
# It ticks a real row and finishes a real session, so point it at an instance whose data you do
# not mind advancing by one planned day. CADENCE_VITALFORGE_MODE=mock keeps the write-back off
# real VitalForge and therefore off Garmin; step 1 refuses to continue without it unless
# SMOKE_ALLOW_LIVE=1 says the operator meant it.
#
# Exits non-zero on the first failure, naming the step number and printing the response.

set -euo pipefail

BASE="${BASE:-http://localhost:8090}"
BOOT_TIMEOUT_S="${SMOKE_BOOT_TIMEOUT_S:-40}"
PROFILE="${SMOKE_PROFILE:-me}"
BODY=""
STATUS=""

command -v jq >/dev/null 2>&1 || { echo "smoke: jq is not installed" >&2; exit 2; }

fail() {
  echo "smoke: FAILED at step $1: $2" >&2
  [[ -n "$BODY" ]] && echo "--- response (HTTP ${STATUS:-?}) ---" >&2 && printf '%s\n' "${BODY:0:2000}" >&2
  exit 1
}

# Sets BODY and STATUS. Never `set -e`-exits on a non-2xx: the caller decides what a status means.
request() {
  local method="$1" path="$2" payload="${3:-}"
  local args=(-sS -o - -w '\n%{http_code}' -X "$method" --max-time 20)
  [[ -n "$payload" ]] && args+=(-H 'Content-Type: application/json' -d "$payload")
  local raw
  raw="$(curl "${args[@]}" "$BASE$path" || true)"
  STATUS="${raw##*$'\n'}"
  BODY="${raw%$'\n'*}"
}

step() { echo "smoke: step $1 - $2"; }

# --------------------------------------------------------------- 1. health, and the boot wait
#
# The wait is part of step 1 on purpose: an app that never listened and an app that answers
# `ok: false` are the same failure from the operator's chair, and both must name step 1.
step 1 "GET /api/health"
deadline=$(( $(date +%s) + BOOT_TIMEOUT_S ))
while :; do
  request GET /api/health
  [[ "$STATUS" == "200" ]] && break
  [[ $(date +%s) -ge $deadline ]] && fail 1 "no HTTP 200 from $BASE/api/health within ${BOOT_TIMEOUT_S}s"
  sleep 1
done
jq -e '.ok == true' >/dev/null <<<"$BODY" || fail 1 "/api/health returned ok != true (a 200 with ok:false is a broken app)"
jq -e '.data.db == "ok"' >/dev/null <<<"$BODY" || fail 1 "/api/health reports the database is not ok"

# CADENCE_VITALFORGE_MODE, read back off the health payload rather than off this shell's
# environment: what matters is the mode the running server booted with.
MODE="$(jq -r '.data.vitalforge.mode // .data.vitalforge_mode // "unknown"' <<<"$BODY")"
echo "smoke: vitalforge mode = $MODE"
if [[ "$MODE" != "mock" && "${SMOKE_ALLOW_LIVE:-0}" != "1" ]]; then
  fail 1 "refusing to run: vitalforge mode is '$MODE', not 'mock'. Set CADENCE_VITALFORGE_MODE=mock on the server, or SMOKE_ALLOW_LIVE=1 to accept a real write-back."
fi

# ------------------------------------------------------------------------ 2. the Today screen
step 2 "GET /today?profile=$PROFILE"
request GET "/today?profile=$PROFILE"
if [[ "$STATUS" == "303" ]]; then
  fail 2 "/today redirected to Setup: this install has not been seeded or set up yet. Run 'docker compose exec cadence python -m cadence.bibliotheque.seed' and finish Setup in a browser."
fi
[[ "$STATUS" == "200" ]] || fail 2 "expected HTTP 200, got $STATUS"
grep -q "id=\"row-$PROFILE-" <<<"$BODY" || fail 2 "no checklist row rendered on /today"

# ------------------------------------------------------------------- 3. the JSON Today payload
step 3 "GET /api/today?profile=$PROFILE"
request GET "/api/today?profile=$PROFILE"
[[ "$STATUS" == "200" ]] || fail 3 "expected HTTP 200, got $STATUS"
SESSION_ID="$(jq -r '.data.session_id // empty' <<<"$BODY")"
POSITION="$(jq -r '.data.rows[0].position // empty' <<<"$BODY")"
[[ -n "$SESSION_ID" && -n "$POSITION" ]] || fail 3 "no session_id or no rows in the payload"
echo "smoke: session $SESSION_ID, first row at position $POSITION"

# ----------------------------------------------------------------------------- 4. tick a row
step 4 "POST /api/sessions/$SESSION_ID/rows/$POSITION"
request POST "/api/sessions/$SESSION_ID/rows/$POSITION" '{"done": true}'
[[ "$STATUS" == "200" ]] || fail 4 "expected HTTP 200, got $STATUS"
jq -e '.data.done == true' >/dev/null <<<"$BODY" || fail 4 "the row did not come back done"

# ---------------------------------------------------------------- 5. tick it again, unchanged
#
# The one operation the PWA replays out of IndexedDB after an offline spell. A tick that is not
# idempotent corrupts the count silently, and nothing on screen says so.
step 5 "POST the same tick again (idempotency)"
request POST "/api/sessions/$SESSION_ID/rows/$POSITION" '{"done": true}'
[[ "$STATUS" == "200" ]] || fail 5 "a replayed tick returned $STATUS, not 200"
jq -e '.data.done == true' >/dev/null <<<"$BODY" || fail 5 "a replayed tick left the row not done"

# --------------------------------------------------------------------------- 6. finish it
step 6 "POST /api/sessions/$SESSION_ID/done"
request POST "/api/sessions/$SESSION_ID/done" '{"felt": "right"}'
[[ "$STATUS" == "200" ]] || fail 6 "expected HTTP 200, got $STATUS"

# ------------------------------------------------------------------------- 7. the Done screen
#
# Two halves of one question. The sync line reports the server-side write-back to VitalForge
# (PRP-06 §4.3); in mock mode the job reaches `sent`, so the honest line is "synced ✓" and
# anything else means the write-back did not complete.
#
# The line alone cannot prove an activity was actually handed over: it renders a `sync_job`
# row, so a write-back that recorded nothing and one that recorded twice produce identical
# words. The mock's recorder is asked directly for that, through the mock-only inspection
# route (D-169).
#
# Scoped to the element, never to the page. Every screen that extends base.html carries a
# hidden offline banner reading "Saved on this phone - will sync", so a grep over the whole
# body matches "will sync" on any page at all - including one with no sync line on it.
# PRP-06 section 4.3 is explicit that the banner and the sync line must stay distinct.
step 7 "GET /done/$SESSION_ID, and what the mock actually recorded"
request GET "/done/$SESSION_ID?profile=$PROFILE"
[[ "$STATUS" == "200" ]] || fail 7 "expected HTTP 200, got $STATUS"

# Flattened first: the template puts the text on the line after the tag, and grep is
# line-oriented, so an unflattened match returns the attributes and none of the words.
SYNC_EL="$(tr '\n' ' ' <<<"$BODY" | grep -o 'id="sync-status"[^>]*>[^<]*' || true)"
[[ -n "$SYNC_EL" ]] || fail 7 "no #sync-status element on /done/$SESSION_ID"

# What counts as a pass here depends on the mode, because the two are asking different
# questions. Under mock nothing can go wrong at the network: the job must reach `sent` and read
# "synced ✓", and anything else is a real defect.
#
# Against a live VitalForge the smoke test's job is to prove *Cadence* works, not that
# VitalForge is reachable - PRP-05's endpoint branch is deliberately unpushed (D-005), so on
# VM-201 today the POST gets a 404. That path saves the job FAILED with `attempts` untouched
# (cadence/vitalforge/outcomes.py, D-137), which `line_for` maps to "will sync", *not* to
# "sync failed (retrying)". Accepting only the two failure wordings would therefore reject the
# exact state this relaxation exists for (D-207).
LINE_SENT="synced ✓"
LINE_PENDING="will sync"
LINE_RETRYING="sync failed (retrying)"
LINE_SKIPPED="stored locally — VitalForge not configured"

SYNC_STATUS="$(grep -o 'data-sync="[^"]*"' <<<"$SYNC_EL" | head -1 | cut -d'"' -f2)"
[[ -n "$SYNC_STATUS" ]] || fail 7 "no data-sync attribute on #sync-status. Element: $SYNC_EL"

# Rejected in every mode, and first, so the message names the real problem rather than the
# generic one. `skipped` means no token reached the container - a prod misconfiguration that
# looks like a working deploy until someone opens Done.
if [[ "$SYNC_STATUS" == "skipped" ]]; then
  fail 7 "VitalForge is not configured on this server ('$LINE_SKIPPED'): check VITALFORGE_TOKEN in the VM's .env"
fi

if [[ "$MODE" == "mock" ]]; then
  [[ "$SYNC_STATUS" == "sent" ]] \
    || fail 7 "mock mode must reach 'sent', got data-sync='$SYNC_STATUS'. Element: $SYNC_EL"
  grep -qF "$LINE_SENT" <<<"$SYNC_EL" \
    || fail 7 "the Done screen says 'sent' but does not read '$LINE_SENT'. Element: $SYNC_EL"
  echo "smoke: sync line = '$LINE_SENT' (data-sync=sent)"
else
  # `failed` is matched on the full retrying sentence, never a substring: LINE_TERMINAL is
  # "sync failed", a strict prefix of "sync failed (retrying)", so a loose match would accept
  # the terminal 409/422 case - which has given up and is a genuine failure.
  case "$SYNC_STATUS" in
    sent)
      grep -qF "$LINE_SENT" <<<"$SYNC_EL" \
        || fail 7 "data-sync='sent' but the line does not read '$LINE_SENT'. Element: $SYNC_EL"
      echo "smoke: sync line = '$LINE_SENT' - VitalForge accepted the activity" ;;
    pending)
      grep -qF "$LINE_PENDING" <<<"$SYNC_EL" \
        || fail 7 "data-sync='pending' but the line does not read '$LINE_PENDING'. Element: $SYNC_EL"
      echo "smoke: sync line = '$LINE_PENDING' - queued; expected until VitalForge's activity endpoint is deployed" ;;
    failed)
      grep -qF "$LINE_RETRYING" <<<"$SYNC_EL" \
        || fail 7 "the write-back failed and is not retrying (terminal). Element: $SYNC_EL"
      echo "smoke: sync line = '$LINE_RETRYING' - queued for retry" ;;
    *)
      fail 7 "unexpected data-sync='$SYNC_STATUS'. Element: $SYNC_EL" ;;
  esac
fi

# Only under mock. The route is mounted when the mode is mock and CADENCE_ENV is not prod, so on
# VM-201 it answers 404 and an unconditional assertion here made `make smoke BASE=https://...`
# impossible to pass - which PRP-09 §9 requires. Reaching this point at all means SMOKE_ALLOW_LIVE=1.
if [[ "$MODE" == "mock" ]]; then
  request GET /api/_mock/activities
  [[ "$STATUS" == "200" ]] || fail 7 "/api/_mock/activities returned $STATUS (mock mode should mount it)"
  RECORDED="$(jq --arg id "$SESSION_ID" '[.data.activities[] | select(.session_id == $id)] | length' <<<"$BODY")"
  [[ "$RECORDED" == "1" ]] \
    || fail 7 "the mock VitalForge recorded $RECORDED activities for this session, expected exactly 1"
  echo "smoke: mock recorded exactly 1 activity for $SESSION_ID"
else
  echo "smoke: mode is '$MODE', not mock - skipping the mock recorder check (the route is not mounted)"
fi

# --------------------------------------------------------------------------- 8. the metrics
step 8 "GET /api/metrics?profile=$PROFILE"
request GET "/api/metrics?profile=$PROFILE"
[[ "$STATUS" == "200" ]] || fail 8 "expected HTTP 200, got $STATUS"
jq -e 'has("ok") and has("data") and has("error") and has("meta")' >/dev/null <<<"$BODY" \
  || fail 8 "the response envelope is missing one of ok/data/error/meta"
jq -e '.ok == true' >/dev/null <<<"$BODY" || fail 8 "/api/metrics answered ok:false"

echo "smoke: OK - all steps passed against $BASE"
