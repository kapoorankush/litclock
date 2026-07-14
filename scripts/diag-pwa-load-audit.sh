#!/usr/bin/env bash
# diag-pwa-load-audit.sh — measure PWA page TTFB to confirm/refute #436 bottleneck #2
# (the synchronous SSR subprocess fan-out that gates /diagnostics first paint).
#
# Run this ON authorclock. It does NOT need a reboot — a `systemctl restart
# litclock-control` gives the cold subprocess cache we want, cleanly and repeatably.
# The optional final section covers the full-cold reboot case; run it separately.
#
# Reference: issue #436 measurement plan + #430 (DIAG_SUBPROC_TTL_S = 20s).
set -u

BASE="${LITCLOCK_CONTROL_BASE:-http://localhost}"  # #343: control_server on port 80
CURL_FMT='ttfb=%{time_starttransfer}  total=%{time_total}  http=%{http_code}  bytes=%{size_download}\n'
SVC="litclock-control.service"
TTL=20   # DIAG_SUBPROC_TTL_S — subprocess cache lifetime, seconds

hit() {  # hit <path> <label>
  local path="$1" label="$2"
  printf '  %-14s ' "$label"
  curl -s -o /dev/null -w "$CURL_FMT" "${BASE}${path}"
}

echo "############################################################"
echo "# #436 PWA load audit — $(date -Is)"
echo "# host=$(hostname)  base=${BASE}"
echo "############################################################"

# ---------------------------------------------------------------------------
echo
echo "### STEP 1 — /diagnostics TTFB: cold vs warm ###############"
echo "Restarting ${SVC} to force a cold subprocess cache..."
sudo systemctl restart "$SVC"
# wait for waitress to accept connections again (don't measure the boot race)
for _ in $(seq 1 30); do
  curl -s -o /dev/null "${BASE}/" && break
  sleep 0.5
done
echo "Server up. Firing 5 back-to-back requests (1st = cold, 2-5 = warm within ${TTL}s TTL):"
for n in 1 2 3 4 5; do hit /diagnostics "req#${n}"; done
echo "Waiting $((TTL + 5))s for the subprocess cache TTL to expire..."
sleep $((TTL + 5))
hit /diagnostics "TTL-expired"

# ---------------------------------------------------------------------------
echo
echo "### STEP 2 — compare to no-subprocess pages ###############"
echo "(isolates collect_diagnostics() fan-out vs. baseline Flask/waitress+template)"
echo "Restarting ${SVC} again so all three pages start equally cold..."
sudo systemctl restart "$SVC"
for _ in $(seq 1 30); do curl -s -o /dev/null "${BASE}/" && break; sleep 0.5; done
hit /            "Status (/)"
hit /settings    "Settings"
hit /diagnostics "Diagnostics"
echo "Warm re-hit of the same three:"
hit /            "Status (/)"
hit /settings    "Settings"
hit /diagnostics "Diagnostics"

# ---------------------------------------------------------------------------
echo
echo "### STEP 3 — attribute cost within the readout ###########"
echo "Running scripts/diag-subprocess-timing.py (idle). For paint-cycle / memory-"
echo "pressure runs, re-run this script's STEP 3 command while those conditions hold."
TIMING="$(dirname "$0")/diag-subprocess-timing.py"
VENV_PY="/home/pi/litclock/venv/bin/python3"
if [ -x "$VENV_PY" ] && [ -f "$TIMING" ]; then
  "$VENV_PY" "$TIMING"
else
  echo "  (skipped — venv python or timing script not found; run manually:"
  echo "   $VENV_PY $TIMING )"
fi

echo
echo "############################################################"
echo "# DONE (no-reboot path)."
echo "#"
echo "# OPTIONAL full-cold case (run separately): sudo reboot, wait ~90s for the"
echo "# clock to settle, then immediately:"
echo "#   curl -s -o /dev/null -w '$CURL_FMT' ${BASE}/diagnostics"
echo "#"
echo "# READ: if cold req#1 and TTL-expired TTFB are materially above warm (2-5)"
echo "# AND above Status/Settings, bottleneck #2 is real -> skeleton+hydrate PR."
echo "# If the cold delta is small, #465 was enough -> close #436 lean."
echo "############################################################"
