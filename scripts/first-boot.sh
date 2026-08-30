#!/bin/bash
#
# First-Boot Orchestration for LitClock
#
# This script runs on first boot to guide the user through setup:
# 1. Display welcome message on e-ink
# 2. Create WiFi hotspot if needed
# 3. Show hotspot credentials + QR code on e-ink
# 4. Start web setup server (WiFi selection + settings)
# 5. Wait for user to complete setup
# 6. Mark setup as complete and start clock
#

# Configuration
INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
PYTHON="$INSTALL_DIR/venv/bin/python3"
CONFIG_DIR="/etc/litclock"
SETUP_COMPLETE_FLAG="$CONFIG_DIR/.setup-complete"
ENV_FILE="$INSTALL_DIR/env.sh"
SIGNAL_FILE="/tmp/litclock-setup-done"
LOG_FILE="$INSTALL_DIR/first-boot.log"
HOTSPOT_MAX_RETRIES=5
HOTSPOT_RETRY_DELAY=15

# Shared env.sh writer helpers (issue litclock-dev#274). Sourced so the default-env-
# creation path below routes through the same sidecar-flock atomic writer
# that update.sh / reset-setup.sh / prepare-for-cloning.sh use. Without
# this, first-boot.sh would be the only env.sh writer not respecting the
# cross-writer interlock with the Python PWA writer in src/config.py.
_FIRST_BOOT_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$_FIRST_BOOT_SCRIPT_DIR/lib/state.sh" ]]; then
    # shellcheck source=/dev/null
    . "$_FIRST_BOOT_SCRIPT_DIR/lib/state.sh"
fi

# Restore /etc/issue to the default saved during image build.
# Falls back to a minimal default if /etc/issue.default is missing (e.g. dev Pi
# not built with pi-gen). Also saves a backup before writing the hotspot banner.
restore_issue() {
    if [[ -f /etc/issue.default ]]; then
        sudo cp /etc/issue.default /etc/issue 2>/dev/null || true
    elif [[ -f /etc/issue.bak ]]; then
        sudo cp /etc/issue.bak /etc/issue 2>/dev/null || true
    else
        # Minimal fallback — just the OS identity line
        printf 'Raspberry Pi OS \\n \\l\n\n' | sudo tee /etc/issue > /dev/null 2>/dev/null || true
    fi
}

# Cleanup: kill setup server, DNS server, and restore /etc/issue on any exit
cleanup() {
    if [[ -n "${SETUP_SERVER_PID:-}" ]]; then
        kill "$SETUP_SERVER_PID" 2>/dev/null || true
        wait "$SETUP_SERVER_PID" 2>/dev/null || true
    fi
    # Always restore /etc/issue so hotspot credentials don't persist
    restore_issue
}
trap cleanup EXIT

# Logging
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

log_error() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: $1" | tee -a "$LOG_FILE" >&2
}

# Check if setup is already complete
check_setup_complete() {
    if [[ -f "$SETUP_COMPLETE_FLAG" ]]; then
        log "Setup already complete, skipping first-boot"
        return 0
    fi
    return 1
}

# Display a catalog-resolved splash triplet on the e-ink
# (litclock-dev#532 bulk extraction). Callers pass a catalog PREFIX
# (<prefix>.title/.message/.submessage in languages/<code>/strings.json)
# plus optional repeatable "--slot NAME=VALUE" args — the triplet resolves
# inside the ONE eink_display.py process this function already spawns, so
# localization costs zero extra python startups on the Pi Zero.
display_message() {
    local prefix="$1"
    shift

    if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
        cd "$INSTALL_DIR" || return
        timeout 20 "$PYTHON" src/eink_display.py status --catalog-prefix "$prefix" "$@" || true
    fi
}

# litclock-dev#532 pickers 5b, trap (b): the .gift-language marker is
# consumed on setup success ONLY if it was honored — i.e. its code is
# ACTIVE in the registry at this moment, so the picker offered it (applied,
# or knowingly overridden by the recipient). An inactive-but-valid code
# (registry regression, OTA lag) keeps the marker so the gifter's intent
# survives to a future re-provisioning instead of being rm'd forever.
# Exit 0 = consume; any other outcome (inactive, unreadable, python error)
# = keep, the conservative side: a kept stale marker costs one picker-
# default tap on some future setup, a wrongly consumed one loses the
# gifter's choice permanently. strings_catalog is stdlib-only and resolves
# the registry from its own file location, so no cwd dependence.
gift_language_marker_consumable() {
    [[ -f /etc/litclock/.gift-language ]] || return 1
    "$PYTHON" - "$INSTALL_DIR/src" /etc/litclock/.gift-language <<'PY'
import os
import sys

src_dir, marker = sys.argv[1], sys.argv[2]
sys.path.insert(0, src_dir)
try:
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
except OSError:
    sys.exit(1)
try:
    code = os.read(fd, 64).decode("utf-8", errors="replace").strip()
finally:
    os.close(fd)
import strings_catalog  # noqa: E402

sys.exit(0 if code and code in strings_catalog.active_codes() else 1)
PY
}

