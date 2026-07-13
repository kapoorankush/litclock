# Control PWA — platform limits on the v1 plain-HTTP origin

Hardware-found during M6 QA, 2026-04-30. Documents what works and what doesn't,
why, and how v2's cloud-relay TLS closes the gap. This file is referenced from
`DESIGN.md` §PWA Shell Requirements and from `PLAN-LitClock-Control-PWA.md` M6
row.

## TL;DR

The Control PWA ships at `http://litclock.local` (plain HTTP on **port 80**,
private IP — #343, so a recipient never types a port). Browsers consider this
origin **not a "secure context"** per the W3C spec.

> **Note on ports (#343).** `litclock-control.service` (post-setup, always-on)
> is plain HTTP on **port 80** — no port in the URL. The first-boot
> `setup_server.py` is HTTPS on **8443** (the iOS captive trust dance needs
> TLS). They are DIFFERENT ports for different phases; always probe control_server
> with `http://` on 80 (or bare), never `https://`. Port 80 is bound by the
> non-root `pi` service account via the `ip_unprivileged_port_start=80` sysctl
> (`/etc/sysctl.d/30-litclock-unprivileged-ports.conf`), not a capability — so it
> never disturbs the unit's setuid-sudo reboot path. (Before #343 control_server
> was on 8443, which looked like HTTPS and cost ~10 min in a v0.211.2 soak
> debugging a non-existent TLS bug — the root reason this moved.)
That gates several PWA features:

| Feature | Status v1 | Why |
|---|---|---|
| iOS apple-touch-startup-image splash matrix (34 PNGs) | ✅ works | Static HTML link tags, no SW dependency |
| iOS Add-to-Home-Screen (Safari share → Add to Home Screen) | ✅ works | iOS Safari accepts the manifest + apple-mobile-web-app-capable, runs in standalone mode with our chrome |
| Android Add-to-Home-Screen menu item | ✅ works (as a Chrome shortcut, not a PWA) | Chrome creates a bookmark icon, not an installed PWA, because PWA-install criteria require `isSecureContext` |
| Self-hosted variable woff2 fonts (Fraunces / Instrument Sans / Geist Mono) | ✅ works | Static file serving + @font-face, no SW dependency |
| Manifest theme_color, background_color, icons | ✅ works (as far as the platform reads them) | Manifest is fetched and parsed; iOS Safari ignores some manifest fields and falls back to apple-touch-icon + meta tags, which we ship in parallel |
| AtHS first-run hint (variant B card with iOS-share / Android-download icons) | ✅ works | HTML/CSS/JS only |
| Caption ceiling fix (#258) — Dynamic Type tab labels | ✅ works | Pure CSS |
| **Service worker `/sw.js`** | ❌ **inert** | `!isSecureContext` → `sw-register.js` short-circuits |
| **During-reboot cached shell rendering** | ❌ inert | Requires SW |
| **Offline cache for static assets** | ❌ inert | Requires SW |
| **Install as PWA on Android (separate task in app drawer)** | ❌ inert | Chrome's PWA-install heuristic requires secure context |

## Why isSecureContext is false on a private-IP plain-HTTP origin

W3C's [Secure Contexts](https://w3c.github.io/webappsec-secure-contexts/) spec
defines a finite list of "potentially trustworthy" non-HTTPS origins:

- `localhost` (and any host that resolves to `127.0.0.0/8`)
- `[::1]`
- `file://`

`192.168.x.x` and `litclock.local` are not on the list. Browsers strictly
implement this. Both iOS Safari and Android Chrome report `isSecureContext =
false` at our origin. There's no spec-compliant way to opt in.

Workarounds that exist but don't fit a non-tech-user appliance:

- `chrome://flags/#unsafely-treat-insecure-origin-as-secure` — per-device flag,
  user opt-in, dev-only signage.
- Self-signed TLS — explicitly rejected in #257 because iOS Safari prompts for
  cert acceptance every PWA launch, suppresses the AtHS icon, and disables
  iOS Larger Text in standalone mode.

## What we ship anyway and why

The /sw.js route, manifest endpoint, sw-register.js feature-detect guard, and
the 11 SW-specific tests stay in the M6 PR. Two reasons:

1. **The wiring is correct for v2.** When the v2 cloud relay introduces TLS at
   our origin, every line of M6's SW code will activate without any further
   change. Removing it now and re-adding later doubles the work and risks
   bit-rot.

2. **The headline M6 user value lands today.** iPhone cold-launch ~250ms tinted
   splash (closes #259), self-hosted fonts, AtHS first-run hint, Dynamic Type
   tab labels — all of these work without the SW. The visible-to-users wins
   ship in v1; the during-reboot caching + offline benefits ship in v2.

## Tests pinning the inert wiring

The 11 SW tests in `tests/test_control_server_m6_pwa_shell.py` validate the
contract the SW will honor when it activates. They run against the Flask test
client which doesn't care about `isSecureContext`. Two contracts pinned in
particular:

- `/api/*` always passes through (no caching of API responses)
- Non-GET requests always pass through (no caching of destructive POSTs)

If a future refactor breaks either invariant, the tests fail even though no
real browser today executes the SW code. The pins protect the v2 day.

## v2 path

The PRD v2 cloud relay (separate effort) introduces a Cloudflare-tunnel-style
hop at `https://<device-id>.litclock.cloud` that:

- Terminates TLS at the relay edge
- Forwards to the appliance's local control_server over the LAN tunnel
- Surfaces a true HTTPS origin to the user's browser

On that origin, `isSecureContext = true`, the SW registers, the cache populates,
and Android Chrome offers a real PWA install. M6's existing SW code activates
unmodified.

## How to verify the limit on a real device

iOS Safari (Develop menu over USB):
```
console: window.isSecureContext  // → false
console: navigator.serviceWorker  // → undefined
```

Android Chrome (`chrome://serviceworker-internals/`):
- No entry for `litclock.local` or `192.168.x.x` after visiting the site

Android Chrome (`chrome://flags/#unsafely-treat-insecure-origin-as-secure`):
- Adding `http://litclock.local` to the allowlist + restarting Chrome
  flips `isSecureContext` to true. The SW then registers and the cached shell
  works. Useful for dev verification; not a v1 user path.

## Discovered

PR #289 hardware QA, 2026-04-30. Test Pi `192.168.2.132` on Android Chrome:
`chrome://serviceworker-internals/` showed no entry for our origin; reload
with WiFi off showed Chrome's dino offline page (not the cached shell);
"Add to Home Screen" menu created a shortcut, not an installed PWA.
