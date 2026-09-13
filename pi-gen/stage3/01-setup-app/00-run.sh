#!/bin/bash
# Copy the LitClock repository into the image and set up Python venv
#
# The repo source is pre-staged at /pi-gen/litclock-src by the CI workflow
# (copied before build-docker.sh bakes the pi-gen directory into the Docker
# image).  We copy it into the rootfs on the host side, then run pip install
# inside on_chroot for correct ARM compilation.
set -e

LITCLOCK_SRC="/pi-gen/litclock-src"
INSTALL_DIR="${ROOTFS_DIR}/home/pi/litclock"

if [ ! -d "${LITCLOCK_SRC}" ]; then
    echo "ERROR: ${LITCLOCK_SRC} not found. The CI workflow must copy the repo there before building."
    exit 1
fi

# Copy repo into rootfs (host side — no chroot needed)
cp -a "${LITCLOCK_SRC}" "${INSTALL_DIR}"

on_chroot << 'CHROOT'
set -e

cd /home/pi/litclock

# Ensure piwheels is configured in /etc/pip.conf. Raspberry Pi OS ships
# this by default, but reinforce so on-device pip installs (update.sh)
# always see pre-built aarch64 wheels and don't fall back to sdist
# compilation on a gcc-less image (litclock-dev#214). Idempotent: only write if
# piwheels isn't already configured, so we don't clobber settings a
# future Pi OS release might add.
if [ ! -f /etc/pip.conf ] || ! grep -q 'piwheels' /etc/pip.conf 2>/dev/null; then
    cat > /etc/pip.conf << 'EOF'
[global]
extra-index-url=https://www.piwheels.org/simple
EOF
fi

# Create venv with access to system packages (GPIO libs are apt-installed
# to avoid QEMU compilation issues — see litclock-dev#127)
python3 -m venv --system-site-packages venv
./venv/bin/pip install --upgrade pip

# Install non-hardware packages from requirements.txt. Hardware packages
# listed in requirements-apt.txt are apt-provisioned and visible via
# --system-site-packages — skip them here so pip doesn't try to install
# (and fail to compile) what apt already provides. Single source of truth
# for apt-provisioned names is requirements-apt.txt (litclock-dev#214).
EXCLUDE_RE=$(grep -vE '^[[:space:]]*(#|$)' requirements-apt.txt | sed 's/\./\\./g' | paste -sd'|')
grep -vE "^(${EXCLUDE_RE})==" requirements.txt > /tmp/requirements-pigen.txt
# --upgrade mirrors update.sh (litclock-dev#321). Image build is a fresh
# venv so this is a no-op here, but parity matters: anyone reading these
# three install paths should see the same pip posture. Eager strategy
# intentionally NOT used — see update.sh comment for the rationale.
./venv/bin/pip install --upgrade -r /tmp/requirements-pigen.txt
rm -f /tmp/requirements-pigen.txt

# litclock-dev#531 — stamp the runtime-render validation marker at BUILD time,
# so a freshly flashed card can use the runtime renderer without waiting for an
# OTA to certify it. update.sh re-stamps after any update that revokes it.
#
# This has to run HERE, after pip, and it has to run IN THE CHROOT: the check
# proves that THIS environment's freetype-py — whose wheel bundles its own
# libfreetype, and whose hinted metrics decide every line break — reproduces
# GD's measurements exactly. Running it on the build host would certify the
# host's wheel, which is not the one the device runs. The chroot is arm64 and
# the venv above is the device's, so the wheel under test is the shipped one.
#
# NON-FATAL by design. This script runs under `set -e`, but a failing command
# in an `if` CONDITION does not trip errexit (verified, not assumed) — which is
# why no `|| true` is needed here and why adding one would be decoration. A
# validation failure must not fail the image build: no marker simply means the
# device serves pre-rendered PNGs, the tier that has always shipped. A wrong
# fallback is impossible; a wrong render is not. The failure is logged loudly
# so a build that quietly lost the runtime tier shows up in the CI log rather
# than being discovered on a device.
echo "Validating GD-exact measurement (litclock-dev#531)..."
if ./venv/bin/python3 tools/validate_measurement.py check --stamp; then
    echo "  runtime-render validation PASSED — marker stamped into the image"
else
    echo "  WARNING: runtime-render validation FAILED in the build chroot."
    echo "  The image is usable: it will serve pre-rendered images and ignore"
    echo "  LITCLOCK_RUNTIME_RENDER. Investigate before relying on the runtime tier."
fi

# Clean pip cache to reduce image size (litclock-dev#112)
rm -rf /root/.cache/pip /home/pi/.cache/pip

# WiFi provisioning now uses native nmcli (NetworkManager) instead of the
# Balena wifi-connect binary, which is incompatible with NetworkManager on
# Bookworm. No binary download needed — nmcli is part of the base OS.

# Set ownership — everything belongs to pi
chown -R pi:pi /home/pi/litclock
CHROOT