# Write hotspot credentials to /etc/issue so they appear on the login terminal
# (visible on HDMI console for testing/troubleshooting without e-ink)
# Same as display_message but PROPAGATES the painter's exit status instead of
# swallowing it (litclock-dev#657 review). Only the Setup-Incomplete arm needs
# this. That arm suppresses the shutdown splash so its own copy persists, and
# if the paint did NOT land, suppressing means the device powers off still
# showing the stale hotspot QR — an SSID and password nobody can join —
# instead of the "Powered Off" screen it would otherwise have got. Everywhere
# else a failed paint is genuinely best-effort, which is why display_message
# keeps its `|| true`.
display_message_strict() {
    local prefix="$1"
    shift

    [[ -f "$INSTALL_DIR/src/eink_display.py" ]] || return 1
    cd "$INSTALL_DIR" || return 1
    timeout 20 "$PYTHON" src/eink_display.py status --catalog-prefix "$prefix" "$@"
}

update_issue_hotspot() {
    local ssid="$1"
    local password="$2"
    local ip="$3"

    log "Updating /etc/issue with hotspot credentials"

    # Save a backup before overwriting (fallback if /etc/issue.default is missing)
    if [[ ! -f /etc/issue.bak ]]; then
        sudo cp /etc/issue /etc/issue.bak 2>/dev/null || true
    fi

    # Box width is derived from the content, floored at 45 so the shipped-path
    # box (default 14-char SSID, 9-char password, IPv4 URL) stays byte-for-byte
    # what it was. litclock-dev#589 item 4 fixed the border overflowing the
    # fixed-width fields; litclock-dev#626 closes the residual gap — the old
    # %-16s/%-15s/%-30s fields PADDED but never GREW, so any value longer than
    # its field (a 32-char --ssid, a 63-char password, a non-IPv4 ip) broke the
    # right border again. Growing the box keeps the border aligned for every
    # value wifi_provision's boundary validation admits, without truncating —
    # this banner exists so a tester can read the EXACT credentials.
    # The char-count arithmetic below (${#var} vs printf byte-width padding)
    # is only coherent because the validator restricts credentials to
    # printable ASCII, where chars == bytes == terminal columns. A 63-char
    # password grows the box past 80 columns and wraps on a narrow console —
    # accepted: exactness over truncation, custom-credential path only.
    # Wording tracks the e-ink panel deliberately.
    # agetty interprets backslash escapes in /etc/issue (\d date, \l tty,
    # \e ESC...). A credential containing a backslash — printable ASCII, so
    # the validator admits it — would DISPLAY as something other than itself
    # (the exact divergence litclock-dev#626 exists to prevent), and \e is
    # terminal-escape injection into the login console. Double every
    # backslash so getty renders the literal credential. Alignment for such
    # values degrades gracefully (the file holds \\ = 2 cols, getty shows
    # \ = 1): an honest credential beats a perfect border. Shipped-path
    # values never contain backslashes, so this is a no-op there.
    ssid="${ssid//\\/\\\\}"
    password="${password//\\/\\\\}"
    ip="${ip//\\/\\\\}"

    local c_ssid c_pass c_ip title
    c_ssid="  LitClock's WiFi network: ${ssid}"
    c_pass="  LitClock's WiFi password: ${password}"
    c_ip="  Setup URL: http://${ip}:8080"
    title="LitClock WiFi Setup"

    local interior=45 line
    for line in "$c_ssid" "$c_pass" "$c_ip"; do
        # +2 keeps the two trailing spaces the fixed template had
        if (( ${#line} + 2 > interior )); then
            interior=$(( ${#line} + 2 ))
        fi
    done

    local border pad_l pad_r
    # sed, not tr: tr is byte-wise and would emit only the first byte of the
    # multibyte ═ per space, producing invalid UTF-8
    border=$(printf '%*s' "$interior" '' | sed 's/ /═/g')
    pad_l=$(( (interior - ${#title}) / 2 ))
    pad_r=$(( interior - ${#title} - pad_l ))

    sudo tee /etc/issue > /dev/null << ISSUEEOF

  ╔${border}╗
  ║$(printf '%*s%s%*s' "$pad_l" '' "$title" "$pad_r" '')║
  ╠${border}╣
  ║$(printf '%-*s' "$interior" "$c_ssid")║
  ║$(printf '%-*s' "$interior" "$c_pass")║
  ║$(printf '%-*s' "$interior" "$c_ip")║
  ╚${border}╝

  Connect to the WiFi network above,
  then open the Setup URL in your browser.

ISSUEEOF
}


# Display hotspot info with QR code on e-ink
display_hotspot() {
    local ssid="$1"
    local password="$2"
    local ip="$3"

    if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
        cd "$INSTALL_DIR" || return
        timeout 20 "$PYTHON" src/eink_display.py hotspot "$ssid" "$password" "$ip" || true
    fi
}

# Display QR code on e-ink
display_qr() {
    local url="$1"
    local title="$2"
    local caption="$3"

    if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
        cd "$INSTALL_DIR" || return
        timeout 20 "$PYTHON" src/eink_display.py qr "$url" ${title:+--title "$title"} ${caption:+--caption "$caption"} || true
    fi
}

# Check if WiFi is connected
is_wifi_connected() {
    if ip addr show wlan0 2>/dev/null | grep -q 'inet '; then
        return 0
    fi
    return 1
}

# Create WiFi hotspot (display is updated separately in main)
create_hotspot() {
    log "Creating WiFi hotspot..."

    cd "$INSTALL_DIR" || return 1

    # Create hotspot and capture credentials (JSON output)
    local hotspot_json
    if ! hotspot_json=$("$PYTHON" -c "
import sys
sys.path.insert(0, 'src')
from wifi_provision import create_hotspot
import json
result = create_hotspot()
if result:
    print(json.dumps(result))
    sys.exit(0)
else:
    sys.exit(1)
" 2>>"$LOG_FILE") || [[ -z "$hotspot_json" ]]; then
        log_error "Failed to create hotspot"
        return 1
    fi

    # Parse JSON output
    HOTSPOT_SSID=$(echo "$hotspot_json" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['ssid'])")
    HOTSPOT_PASSWORD=$(echo "$hotspot_json" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['password'])")
    HOTSPOT_IP=$(echo "$hotspot_json" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['ip'])")

    log "Hotspot created: SSID=$HOTSPOT_SSID, IP=$HOTSPOT_IP"

    # NOTE: display_hotspot is called AFTER start_setup_server_provisioning
    # in main() to avoid a race — phones can probe port 80 within seconds of
    # connecting, and the e-ink update takes ~15s.

    return 0
}


# Start the setup web server in provisioning mode
start_setup_server_provisioning() {
    log "Starting setup server in provisioning mode..."

    # Clean up any existing signal file
    rm -f "$SIGNAL_FILE"

    # Start server in background — provisioning mode uses HTTP on port 8080
    cd "$INSTALL_DIR" || return
    "$PYTHON" src/setup_server.py "$ENV_FILE" "$SIGNAL_FILE" --provisioning \
        --hotspot-ssid "$HOTSPOT_SSID" --hotspot-password "$HOTSPOT_PASSWORD" &
    SETUP_SERVER_PID=$!

    log "Setup server started (PID: $SETUP_SERVER_PID)"
}

# Block until the provisioning setup server is actually accepting connections on
# port 8080, so the hotspot QR (and therefore the user's join) only appears AFTER
# the server can answer a captive-portal probe. Previously first-boot.sh just
# backgrounded the server and relied on the ~15s e-ink QR paint as an *implicit*
# buffer. If the server isn't listening yet when iOS fires its first probe (~1s
# after join), that probe fails and iOS can cache a "no captive portal" verdict,
# so the auto-open never fires until forced traffic (litclock-dev#483). Making readiness
# explicit closes that race regardless of why startup is slow on a given boot.
# Uses bash's /dev/tcp (no external dep, no per-probe process spawn). Best-effort:
# on timeout we paint the QR anyway rather than stall setup forever.
wait_for_setup_server_listening() {
    local timeout="${1:-25}"
    local deadline=$((timeout * 2))  # 0.5s per iteration
    log "Waiting for setup server to accept connections on port 8080..."
    for _i in $(seq 1 "$deadline"); do
        # Subshell opens fd 3 to the port; it auto-closes on subshell exit. A
        # successful connect means the server has bound + is listening.
        if (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null; then
            log "Setup server is listening"
            return 0
        fi
        sleep 0.5
    done
    log_error "Setup server not listening after ${timeout}s — painting QR anyway"
    return 1
}

# Start the setup server with no mode flag. Since litclock-dev#715 this
# exits 1 immediately — which is the point: this function exists only for
# the missing-env fallback arm, whose recovery is the exit-1 restart-loop
# into the 1800s Setup-Incomplete ceiling (see the call site).
start_setup_server() {
    log "Starting setup server..."

    # Clean up any existing signal file
    rm -f "$SIGNAL_FILE"

    # Start server in background
    cd "$INSTALL_DIR" || return
    "$PYTHON" src/setup_server.py "$ENV_FILE" "$SIGNAL_FILE" &
    SETUP_SERVER_PID=$!

    log "Setup server started (PID: $SETUP_SERVER_PID)"
}

# Wait for setup completion
wait_for_setup() {
    local server_pid="$1"
    local timeout="${2:-1800}"  # Default 30 minutes
    local elapsed=0
    local restart_splash_painted="false"

    log "Waiting for setup completion (timeout: ${timeout}s)..."

    while [[ $elapsed -lt $timeout ]]; do
        # Check if signal file exists
        if [[ -f "$SIGNAL_FILE" ]]; then
            log "Setup completion signal received"
            return 0
        fi

        # Check if server is still running
        if ! kill -0 "$server_pid" 2>/dev/null; then
            # Server exited - check if it was successful
            if [[ -f "$SIGNAL_FILE" ]]; then
                log "Setup completed successfully"
                return 0
            fi

            # Server died unexpectedly — restart it
            log "Setup server exited unexpectedly, restarting..."
            # litclock-dev#733: paint ONCE per wait, not per iteration. On the
            # missing-env doom path the server exits 1 every ~8s, and a full
            # e-ink refresh per iteration (~5-20s) both wore the panel
            # (~360 refreshes) and stretched the "1800s" ceiling to 1-2h of
            # wall clock. The content never changes between iterations, so
            # repainting bought nothing. Arm-aware (/review litclock-dev#735, both passes):
            if [[ "$restart_splash_painted" != "true" ]]; then
                if [[ "${PROVISIONING:-}" == "true" ]]; then
                    # Repaint the hotspot QR, not an interstitial. A crash
                    # used to replace the QR with "Restarting setup page..."
                    # and NOTHING ever painted the credentials again —
                    # stranding a user who hadn't joined yet. The QR is what
                    # the user needs to continue. display_hotspot is
                    # best-effort; on a wedged paint the panel keeps its
                    # pre-crash content, still strictly better than the old
                    # permanent interstitial.
                    display_hotspot "$HOTSPOT_SSID" "$HOTSPOT_PASSWORD" "$HOTSPOT_IP"
                    restart_splash_painted="true"
                else
                    # The fallback arm has no page to restart
                    # (litclock-dev#715). STRICT + flag-on-success (/review
                    # litclock-dev#735 F1): plain display_message ends `|| true`, so a
                    # wedged 20s paint would latch the flag with stale
                    # content on the panel for the whole 30-min ceiling.
                    # Failure → retry next iteration, which is exactly the
                    # retry the old per-iteration paint provided.
                    if display_message_strict firstboot.splash.recovering; then
                        restart_splash_painted="true"
                    fi
                fi
            fi
            cd "$INSTALL_DIR" || return
            if [[ "${PROVISIONING:-}" == "true" ]]; then
                "$PYTHON" src/setup_server.py "$ENV_FILE" "$SIGNAL_FILE" --provisioning \
                    --hotspot-ssid "${HOTSPOT_SSID:-}" --hotspot-password "${HOTSPOT_PASSWORD:-}" &
            else
                "$PYTHON" src/setup_server.py "$ENV_FILE" "$SIGNAL_FILE" &
            fi
            server_pid=$!
            SETUP_SERVER_PID=$server_pid
            log "Setup server restarted (PID: $server_pid)"
            sleep 2
            # litclock-dev#733: the restart sleep is real elapsed time — not
            # counting it stretched the ceiling. (Deliberately NOT wall-clock:
            # first-boot runs across the NTP step, which would jump a
            # date-based elapsed in either direction — the no-RTC lesson.)
            elapsed=$((elapsed + 2))
        fi

        sleep 5
        elapsed=$((elapsed + 5))
    done

    log_error "Setup timed out after ${timeout}s"
    kill "$server_pid" 2>/dev/null || true
    return 1
}

# Mark setup as complete
mark_setup_complete() {
    log "Marking setup as complete..."

    sudo mkdir -p "$CONFIG_DIR"
    date | sudo tee "$SETUP_COMPLETE_FLAG" > /dev/null

    log "Setup marked complete"
}

# Start the clock service
start_clock_service() {
    log "Starting clock service..."

    if systemctl list-unit-files | grep -q litclock.timer; then
        sudo systemctl enable litclock.timer
        sudo systemctl start --no-block litclock.timer
        log "Clock timer started"
    else
        # Run the clock directly
        log "Running clock directly..."
        cd "$INSTALL_DIR" || return
        ./scripts/runtheclock.sh &
    fi

    # litclock-dev#245 M5 hardware QA fix — also start the Control PWA server.
    #
    # litclock-control.service has ConditionPathExists=/etc/litclock/.setup-complete.
    # systemd evaluates Condition= directives at job-start time, so a unit
    # that's enabled but had its condition fail at boot does NOT get a
    # second chance when the condition becomes true later. Pre-M5 this
    # was masked because every fresh-image install path went through a
    # reboot before the user touched the PWA. The Reset-WiFi flow exposed
    # the gap: firstboot writes .setup-complete back, but nothing kicks
    # litclock-control.service unless we do it here.
    if systemctl list-unit-files | grep -q litclock-control.service; then
        if systemctl is-enabled --quiet litclock-control.service 2>/dev/null; then
            log "Starting Control PWA server..."
            sudo systemctl start --no-block litclock-control.service
        fi
    fi
}

# Disable first-boot service (it's done its job)
disable_first_boot() {
    log "Disabling first-boot service..."

    if systemctl list-unit-files | grep -q litclock-firstboot.service; then
        sudo systemctl disable litclock-firstboot.service
    fi
}

# Main orchestration flow
main() {
    log "======================================"
    log "LitClock First-Boot Setup Starting"
    log "======================================"

    # Unblock WiFi — rc.local may not run reliably on Bookworm
    sudo rfkill unblock wifi 2>/dev/null || true

    # Check if already configured
    if check_setup_complete; then
        start_clock_service
        exit 0
    fi

    # Stop the clock timer — if re-running first-boot (e.g. after removing
    # .setup-complete for testing), the timer may still be enabled from a
    # previous setup cycle and would show quotes during hotspot setup.
    if systemctl is-active litclock.timer &>/dev/null; then
        log "Stopping active clock timer (setup not complete)"
        sudo systemctl stop litclock.timer litclock.service 2>/dev/null || true
    fi
    if systemctl is-enabled litclock.timer &>/dev/null; then
        log "Disabling clock timer (setup not complete)"
        sudo systemctl disable litclock.timer 2>/dev/null || true
    fi

    # Ensure env.sh exists. Route through the shared sidecar-flock writer
    # (litclock-dev#274) so a power loss mid-write can't leave a half-truncated file,
    # and a concurrent Python PWA writer can't race the heredoc on a boot
    # where setup-complete didn't land before reboot.
    if [[ ! -f "$ENV_FILE" ]]; then
        log "Creating default env.sh..."
        # litclock-dev#337 A3: WEATHER_LOCATION_MODE + WEATHER_IP_COUNTRY shipped from
        # the very first boot. MODE=auto means the on-boot reresolve service
        # will populate the rest once WiFi connects + IP-geo succeeds.
        local _defaults
        _defaults='export OPENWEATHERMAP_APIKEY=
export WEATHER_LATITUDE=
export WEATHER_LONGITUDE=
export WEATHER_UNITS=imperial
export WEATHER_LOCATION_MODE=auto
export WEATHER_IP_COUNTRY=
export WEATHER_TTL=3600
export ALLOW_NSFW_QUOTES=false
export LITCLOCK_LANGUAGE=
'
        if declare -F atomic_write_env_sh >/dev/null 2>&1; then
            if ! atomic_write_env_sh "$ENV_FILE" "$_defaults"; then
                local _rc=$?
                if [[ "$_rc" == "75" ]]; then
                    log "WARN env.sh locked by another writer — leaving default-creation to next boot"
                else
                    log "WARN env.sh write failed (rc=$_rc) — proceeding without default file"
                fi
            fi
        else
            # state.sh not on disk (partial checkout / dev sandbox). Degrade
            # to the legacy heredoc; production Pis always have state.sh
            # because it ships in the same release as first-boot.sh.
            log "WARN scripts/lib/state.sh missing — falling back to unlocked default-env write"
            cat > "$ENV_FILE" << 'ENVEOF'
export OPENWEATHERMAP_APIKEY=
export WEATHER_LATITUDE=
export WEATHER_LONGITUDE=
export WEATHER_UNITS=imperial
export WEATHER_LOCATION_MODE=auto
export WEATHER_IP_COUNTRY=
export WEATHER_TTL=3600
export ALLOW_NSFW_QUOTES=false
export LITCLOCK_LANGUAGE=
ENVEOF
        fi
    fi

    # Step 1: Display setup message
    # (Welcome splash is handled by litclock-splash.service)
    log "Displaying setup message..."
    display_message firstboot.splash.preparing

    # litclock-dev#647: set by the pre-connected branch, which completes setup
    # inline instead of serving a page and waiting on a tap.
    SETUP_DONE_INLINE="false"

    # Step 2: Check WiFi / create hotspot
    if is_wifi_connected; then
        local ssid
        ssid=$(iwgetid -r 2>/dev/null || echo "WiFi")
        log "WiFi already connected ($ssid)"
        display_message firstboot.splash.wifi_connected --slot "ssid=$ssid"

        # Already on WiFi — litclock-dev#647: NO setup page on this path.
        #
        # The page this branch used to serve (https://<IP>:8443, self-signed)
        # had zero inputs: location, timezone and units are IP-geo-resolved
        # with no user decision, so the flow was a QR scan, a full-page
        # browser security warning whose primary action is "Close Page", and
        # a tap on a button whose own copy explained nothing needed
        # configuring. Two separate bench runs (dev-20260815-b0c0590 and the
        # v0.224.0 RC) produced the same owner reaction. The tap's only real
        # job — a liveness signal that someone can reach the device — is
        # covered better by the handoff splash one screen later, which
        # carries the PWA QR plus its own Done / 120s-fallback flow.
        #
        # So: resolve location directly and complete setup inline. The
        # handoff phase (litclock-control.service, gated on .setup-complete
        # without .handoff-complete) paints the "Ready to read." splash
        # exactly as it does after hotspot provisioning.
        sleep 3

        # Wait for NTP sync
        log "Enabling NTP time sync..."
        display_message firstboot.splash.ntp_sync
        sudo timedatectl set-ntp true || log "Warning: Could not enable NTP"
        for _i in $(seq 1 30); do
            if timedatectl show 2>/dev/null | grep -q 'NTPSynchronized=yes'; then
                log "Time synchronized"
                break
            fi
            sleep 1
        done

        # The same resolver the setup page's Complete Setup button used to
        # trigger, minus the button. location_resolver.main() reads
        # LITCLOCK_ENV_FILE, gates on WEATHER_LOCATION_MODE=auto (a Specific
        # location survives a re-run of first-boot untouched, litclock-dev#337 A15) and
        # always exits 0 — on a hard IP-geo failure the env keys stay empty
        # and the handoff splash + PWA browser-tz fallback take over
        # (litclock-dev#337 A18), which is the same degraded path the page had.
        # Synchronous on purpose: the full retry budget is 1+3+9s backoff,
        # ~33s worst case, and the "Setting Up" splash covers it.
        if [[ -f "$ENV_FILE" ]]; then
            log "Pre-connected path: resolving location inline (no setup page, litclock-dev#647)"
            display_message firstboot.splash.detecting_location
            # `timeout 120`: the old path had a hard 1800s ceiling with an
            # on-panel recovery; an unbounded inline call would trade that for
            # a forever "Setting Up" splash on a wedged timedatectl D-Bus or a
            # byte-dripping ip-api middlebox (/review litclock-dev#712). The resolver's own
            # retry budget is ~33s, and litclock-reresolve-location.service
            # pins the same code at 60s — 120s is generous, and the resolver
            # is best-effort by contract: on expiry the env keys stay empty
            # and the handoff splash's browser-tz fallback takes over.
            LITCLOCK_ENV_FILE="$ENV_FILE" timeout 120 "$PYTHON" "$INSTALL_DIR/src/location_resolver.py" \
                >>"$LOG_FILE" 2>&1 || log "Warning: location resolver exited non-zero or timed out"
            SETUP_DONE_INLINE="true"
        else
            # env.sh could not be created earlier (the rc=75 lock arm, or any
            # write failure). Completing setup inline HERE would make that
            # permanent: first-boot disables itself, no later boot re-creates
            # env.sh, and every future PWA env write raises FileNotFoundError
            # (/review litclock-dev#712). Fall back to the setup-server ceiling instead:
            # the server exits 1 immediately (missing env file — and since
            # litclock-dev#715 the no-flag mode always exits 1; it serves no
            # page), wait_for_setup restart-loops the exits, and the 1800s
            # ceiling paints Setup Incomplete and powers off — a RETRY on
            # the next boot. This is the loud-failure contract the retired
            # normal-mode page was kept alive for; the contract survives it.
            log_error "env.sh missing on the pre-connected path; falling back to the setup page"
            start_setup_server
            SERVER_PID=$SETUP_SERVER_PID
        fi
    else
        # No WiFi — create hotspot and run provisioning setup
        log "No WiFi connection, creating hotspot..."
        PROVISIONING="true"
        hotspot_ok=false

        # Hotspot creation can fail on Pi Zero 2W when the BCM43436 SDIO chip
        # is left in a stuck state by a rapid reboot (reboot doesn't power-cycle
        # the chip — only a poweroff does). Between attempts we escalate
        # recovery actions: NM restart → driver reload. The final fallback is
        # telling the user to pull power.
        for attempt in $(seq 1 "$HOTSPOT_MAX_RETRIES"); do
            if create_hotspot; then
                hotspot_ok=true
                break
            fi
            if [[ $attempt -lt $HOTSPOT_MAX_RETRIES ]]; then
                log "Hotspot attempt $attempt/$HOTSPOT_MAX_RETRIES failed, retrying..."
                display_message firstboot.splash.wifi_retry --slot "attempt=$attempt" --slot "max=$HOTSPOT_MAX_RETRIES"

                # Escalate recovery as attempts progress:
                #   attempt 1 failed → restart NetworkManager before retry 2
                #   attempt 2 failed → reload brcmfmac driver before retry 3
                #   attempt 3+ failed → just wait; chip may be resetting itself
                if [[ $attempt -eq 1 ]]; then
                    log "Recovery: restarting NetworkManager"
                    sudo systemctl restart NetworkManager 2>/dev/null || true
                elif [[ $attempt -eq 2 ]]; then
                    log "Recovery: reloading brcmfmac driver"
                    sudo rmmod brcmfmac_wcc 2>/dev/null || true
                    sudo rmmod brcmfmac 2>/dev/null || true
                    sleep 2
                    sudo modprobe brcmfmac 2>/dev/null || true
                    sudo systemctl restart NetworkManager 2>/dev/null || true
                fi
                sleep "$HOTSPOT_RETRY_DELAY"
            fi
        done

        if [[ "$hotspot_ok" != "true" ]]; then
            log_error "Could not create hotspot after $HOTSPOT_MAX_RETRIES attempts"
            display_message firstboot.splash.setup_failed
            exit 1
        fi

        # Captive portal DNS + nftables redirect are set up by create_hotspot()
        # via wifi_provision.py — NM's dnsmasq resolves all domains to hotspot IP,
        # and nftables redirects port 80→8080 so probe requests hit the setup server.

        # Captive portal DNS is handled by NM's dnsmasq (started automatically
        # in shared mode) via the address=/#/ config in dnsmasq-shared.d/.
        # Do NOT start a separate DNS server — it conflicts with dnsmasq on port 53.

        # Start setup server BEFORE displaying hotspot info — phones can connect
        # and probe for captive portal within seconds, and the e-ink display update
        # takes ~15s. The server must be listening before the first probe arrives.
        start_setup_server_provisioning
        SERVER_PID=$SETUP_SERVER_PID

        # Confirm the server is actually accepting connections BEFORE painting the
        # QR — the QR is the user's cue to join, and iOS probes for a captive
        # portal within a second of joining. Showing the QR before the server can
        # answer is what lets that first probe fail and get cached (litclock-dev#483).
        wait_for_setup_server_listening 25 || true

        # Now show hotspot credentials + QR code on e-ink (safe to take time here)
        display_hotspot "$HOTSPOT_SSID" "$HOTSPOT_PASSWORD" "$HOTSPOT_IP"

        # Also show credentials on HDMI login terminal for testing/troubleshooting
        update_issue_hotspot "$HOTSPOT_SSID" "$HOTSPOT_PASSWORD" "$HOTSPOT_IP"
    fi

    # Step 3: Wait for setup completion. The pre-connected branch already
    # completed inline (litclock-dev#647) — the short-circuit means no server
    # was started and $SERVER_PID is unset on that path.
    if [[ "$SETUP_DONE_INLINE" == "true" ]] || wait_for_setup "$SERVER_PID" 1800; then
        log "Setup completed successfully!"

        # Step 4: Show success and finalize
        display_message firstboot.splash.setup_complete
        sleep 3

        # Restore default login terminal (remove hotspot credentials)
        restore_issue

        # Enable NTP if not already done (provisioning mode skipped it)
        sudo timedatectl set-ntp true 2>/dev/null || true

        # litclock-dev#316 /review CRITICAL ordering fix — consume the gift-mode markers
        # BEFORE mark_setup_complete. The previous order had a window where
        # power loss / a SIGTERM between mark_setup_complete and the rm
        # would leave .welcome-mode + .welcome-message stranded with
        # .setup-complete already present. On next boot, first-boot.sh
        # short-circuits (setup already complete), the cleanup never runs,
        # and every subsequent shutdown paints the gift welcome instead of
        # "Powered Off" — with no PWA recovery path. New order means the
        # worst-case failure is "first-boot runs the user through setup
        # again on next boot" (acceptable retry semantics), not "device is
        # permanently stuck showing the welcome splash on every shutdown."
        # litclock-dev#532 pickers 5b: .gift-language consumption is
        # CONDITIONAL (trap b) — one-shot only when the code was active
        # (honored) at this setup; kept through registry regressions / OTA
        # lag so the gifter's intent survives to a future re-provisioning.
        # Plain resets clear a kept-but-stale marker (reset-setup.sh).
        if gift_language_marker_consumable; then
            sudo rm -f /etc/litclock/.gift-language
        elif [[ -f /etc/litclock/.gift-language ]]; then
            log "keeping .gift-language: code not active yet (honored on a future setup)"
        fi
        sudo rm -f /etc/litclock/.welcome-mode /etc/litclock/.welcome-message
        mark_setup_complete
        disable_first_boot
        start_clock_service

        log "First-boot setup finished successfully"
    else
        log_error "Setup did not complete"
        # litclock-dev#529: paint the recovery instructions, then power off
        # instead of idling in a half-provisioned state (burning power, holding
        # the hotspot, reading as "stuck" on a shelf). A power-cycle IS the
        # on-screen recovery instruction, so the off state and the copy agree.
        # No SSH mention — the device is about to be off, and gift recipients
        # don't SSH.
        _incomplete_painted=false
        if display_message_strict firstboot.splash.setup_incomplete; then
            _incomplete_painted=true
        else
            log_error "Could not paint the Setup Incomplete screen"
        fi
        # Deliberately NO grace sleep between paint and poweroff (owner
        # decision on litclock-dev#529): the on-screen copy invites the user to
        # pull power, so every second the Pi keeps running after painting it is
        # a window for an unclean power cut (SD-corruption class). The
        # 30-minute setup timeout above already was the grace period.
        #
        # Keep the message on-screen through the shutdown: the bistable e-ink
        # persists it while off, but litclock-shutdown.service's ExecStop would
        # repaint (welcome splash on a gifted device, "Powered Off" otherwise).
        # The root-only marker makes shutdown-splash.sh exit without painting;
        # /run is tmpfs so the marker self-clears and the NEXT boot re-enters
        # provisioning with normal splash behavior. Setup/handoff markers are
        # untouched here — .setup-complete was never written on this path, so
        # the next power-on re-runs first-boot cleanly.
        # Gated on the paint (litclock-dev#657 review, not in #22): if the
        # panel never took the recovery copy, suppressing the shutdown splash
        # powers the device off still showing the stale hotspot QR. A "Powered
        # Off" screen is a worse recovery hint than the copy, but it is much
        # better than an SSID and password nobody can join.
        #
        # NOTE the asymmetry, and it is not an oversight: `systemctl poweroff`
        # IS in the scoped 020 allowlist, this `touch` is NOT (020 grants
        # `touch` for /etc/litclock/.handoff-complete only), so today this line
        # works solely via the broad 010 passwordless grant. If 010 is ever
        # dropped the touch fails silently and the shutdown splash repaints
        # over the recovery copy — the device still powers off, it just loses
        # the message. Granting it in 020 is NOT the fix: shutdown-splash.sh
        # justifies the root-owned path on the opposite ground (a pi-level
        # process must not be able to plant it and mute the gift welcome), so
        # closing this needs a root-owned wrapper like
        # /usr/local/lib/litclock/litclock-set-timezone, not a wider allowlist.
        # tests/test_sudoers_install.py pins both halves of that statement.
        if [[ "$_incomplete_painted" == true ]]; then
            sudo touch /run/litclock-splash-suppress 2>/dev/null || true
        fi
        # `sudo systemctl poweroff` (not bare `sudo poweroff`) — matches the
        # sudo-systemctl form used everywhere else in this script and the
        # scoped 020 sudoers allowlist, so it survives a future drop of the 010
        # passwordless-sudo grant. If the marker touch above failed, we still
        # power off (a device stranded ON is worse than the splash getting
        # repainted) — the poweroff is deliberately not gated on it.
        # Not in #22: dev already decided at the sibling call site
        # (prepare-for-cloning.sh, litclock-dev#660 review) that `poweroff` can
        # fail — logind/D-Bus unavailable is a realistic state on exactly the
        # degrading card this arm is reached on — and that a silent failure
        # leaves the operator a false statement and a running Pi. Clear the
        # suppression when it does: the marker exists to protect the ONE
        # shutdown just requested, and if that shutdown did not happen it must
        # not go on muting every later one for the rest of the boot.
        sudo systemctl poweroff || {
            log_error "Power-off FAILED after the setup timeout — the device is still running. Pull power once the panel shows the recovery message."
            sudo rm -f /run/litclock-splash-suppress 2>/dev/null || true
        }
        exit 1
    fi
}

# Run main
main "$@"
