# Captive-portal setup states

Every state the WiFi setup flow can be in, from the owner's point of view:
what puts it there, what they read, what they can do, what times out, and
where it goes next. Written for litclock-dev#908; keep it current when a state
changes.

Copy is named by its catalog key in `languages/en/strings.json`, with today's
English beside it. Code is named by function, not line number. "Page" is the
setup page served by `src/setup_server.py`; "panel" is the e-ink display.

The single fact that shapes everything below: **the Pi has one radio.** It
can host the `LitClock-Setup` hotspot or join the owner's WiFi, never both.
The moment a join starts, the phone loses the page.

## The happy path at a glance

```
scan ──► pick network + password ──► Complete Setup ──► "Joining your WiFi"
                                                            │ (hotspot drops ~1s later)
                                                            ▼
                                           join ──► IP-geo ──► handoff splash
                                                                 (panel QR → PWA)
```

## 1. Scanning

| | |
|---|---|
| **Trigger** | First load of `/` or `/setup` with no scan in the last 30s (`_WIFI_SCAN_TTL`); the JS Refresh; or the no-JS Refresh (`/setup?rescan=1`). |
| **Copy** | Server render: none — the browser's own loading indicator while the page builds. JS Refresh: the picker reads `setup.wifi.scanning` "Scanning...". |
| **Action** | JS: the picker stays live during the scan (never `disabled` — a disabled select drops out of the POST). No-JS: wait for the page. |
| **Timeout** | Server: rescan ≤10s (`SCAN_RESCAN_TIMEOUT`) + 2s settle + list ≤15s (`SCAN_LIST_TIMEOUT`) = 27s worst case, under `_SCAN_CACHE_LOCK`. JS: the fetch aborts at `SCAN_TIMEOUT_MS` = 30s, deliberately above the server's 27s. |
| **Next** | The network list (`setup.wifi.placeholder` "Choose your WiFi network"), **2**, or **3**. |

Concurrent scans share one radio and one lock. A Refresh, JS or not, that
waited on the lock behind another scan takes that scan's result (any scan that
*finished* after the Refresh was tapped, empty included) rather than starting
its own. A queued Refresh therefore still lands inside the 30s abort. A plain
page load uses the 30s cache instead.

## 2. No networks found

| | |
|---|---|
| **Trigger** | The scan returns nothing once the clock's own hotspot is filtered out. |
| **Copy** | `setup.wifi.placeholder_empty` "No networks found - tap Refresh". |
| **Action** | Refresh; or type a network name — the page pre-selects `setup.wifi.manual_option` "My network isn't listed" and opens the name field, since it is the only way forward. |
| **Timeout** | None. Empty results are never cached, so the next load or Refresh scans again. |
| **Next** | **1** (Refresh) or **4**. |

## 3. Scan failed

| | |
|---|---|
| **Trigger** | The scan's list step fails: nmcli exits non-zero or hits `SCAN_LIST_TIMEOUT`, and `scan_wifi_networks()` raises `WifiScanError`. The page shows this state on load; `/scan-wifi` answers 503. JS also lands here when the fetch rejects, times out (30s), or returns a non-OK status. A refused *rescan* is not a failure: the list still reports what the radio last saw. |
| **Copy** | `setup.wifi.scan_failed` "Scan failed - tap Refresh". |
| **Action** | Refresh. Unlike state 2, the name field is NOT opened and the manual option NOT pre-selected: the owner's network is almost certainly in range, so Refresh is the next step. A manual selection made before a JS Refresh is kept. |
| **Timeout** | — |
| **Next** | **1**. |

A failed scan changes no scan state: nothing is cached, earlier evidence is
kept, and a Refresh queued behind it tries the radio again rather than
inheriting the failure.

## 4. Hidden network

| | |
|---|---|
| **Trigger** | The owner picks `setup.wifi.manual_option` "My network isn't listed". |
| **Copy** | `setup.wifi.manual_help`: "Hidden networks never show up in a scan. Pick "{summary}" above, then type its name exactly — capitals and inner spaces count." Field placeholder `setup.wifi.manual_placeholder` "Network name". |
| **Action** | Type the name, then the password, then Complete Setup. |
| **Timeout** | — |
| **Next** | Submission. A rejected name returns an error page (below); a join failure returns to the page with the typed name echoed and the manual option pre-selected. |

