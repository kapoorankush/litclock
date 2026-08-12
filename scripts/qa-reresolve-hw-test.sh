#!/bin/bash
#
# Deterministic hardware test for on-boot location re-resolution
# (litclock-dev#549). Replaces the "VPN to another country and hope"
# QA scenario with a synthetic-prior-country injection: setting
# WEATHER_IP_COUNTRY to an impossible value (ZZ) makes the resolver's
# country-change branch fire deterministically on the next run, with no
# VPN, no relocation, and no dependence on ip-api answering DIFFERENTLY
# than last boot. ip-api must still be reachable — but an outage now
# fails with a distinguishable message instead of masquerading as an
# ordering bug.
#
# Run ON the Pi (as pi; sudo used where needed):
#
#   sudo ./scripts/qa-reresolve-hw-test.sh run       # no reboot: decision branch only
#   sudo ./scripts/qa-reresolve-hw-test.sh prepare   # inject ZZ, then: sudo reboot
#   sudo ./scripts/qa-reresolve-hw-test.sh verify    # after the reboot: full assert
#   sudo ./scripts/qa-reresolve-hw-test.sh restore   # put back the recorded pre-test state
#
# `run` proves the resolver + country-change branch + env.sh write chain.
# `prepare`/`verify` across a reboot ADDITIONALLY proves the on-boot
# systemd path: the unit fired this boot, and it started only after
# NetworkManager-wait-online finished (the litclock-dev#549 ordering
# assertion — the suspected root of the historical flakiness).
#
# State + recovery contract (/review on litclock-dev#631):
# - prepare/run record the pre-test WEATHER_IP_COUNTRY and WEATHER_UNITS
#   in $STATE_FILE and REFUSE to run if a prior test's state exists.
# - Preconditions failing → NOTHING is written; env.sh is untouched.
# - A successful test restores WEATHER_UNITS (the resolver's country-change
#   branch deliberately resets units to the new country's default — litclock-dev#337
#   A16 — which would silently wipe a manual Celsius/Fahrenheit override).
# - A FAILED test keeps $STATE_FILE so `restore` can put everything back;
#   env.sh may hold ZZ until then (or until the next successful auto-mode
#   resolve self-heals it). The script SAYS so on failure.

INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
ENV_FILE="$INSTALL_DIR/env.sh"
UNIT="litclock-reresolve-location.service"
WAITER="NetworkManager-wait-online.service"
STATE_FILE="/var/lib/litclock/qa-reresolve-549.state"
SENTINEL="ZZ"  # ISO 3166 user-assigned range — no geolocation can ever return it
STATE_MAX_AGE_S=86400  # a day-old prepare proves nothing about THIS boot

PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "FAIL: $1"; }
die()  { echo "ABORT: $1" >&2; exit 2; }

# shellcheck source=/dev/null
. "$INSTALL_DIR/scripts/lib/state.sh" 2>/dev/null || die "cannot source scripts/lib/state.sh"
# with_env_lock locks ${ENV_FILE_DEFAULT}.lock — pin it to OUR env file so a
# LITCLOCK_DIR override can't lock one path while writing another.
# shellcheck disable=SC2034  # consumed inside state.sh's with_env_lock
ENV_FILE_DEFAULT="$ENV_FILE"

read_env_key() {
    sed -nE "s/^(export )?$1=//p" "$ENV_FILE" | tail -1
}

# Runs UNDER with_env_lock: read-modify-write inside the same critical
# section (/review F6 — computing content outside the lock and only locking
# the rename is the exact lost-update class the litclock-dev#274 sidecar exists for).
# Uses the shared finalize helper (ownership/mode-preserving atomic rename)
# rather than atomic_write_env_sh, which would try to re-take the same lock.
_locked_set_env_key() {
    local key="$1" value="$2" content tmp
    content=$(cat "$ENV_FILE") || return 1
    if printf '%s\n' "$content" | grep -qE "^(export )?${key}="; then
        # Rewrites EVERY matching line (export or bare) — a duplicate key
        # would otherwise survive as a last-wins shadow of the injected value.
        content=$(printf '%s\n' "$content" | sed -E "s|^(export )?${key}=.*|export ${key}=${value}|")
    else
        content=$(printf '%s\n%s' "$content" "export ${key}=${value}")
    fi
    tmp=$(mktemp "${ENV_FILE}.XXXXXX") || return 1
    # Trailing newline preserved deliberately ($(cat) strips it — the
    # env.sh splitlines-roundtrip hygiene class).
    if ! printf '%s\n' "$content" > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    _atomic_write_env_sh_finalize "$tmp" "$ENV_FILE"
}

