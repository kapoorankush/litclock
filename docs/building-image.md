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

**Tag push** (creates a GitHub Release) — use `scripts/cut-release.sh`:

```bash
# Check out the commit you validated, then:
./scripts/cut-release.sh v0.218.0 --expect-sha <validated-sha> -m "short summary"

# It promotes the CHANGELOG heading, commits, and tags — then stops and prints
# the push for you to run:
git push --atomic origin master v0.218.0
```

`--atomic` so the branch cannot land without the tag, and the refs are printed
shell-escaped because that line is meant to be copy-pasted into a shell.

#### Why the CHANGELOG promotion is part of tagging

The Control PWA's update card does **not** read the GitHub release body —
`control_server.update_state.fetch_release_notes()` reads `CHANGELOG.md` *at the
tag* and matches a heading for that exact tag. With no matching heading it
renders **no release notes at all**, so the owner gets an update prompt with
nothing explaining what is in it.

v0.223.0 and v0.224.0 both shipped that way because the step was written down
nowhere (litclock-dev#681). `build-image.yml` now fails a tag build whose
CHANGELOG has no matching section, and that gate is worth keeping — but it
**cannot prevent the defect** (litclock-dev#687):

- It fires *after* the tag is public. The PWA resolves updates from `/tags`, so
  the blank card reaches the fleet at `git push --tags` — the event that
  triggers the workflow.
- The OTA path never touches the image asset. `scripts/update.sh` applies an
  update with `git fetch --tags` + `git reset --hard <tag>`; it never downloads
  the `.img`. Failing the build stops the **fresh-flash artifact** and has no
  bearing on updating at all.

So the gate protects flashing, not updating. `cut-release.sh` is the fix: it
makes the promotion and the tag one step, and runs
`scripts/check-changelog-section.py` — the same check CI runs — against its own
output, so the two agree by construction.

#### What cut-release.sh refuses

| refusal | why |
|---|---|
| dirty working tree | the release commit must be the promotion and nothing else |
| detached HEAD | `git push origin <branch>` there silently no-ops, orphaning the tag |
| a tag that already exists (locally or on origin) | tags are never moved — the fleet resolves updates from `/tags` |
| a non-release tag shape | RC/QA tags are never offered to the fleet, so they must not consume `[Unreleased]` |
| an empty `[Unreleased]`, or headings/bare bullets with no content | releasing nothing is the other half of the same mistake |
| two `[Unreleased]` headings | promoting the wrong one would release the wrong notes |
| `--expect-sha` not matching HEAD | validate-first-tag-last: this is how you state which commit you QA'd |
| `--expect-sha` given a ref name rather than a sha | `--expect-sha HEAD` resolves to HEAD by definition, so it can never fail |
| a `CHANGELOG.md` that is a symlink | the promotion would be written through it, outside the repo |
| HEAD or the branch moving while it runs | the tag would land on a commit nobody validated |
| a version that is not the immediate successor | both resolvers pick the **highest** semver, so a tag cut too high permanently hides every release below it (`--allow-version-jump` to override) |
| a version at or below the highest that exists | it would never be offered to anyone |

It never pushes. That keeps a human beat between cutting a release and the fleet
seeing it, and everything before the push is undoable — the script prints how.
A failure after the CHANGELOG is rewritten rolls all the way back, including the
release commit, so a retry is never blocked by an `[Unreleased]` the failed run
already consumed.

The duplicate-tag and version-order checks resolve the fleet's namespace from the
same `DEFAULT_OWNER`/`DEFAULT_REPO` constants the device uses
(`src/control_server/update_state.py`) and probe **every** remote that matches it,
not just `origin` — a maintainer's working clone may carry several remotes, and
the fleet resolves from whichever one matches those constants, so probing origin
alone would have let a version already live on the fleet be re-cut. If no configured remote matches, it says so loudly rather
than reporting a clean pass it did not earn.

To check a CHANGELOG by hand without cutting anything:

```bash
python3 scripts/check-changelog-section.py v0.218.0
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

The custom pi-gen stage (`pi-gen/stage3/`) provisions the whole appliance. It
used to be described as replicating `scripts/install.sh`; that installer is
retired (litclock-dev#546/litclock-dev#547) and this stage is now the only provisioning path:

1. **System packages** — Python, image libraries, fonts, wireless tools
2. **BCM2835 library** — compiled from source for GPIO/SPI access
3. **Application** — repo cloned to `/home/pi/litclock` with Python venv
4. **System config** — SPI enabled, journald persistent storage (litclock-dev#172), WiFi stability fixes
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
2. **Setup network** — "LitClock-Setup" becomes available in nearby WiFi lists
3. **Phone setup** — join that network, scan the QR code on the display, and submit the setup form (WiFi network and password only — location, timezone and units are detected automatically after the Pi joins your network)
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
