<p align="center">
  <img src="logo.png" alt="LitClock" width="160">
</p>

<h1 align="center">LitClock</h1>

<p align="center">
  <a href="https://github.com/kapoorankush/litclock/actions/workflows/lint.yml"><img src="https://github.com/kapoorankush/litclock/actions/workflows/lint.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/kapoorankush/litclock/actions/workflows/build-image.yml"><img src="https://github.com/kapoorankush/litclock/actions/workflows/build-image.yml/badge.svg" alt="Image Build"></a>
  <a href="https://github.com/kapoorankush/litclock/releases/latest"><img src="https://img.shields.io/github/v/release/kapoorankush/litclock?label=Download%20Image&color=brightgreen" alt="Download Image"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11-blue.svg" alt="Python 3.11"></a>
  <a href="https://www.raspberrypi.com/"><img src="https://img.shields.io/badge/platform-Raspberry%20Pi-c51a4a.svg" alt="Platform"></a>
</p>

<p align="center">A clock that tells the time with literature: 4,800+ curated book passages, one for every minute of the day, on a paper-like e-ink display — with the date and weather.</p>

![LitClock on a shelf, showing a passage from Confess, Fletch at twenty-six minutes past ten](docs/media/litclock-photo.jpg)

LitClock is built for people who want a literary clock on their shelf and never want to think about it again: setup is a two-minute, phone-only affair, and after that the clock configures, updates, and heals itself. **The defaults are the product.**