set_env_key() {
    with_env_lock _locked_set_env_key "$1" "$2" || die "locked env.sh write failed for $1"
}

write_state_file() {
    mkdir -p "$(dirname "$STATE_FILE")" || die "cannot create $(dirname "$STATE_FILE")"
    atomic_write_file "$STATE_FILE" "$(printf 'original_country=%s\noriginal_units=%s\nprepared_at_boot=%s\nprepared_at_epoch=%s' \
        "$1" "$2" "$(cat /proc/sys/kernel/random/boot_id)" "$(date +%s)")" \
        || die "cannot write $STATE_FILE"
}

state_get() {
    sed -n "s/^$1=//p" "$STATE_FILE" | tail -1
}

check_preconditions() {
    # Every precondition that historically made this QA scenario flaky is
    # asserted explicitly, so a failure names its cause instead of reading
    # as "geolocation is unreliable".
    [[ -f "$ENV_FILE" ]] || die "$ENV_FILE missing — not a provisioned Pi"
    if [[ "$(systemctl is-enabled "$UNIT" 2>/dev/null)" == "enabled" ]]; then
        pass "$UNIT is enabled"
    else
        fail "$UNIT is not enabled — fresh-flash install gap (see public#46 class)"
    fi
    # The ordering is non-vacuous when EITHER the resolver unit itself pulls
    # the waiter in (Wants= — the litclock-dev#549 unit fix) OR the distro
    # preset enables it. Requiring global enablement alone would false-fail
    # the exact self-sufficient dependency the fix adds (/review F4).
    if systemctl show "$UNIT" -p Wants --value 2>/dev/null | grep -q "$WAITER"; then
        pass "$UNIT Wants= pulls in $WAITER (ordering self-sufficient)"
    elif [[ "$(systemctl is-enabled "$WAITER" 2>/dev/null)" == "enabled" ]]; then
        pass "$WAITER is preset-enabled (ordering non-vacuous; unit predates the litclock-dev#549 Wants= fix)"
    else
        fail "neither $UNIT Wants= nor enablement pulls in $WAITER — After= ordering is VACUOUS (litclock-dev#549)"
    fi
    if [[ -f /etc/litclock/.handoff-complete ]]; then
        pass "handoff-complete marker present (ConditionPathExists will hold)"
    else
        fail "no /etc/litclock/.handoff-complete — the unit will be skipped by its Condition"
    fi
    local mode
    mode=$(read_env_key WEATHER_LOCATION_MODE)
    if [[ "$mode" == "auto" || -z "$mode" ]]; then
        pass "WEATHER_LOCATION_MODE is auto (resolver will act)"
    else
        fail "WEATHER_LOCATION_MODE=$mode — a 'specific' Pi correctly no-ops; test needs auto"
    fi
}

# Nothing may be written when preconditions fail: a MODE=specific Pi
# injected with ZZ can never self-heal (the resolver no-ops), and a later
# switch to Automatic would read ZZ->real as a country change and reset
# the units (/review F1 — the poison-then-exit-1 shape).
abort_if_preconditions_failed() {
    if [[ $FAIL_COUNT -gt 0 ]]; then
        echo "----------------------------------------"
        echo "RESULT: $PASS_COUNT pass, $FAIL_COUNT fail — env.sh NOT touched"
        exit 1
    fi
}

