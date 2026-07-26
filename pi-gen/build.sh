#!/bin/bash
#
# Build a LitClock Raspberry Pi OS image using pi-gen (Docker).
#
# Usage:
#   ./pi-gen/build.sh                  # Build from master (64-bit image)
#   LITCLOCK_REF=v2026.03.0 ./pi-gen/build.sh  # Build from a tag
#   LITCLOCK_ARCH=armhf ./pi-gen/build.sh      # 32-bit image (Pi Zero W / Pi 1)
#
# Architectures:
#   arm64 (default) — Pi Zero 2 W, Pi 3/4/5
#   armhf           — 32-bit ARMv6 image for the original Pi Zero W and Pi 1
#                     (an arm64 image shows ZERO boot activity on those boards)
#
# Requires: Docker
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LITCLOCK_ARCH="${LITCLOCK_ARCH:-arm64}"
case "${LITCLOCK_ARCH}" in
    arm64|armhf) ;;
    *) echo "ERROR: LITCLOCK_ARCH must be arm64 or armhf (got '${LITCLOCK_ARCH}')" >&2; exit 1 ;;
esac
# The armhf and arm64 releases are cut from different pi-gen branches, so the
# tag encodes the arch. Keep both halves of this name in sync with the CI
# pins in .github/workflows/build-image.yml.
PI_GEN_TAG="2025-05-06-raspios-bookworm-${LITCLOCK_ARCH}"
WORK_DIR="${SCRIPT_DIR}/work-${LITCLOCK_ARCH}"

echo "=== LitClock Image Builder ==="
echo "arch: ${LITCLOCK_ARCH}"
echo "pi-gen tag: ${PI_GEN_TAG}"
echo "LITCLOCK_REF: ${LITCLOCK_REF:-master}"
echo ""

# Clone pi-gen if not already present
PI_GEN_DIR="${WORK_DIR}/pi-gen"
if [ ! -d "${PI_GEN_DIR}" ]; then
    echo "Cloning pi-gen..."
    mkdir -p "${WORK_DIR}"
    git clone --depth 1 --branch "${PI_GEN_TAG}" \
        https://github.com/RPi-Distro/pi-gen.git "${PI_GEN_DIR}"
else
    echo "Using existing pi-gen clone at ${PI_GEN_DIR}"
fi

# Copy our config
cp "${SCRIPT_DIR}/config" "${PI_GEN_DIR}/config"

# Append build-time variables to config
cat >> "${PI_GEN_DIR}/config" << EOF

# Build-time overrides (appended by build.sh)
LITCLOCK_REF=${LITCLOCK_REF:-master}
LITCLOCK_VERSION=${LITCLOCK_VERSION:-dev}
LITCLOCK_SHA=${LITCLOCK_SHA:-$(cd "${REPO_DIR}" && git rev-parse --short HEAD)}
EOF

# Copy our custom stage
rm -rf "${PI_GEN_DIR}/stage3"
cp -r "${SCRIPT_DIR}/stage3" "${PI_GEN_DIR}/stage3"

# Ensure chroot scripts are executable
find "${PI_GEN_DIR}/stage3" -name "*.sh" -exec chmod +x {} +

# Skip image export for earlier stages (we only want our stage's image)
touch "${PI_GEN_DIR}/stage2/SKIP_IMAGES"

# pi-gen's export-image unconditionally copies .bmap but bmap-tools
# is not in its Dockerfile — make the copy conditional
sed -i 's|cp "$BMAP_FILE" "$DEPLOY_DIR/"|[ -f "$BMAP_FILE" ] \&\& cp "$BMAP_FILE" "$DEPLOY_DIR/"|' \
    "${PI_GEN_DIR}/export-image/05-finalise/01-run.sh"

# Stage repo source for the Docker build (chroot has no network/credentials)
LITCLOCK_SRC="${PI_GEN_DIR}/litclock-src"
rm -rf "${LITCLOCK_SRC}"
LITCLOCK_REF="${LITCLOCK_REF:-master}"
git clone --depth 1 --single-branch --branch "${LITCLOCK_REF}" --recurse-submodules "${REPO_DIR}" "${LITCLOCK_SRC}"
rm -rf "${LITCLOCK_SRC}/pi-gen"

# Build
cd "${PI_GEN_DIR}"
echo "Starting pi-gen Docker build..."
./build-docker.sh

# Report output
echo ""
echo "=== Build complete ==="
DEPLOY_DIR="${PI_GEN_DIR}/deploy"
if [ -d "${DEPLOY_DIR}" ]; then
    echo "Images available in: ${DEPLOY_DIR}"
    ls -lh "${DEPLOY_DIR}"/*.img* 2>/dev/null || echo "(no .img files found)"
else
    echo "Warning: deploy directory not found at ${DEPLOY_DIR}"
fi