**[Build one](#build-one)** · **[Living with it](#living-with-it)** · **[Give one away](#give-one-away)** · **[Under the hood](#under-the-hood)** · **[Credits](#credits)**

### One-minute tour

![A one-minute tour of LitClock: the literary clock face, the two-minute WiFi-only setup, and the phone control app](docs/media/litclock-intro.gif)

## Build one

Four steps, roughly an afternoon (most of it waiting for the 3D printer).

### 1. Get the parts

| Part | Notes | ~Cost |
|------|-------|-------|
| [Raspberry Pi Zero 2 W](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) | Get the **WH** variant (pre-soldered header) unless you like soldering | $15 |
| [Waveshare 7.5" e-Paper HAT (V2)](https://www.amazon.com/dp/B075R4QY3L) | 800×480, black/white ([product page](https://www.waveshare.com/7.5inch-e-paper-hat.htm)) | $60 |
| microSDHC card | 32 GB recommended | $10 |
| Micro-USB power supply | 5V/2A minimum | $10 |
| 3D-printed case *(optional)* | PLA — see step 3 | $5–30 |
| M2.5 threaded inserts + screws, USB-C→Micro-USB adapter *(case only)* | Inserts secure the case; the adapter puts a clean USB-C port on the back | $8 |

Full details in **[Hardware Assembly](docs/hardware-assembly.md)**.

### 2. Flash the SD card

1. Download the latest `.img.xz` from **[Releases](https://github.com/kapoorankush/litclock/releases/latest)**
2. Flash it to the microSD card with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) or [balenaEtcher](https://etcher.balena.io/)

That's it — no config files to edit, no SSH to set up. Everything else happens from your phone after power-on.

<details>
<summary><b>Installing on an existing Raspberry Pi OS instead</b> (terminal required)</summary>

On a fresh Raspberry Pi OS Lite install, SSH in (or connect a keyboard) and run:

```bash
curl -sSL https://raw.githubusercontent.com/kapoorankush/litclock/master/scripts/install.sh | bash
```

The installer sets up system dependencies, the BCM2835 driver, SPI, NTP sync, the Python venv, all systemd services, and downloads the quote-image set (~130 MB — needs network; the clock falls back to a time-only display if the download fails). It also detects Pi Zero W hardware and offers the [WiFi stability fixes](#wifi-stability-pi-zero-w--zero-2-w). Reboot when it finishes — from there the flow matches the flashed image.

The installer and updater are for existing OS installs only; for a fresh SD card, the downloadable image above is the way.
</details>

### 3. Print and assemble the case

Print-ready STLs are in [`3d-models/`](3d-models/) — three PLA parts, lightly modified from [Arthur Gassner's Time Teller](https://timeteller.arthurgassner.com) case design (CC BY). Then:

1. Connect the e-Paper HAT to the Pi's 40-pin header, and the display to the HAT via the ribbon cable
2. Assemble the case around it — threaded inserts, the USB-C adapter on the back, screws

Arthur's guide covers the case assembly beautifully, and our **[Hardware Assembly](docs/hardware-assembly.md)** page has the LitClock-specific details (ribbon-cable orientation, e-ink handling notes). No case? The bare HAT sandwich works fine on a shelf while you decide.

### 4. Power on — two-minute setup

1. Insert the SD card and power on. Within a minute the display shows a **"LitClock-Setup"** hotspot with its password and a QR code.
2. Join the hotspot from your phone — the setup page opens automatically (or browse to the address shown on the display).
3. Pick your home WiFi and enter its password. **That's the whole form** — location, timezone, and temperature units auto-detect once the clock is online.
4. The display shows "Ready to read." with a QR code to the clock's control app. Scan it, tap **"Done — Start the Clock"** (or just wait — it starts on its own), and add the app to your home screen.

<table>
  <tr>
    <td align="center"><b>1.</b> The clock shows its setup hotspot + QR</td>
    <td align="center"><b>2.</b> Your phone joins; the portal opens — pick your WiFi</td>
  </tr>
  <tr>
    <td align="center"><img src="docs/images/setup-1-hotspot.png" alt="E-ink WiFi Setup screen with hotspot name, password, and QR code" width="420"></td>
    <td align="center"><img src="docs/images/setup-2-portal.png" alt="Hotspot portal: WiFi network picker, password field, Complete Setup button" width="200"></td>
  </tr>
  <tr>
    <td align="center"><b>3.</b> The clock joins your WiFi and auto-detects your location</td>
    <td align="center"><b>4.</b> "Ready to read." — scan the QR for the control app</td>
  </tr>
  <tr>
    <td align="center"><img src="docs/images/setup-3-connecting.png" alt="E-ink connecting splash" width="420"></td>
    <td align="center"><img src="docs/images/setup-4-handoff.png" alt="E-ink handoff splash with auto-detected settings and control-app QR" width="420"></td>
  </tr>
</table>

Anything the auto-detection got wrong — city, units, mature-content filter — takes a few taps to fix in the app's Settings tab. Prefer paper instructions? Print the **[quick-start booklet](docs/manual/)** ([A4](docs/manual/litclock-manual-A4-booklet.pdf) / [US Letter](docs/manual/litclock-manual-Letter-booklet.pdf)) — a one-sheet, fold-in-half guide written for non-technical users.

## Living with it

### The Control App

Every LitClock serves a small web app on your home network at **`http://litclock.local`** (or the clock's IP — the QR in the corner of every quote points there). Open it in any browser, or add it to your phone's home screen. No account, no cloud — the app is served by the clock itself and works only on your LAN.

| Tab | What it does |
|-----|--------------|
| **Status** | Current quote, weather, WiFi, and version at a glance |
| **Settings** | Location (automatic by IP, or type any place worldwide), weather on/off, Fahrenheit/Celsius, mature-content filter |
| **Updates** | Current version, release notes, and a button to apply an update now |
| **System** | Restart, power off, reset WiFi, factory reset, and "Prepare for Gifting" |
| **Diagnostics** | Read-only health panel: last render, network, services, recent logs, and a downloadable support-log bundle |

<p align="center">
  <img src="docs/images/pwa-status.png" alt="Status tab — current quote, weather, version" width="260">
  <img src="docs/images/pwa-settings.png" alt="Settings tab — location, weather, temperature" width="260">
  <img src="docs/images/pwa-system.png" alt="System tab — restart, reset WiFi, factory reset" width="260">
</p>

### Configuration

Weather works out of the box — no signup, no API key. The clock uses [Open-Meteo](https://open-meteo.com) as its default forecast provider, and location, timezone, and units are auto-detected during setup. Day-to-day changes are made in the control app's **Settings** tab. That's the whole configuration.

Under the hood, settings live in `/home/pi/litclock/env.sh`. You only need to touch this file on a DIY install or for the advanced options below:

```bash
# Weather location (written by setup / the control app; override here if you want)
export WEATHER_LATITUDE=30.27
export WEATHER_LONGITUDE=-97.74
export WEATHER_UNITS=imperial

# Quote content
export ALLOW_NSFW_QUOTES=false

# Weather cache duration in seconds (default: 3600)
export WEATHER_TTL=3600

# Optional: use OpenWeatherMap instead of the default Open-Meteo (leave blank for Open-Meteo)
# export OPENWEATHERMAP_APIKEY=
```

| Variable | Description |
|----------|-------------|
| `WEATHER_LATITUDE` | Location latitude (set by setup / the app; see "Overriding location coordinates" below) |
| `WEATHER_LONGITUDE` | Location longitude (set by setup / the app; see "Overriding location coordinates" below) |
| `WEATHER_UNITS` | `imperial` (Fahrenheit) or `metric` (Celsius) |
| `ALLOW_NSFW_QUOTES` | Show quotes with mature content (default: `false`) |
| `WEATHER_TTL` | Weather cache duration in seconds (default: `3600`) |
| `OPENWEATHERMAP_APIKEY` | Optional — use OpenWeatherMap instead of Open-Meteo (see below) |

<details>
<summary><b>Advanced: using OpenWeatherMap instead of Open-Meteo</b></summary>

By default the clock uses Open-Meteo, which is free and requires no signup. If you'd rather use OpenWeatherMap (for example, you already have a key or prefer its forecast model):

1. Sign up for a free account at [OpenWeatherMap](https://openweathermap.org/) and grab a key from [API Keys](https://home.openweathermap.org/api_keys).
2. Edit `/home/pi/litclock/env.sh` and set `OPENWEATHERMAP_APIKEY=your_key_here`.
3. Restart the timer: `sudo systemctl restart litclock.timer`.

The OpenWeatherMap free tier allows 1,000 calls per day; the clock caches forecasts for an hour by default, so usage stays well within limits.
</details>

<details>
<summary><b>Advanced: overriding location coordinates</b></summary>

The control app's Location setting handles geocoding for most users (it accepts city names, postal codes, and has an Advanced panel for raw coordinates). If you'd rather edit the file directly — for example on a DIY install — set `WEATHER_LATITUDE` and `WEATHER_LONGITUDE` in `env.sh`.

Finding coordinates:

- **Google Maps**: right-click your location; the coordinates appear at the top of the menu (e.g., "30.27, -97.74"). Latitude first, longitude second.
- **[latlong.net](https://www.latlong.net/)**: search an address and copy the values.
- **iPhone**: open the Compass app; coordinates are at the bottom.
- **Android**: long-press your location in Google Maps; coordinates appear at the top.
</details>

### Updating

**Short version:** you don't need to do anything. The clock updates itself.

LitClock pulls the latest blessed release **once a week, Sunday 03:00 local time, with up to 7 days of randomization** so the fleet doesn't all update at the same moment. You can also apply an update immediately from the control app's **Updates** tab, or from a shell: `/home/pi/litclock/scripts/update.sh`.

What updates automatically:
- LitClock code (clock logic, control app, setup server, shell scripts)
- Python packages in the venv
- systemd units (new timers/services are auto-enabled)
- `env.sh` — **new** variables from `env.sh.sample` are merged in; your existing values are preserved
- Quote images, pinned by `.images-version`

What does NOT update automatically — **by design**:
- OS packages (`apt upgrade`) and Raspberry Pi firmware. On the flashed image, OS auto-updates are explicitly disabled: a surprise apt upgrade could break the display stack with nobody at the keyboard. OS-level updates happen when you flash a newer image (or run them yourself over SSH).

Two safety nets protect every update:
- A **pre-wiring smoke test**: after rebuilding the venv, the updater renders an in-memory quote image. If the render fails, the update reverts to the previous SHA before touching the running clock. If that happens, a subtle "!" glyph appears in the top-right corner of the clock face until the next successful update.
- A **boot check with automatic rollback**: if the clock ever boots and can't paint a quote, it retries, and after repeated failures automatically reinstalls the last release that is known to have painted. A bad update can't brick a clock that has no keyboard and no SSH.

<details>
<summary><b>Inspecting or opting out of auto-updates</b></summary>

Check when the timer last fired and its next scheduled run:

```bash
systemctl status litclock-update.timer
```

See the logs of the last update attempt:

```bash
journalctl -u litclock-update.service --since "1 week ago"
```

If you'd rather manage updates yourself, disable the timer via SSH:

```bash
sudo systemctl disable --now litclock-update.timer
```

The clock keeps working on whatever SHA it's pinned to; manual updates via the app or `update.sh` still work, and the updater respects a manually-disabled timer — it won't silently re-enable it. Re-enable with `sudo systemctl enable --now litclock-update.timer`.
</details>

### Resetting

Most resets don't need a shell — use the control app's **System** tab:

- **Reset WiFi** — forget saved networks and return to the setup hotspot (settings and quote history are kept)
- **Factory reset** — wipe all settings and start over from the first-boot experience
- **Prepare for Gifting** — wipe WiFi, write a welcome message for the recipient, and power off ready to box up

From a shell, the equivalent is:

```bash
sudo ./scripts/reset-setup.sh
sudo reboot
```

Flags:
- `--yes` — skip the confirmation prompt
- `--reboot` — reboot automatically after reset
- `--wipe-wifi` — also delete saved WiFi networks (full fresh-flash simulation)
- `--gift-mode` — prepare for shipping: wipes WiFi, paints a welcome splash on the e-ink, and powers off. Implies `--wipe-wifi --yes`.

### Troubleshooting

**Start here (no shell needed):** open the control app → **Diagnostics** tab. It shows version, last render time, WiFi + weather status, error flags, and recent logs. Screenshot it, or use "Download full logs" to export a redacted support bundle safe to attach to an issue.

- **Wrong city, units, or timezone**: fix it in the app → Settings. If setup couldn't detect your location at all (some networks block IP geolocation), the app offers a one-tap "use my browser's timezone" fallback so the clock runs correctly with weather off.
- **Display not updating**: Check SPI is enabled with `ls /dev/spi*`
- **Check service status**: `systemctl status litclock.timer`
- **View service logs**: `journalctl -u litclock.service --since today`
- **Force weather update**: `rm /home/pi/litclock/weather-cache*.json`
- **WiFi disconnects (Pi Zero)**: see [WiFi stability](#wifi-stability-pi-zero-w--zero-2-w), or check `dmesg | grep brcmfmac` for errors
- **WiFi fails during setup**: The setup page shows an error banner and lets you fix the password and resubmit. If it keeps failing, restart the Pi closer to your router.
- **Start setup over**: app → System → Factory reset, or `sudo ./scripts/reset-setup.sh && sudo reboot` from a shell
- **Clock stuck, or you need a shell**: SSH ships **off**. See **[Recovering a LitClock](docs/recovery.md)** for getting a shell via the console, enabling SSH from the SD card, resetting to first-boot, and the read-only Diagnostics tab.

For hardware issues, refer to the [Waveshare wiki](https://www.waveshare.com/wiki/7.5inch_e-Paper_HAT) and [demo repository](https://github.com/waveshare/e-Paper).

## Give one away

LitClock is designed to be gifted to someone who will never open a terminal:

1. In the control app: **System → Prepare for Gifting** — wipes your WiFi, writes an optional welcome message, and powers the clock down ready to box up
2. Print the **[quick-start booklet](docs/manual/)** and enclose it — one folded sheet covers everything the recipient needs
3. The recipient plugs it in and gets the same two-minute setup you did, on their own WiFi

To produce several clocks, see **[SD Card Cloning](docs/sd-card-cloning.md)** for duplicating pre-configured cards. (If *you* received a pre-configured card: just insert it and power on — setup starts at the hotspot step.)

## Under the hood

### Philosophy

LitClock is not an SSH-optional developer tool. If you are a non-technical user, you should never need to do anything after first boot: everything configures itself, updates itself, and recovers itself. If you are a technically comfortable user, you can enable SSH (it ships off — see [Recovering a LitClock](docs/recovery.md)) and disable or customize any of this. That's why updates are silent and automatic, why the failure story is automatic rollback rather than error messages, and why day-to-day control happens from a phone rather than a terminal.

### How it works

On boot, the clock goes through this sequence:

1. **Splash screen** — "LitClock / Starting..." shown on the e-ink display
2. **First-boot setup** (first time only) — the Pi creates the "LitClock-Setup" hotspot and shows joining instructions on the e-ink. After you submit your WiFi, it connects, auto-detects location/timezone/units by IP geolocation, and shows the "Ready to read." handoff screen with a QR code to the control app.
3. **Clock timer** — updates the display at the top of every minute with a new literary quote
4. **Control app** — served continuously on your LAN at `http://litclock.local`

Each frame is composed like this — quote with the time reference bolded, date, weather, and the control-app QR:

![Rendered clock frame](example.png)

The Pi has no hardware clock, so NTP time sync is enabled automatically during installation and first boot to ensure accurate time after every power cycle.

This is managed by systemd services — the important ones:

| Service | Purpose |
|---------|---------|
| `litclock.timer` / `litclock.service` | Renders a quote to the display every minute at :00 |
| `litclock-control.service` | Serves the control app on port 80 |
| `litclock-splash.service` | Shows the welcome splash on every boot |
| `litclock-firstboot.service` | Runs first-boot setup (disables itself after) |
| `litclock-update.timer` / `.service` | Weekly self-update |
| `litclock-lkg.service` / `litclock-bootcheck.service` | Record the last release that painted; auto-rollback if a boot can't paint |
| `litclock-reresolve-location.service` | Re-checks IP geolocation on boot (Automatic location mode only) |
| `litclock-shutdown.service` | Displays a shutdown splash on poweroff/halt |
| `wifi-watchdog.timer` / `.service` | Reboots the Pi if WiFi becomes unreachable (Pi Zero W stability fix) |

Useful commands:
```bash
# Check timer status and next trigger
systemctl status litclock.timer

# View clock update logs
journalctl -u litclock.service -f

# Manually trigger a display update
sudo systemctl start litclock.service

# Run the clock by hand
cd /home/pi/litclock && ./scripts/runtheclock.sh
```

### WiFi Stability (Pi Zero W / Zero 2 W)

The Raspberry Pi Zero W and Zero 2 W have a known WiFi stability issue with the BCM43430 chip that can cause the system to hang and become unreachable. The flashed image applies mitigations out of the box, and the DIY installer detects the hardware and offers them:

- **Driver parameters**: Disables roaming and problematic power features
- **Power management**: Disables WiFi power saving
- **Watchdog**: Automatically reboots if WiFi becomes unreachable

If you experience WiFi disconnections or system hangs, re-run the installer to re-apply the fixes.

### Going deeper

- **[Script Reference](docs/script-reference.md)** — every script and its usage
- **[Building the Image](docs/building-image.md)** — build your own `.img.xz` from source
- **[SD Card Cloning](docs/sd-card-cloning.md)** — duplicating configured cards
- **[Recovering a LitClock](docs/recovery.md)** — console access, enabling SSH, reset paths

## Credits

This project was originally forked from [jadonn/literary-clock](https://github.com/jadonn/literary-clock) and has since been extensively rewritten. The literary clock concept originates from [Jaap Meijers's Instructables project](https://www.instructables.com/Literary-Clock-Made-From-E-reader/) (2018).

**Quote database sources:**
- [JohannesNE/literature-clock](https://github.com/JohannesNE/literature-clock) (CC BY-NC-SA 2.5)
- [cdmoro/literature-clock](https://github.com/cdmoro/literature-clock) (MIT)
- [The Guardian "Books blog" reader thread](https://www.theguardian.com/books/booksblog/2011/apr/21/literary-clock) — community-sourced time-referential quotes from the 2011–2018 reader comments

**Case design:**
- [Arthur Gassner's Time Teller](https://github.com/arthurgassner/timeteller) (CC BY) — the 3D-printed case; lightly modified STLs ship in [`3d-models/`](3d-models/)

**Code & display inspiration:**
- [Jake Krajewski's Raspberry Pi + e-Paper Tutorial](https://medium.com/swlh/create-an-e-paper-display-for-your-raspberry-pi-with-python-2b0de7c8820c)
- [Mendhak's Waveshare e-Paper Display](https://github.com/mendhak/waveshare-epaper-display) (MIT)

**Assets:**
- [Dhole's Monochrome Weather Icons](https://github.com/Dhole/weather-pixel-icons) (CC BY-SA 4.0)
- [Google Fonts](https://fonts.google.com) — Literata (OFL 1.1)

See [NOTICE.md](NOTICE.md) for full license details on third-party components.

> LitClock was developed privately before this repository was published, so issue/PR numbers referenced in the [CHANGELOG](CHANGELOG.md) predate the public issue tracker.

## Disclaimer

LitClock is a hobbyist, open-source project provided free of charge and released under the [MIT License](LICENSE).

**THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED**, including but not limited to warranties of merchantability, fitness for a particular purpose, non-infringement, or that the software is free of defects, errors, or vulnerabilities.

By downloading, flashing, building, modifying, or otherwise using LitClock, its pre-built images, or any associated scripts or documentation, you acknowledge and agree that:

- **You use it entirely at your own risk.** The authors, contributors, and copyright holders shall not be liable for any claim, damages, or other liability, whether in contract, tort, or otherwise, arising from or in connection with the software or its use. This includes, without limitation: damage to hardware (Raspberry Pi, e-Paper display, SD cards, power supplies, or other connected equipment), data loss, network-related issues, fire, electrical damage, property damage, personal injury, or any indirect, incidental, special, consequential, or punitive damages.
- **No support is guaranteed.** Bug reports and pull requests are welcome but responses, fixes, and continued maintenance are entirely at the maintainer's discretion.
- **Third-party services and content are out of scope.** LitClock integrates with third-party services (e.g., Open-Meteo, OpenWeatherMap, IP geolocation providers) and displays literary quotes derived from public sources. The maintainer is not responsible for the availability, accuracy, licensing, or content of any third-party service or quote corpus. You are responsible for complying with the terms of any service you enable and for ensuring your use of any included content is lawful in your jurisdiction.
- **Not fit for safety-critical, commercial, or production use.** LitClock is designed as a novelty clock for personal, non-commercial use. It must not be relied upon for timekeeping, scheduling, safety, medical, industrial, or any other purpose where failure could cause harm or loss.
- **Trademarks, copyrights, and attributions** for any quoted works, referenced products, or third-party libraries remain the property of their respective owners. Inclusion does not imply endorsement.

If you do not agree with these terms, do not install or use the software.