# Monotonic ordering: the unit's main process must have STARTED at-or-after
# the waiter FINISHED. Both timestamps are per-boot (0 when the unit never
# ran this boot), which is exactly the discriminator we need.
check_boot_ordering() {
    # The timestamps reflect the LAST start — a same-boot manual restart
    # (e.g. `run` mode) would mask a boot-time ordering violation, so refuse
    # ambiguous evidence outright (/review F5).
    local starts
    starts=$(journalctl -b 0 -u "$UNIT" --no-pager 2>/dev/null | grep -c "Starting ")
    if [[ "$starts" -gt 1 ]]; then
        fail "$UNIT started $starts times this boot — ordering evidence ambiguous; re-run prepare + reboot without intervening restarts"
        return
    fi
    local waiter_done unit_start
    waiter_done=$(systemctl show "$WAITER" -p ExecMainExitTimestampMonotonic --value)
    unit_start=$(systemctl show "$UNIT" -p ExecMainStartTimestampMonotonic --value)
    if [[ -z "$unit_start" || "$unit_start" == "0" ]]; then
        fail "$UNIT did not run this boot (ExecMainStartTimestampMonotonic=0) — check its Condition and journal"
        return
    fi
    pass "$UNIT ran this boot (monotonic start ${unit_start}us)"
    if [[ -z "$waiter_done" || "$waiter_done" == "0" ]]; then
        fail "$WAITER did not finish this boot — ordering unprovable and network readiness unknown"
        return
    fi
    if (( unit_start >= waiter_done )); then
        pass "ordering: resolver started ${unit_start}us >= network-online ${waiter_done}us"
    else
        fail "ORDERING BUG: resolver started ${unit_start}us BEFORE network-online finished ${waiter_done}us (litclock-dev#549)"
    fi
}

check_country_flipped() {
    local country
    country=$(read_env_key WEATHER_IP_COUNTRY)
    if [[ "$country" == "$SENTINEL" ]]; then
        # Distinguish "resolver never acted" from "geo lookup failed":
        # the resolver's own journal lines name the retry/backoff failure.
        if journalctl -b 0 -u "$UNIT" --no-pager 2>/dev/null | grep -qi "fail\|error\|timed out"; then
            fail "country still $SENTINEL and the resolver journal shows errors — ip-api/network problem, NOT an ordering bug"
        else
            fail "country still $SENTINEL — the country-change branch never fired (check WEATHER_LOCATION_MODE and the unit journal)"
        fi
        return
    fi
    if [[ -z "$country" ]]; then
        fail "WEATHER_IP_COUNTRY is empty after the run — resolver wrote an empty result"
        return
    fi
    pass "country-change branch fired: WEATHER_IP_COUNTRY=$SENTINEL -> $country"
}

# The resolver's country-change branch resets WEATHER_UNITS to the new
# country's default (litclock-dev#337 A16) — correct for a real move, but this test's
# ZZ->real "move" is synthetic, so a manual Celsius/Fahrenheit override
# must be put back (/review F2).
restore_units_if_overridden() {
    local original_units="$1" now_units
    [[ -n "$original_units" ]] || return 0
    now_units=$(read_env_key WEATHER_UNITS)
    if [[ "$now_units" != "$original_units" ]]; then
        set_env_key WEATHER_UNITS "$original_units"
        pass "restored WEATHER_UNITS=$original_units (the synthetic country change had reset it to $now_units)"
    fi
}

report() {
    echo "----------------------------------------"
    echo "RESULT: $PASS_COUNT pass, $FAIL_COUNT fail"
    [[ $FAIL_COUNT -eq 0 ]] || exit 1
}

report_failure_with_recovery() {
    echo "----------------------------------------"
    echo "RESULT: $PASS_COUNT pass, $FAIL_COUNT fail"
    if [[ $FAIL_COUNT -gt 0 ]]; then
        echo "env.sh may still hold WEATHER_IP_COUNTRY=$SENTINEL. State kept in $STATE_FILE."
        echo "Recover with: sudo $0 restore   (or fix the cause and re-run verify after another reboot)"
        exit 1
    fi
}

