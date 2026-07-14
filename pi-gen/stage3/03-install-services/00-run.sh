#!/bin/bash
# Install and enable systemd services and timers
set -e

on_chroot << 'CHROOT'
set -e

INSTALL_DIR="/home/pi/litclock"

# Copy all systemd units
cp "${INSTALL_DIR}/systemd/litclock-splash.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock-firstboot.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock.timer" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock-shutdown.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/wifi-watchdog.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/wifi-watchdog.timer" /etc/systemd/system/
# #209 — weekly auto-update + #241 — LKG poll timer
cp "${INSTALL_DIR}/systemd/litclock-update.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock-update.timer" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock-lkg.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock-lkg.timer" /etc/systemd/system/
# #209 follow-up — LKG auto-revert consumer (bootcheck). Timer-driven
# oneshot; service has no [Install] (started by the .timer), so only the
# timer is enabled below. Same missing-cp-breaks-fresh-image trap.
cp "${INSTALL_DIR}/systemd/litclock-bootcheck.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock-bootcheck.timer" /etc/systemd/system/
# EPIC #383 PR2 (#388) — handoff last-resort completer. Timer-driven oneshot;
# the .service has no [Install] (started by the .timer), so only the timer is
# enabled below. Same missing-cp-breaks-fresh-image trap as wifi-reset.
cp "${INSTALL_DIR}/systemd/litclock-handoff-fallback.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/litclock-handoff-fallback.timer" /etc/systemd/system/
# #245 M1 — Control PWA always-on management surface
cp "${INSTALL_DIR}/systemd/litclock-control.service" /etc/systemd/system/
# #245 M5 D11 — Reset-WiFi flow (fired on demand by /api/wifi/reset).
# Type=oneshot + no [Install] → no enable needed; sudoers only allows
# `systemctl start --no-block litclock-wifi-reset.service`. Missing this
# cp silently broke Reset WiFi on every fresh-image install from v0.211.0
# until v0.211.1; install.sh + update.sh paths were unaffected (caught in
# M8 hardware QA on test Pi 2026-05-13).
cp "${INSTALL_DIR}/systemd/litclock-wifi-reset.service" /etc/systemd/system/
# #245 M8-prep / #280 — Prepare-for-Gifting (fired by /api/system/prepare-for-gift).
# Same shape as wifi-reset above; same install-path gap; same fix.
cp "${INSTALL_DIR}/systemd/litclock-prepare-for-gift.service" /etc/systemd/system/
# #510 — Factory reset (fired by /api/system/reset). Same shape as the two units
# above (Type=oneshot, no [Install], sudoers start-only); same install-path gap.
cp "${INSTALL_DIR}/systemd/litclock-reset.service" /etc/systemd/system/

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

# #241 — tmpfs heartbeat directory created on every boot
cp "${INSTALL_DIR}/systemd/tmpfiles.d/litclock.conf" /etc/tmpfiles.d/

# Enable services (systemctl enable works in chroot — it creates symlinks)
systemctl enable litclock-splash.service
systemctl enable litclock-firstboot.service
# litclock.timer is NOT enabled here — first-boot.sh enables it after setup completes,
# and it stays enabled for subsequent boots. Enabling it at build time would race with
# splash/firstboot for GPIO access before setup is done.
systemctl enable litclock-shutdown.service
systemctl enable wifi-watchdog.timer
# #209 — weekly auto-update. Timer safe to enable at build time: first
# trigger is OnCalendar=Sun 03:00 + up to 7d jitter, which only fires after
# first-boot finishes and litclock.service has been running for ≥1 hour.
# ConditionPathExists=/etc/litclock/.setup-complete in the service blocks
# any pre-firstboot fire (the flag is only written by first-boot.sh on
# successful WiFi/setup completion).
# #241 — LKG poll timer; service has no [Install], so enable the timer.
systemctl enable litclock-update.timer
systemctl enable litclock-lkg.timer
# #209 follow-up — LKG auto-revert (bootcheck) poll timer.
systemctl enable litclock-bootcheck.timer
# EPIC #383 PR2 (#388) — handoff fallback poll timer (service has no [Install]).
systemctl enable litclock-handoff-fallback.timer
# #245 M1 — Control PWA. ConditionPathExists=/etc/litclock/.setup-complete
# gates startup; on a fresh image the unit waits until first-boot writes the
# flag, then comes up automatically.
systemctl enable litclock-control.service
CHROOT
