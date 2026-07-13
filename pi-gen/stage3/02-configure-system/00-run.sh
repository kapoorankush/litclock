#!/bin/bash
# Configure system settings: SPI, journald, NTP, WiFi stability, user groups
#
# This is a host-side script (not *-run-chroot.sh) because it needs to copy
# files from the substage's files/ directory into ${ROOTFS_DIR}.
set -e

# --- SPI ---
BOOT_CONFIG="${ROOTFS_DIR}/boot/firmware/config.txt"
if [ ! -f "${BOOT_CONFIG}" ]; then
    BOOT_CONFIG="${ROOTFS_DIR}/boot/config.txt"
fi

if grep -q "^dtparam=spi=on" "${BOOT_CONFIG}"; then
    : # already enabled
elif grep -q "^#dtparam=spi=on" "${BOOT_CONFIG}"; then
    sed -i 's/^#dtparam=spi=on/dtparam=spi=on/' "${BOOT_CONFIG}"
else
    echo "dtparam=spi=on" >> "${BOOT_CONFIG}"
fi

# Waveshare e-Paper HAT uses SPI0 with a single chip-select (CE0).
# The default SPI overlay exposes two chip-selects; spi0-1cs restricts
# to CE0 only, matching the working hardware configuration.
if ! grep -q "^dtoverlay=spi0-1cs" "${BOOT_CONFIG}"; then
    echo "dtoverlay=spi0-1cs" >> "${BOOT_CONFIG}"
fi

# --- NTP ---
# timedatectl doesn't work inside chroot (no systemd running).
# NTP is enabled by default on Raspberry Pi OS; first-boot.sh also calls
# timedatectl set-ntp true at runtime to be safe.

# --- APT: disable automatic OS updates ---
# LitClock is an appliance: a fielded / gifted clock must never change its OS
# packages behind the owner's back, where a surprise upgrade could break the
# e-ink stack, GPIO, or the venv with no one at the keyboard. This is OS-only —
# LitClock's own software updater (litclock-update.timer) is unaffected and
# stays enabled.
#
# Two layers, both fail-closed:
#   1. 20auto-upgrades config sets the apt periodic knobs to 0, so even if the
#      apt-daily services run they install nothing (canonical Debian mechanism).
#   2. Mask the apt-daily timers so the periodic units can't start at all, in
#      case a future base image ships unattended-upgrades + a non-zero config.
# The owner can still `sudo apt update && sudo apt upgrade` by hand.
install -d "${ROOTFS_DIR}/etc/apt/apt.conf.d"
cat > "${ROOTFS_DIR}/etc/apt/apt.conf.d/20auto-upgrades" << 'APTEOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::Unattended-Upgrade "0";
APTEOF
chmod 644 "${ROOTFS_DIR}/etc/apt/apt.conf.d/20auto-upgrades"
# Mask (not just disable) via the systemd convention: a symlink to /dev/null in
# /etc/systemd/system shadows the vendor unit so it can never be started/enabled.
ln -sf /dev/null "${ROOTFS_DIR}/etc/systemd/system/apt-daily.timer"
ln -sf /dev/null "${ROOTFS_DIR}/etc/systemd/system/apt-daily-upgrade.timer"

# --- Journald: persistent storage, size-capped for SD card wear ---
install -d "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
install -m 644 files/litclock-journald.conf "${ROOTFS_DIR}/etc/systemd/journald.conf.d/litclock.conf"

# --- WiFi stability fixes (Pi Zero 2W target) ---
install -m 644 files/brcmfmac.conf "${ROOTFS_DIR}/etc/modprobe.d/brcmfmac.conf"
# #245 M5 D8 — single canonical wifi-watchdog.sh under scripts/. The pi-gen
# files/wifi-watchdog.sh copy is gone; we install from the same source as
# scripts/install.sh and scripts/update.sh do.
install -m 755 "${ROOTFS_DIR}/home/pi/litclock/scripts/wifi-watchdog.sh" \
    "${ROOTFS_DIR}/usr/local/bin/wifi-watchdog.sh"
# #245 M5 D11 — Reset-WiFi helper invoked by litclock-wifi-reset.service.
install -m 755 "${ROOTFS_DIR}/home/pi/litclock/scripts/litclock-wifi-reset.sh" \
    "${ROOTFS_DIR}/usr/local/bin/litclock-wifi-reset.sh"

# rc.local for WiFi: unblock rfkill + disable power management
# Bookworm soft-blocks WiFi when no wpa_supplicant.conf is preconfigured;
# first-boot needs it unblocked to start the captive portal.
cat > "${ROOTFS_DIR}/etc/rc.local" << 'RCEOF'
#!/bin/bash
/usr/sbin/rfkill unblock wifi
/usr/sbin/iwconfig wlan0 power off
exit 0
RCEOF
chmod +x "${ROOTFS_DIR}/etc/rc.local"

# --- Login terminal: back up default /etc/issue for restore after first-boot ---
cp "${ROOTFS_DIR}/etc/issue" "${ROOTFS_DIR}/etc/issue.default"

# --- User groups, sudo, config dir (must run inside chroot) ---
on_chroot << 'CHROOT'
usermod -aG gpio,spi,i2c pi

# #433: ensure pi can read systemd journals (required for the diagnostics
# page's journal_tail rendering). pi-gen stage1's default user setup
# normally includes systemd-journal; this is belt-and-suspenders against
# variants of the base image that drop it. Idempotent: usermod -aG is a
# no-op when pi is already in the group. The same migration runs in
# scripts/update.sh for already-deployed Pis.
if getent group systemd-journal >/dev/null 2>&1; then
    usermod -aG systemd-journal pi
fi

install -d /etc/sudoers.d
echo "pi ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/010_pi-nopasswd
chmod 440 /etc/sudoers.d/010_pi-nopasswd

# #245 M4 — Control PWA scoped sudo. Validate the source first; visudo -c -f
# exits non-zero on bad syntax, set -e at the top of this script aborts the
# build before a broken file lands. No `if [ -f ]` guard — a missing source
# file means the repo reorganized and the install path silently dropped a
# critical security drop. Fail loud at build time, not silently at runtime.
SUDOERS_SRC="/home/pi/litclock/sudoers/020_litclock-control"
visudo -c -f "$SUDOERS_SRC"
install -m 0440 -o root -g root "$SUDOERS_SRC" /etc/sudoers.d/020_litclock-control

# #343 — let the `pi` service account bind port 80 for control_server without a
# capability (sysctl, NOT AmbientCapabilities, to avoid flipping NoNewPrivs and
# breaking the litclock-control setuid-sudo reboot path). Applied at first boot
# by systemd-sysctl.service before litclock-control starts.
install -m 0644 -o root -g root \
    "/home/pi/litclock/sysctl.d/30-litclock-unprivileged-ports.conf" \
    /etc/sysctl.d/30-litclock-unprivileged-ports.conf

install -d /etc/litclock
CHROOT
