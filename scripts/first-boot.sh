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
IP_MAX_RETRIES=10
IP_RETRY_DELAY=3

# Shared env.sh writer helpers (issue #274). Sourced so the default-env-
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

# Display message on e-ink
display_message() {
    local title="$1"
    local message="$2"
    local submessage="$3"

    if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
        cd "$INSTALL_DIR" || return || return
        timeout 20 "$PYTHON" src/eink_display.py status "$title" ${message:+--message "$message"} ${submessage:+--submessage "$submessage"} || true
    fi
}

# Write hotspot credentials to /etc/issue so they appear on the login terminal
# (visible on HDMI console for testing/troubleshooting without e-ink)
update_issue_hotspot() {
    local ssid="$1"
    local password="$2"
    local ip="$3"

    log "Updating /etc/issue with hotspot credentials"

    # Save a backup before overwriting (fallback if /etc/issue.default is missing)
    if [[ ! -f /etc/issue.bak ]]; then
        sudo cp /etc/issue /etc/issue.bak 2>/dev/null || true
    fi

    # Pad values to fixed width so the box border aligns
    local line_ssid line_pass line_ip
    line_ssid=$(printf "  SSID:      %-23s" "$ssid")
    line_pass=$(printf "  Password:  %-23s" "$password")
    line_ip=$(printf "  Setup URL: %-23s" "http://${ip}:8080")

    sudo tee /etc/issue > /dev/null << ISSUEEOF

  ╔══════════════════════════════════════╗
  ║       LitClock WiFi Setup            ║
  ╠══════════════════════════════════════╣
  ║${line_ssid}  ║
  ║${line_pass}  ║
  ║${line_ip}  ║
  ╚══════════════════════════════════════╝

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
        cd "$INSTALL_DIR" || return || return
        timeout 20 "$PYTHON" src/eink_display.py hotspot "$ssid" "$password" "$ip" || true
    fi
}

# Display QR code on e-ink
display_qr() {
    local url="$1"
    local title="$2"
    local caption="$3"

    if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
        cd "$INSTALL_DIR" || return || return
        timeout 20 "$PYTHON" src/eink_display.py qr "$url" ${title:+--title "$title"} ${caption:+--caption "$caption"} || true
    fi
}

# Get device IP address
get_ip_address() {
    # Try to get IP from wlan0 first, then eth0
    ip -4 addr show wlan0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1 ||
    ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1 ||
    echo "unknown"
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
# so the auto-open never fires until forced traffic (#483). Making readiness
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

# Start the setup web server in normal mode (HTTPS)
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
            display_message "Setup Server" "Restarting setup page..." ""
            cd "$INSTALL_DIR" || return || return
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
        cd "$INSTALL_DIR" || return || return
        ./scripts/runtheclock.sh &
    fi

    # #245 M5 hardware QA fix — also start the Control PWA server.
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
    # (#274) so a power loss mid-write can't leave a half-truncated file,
    # and a concurrent Python PWA writer can't race the heredoc on a boot
    # where setup-complete didn't land before reboot.
    if [[ ! -f "$ENV_FILE" ]]; then
        log "Creating default env.sh..."
        # #337 A3: WEATHER_LOCATION_MODE + WEATHER_IP_COUNTRY shipped from
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
ENVEOF
        fi
    fi

    # Step 1: Display setup message
    # (Welcome splash is handled by litclock-splash.service)
    log "Displaying setup message..."
    display_message "Setup" "LitClock" "Preparing setup..."

    # Step 2: Check WiFi / create hotspot
    if is_wifi_connected; then
        local ssid
        ssid=$(iwgetid -r 2>/dev/null || echo "WiFi")
        log "WiFi already connected ($ssid)"
        display_message "WiFi Connected" "Network: $ssid" ""

        # Already on WiFi — use normal setup flow
        sleep 3

        # Wait for NTP sync
        log "Enabling NTP time sync..."
        display_message "Syncing Time" "Setting clock via NTP..." ""
        sudo timedatectl set-ntp true || log "Warning: Could not enable NTP"
        for _i in $(seq 1 30); do
            if timedatectl show 2>/dev/null | grep -q 'NTPSynchronized=yes'; then
                log "Time synchronized"
                break
            fi
            sleep 1
        done

        # Get IP and show QR code for setup
        IP_ADDRESS=$(get_ip_address)
        if [[ "$IP_ADDRESS" == "unknown" ]]; then
            log "IP not yet available, retrying..."
            for attempt in $(seq 1 "$IP_MAX_RETRIES"); do
                sleep "$IP_RETRY_DELAY"
                IP_ADDRESS=$(get_ip_address)
                if [[ "$IP_ADDRESS" != "unknown" ]]; then
                    break
                fi
                log "IP retry $attempt/$IP_MAX_RETRIES..."
            done
        fi
        if [[ "$IP_ADDRESS" == "unknown" ]]; then
            log_error "Could not determine IP address"
            display_message "Network Error" "Could not get IP address" "Please restart and try again"
            exit 1
        fi
        SETUP_URL="https://${IP_ADDRESS}:8443"
        log "Setup URL: $SETUP_URL"
        display_qr "$SETUP_URL" "Scan to Setup" "Open on your phone"

        # Start normal setup server (HTTPS, no WiFi section)
        start_setup_server
        SERVER_PID=$SETUP_SERVER_PID
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
                display_message "Hotspot Failed" "Retrying... ($attempt/$HOTSPOT_MAX_RETRIES)" "Please wait"

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
            display_message "Setup Failed" "Unplug power for 10 seconds" "Then plug back in"
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
        # answer is what lets that first probe fail and get cached (#483).
        wait_for_setup_server_listening 25 || true

        # Now show hotspot credentials + QR code on e-ink (safe to take time here)
        display_hotspot "$HOTSPOT_SSID" "$HOTSPOT_PASSWORD" "$HOTSPOT_IP"

        # Also show credentials on HDMI login terminal for testing/troubleshooting
        update_issue_hotspot "$HOTSPOT_SSID" "$HOTSPOT_PASSWORD" "$HOTSPOT_IP"
    fi

    # Step 3: Wait for setup completion
    if wait_for_setup "$SERVER_PID" 1800; then
        log "Setup completed successfully!"

        # Step 4: Show success and finalize
        display_message "Setup Complete!" "Starting your clock..." ""
        sleep 3

        # Restore default login terminal (remove hotspot credentials)
        restore_issue

        # Enable NTP if not already done (provisioning mode skipped it)
        sudo timedatectl set-ntp true 2>/dev/null || true

        # #316 /review CRITICAL ordering fix — consume the gift-mode markers
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
        sudo rm -f /etc/litclock/.welcome-mode /etc/litclock/.welcome-message
        mark_setup_complete
        disable_first_boot
        start_clock_service

        log "First-boot setup finished successfully"
    else
        log_error "Setup did not complete"
        display_message "Setup Incomplete" "Restart to try again" "Or SSH in to configure manually"
        exit 1
    fi
}

# Run main
main "$@"
