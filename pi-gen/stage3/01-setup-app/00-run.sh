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
#
# BOUNDED (litclock-dev#840). Unbounded, a validator that hangs under qemu is
# killed by the job's 180-minute limit, which fails the whole image build
# instead of degrading to "no marker". Measured: 179s in the build chroot on
# the 2026-09-13 master build (176s of it inside the check; the bench Pi Zero
# 2W measures 182s natively). The bound is ~5x that -- qemu on a shared runner
# is the least stable clock in this repo -- and a twelfth of the job budget,
# so a hung check costs the build minutes, not the build. update.sh has its
# own bound (VALIDATOR_TIMEOUT_S) sized against a systemd budget; this one is
# not it and must not be copied there.
#
# `-k`: coreutils timeout sends ONE SIGTERM and then waits for the child
# forever. The case this bound exists for -- a validator wedged under
# qemu-user -- is exactly the one where SIGTERM is not acted on promptly (qemu
# delivers host signals at a translation-block exit or syscall return, and a
# wedged guest reaches neither), so without a kill the build still ran to the
# 180-minute job kill (PR litclock-dev#852 review, reproduced with a TERM-ignoring child).
# The grace is generous: the validator has no cleanup worth waiting for.
#
# The FAIL and timeout arms also emit a GitHub Actions `::warning::`
# annotation, so a build that lost the runtime tier shows on the run summary
# and the PR checks rather than as one echo in a 6000-line log. The annotation
# is a workflow command and only works on stdout at line start -- keep it a
# bare echo, unprefixed.
PIGEN_VALIDATOR_TIMEOUT_S=900
PIGEN_VALIDATOR_KILL_GRACE_S=30
echo "Validating GD-exact measurement (litclock-dev#531)..."
if timeout -k "$PIGEN_VALIDATOR_KILL_GRACE_S" "$PIGEN_VALIDATOR_TIMEOUT_S" ./venv/bin/python3 tools/validate_measurement.py check --stamp; then
    echo "  runtime-render validation PASSED — marker stamped into the image"
else
    # $? at the top of an else arm is the condition's status (bash; the
    # executed test in tests/test_runtime_render_autostamp.py pins it). No
    # pipe: `if timeout ... | sed ...; then` would test sed's exit, not the
    # validator's -- the trap update.sh already fell into once.
    validate_rc=$?
    # 124 AND 137. coreutils reports 124 when the child dies to its SIGTERM,
    # but 128+9 = 137 when `-k` has to escalate to SIGKILL -- which is the
    # qemu-wedge case this bound exists for, so testing 124 alone sent exactly
    # that run down the FAIL arm, mislabelled "exited 137" and skipping the
    # marker removal below (found by the executed TERM-ignoring test, not by
    # reading). 137 from an OOM kill reads the same way and wants the same
    # treatment: the check did not complete, so there is no proof.
    if [ "$validate_rc" -eq 124 ] || [ "$validate_rc" -eq 137 ]; then
        echo "::warning title=runtime-render validation timed out::validate_measurement.py check --stamp exceeded ${PIGEN_VALIDATOR_TIMEOUT_S}s in the build chroot and was killed (rc=${validate_rc}); the image ships WITHOUT the runtime-render marker (litclock-dev#531)"
        echo "  WARNING: runtime-render validation TIMED OUT after ${PIGEN_VALIDATOR_TIMEOUT_S}s in the build chroot (rc=${validate_rc})."
        # A run that overran its bound is not a trusted proof, and a validator
        # that survived the TERM long enough to finish would have stamped one
        # -- the annotation above would then be a lie (PR litclock-dev#852 review, F2).
        rm -f .runtime-render-validated
    else
        echo "::warning title=runtime-render validation failed::validate_measurement.py check --stamp exited ${validate_rc} in the build chroot; the image ships WITHOUT the runtime-render marker (litclock-dev#531)"
        echo "  WARNING: runtime-render validation FAILED in the build chroot (rc=${validate_rc})."
    fi
    echo "  The image is usable: it will serve pre-rendered images and ignore"
    echo "  LITCLOCK_RUNTIME_RENDER. Investigate before relying on the runtime tier."
    # A validator killed mid-write leaves its mkstemp litter next to the
    # marker, and cp -a would ship it. Same glob as update.sh: `<marker>.`
    # plus EIGHT [a-z0-9_] characters, which is Python's tempfile scheme
    # exactly, so a `.backup` sibling cannot match.
    rm -f .runtime-render-validated.[a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_]
fi

# Clean pip cache to reduce image size (litclock-dev#112)
rm -rf /root/.cache/pip /home/pi/.cache/pip

# WiFi provisioning now uses native nmcli (NetworkManager) instead of the
# Balena wifi-connect binary, which is incompatible with NetworkManager on
# Bookworm. No binary download needed — nmcli is part of the base OS.

# Set ownership — everything belongs to pi
chown -R pi:pi /home/pi/litclock
CHROOT
