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
# compilation on a gcc-less image (#214). Idempotent: only write if
# piwheels isn't already configured, so we don't clobber settings a
# future Pi OS release might add.
if [ ! -f /etc/pip.conf ] || ! grep -q 'piwheels' /etc/pip.conf 2>/dev/null; then
    cat > /etc/pip.conf << 'EOF'
[global]
extra-index-url=https://www.piwheels.org/simple
EOF
fi

# Create venv with access to system packages (GPIO libs are apt-installed
# to avoid QEMU compilation issues — see #127)
python3 -m venv --system-site-packages venv
./venv/bin/pip install --upgrade pip

# Install non-hardware packages from requirements.txt. Hardware packages
# listed in requirements-apt.txt are apt-provisioned and visible via
# --system-site-packages — skip them here so pip doesn't try to install
# (and fail to compile) what apt already provides. Single source of truth
# for apt-provisioned names is requirements-apt.txt (#214).
EXCLUDE_RE=$(grep -vE '^[[:space:]]*(#|$)' requirements-apt.txt | sed 's/\./\\./g' | paste -sd'|')
grep -vE "^(${EXCLUDE_RE})==" requirements.txt > /tmp/requirements-pigen.txt
# --upgrade mirrors update.sh / install.sh (#321). Image build is a fresh
# venv so this is a no-op here, but parity matters: anyone reading these
# three install paths should see the same pip posture. Eager strategy
# intentionally NOT used — see update.sh comment for the rationale.
./venv/bin/pip install --upgrade -r /tmp/requirements-pigen.txt
rm -f /tmp/requirements-pigen.txt

# Clean pip cache to reduce image size (#112)
rm -rf /root/.cache/pip /home/pi/.cache/pip

# WiFi provisioning now uses native nmcli (NetworkManager) instead of the
# Balena wifi-connect binary, which is incompatible with NetworkManager on
# Bookworm. No binary download needed — nmcli is part of the base OS.

# Set ownership — everything belongs to pi
chown -R pi:pi /home/pi/litclock
CHROOT
