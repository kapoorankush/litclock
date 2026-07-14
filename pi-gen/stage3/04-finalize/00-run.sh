#!/bin/bash
# Write version metadata and clean up build artifacts
#
# pi-gen sources config internally but does NOT export custom variables to
# stage scripts. Read values directly from the config file.
set -e

# shellcheck disable=SC1091
[ -f /pi-gen/config ] && . /pi-gen/config

LITCLOCK_VERSION="${LITCLOCK_VERSION:-dev}"
LITCLOCK_SHA="${LITCLOCK_SHA:-unknown}"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- Version metadata (#110) ---
cat > "${ROOTFS_DIR}/etc/litclock-version" << EOF
version=${LITCLOCK_VERSION}
git_sha=${LITCLOCK_SHA}
build_date=${BUILD_DATE}
EOF

on_chroot << 'CHROOT'
set -e

# --- Image size optimization (#112) ---

# Protect runtime libraries from being auto-removed with build deps
apt-mark manual libgcc-s1 || true

# Remove build-only dependencies no longer needed
apt-get purge -y --auto-remove \
    gcc \
    g++ \
    make \
    cpp \
    dpkg-dev \
    libc6-dev \
    linux-libc-dev \
    python3-dev

# Remove unnecessary Lite packages
apt-get purge -y --auto-remove \
    triggerhappy \
    bluez \
    nfs-common

# Clean package caches
apt-get clean
rm -rf /var/lib/apt/lists/*

# Clean misc caches and tmp files (preserve /var/log for first-boot debugging)
rm -rf /tmp/* /var/tmp/*

# Pre-compile Python bytecode INTO the image (#483). This stage used to DELETE
# all __pycache__, so a fresh flash always compiled src/ cold on its first
# litclock-firstboot run. Shipping the .pyc removes that first-run compile from
# the setup/captive-portal path. checked-hash invalidation keeps the .pyc
# deterministic (no embedded source mtimes -> reproducible image) AND correct
# across update.sh: Python re-hashes the source on import and recompiles if it
# changed, so a code update is never masked by a stale .pyc. Best-effort - a
# compile failure just falls back to the old compile-on-first-run behaviour.
#
# Scoped to src/ deliberately: the setup/captive path imports only src/ modules +
# stdlib (already compiled in the base image). The venv libraries (Pillow, waitress,
# etc.) load when litclock.service starts AFTER setup completes, not during the
# time-critical hotspot window, so compiling them here wouldn't help #483 and would
# add tens of MB to the image plus a slow qemu-emulated compile to the build.
# First strip ALL bytecode under the tree (this is what the old line did): the
# host-side `cp -a` in 01-setup-app can carry in stale/foreign .pyc from the build
# checkout, and pip generates its own — none of that should ship. THEN force (-f) a
# fresh, deterministic src/ cache, compiled with the RUNTIME interpreter
# (venv/bin/python3) so the .pyc magic number always matches what imports them at
# runtime (the chroot system python3 happens to match today, but only because the
# venv is built from it — compiling with the venv python removes that coupling).
find /home/pi/litclock -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
/home/pi/litclock/venv/bin/python3 -m compileall -q -f \
    --invalidation-mode checked-hash /home/pi/litclock/src 2>/dev/null || true
# on_chroot runs as root (after 01-setup-app's `chown -R pi:pi`), so the freshly
# written .pyc are root-owned. Restore pi ownership — otherwise the pi-user runtime
# can't refresh them after update.sh changes a source, and src/ recompiles from
# source on every process start (checked-hash keeps it correct, just slow) with
# root-owned files lingering under a pi tree.
chown -R pi:pi /home/pi/litclock/src 2>/dev/null || true
CHROOT
