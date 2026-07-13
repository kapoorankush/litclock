# Building the Image

Build your own LitClock Raspberry Pi OS image using [pi-gen](https://github.com/RPi-Distro/pi-gen).

There are two ways to build: locally with Docker, or via GitHub Actions CI.

## Option 1: CI Build (GitHub Actions)

No local prerequisites — builds run on GitHub's infrastructure.

### Trigger a build

**Manual dispatch** (any branch/ref):

```bash
gh workflow run build-image.yml
# Or with a specific ref:
gh workflow run build-image.yml -f litclock_ref=my-branch
```

You can also trigger it from the GitHub UI: **Actions > Build Image > Run workflow**.

**Tag push** (creates a GitHub Release):

```bash
git tag v0.218.0
git push --tags
```

### Download the image

For manual dispatch builds, the image is uploaded as a dev pre-release:

```bash
# List dev builds
gh release list

# Download a dev build
gh release download dev-20260712-abc1234
```

For tag push builds, the image is attached to the GitHub Release:

```bash
gh release download v0.218.0
```

Both contain a compressed image (`litclock-*.img.xz`) and a SHA256 checksum file.

## Option 2: Local Docker Build

### Prerequisites

- **Docker** — [install instructions](https://docs.docker.com/engine/install/) (or `curl -fsSL https://get.docker.com | sh`)
- ~10 GB free disk space

Verify Docker is working:

```bash
docker run --rm hello-world
```

### Build

```bash
./pi-gen/build.sh
```

This clones pi-gen into `pi-gen/work/`, copies the custom stage, and builds via Docker. The output `.img` file appears in `pi-gen/work/pi-gen/deploy/`.

### Build Options

| Variable | Default | Description |
|----------|---------|-------------|
| `LITCLOCK_REF` | `master` | Git ref to bake into the image (tag, branch, or SHA) |
| `LITCLOCK_VERSION` | `dev` | Version string written to `/etc/litclock-version` |
| `LITCLOCK_SHA` | current HEAD | Git SHA written to `/etc/litclock-version` |

Example: build from a specific tag:

```bash
LITCLOCK_REF=v0.218.0 LITCLOCK_VERSION=0.218.0 ./pi-gen/build.sh
```

## What the Image Includes

The custom pi-gen stage (`pi-gen/stage3/`) replicates everything `scripts/install.sh` does:

1. **System packages** — Python, image libraries, fonts, wireless tools, qrencode
2. **BCM2835 library** — compiled from source for GPIO/SPI access
3. **Application** — repo cloned to `/home/pi/litclock` with Python venv
4. **System config** — SPI enabled, journald volatile storage, WiFi stability fixes
5. **Systemd services** — splash, firstboot, timer, shutdown, wifi-watchdog

Build-only dependencies (gcc, make, etc.) are removed in the finalize step to minimize image size.

## Image Versioning

Images use the app SemVer: `MAJOR.MINOR.PATCH` (e.g., `0.218.0`). Tags follow `v0.218.0` format.

The version is embedded in `/etc/litclock-version`:

```
version=0.218.0
git_sha=abc1234
build_date=2026-03-12T00:00:00Z
```

## Flashing and Testing

### Flash the image

Decompress (if needed) and flash to a microSD card:

```bash
# Decompress
xz -d litclock-*.img.xz

# Flash using Raspberry Pi Imager (recommended), balenaEtcher, or dd:
sudo dd if=litclock-*.img of=/dev/sdX bs=4M status=progress
```

Replace `/dev/sdX` with your SD card device (check with `lsblk`).

### Verify the image

Insert the SD card into a Pi Zero 2W and power on, then check:

1. **Splash screen** — "LitClock / Starting..." appears on the e-ink display
2. **WiFi hotspot** — "LitClock-Setup" network becomes available
3. **Phone setup** — connect to the hotspot, scan the QR code on the display, complete the setup form (location, timezone, API key)
4. **Clock starts** — after setup, the display updates every minute with a literary quote
5. **Version metadata** — SSH in and verify:
   ```bash
   cat /etc/litclock-version
   ```

### Verify checksum

If you downloaded from a release or CI artifact:

```bash
sha256sum -c litclock-*.img.xz.sha256
```
