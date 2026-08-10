#!/bin/bash
# Install and enable systemd services and timers
set -e

on_chroot << 'CHROOT'
set -e

INSTALL_DIR="/home/pi/litclock"

# Destinations, named so tests/test_pi_gen.py can run the shipped loops below
# against a fake tree. Unconditional assignments, never `${VAR:-default}`:
# on_chroot uses capsh, which does not scrub the environment, so an overridable
# seam would let a stray exported variable redirect where a ROOT-run build
# script installs. The tests inject their own assignments ahead of the
# sentinel-extracted blocks, so they need no seam here — see the same decision
# in 05-smoke-test.
ETC_SYSTEMD_DIR=/etc/systemd/system
ETC_TMPFILES_DIR=/etc/tmpfiles.d

# ── Copy systemd units ───────────────────────────────────────────────
#
# Globbed, NOT enumerated. A hand-maintained cp list drifted three times:
#   * v0.211.0 — litclock-wifi-reset.service + litclock-prepare-for-gift.service
#     were never copied. The PWA's Reset WiFi and Prepare-for-Gifting buttons
#     accepted the confirm tap then failed with `Unit not found` on every
#     flashed image until v0.211.1. install.sh + update.sh were unaffected.
#   * #14 — install.sh enabled units it never copied; under set -e the
#     systemctl enable aborted the whole DIY install.
#   * litclock-dev#547 — litclock-reresolve-location.service reached NO flashed image
#     at all, so #337's on-boot IP-geo re-resolve silently never ran on a
#     fresh flash. update.sh's glob installed it, so it only appeared after
#     the first OTA.
# A glob cannot forget a new unit. scripts/update.sh has used one for years —
# see its `for unit in "$INSTALL_DIR"/systemd/*.service "$INSTALL_DIR"/systemd/*.timer`
# loop — and its UNIT install has never had a member of this bug class. Note the
# suffix filter: a bare `/systemd/*` is exactly the glob that would also have
# covered tmpfiles.d/, and the drop-in below was in fact a hardcoded name in
# both this file and update.sh until litclock-dev#547. The glob habit held for units and
# did not extend to the one thing that was not a unit.
#
# The tradeoff a glob introduces: an unmatched glob is NOT an error. A wrong
# INSTALL_DIR, a renamed systemd/, or a half-failed 01-setup-app would copy
# zero units, exit 0, and ship an inert image. The hardcoded list failed
# LOUDLY there (cp errors under set -e). The assertions below restore that
# loudness. They are derived (found vs copied) rather than an exact inventory
# so that adding a unit needs no edit here — an exact count would just be the
# next drift bug.
#
# `install -m 0644 -o root -g root` rather than `cp`: cp applies the SOURCE
# file's mode to a new destination, so a unit committed with the exec bit set
# would land executable in /etc/systemd/system/. This also matches the helper
# installs further down instead of using two different idioms for the same job.
# BEGIN unit-copy-loop (tests/test_pi_gen.py extracts between these sentinels
# and runs them under bash against a fake tree with a recording `install` stub)
shopt -s nullglob
units=( "${INSTALL_DIR}"/systemd/*.service "${INSTALL_DIR}"/systemd/*.timer )
shopt -u nullglob

found=${#units[@]}
copied=0
for unit in "${units[@]}"; do
    # 01-setup-app stages the repo with `cp -a`, which preserves symlinks. A
    # symlinked unit would have its target's content silently copied out, so
    # require a plain regular file.
    if [ ! -f "$unit" ] || [ -L "$unit" ]; then
        echo "FAIL: ${unit} is not a regular file." >&2
        exit 1
    fi
    install -m 0644 -o root -g root "$unit" "${ETC_SYSTEMD_DIR}/"
    copied=$(( copied + 1 ))
done
# END unit-copy-loop

# Sanity floor. Deliberately well BELOW the real unit count and deliberately
# NOT an inventory: adding units must never require touching this number. It
# exists only to catch a source tree that is empty or nearly so — the
# half-failed-01-setup-app case, which found==copied cannot detect because
# both would simply be small.
# BEGIN copy-guard (tests/test_pi_gen.py extracts the assertions between these
# sentinels and runs them under bash with tampered counts)
MIN_EXPECTED_UNITS=10
if [ "$found" -lt "$MIN_EXPECTED_UNITS" ]; then
    echo "FAIL: only ${found} unit files under ${INSTALL_DIR}/systemd/ (floor is ${MIN_EXPECTED_UNITS})." >&2
    echo "      The repo was probably not staged correctly by 01-setup-app." >&2
    exit 1
fi
# Belt-and-braces only: on_chroot runs bash with errexit, so a failing install
# aborts before this comparison is ever reached. The FLOOR above is the guard
# that actually restores the loudness the hardcoded cp list gave us. Kept
# because it is free and would catch a future refactor that drops errexit.
if [ "$copied" -ne "$found" ]; then
    echo "FAIL: found ${found} unit files but copied ${copied}." >&2
    exit 1
fi
# END copy-guard
echo "  OK: copied ${copied} systemd units from ${INSTALL_DIR}/systemd/"

# #309 — NetworkManager dispatcher: re-render the e-ink corner QR when
# wlan0's IP changes so the displayed address tracks reality after DHCP
# churn. Mode 0755 root:root — NM silently skips dispatcher scripts that
# don't match these permissions (group/world-writable = rejected).
install -d -m 0755 /etc/NetworkManager/dispatcher.d
install -m 0755 -o root -g root \
    "${INSTALL_DIR}/scripts/nm-dispatcher/99-litclock-ip-change" \
    /etc/NetworkManager/dispatcher.d/99-litclock-ip-change

# #387 — root-owned privilege helpers, installed OUTSIDE the pi-writable repo so
# the pi user cannot rewrite what runs as root:
#   litclock-set-timezone       — sudo tz-wrapper for the arbitrary-tz path
#   litclock-mark-collected.sh  — invoked by the root NM dispatcher above (C1)
#   reset-setup.sh + lib/state.sh — run as root by litclock-prepare-for-gift
#                                   .service (pi can `systemctl start` it via 020)
install -d -m 0755 /usr/local/lib/litclock
install -d -m 0755 /usr/local/lib/litclock/lib
install -m 0755 -o root -g root \
    "${INSTALL_DIR}/scripts/litclock-set-timezone" \
    /usr/local/lib/litclock/litclock-set-timezone
install -m 0755 -o root -g root \
    "${INSTALL_DIR}/scripts/litclock-mark-collected.sh" \
    /usr/local/lib/litclock/litclock-mark-collected.sh
install -m 0755 -o root -g root \
    "${INSTALL_DIR}/scripts/reset-setup.sh" \
    /usr/local/lib/litclock/reset-setup.sh
install -m 0644 -o root -g root \
    "${INSTALL_DIR}/scripts/lib/state.sh" \
    /usr/local/lib/litclock/lib/state.sh

# ── Copy systemd-tmpfiles drop-ins ───────────────────────────────────
#
# #241 — /run/litclock, the tmpfs heartbeat directory, plus #245 M5's
# /var/lib/litclock persistent state directory. Both are created on every boot
# by systemd-tmpfiles from the drop-in copied here.
#
# Globbed and guarded for exactly the reason the unit copy above is. This WAS a
# bare hardcoded `cp` — invisible to every guard in this file, and invisible to
# 05-smoke-test too, whose derived checks globbed *.service/*.timer at the first
# level of systemd/ and never descended into tmpfiles.d/. So it was litclock-dev#547's own
# shape sitting inside litclock-dev#547's directory. 05 now derives *.conf from
# systemd/tmpfiles.d/ and also asserts /etc/tmpfiles.d/litclock.conf by absolute
# path, so both gates cover it.
#
# What a missing drop-in actually breaks, precisely: the writers that run as
# root or via sudo survive it, because scripts/lib/state.sh:62,
# scripts/lib/update_status.sh:77, scripts/wifi-watchdog.sh:130,
# scripts/litclock-lkg-record.sh:63 and scripts/litclock-bootcheck.sh:128 all
# `mkdir -p` their parent with a sudo fallback, and the NM dispatcher already
# creates /run/litclock outright. So lkg-sha, update-failed, the post-update
# grace marker and update.status keep working. What genuinely breaks is the
# PI-OWNED /run writers, which cannot mkdir in a root-owned /run:
# src/literary_clock.py's HEARTBEAT_FILE and the weather cache in
# src/weather_providers/base_provider.py. The drop-in's other job is ownership
# normalisation — pre-M5 installs left /var/lib/litclock root-owned.
#
# (An earlier version of this comment claimed the whole list no-ops. It does
# not, and the sudo-mkdir fallbacks are the reason — do not delete them as
# redundant on the strength of a doom comment.)
# BEGIN tmpfiles-copy-loop (tests/test_pi_gen.py extracts between these sentinels)
shopt -s nullglob
tmpfiles_confs=( "${INSTALL_DIR}"/systemd/tmpfiles.d/*.conf )
shopt -u nullglob

install -d -m 0755 "${ETC_TMPFILES_DIR}"
tmpfiles_found=${#tmpfiles_confs[@]}
tmpfiles_copied=0
for conf in "${tmpfiles_confs[@]}"; do
    # Same regular-file requirement as the units: 01-setup-app stages with
    # `cp -a`, so a symlink here would have its target's content copied out.
    if [ ! -f "$conf" ] || [ -L "$conf" ]; then
        echo "FAIL: ${conf} is not a regular file." >&2
        exit 1
    fi
    install -m 0644 -o root -g root "$conf" "${ETC_TMPFILES_DIR}/"
    tmpfiles_copied=$(( tmpfiles_copied + 1 ))
done
# END tmpfiles-copy-loop

# BEGIN tmpfiles-guard (tests/test_pi_gen.py extracts the assertions between
# these sentinels and runs them under bash with tampered counts)
# Floor of 1, not an inventory: the glob matching nothing is the whole failure
# this restores loudness for. `cp` failed loudly under errexit; a glob does not.
MIN_EXPECTED_TMPFILES=1
if [ "$tmpfiles_found" -lt "$MIN_EXPECTED_TMPFILES" ]; then
    echo "FAIL: no tmpfiles.d drop-ins under ${INSTALL_DIR}/systemd/tmpfiles.d/ (floor is ${MIN_EXPECTED_TMPFILES})." >&2
    echo "      /run/litclock and /var/lib/litclock would never be created." >&2
    exit 1
fi
if [ "$tmpfiles_copied" -ne "$tmpfiles_found" ]; then
    echo "FAIL: found ${tmpfiles_found} tmpfiles.d drop-ins but copied ${tmpfiles_copied}." >&2
    exit 1
fi
# END tmpfiles-guard
echo "  OK: copied ${tmpfiles_copied} tmpfiles.d drop-in(s) from ${INSTALL_DIR}/systemd/tmpfiles.d/"

# ── Enable units ─────────────────────────────────────────────────────
#
# This list stays EXPLICIT, unlike the copy above. Enablement is a policy
# decision, not an inventory: litclock.timer ships an [Install] section but
# must NOT be enabled at build time. Deriving enablement from [Install] would
# start the clock before provisioning finishes.
#
# The guard against silent omission lives in tests/test_pi_gen.py: every unit
# in systemd/ carrying an [Install] section must appear either in this list or
# in BUILD_ENABLE_EXCLUSIONS below. A new unit that needs enabling therefore
# fails the test suite rather than shipping quietly disabled.
#
# systemctl enable works in chroot — it creates the .wants symlinks directly.

# Units with [Install] deliberately NOT enabled at image-build time.
# tests/test_pi_gen.py parses this array — keep it one bare name per line.
BUILD_ENABLE_EXCLUSIONS=(
    litclock.timer
)
# litclock.timer — first-boot.sh enables it after setup completes, and it stays
# enabled for subsequent boots. Enabling at build time would race splash and
# firstboot for GPIO access before setup is done.

systemctl enable litclock-splash.service
systemctl enable litclock-firstboot.service
systemctl enable litclock-shutdown.service
systemctl enable wifi-watchdog.timer
# #209 — weekly auto-update. Timer safe to enable at build time: first
# trigger is OnCalendar=Sun 03:00 + up to 7d jitter, which only fires after
# first-boot finishes and litclock.service has been running for >=1 hour.
# ConditionPathExists=/etc/litclock/.setup-complete in the service blocks
# any pre-firstboot fire (the flag is only written by first-boot.sh on
# successful WiFi/setup completion).
# #241 — LKG poll timer; service has no [Install], so enable the timer.
systemctl enable litclock-update.timer
systemctl enable litclock-lkg.timer
# #209 follow-up — LKG auto-revert (bootcheck) poll timer. The .service has no
# [Install] (it is started by the .timer), so only the timer is enabled.
systemctl enable litclock-bootcheck.timer
# EPIC #383 PR2 (#388) — handoff fallback poll timer (service has no [Install]).
systemctl enable litclock-handoff-fallback.timer
# #245 M1 — Control PWA. ConditionPathExists=/etc/litclock/.setup-complete
# gates startup; on a fresh image the unit waits until first-boot writes the
# flag, then comes up automatically.
systemctl enable litclock-control.service
# #337 A2/A8 / litclock-dev#547 — on-boot IP-geo re-resolve oneshot. WantedBy=multi-user
# .target, gated at runtime by ConditionPathExists=/etc/litclock/.handoff-complete
# so it no-ops until setup + handoff finish.
#
# MUST be enabled in the SAME release that first copies it. scripts/update.sh
# decides whether to auto-enable a unit by whether the unit FILE already exists
# in /etc/systemd/system/ (its `was_pre_existing` check). An intermediate image that copied
# this unit without enabling it would leave it PERMANENTLY disabled on every Pi
# flashed from that image: every later OTA would see the file present, treat it
# as pre-existing, and respect a user choice that was never made.
systemctl enable litclock-reresolve-location.service

# Sanity: anything named in BUILD_ENABLE_EXCLUSIONS must actually be un-enabled.
# Catches an enable line added above without removing the unit from the
# exclusion list, which would otherwise leave the two silently contradictory.
#
# Checks EVERY *.wants directory, not a hardcoded pair. An excluded unit wanted
# by sockets.target, graphical.target or a custom target would sail past a
# two-directory check while actually being enabled — a guard that cannot fail
# is the exact failure mode this file exists to remove.
# BEGIN enable-exclusion-check (tests/test_pi_gen.py extracts between these sentinels)
for excluded in "${BUILD_ENABLE_EXCLUSIONS[@]}"; do
    if compgen -G "${ETC_SYSTEMD_DIR}/*.wants/${excluded}" > /dev/null; then
        echo "FAIL: ${excluded} is listed in BUILD_ENABLE_EXCLUSIONS but was enabled:" >&2
        compgen -G "${ETC_SYSTEMD_DIR}/*.wants/${excluded}" >&2
        exit 1
    fi
done
# END enable-exclusion-check
echo "  OK: enable-set applied, ${#BUILD_ENABLE_EXCLUSIONS[@]} deliberate exclusion(s) verified un-enabled"
CHROOT
