# LitClock

## Pre-commit checklist

Before every commit, run these in order:

1. **Lint**: `ruff check src/ image-gen/ tests/ scripts/` (matches CI — full rule set, not just `--select F`)
2. **Tests**: `python3 -m pytest tests/ --ignore=tests/test_eink_display.py -q`
3. **JS tests** (when `node_modules/` is present): `npm run test:js` — covers `src/control_server/static/js/*.js` via vitest + jsdom (#338). Dev/CI only; skip if you haven't run `npm install`. Never required on the Pi.

The eink_display tests require hardware-specific dependencies (Pillow + waveshare display drivers) that aren't available on dev machines. Weather tests now run in CI (astral is lazy-imported inside `is_daytime()`). All non-hardware tests must pass.

Do NOT commit if either step fails.

If the commit touches `image-gen/litclock_annotated.csv`, ship it with `python3 image-gen/corpus_edit.py ship "<message>"` instead of hand-sequencing image regen + `.images-version` bump + release (see CONTRIBUTING.md → "Editing the quote corpus", issue #211).

## Testing

- Framework: pytest (Python) + vitest (JS, control PWA only — #338)
- Python test command: `python3 -m pytest tests/ --ignore=tests/test_eink_display.py -q`
- JS test command: `npm run test:js` (requires `npm install` first; Node 20+)
- Python tests live in `tests/`; JS tests live in `tests/js/`
- Hardware-dependent tests (`test_eink_display.py`) are skipped on dev machines — they need Pillow + waveshare drivers
- JS test infra: vitest + jsdom; loader helpers in `tests/js/helpers/loadScript.js`. See CONTRIBUTING.md → "JavaScript tests" for usage.

## Linting

- Linter: ruff (configured in `pyproject.toml`)
- Check: `ruff check src/ image-gen/ tests/ scripts/` (matches CI)
- Max line length: 120

## First-boot QA checklist

The first-boot flow (`scripts/first-boot.sh`) provisions WiFi via a web UI; everything else (location, timezone, units) auto-populates from IP geolocation after WiFi connects (EPIC #383). Must be tested on real Pi hardware — CI cannot fully simulate hotspot creation, captive portal, IP-geo retry, or e-ink display.

**Critical scenarios to test:**

- **WiFi-only hotspot form**: Verify the setup page shows ONLY the WiFi network picker + password field + Submit button. No Location, Timezone, Temperature, or Mature-content sections — those are PWA-only post-handoff.
- **Hotspot creation**: Power on with no known WiFi networks. Verify the Pi creates a hotspot and displays credentials + QR code on the e-ink screen.
- **Captive portal**: Connect a phone to the hotspot. The setup page should auto-open (or be reachable at the displayed IP). On iOS, the journal should show the #526 coax sequence: `CAPTIVE-PROBE … -> cna-302 status=302` for the `CaptiveNetworkSupport wispr` UA, then (if the sheet rises) a Safari-family UA fetching `/cna` (bridge, 200). A `cna-bridge` 200 answered to a wispr UA means the UA split regressed.
- **WiFi provisioning**: Select a network from the setup page, enter credentials. Verify the Pi connects and transitions to clock mode.
- **WiFi provisioning failure + retry**: Enter a wrong WiFi password. The page should auto-refresh and show an error banner ("Couldn't join your WiFi…"). Fix the password and resubmit. The Pi should connect on retry. Banner must NOT use the deprecated "home WiFi" phrasing.
- **IP-geo auto-populate (US residential)**: On a US residential WiFi, after submit verify env.sh contains `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, `WEATHER_LOCATION_NAME` (City, State), `WEATHER_UNITS=imperial`, and `timedatectl` reports the correct timezone — all from one ip-api.com call.
- **IP-geo auto-populate (non-US)**: On a non-US WiFi (or via VPN), verify `WEATHER_UNITS=metric` and tz matches the egress country.
- **IP-geo hard failure**: Block `ip-api.com` (firewall rule or DNS block) during provisioning. Resolver retries 4 times with 1/3/9s backoff. After hard failure: env.sh location keys stay empty, timezone unset. PR2 handoff splash will surface the browser-tz fallback.
- **Connecting splash (PR2)**: After submitting WiFi creds, the e-ink should swap the hotspot QR for a "Connecting to {SSID}…" splash while WiFi joins + IP-geo runs (~30s), before the handoff splash.
- **Handoff splash + quote gate (PR2)**: After IP-geo succeeds, the e-ink shows the "Ready to read." handoff splash (settings summary block: Location/Timezone/Units/Mature + PWA QR top-right encoding `http://<IP>` — port 80, no port shown, #343). Quotes must NOT start yet — `litclock.service` is gated on `/etc/litclock/.handoff-complete`. Verify the QR scans to the PWA and the Status tab shows the "Setup complete" top-sheet banner with a "Done — Start the Clock" button.
- **Handoff completion paths (PR2)**: Verify ALL of: (a) tapping "Done" in the PWA, (b) saving any setting in PWA Settings, (c) waiting 120s — each writes `.handoff-complete` and the e-ink paints the first quote within ~1 min. The AtHS hint must stay suppressed while the banner is up, then appear after Done.
- **Handoff failure (IP-geo blocked, PR2)**: With `ip-api.com` blocked, the e-ink shows "Almost ready." + "Not detected" rows; the PWA banner shows "Almost there." with a "Use {browser-tz}" button. Tapping it sets the system tz and starts quotes. Quotes must NOT start on the 120s timer when tz is unknown (a wrong-time clock is worse than no clock).
- **Handoff fallback timer (PR2)**: If control_server never completes the handoff but a location WAS detected, `litclock-handoff-fallback.timer` writes `.handoff-complete` ~10 min after boot so the clock isn't stuck on the splash. With NO location detected it must leave the splash up.
- **Upgrade migration (PR2)**: On an already-provisioned Pi, run `update.sh` and confirm quotes keep painting — the new `litclock.service` gate is satisfied by update.sh touching `.handoff-complete` (it never runs the handoff flow). This is the brick-prevention check for authorclock.
- **Clock starts after setup**: After completing setup + handoff, confirm the e-ink display shows a literary quote within ~2 minutes (assuming tz resolved).
- **Pre-connected WiFi path**: Boot with WiFi already configured (e.g., via ethernet or wpa_supplicant). Setup runs normal-mode HTTPS server; submitting the (essentially empty) form triggers the same IP-geo resolver.
- **Post-setup PWA Settings overrides**: After first-boot, open the Control PWA → Settings → Weather and verify auto-populated values are editable. If a city is set, a "Clear" link appears next to the "Currently: …" hint. Tap Clear → city, latitude, longitude all clear together. Weather should stop rendering on the e-ink within ~1 min. Toggling `WEATHER_ENABLED` off WITHOUT tapping Clear must preserve the saved city (regression test for the toggle-only flow). **#337 supersedes this — see the #337 IA section below.**

### #337 — Location/Weather/Temperature IA (post-design-review, A9-A18)

After #337 lands, the Settings tab IA changes substantially. Replace the pre-#337 expectations with these:

- **Section order top-down**: Location → Weather → Temperature → Advanced. ("Units" renamed to "Temperature.")
- **MODE=specific preserved across reboots (CRITICAL)**: PWA → Location → tap Specific pill → type "Austin, TX" → Save. SSH into Pi, reboot. Verify `WEATHER_LOCATION_NAME` is still "Austin, Texas" (not overwritten by on-boot reresolve). `cat /home/pi/litclock/env.sh | grep WEATHER_LOCATION_MODE` returns `specific`.
- **Cold boot at "moved" location (Automatic mode)**: VPN to a different country (or physically transport the Pi to a different country WiFi). Verify on-boot `litclock-reresolve-location.service` fires within first minute of boot (`systemctl status` or `journalctl -u litclock-reresolve-location`); e-ink tz/city updates within ~5 minutes; `WEATHER_IP_COUNTRY` env value matches the new country.
- **Worldwide checkbox on US WiFi**: PWA → Location → Specific → type "SW1A 1AA" → tab out → preview shows "Couldn't find that location." Check the worldwide checkbox → preview re-fires automatically → "Buckingham Palace, London, England" appears. Save → reboot → city persists, on-boot reresolve no-ops (MODE=specific).
- **Failed Specific save = zero env trace (A15)**: PWA → Specific → type "Moon" → Save. Verify 422 + red banner + inline "Location not found." + "Moon" retained in input. Close PWA → reopen → Location section shows Automatic mode + Austin, Texas (last persisted state — no "Moon" anywhere). Optional: `cat /home/pi/litclock/env.sh` before/after diff is empty.
- **Stale preview dim (A14)**: PWA → Specific → type "Mumbai" → tab out → Currently sublabel shows "Mumbai, India" (no `is-stale` class). Type "x" (so input becomes "Mumbaix") → Currently sublabel gets dimmed (visible `is-stale` class). Tab out again → Currently re-resolves and dim clears.
- **Advanced auto-expand (A17)**: PWA → Specific → open `<details>Advanced` → type lat=28.62, lon=77.22 → Save. Reload page. Advanced section is OPEN by default (because `WEATHER_LOCATION_NAME` is empty + lat/lon populated); Currently sublabel shows the coords; Place input is empty.
- **Advanced overrides Place (A17)**: PWA → Specific → Place="Tokyo" + Advanced lat=28.62, lon=77.22 → Save. Verify env.sh holds 28.62/77.22 (Advanced won) and `WEATHER_LOCATION_NAME` is empty.
- **Pill switch to Auto clears Advanced (A17)**: PWA → Specific → type Advanced lat/lon → tap Automatic pill. Verify Advanced lat/lon inputs blank visually (form-state only, no save yet).
- **Save disabled on Specific + empty Place (A10 + A14)**: PWA → Specific → leave Place input empty + Advanced empty → Save button is grayed with tooltip "Type a place or pick Automatic." Type any character → Save enables.
- **Temperature auto-save (A13)**: PWA → Temperature → tap Celsius. Verify `cat /home/pi/litclock/env.sh | grep WEATHER_UNITS` returns `metric` within ~1s (no Save tap). Reload PWA. Celsius is still selected.
- **Country-change UNITS auto-flip (A16)**: Pi in US (WEATHER_IP_COUNTRY=US, WEATHER_UNITS=imperial). PWA → Temperature → manually pick Celsius (WEATHER_UNITS=metric). Reboot. On-boot reresolve detects same country (US still) → UNITS preserved as metric. Now VPN to UK + reboot → on-boot reresolve detects country change US→GB → WEATHER_UNITS auto-flips to metric (already was).
- **Browser-tz fallback (A18)**: Block ip-api.com (firewall rule on test Pi) → reboot → wait for first-boot to complete with empty location → open PWA. Verify Location section shows "Couldn't detect location. [Use my browser's timezone (America/Chicago)] — clock will work, weather stays off." Tap the link. Verify `timedatectl` reports America/Chicago (or whichever your browser detected); quotes resume painting at correct local time within ~1 minute.
- **Gift mode → fresh-flash recipient (A3)**: `sudo ./scripts/reset-setup.sh --gift-mode` on Pi A → SD card to Pi B (or reflash Pi A) → connect to new WiFi → verify first-boot resolver writes `WEATHER_LOCATION_MODE=auto` + `WEATHER_IP_COUNTRY` populated + UNITS matches recipient's country (regardless of what the gifter had).
- **No Clear button anywhere**: PWA → Location section (any state). Verify no "Clear location" button renders. (Per A10 — Automatic pill IS the reset.)
- **No-JS form fallback still works**: Disable JavaScript in browser (Safari → Advanced → "Show Develop menu" → Develop → Disable JavaScript). Reload Settings tab. All forms should still POST; Save buttons render for every section (including Weather and Temperature whose JS-enabled flow is auto-save). Server-side validation (A14) backstops the empty-Specific-Place case with a 422 + "Type a place or pick Automatic." inline error.

**Quick way to re-test first-boot:**

```
sudo rm -f /etc/litclock/.setup-complete /etc/litclock/.handoff-complete && sudo systemctl enable litclock-firstboot.service && sudo reboot
```

(Clear `.handoff-complete` too, or the post-WiFi handoff splash is skipped and quotes start immediately — PR2 #388.)

## Design System (Control PWA)

Always read `DESIGN.md` before making any visual or UI decision in the Control PWA workstream. (`DESIGN.md` is maintainer-local and gitignored — excluded from the public repo per the #82 PII decision, along with `PRD-`/`PLAN-LitClock-Control-PWA.md`.) All font choices, colors, spacing, motion, and aesthetic direction are defined there. Do not deviate without explicit user approval.

In code review and `/design-review` mode, flag any code that doesn't match `DESIGN.md`. The hardware-side e-ink rendering (`image-gen/quote_to_image.php`) is governed by its own constraints and is NOT subject to `DESIGN.md`.
