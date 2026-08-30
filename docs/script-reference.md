# Script Reference

Scripts are organized into `scripts/` (shell) and `src/` (Python):

| Script | Syntax | Purpose |
|--------|--------|---------|
| `scripts/runtheclock.sh` | `./scripts/runtheclock.sh` | Fetch a quote and update the display (normally triggered by the systemd timer) |
| `src/eink_display.py` | `python3 src/eink_display.py <subcommand>` | E-ink display utility (see subcommands below) |
| `src/clear.py` | `python3 src/clear.py` | Clear the e-ink display to white |
| `src/wifi_provision.py` | `python3 src/wifi_provision.py [flags]` | WiFi provisioning via captive portal hotspot |
| `scripts/update.sh` | `./scripts/update.sh` | Pull latest code and apply updates in-place |
| `scripts/reset-setup.sh` | `sudo ./scripts/reset-setup.sh [--yes] [--reboot] [--poweroff] [--keep-wifi] [--gift-mode]` | Reset configuration (see [Resetting the Clock](../README.md#resetting-the-clock)); `--gift-mode` preps the device for shipping with a welcome splash. **`--gift-mode` and `--poweroff` both disable SSH before powering down** (litclock-dev#528) — a handed-on device gets a fresh-flash posture; re-enable per [recovery](recovery.md) |
| `scripts/prepare-for-cloning.sh` | `sudo ./scripts/prepare-for-cloning.sh` | Wipe config and credentials for SD card cloning (see [Creating SD Cards](sd-card-cloning.md)) |
| `scripts/cut-release.sh` | `./scripts/cut-release.sh vX.Y.Z [--expect-sha SHA] [-m MSG] [--yes]` | Promote the CHANGELOG heading, commit and tag a release — does not push (see [Building the Image](building-image.md)) |
| `scripts/install.sh` | _(retired)_ | Superseded by the flashed image; see [Build from source](../README.md#option-2-build-from-source-terminal-required) |

## eink_display.py subcommands

```bash
# Show a QR code
python3 src/eink_display.py qr <url> [--title TEXT] [--caption TEXT] [--save FILE]

# Show a status message (literal form — ad-hoc/QA use)
python3 src/eink_display.py status <title> [--message TEXT] [--submessage TEXT] [--save FILE]

# Show a status message resolved from the language catalog (what the boot /
# first-boot scripts use — litclock-dev#532): the triplet comes from
# <prefix>.title/.message/.submessage in languages/<code>/strings.json
python3 src/eink_display.py status --catalog-prefix PREFIX [--slot NAME=VALUE ...] [--save FILE]

# Print one language-catalog string (shell string-lookup path)
python3 src/eink_display.py catalog-get KEY [--slot NAME=VALUE ...]

# Clear the display
python3 src/eink_display.py clear [--save FILE]
```

Pass `--save FILE` to write a PNG instead of sending to the display.

## wifi_provision.py flags

| Flag | Description |
|------|-------------|
| `--ssid NAME` | Hotspot SSID (default: `LitClock-Setup`) |
| `--timeout SECONDS` | Timeout in seconds (default: `300`) |
| `--check-only` | Check WiFi status and exit (0 = connected, 1 = not) |
| `--no-display` | Don't update the e-ink display during provisioning |
| `--download-only` | Download the wifi-connect binary without running it |

## Internal scripts

These are invoked by systemd services and not meant to be run directly: `scripts/boot-splash.sh`, `scripts/shutdown-splash.sh`, `scripts/first-boot.sh`, `src/setup_server.py`.