- **With JS on,** the name field shows only while the manual option is
  selected. Picking a real network hides it and blanks the typed name, so the
  two can never both be filled (litclock-dev#848).
- **With JS off,** the field sits inside a `<details>` that is always present
  but closed on a clean load. It opens on a retry echo or an empty scan.
  The server rule, *a picked network wins over typed text*, is the guard.
- **Rejected before any join** (error page, "Try again" back to the form):
  - `setup.error.type_or_pick` for an empty name;
  - `setup.error.bad_ssid_chars` for control characters;
  - `setup.error.ssid_too_long` for more than 32 bytes;
  - `setup.error.own_network` for the clock's own hotspot name, compared case-insensitively.
- **`hidden yes`** is written into the saved profile only when the name never
  appeared in any scan this session. Otherwise the clock would broadcast the
  home network's name in probe requests for the life of the install.

## 5–7. Join failures

All three come back the same way: the join fails, `_restore_hotspot()` brings
the setup network back (three attempts, 2s apart) and repaints the panel's
retry QR. The phone has to rejoin `LitClock-Setup`, which state 9 covers. The
page then shows a red banner:

`setup.banner.error_lead` "Couldn't join your WiFi: {error}" + advice + `setup.banner.error_tail` "If this page doesn't reload, rescan the QR code on the display — the clock's own network has restarted."

| State | Trigger (nmcli) | `{error}` | Advice | Panel |
|---|---|---|---|---|
| **5. Wrong password** | "Secrets were required" / "password" → `bad_password` | `setup.cause.bad_password` "Incorrect WiFi password" | `setup.banner.advice_password` "Double-check your WiFi password and try again — not the password shown on the clock's screen." | `firstboot.hotspot.title_retry` + `retry_password_*` steps |
| **6. Network vanished** | "No network with SSID" → `not_found` | `setup.cause.not_found` "Network '{ssid}' not found" (typed names: `setup.cause.not_found_hidden`) | `setup.banner.advice_neutral` "Check the message above, then try again." | `retry_connect_*` steps |
| **7. Timeout** | no answer in 30s (`connect_timeout`) plus a 15s rescue poll → `timeout` | `setup.cause.timeout` "Couldn't reach '{ssid}' — the network didn't answer in time. …" | neutral | `retry_connect_*` steps |

Neighbours of 7:
- `setup.cause.no_ip` "Connected but could not obtain IP address" appears when the join succeeds but no address arrives within 15s.
- `setup.cause.connect_failed` "Connection failed: {detail}" covers anything else. `{detail}` is nmcli's own text and is not translated.

**Action:** fix the field the banner names and resubmit. The typed password
is never echoed back. A typed network name is (state 4).

**Next:** submission again.

## 8. Duplicate submission

| | |
|---|---|
| **Trigger** | A second Complete Setup while a join is already running: a double tap, a second phone, or a resubmit with a corrected password in the ~1s before the hotspot drops. |
| **Copy** | The second submitter gets the same "Joining your WiFi" page as the first. |
| **Action** | — |
| **Next** | The first submission's outcome. **The second submission's credentials are discarded.** `do_POST` sends the success page *before* the in-flight check, so only the first join runs. |

A real double tap sends identical credentials, so it is harmless. A *different*
submission inside that window is silently lost while the page says it was
saved. See *Open gaps*.

A plain page load during a join shows `setup.banner.connecting` "Connecting to
WiFi... This page will refresh automatically.", subtitle
`setup.subtitle.joining` "Joining your WiFi — hang tight.", and reloads every
10s.

## 9. The phone loses the page

| | |
|---|---|
| **Trigger** | Every submission. ~1s after the "Joining your WiFi" page, the join takes the radio and the hotspot disappears. |
| **Copy** | Page: whatever the browser shows for an unreachable host. Panel: `firstboot.splash.connecting.title` "Connecting to {ssid}…" / `.message` "This can take up to a minute." |
| **Action** | Wait. On a failed join, rejoin `LitClock-Setup` by rescanning the panel's QR. That is the panel's own retry step and the only guidance the owner can still see. |
| **Timeout** | The hotspot can be down for ~1s + 30s join + 15s rescue poll + 10s disconnect + restore. Hotspot activation itself is unbounded. |
| **Next** | Join success → **10**; join failure → hotspot restored → **5–7**. |

If all three restore attempts fail, `_restore_hotspot()` logs and returns. The
panel keeps the connecting splash until first-boot's 30-minute setup deadline
paints "Setup Incomplete" and powers off. See *Open gaps*.

## 10. Success

| | |
|---|---|
| **Trigger** | The join succeeds. |
| **Copy** | Page (sent before the join): `setup.success.heading` "Joining your WiFi", `setup.success.subtitle` "Your clock is connecting to {b}{ssid}{/b}. Your phone will lose this page in a moment; that’s expected.", `setup.success.footer` "Watch the clock’s screen. Within a couple of minutes it will show a QR code. Switch your phone back to {ssid}, then scan the QR code to finish setup. If you miss scanning it and the clock is showing quotes now, don’t worry. If the screen says it couldn’t join, scan its QR code to try again. If the screen hasn’t changed after a few minutes, unplug the clock and plug it back in."; refreshes to `/setup` after 15s. Panel: `firstboot.splash.setup_complete.*` "Setup Complete!" / "Starting your clock..." for 3s, then the handoff splash: `firstboot.handoff.heading_ready` "Ready to read." with a QR to the PWA. |
| **Action** | Return the phone to the home WiFi and scan the panel QR. |
| **Timeout** | IP-geo: 4 attempts on a 1/3/9s backoff. Handoff completes on Done, any PWA save, or after 120s. |
| **Next** | The clock (quotes start), or **11**. |

The setup server tears the hotspot down, resolves location, signals first-boot
(`signal_completion()`) and terminates itself, so on success the 15s refresh
reaches nothing. That is why the page sends the owner to the clock's screen
rather than promising a status check. The refresh stays for the failure path:
a wrong password fails in seconds, the hotspot returns, and the refresh lands
on the red banner. Note that iOS closes the captive sheet as soon as the phone
leaves the hotspot, so the page may be visible for only a second or two. The
panel carries the instructions either way.

## 11. Setup complete, timezone unresolved

| | |
|---|---|
| **Trigger** | IP-geo fails (no lat/lon written). |
| **Copy** | Panel handoff splash: `firstboot.handoff.heading_almost` "Almost ready.", location `firstboot.handoff.not_detected` "Not detected", `cta_almost` "Scan the QR to set your timezone.", `bottom_almost` "Quotes start once your timezone is set." PWA banner: `handoff.fail.title` "Almost there", `handoff.fail.body`; the button `handoff.fail.set_tz` becomes `handoff.fail.tz_button` "Use {tz}" from the browser's timezone; link `handoff.fail.pick_other` "Pick a different timezone". |
| **Action** | Accept the browser's timezone (`/api/handoff/set-timezone`) or pick one in Settings. |
| **Timeout** | None, deliberately. The 120s auto-complete does not fire while the timezone is unknown, because a clock showing the wrong time is worse than no clock. The 10-minute fallback timer also waits for a location. |
| **Next** | Quotes start within ~1 minute of the timezone being set. |

## Open gaps

Found while writing this spec. The four still open were reviewed one by one
and **left open by owner decision on 2026-10-16**, each because it is rare,
already covered by copy, or would cost more than it saves. Reopen one only
with new evidence that it bites.

- **Fixed in litclock-dev#908: no rescan without JavaScript.** Refresh is now a real
  submit for a separate, field-less GET form, so `/setup?rescan=1` rescans
  server-side. The password can never ride the URL. With JS on, the in-page
  scan cancels the navigation.
- **Fixed in litclock-dev#908: the page gave up on slow scans.** `SCAN_TIMEOUT_MS` was
  20s against the server's 27s worst case, so a scan about to succeed read as
  "Scan failed".
- **Fixed: a failed scan looked like "no networks"** (state 3). The scan
  returned `[]` on an nmcli failure, so the owner was told there was nothing
  to join, with the name field opened, when the radio had not answered.
- **Open: a second submission during a join is lost, and its page tells that
  owner to switch back to *their* network** (state 8). The page is built from
  the second submission's network name, which the clock is not joining. The
  window is ~1s, and a double tap sends identical credentials, so it names the
  right network. *Left open: only a second phone or a sub-second resubmit is
  affected.*
- **Fixed: "Settings Saved!" overpromised** (state 10). It claimed success,
  with a check mark, before the join had started, and promised a status check
  against a server that exits on success. The page now reads "Joining your
  WiFi", warns that the phone will lose it, and points at the clock's screen,
  which shows the right code for either outcome.
- **Open: `setup.banner.connecting` promises an automatic refresh** while the
  phone is almost always off the hotspot. *Left open: the banner is visible
  only for about a second, and it does reload.*
- **Open: a hotspot that will not come back strands the owner** (state 9).
  After three failed restores the panel keeps the connecting splash for up to
  30 minutes. The post-submit page now covers it in copy ("If the screen hasn't
  changed after a few minutes, unplug the clock and plug it back in"): setup
  never completed, so first boot runs it again. The panel itself still says
  nothing. *Left open: rare, and the phone page's sentence covers it. The
  existing "Setup Failed / Unplug power for 10 seconds" splash is the ready
  fix if it ever bites.*
- **Open: a vanished network can still be listed on the retry page** (state 6).
  The dropdown comes from a cache up to 30s old. *Left open: getting back to
  the page nearly always takes longer than 30s, the banner names the missing
  network, and Refresh is one tap. Forcing a rescan would slow down the
  owners who come back fastest.*