case "${1:-}" in
    prepare)
        [[ -f "$STATE_FILE" ]] && die "prior test state exists ($STATE_FILE) — run 'verify' or 'restore' first (re-preparing would record $SENTINEL as the original)"
        check_preconditions
        abort_if_preconditions_failed
        write_state_file "$(read_env_key WEATHER_IP_COUNTRY)" "$(read_env_key WEATHER_UNITS)"
        set_env_key WEATHER_IP_COUNTRY "$SENTINEL"
        pass "injected WEATHER_IP_COUNTRY=$SENTINEL (was: $(state_get original_country))"
        report
        echo "Now: sudo reboot   — then run: sudo $0 verify"
        ;;
    verify)
        [[ -f "$STATE_FILE" ]] || die "no state file — run 'prepare' (then reboot) first"
        if grep -q "prepared_at_boot=$(cat /proc/sys/kernel/random/boot_id)" "$STATE_FILE"; then
            die "same boot_id as prepare — you must REBOOT between prepare and verify (that's the point)"
        fi
        prepared_epoch=$(state_get prepared_at_epoch)
        if [[ -n "$prepared_epoch" ]] && (( $(date +%s) - prepared_epoch > STATE_MAX_AGE_S )); then
            fail "prepare was $(( ($(date +%s) - prepared_epoch) / 3600 ))h ago — the flip likely happened on an earlier boot; this boot's evidence proves nothing. restore + re-prepare."
        fi
        check_boot_ordering
        check_country_flipped
        if [[ $FAIL_COUNT -eq 0 ]]; then
            restore_units_if_overridden "$(state_get original_units)"
            rm -f "$STATE_FILE"
            report
        else
            report_failure_with_recovery
        fi
        ;;
    run)
        # No-reboot variant: proves the resolver + decision branch + env
        # write chain in ~40s. Does NOT prove boot ordering — use
        # prepare/verify for the full claim.
        [[ -f "$STATE_FILE" ]] && die "prior test state exists ($STATE_FILE) — run 'verify' or 'restore' first"
        check_preconditions
        abort_if_preconditions_failed
        write_state_file "$(read_env_key WEATHER_IP_COUNTRY)" "$(read_env_key WEATHER_UNITS)"
        set_env_key WEATHER_IP_COUNTRY "$SENTINEL"
        pass "injected WEATHER_IP_COUNTRY=$SENTINEL (was: $(state_get original_country))"
        echo "restarting $UNIT (resolver budget ~33s worst case)..."
        sudo systemctl restart "$UNIT" || fail "systemctl restart $UNIT failed"
        check_country_flipped
        if [[ $FAIL_COUNT -eq 0 ]]; then
            restore_units_if_overridden "$(state_get original_units)"
            rm -f "$STATE_FILE"
            report
        else
            report_failure_with_recovery
        fi
        ;;
    restore)
        [[ -f "$STATE_FILE" ]] || die "no state file — nothing to restore"
        original_country=$(state_get original_country)
        original_units=$(state_get original_units)
        [[ "$original_country" == "$SENTINEL" ]] && die "recorded original is $SENTINEL — refusing to restore a sentinel over itself"
        if [[ -n "$original_country" ]]; then
            set_env_key WEATHER_IP_COUNTRY "$original_country"
            pass "restored WEATHER_IP_COUNTRY=$original_country"
        fi
        if [[ -n "$original_units" ]]; then
            set_env_key WEATHER_UNITS "$original_units"
            pass "restored WEATHER_UNITS=$original_units"
        fi
        rm -f "$STATE_FILE"
        report
        ;;
    *)
        echo "usage: $0 {run|prepare|verify|restore}" >&2
        echo "  run      no-reboot: inject sentinel country, restart the unit, assert the flip" >&2
        echo "  prepare  inject sentinel country, then reboot manually" >&2
        echo "  verify   after reboot: assert unit ran, boot ordering, and the country flip" >&2
        echo "  restore  put back the pre-test country + units recorded by prepare/run" >&2
        exit 2
        ;;
esac
