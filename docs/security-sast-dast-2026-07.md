# LitClock — SAST + DAST assessment (2026-07-04)

Whole-codebase static analysis + dynamic probing of the control server. Scope: `src/`,
`image-gen/`, `scripts/`, `tests/js`. Context: single-board appliance (Pi Zero 2W); the
control PWA is **unauthenticated on the LAN by design** (locked security posture — LAN is
the trust boundary; auth+HTTPS is the tripwire before any remote exposure).

## Tools
- **SAST:** `ruff --select S` (flake8-bandit), `bandit 1.9.4`, `pip-audit 2.10.1`, `npm audit`.
- **DAST:** booted `control_server` via `create_app()` on `127.0.0.1:8555` (waitress), probed over HTTP with curl.

## Headline
**No exploitable vulnerabilities found.** Dependencies are CVE-clean (Python + JS). The one
actionable item is a set of missing HTTP security-hardening headers (pre-existing, defense-in-depth).

---

## SAST

| Scanner | Result |
|---|---|
| pip-audit (Python deps) | **0 known vulnerabilities** |
| npm audit (JS deps) | **0 vulnerabilities** |
| bandit MEDIUM+ | 0 real app findings (see triage) |
| ruff `S` ruleset | 52 warnings, all expected-by-design (triaged below) |

**Triage of the notable classes:**
- **S603/S607 (33) — subprocess** — fixed list-form argv to `sudo`/`systemctl`/`nft`/`journalctl`.
  No shell, no string interpolation. The one user-influenced arg (diagnostics `unit`) is
  allowlist-validated against `DIAG_UNITS` before any fork (confirmed statically + via DAST).
  Partial-path (`systemctl` by name) runs under systemd with a controlled PATH. **Not injectable.**
- **B310 / S310 (urlopen, 5) — SSRF?** — **No.** All calls use fixed https hosts
  (`nominatim.openstreetmap.org`, `ip-api.com`, GitHub API). The user place-name is
  `urllib.parse.quote`-encoded into the query string, so the host cannot be redirected.
- **B324 SHA1 (4, `image-gen/corpus_edit.py`)** — content-fingerprint for quote dedup, **not
  security**. Collision-resistance is irrelevant here. Cosmetic: pass `usedforsecurity=False`.
- **S311 non-crypto random (2)** — quote selection (`randrange`) — benign. The one
  security-sensitive RNG (WiFi hotspot password, `wifi_provision._generate_password`) correctly
  uses **`secrets.choice`**; CSRF + confirm tokens also use `secrets`.
- **S105 hardcoded-password (2)** — false positives (`REDACTED_TOKEN` constant, build-tool var).
- **S104 bind-all (1)** — `0.0.0.0` in `app.py` — intentional (LAN PWA + captive portal).
- **S110 try-except-pass (2)** — swallowed exceptions in hotspot-restore cleanup paths. Low.

---

## DAST (control_server over HTTP)

**New `/api/diagnostics/journal?unit=` endpoint (from #436) — all pass:**
| Probe | Result |
|---|---|
| valid allowlisted unit | 200 ✓ |
| disallowed unit (`sshd.service`) | 400 `invalid_unit` ✓ |
| path traversal (`../../etc/passwd`, encoded) | 400 ✓ |
| shell metachars (`litclock.service;id`) | 400 ✓ |
| null byte / newline / uppercase | 400, no 500, no traceback ✓ |
| duplicate `unit` param | first (allowed) wins, no smuggling ✓ |
| POST (method tampering) | 405 ✓ |
| 8KB param | 400, no crash ✓ |
| attacker `<script>` unit | static error, **not reflected** (no XSS) ✓ |

**Broader surface:**
- **CSRF:** state-changing `POST /api/settings` without token/origin → **403** ✓ (`csrf.py` reflexive-host guard active).
- **Redaction:** `/api/diagnostics` without `?reveal` returns sensitive fields redacted (`gateway: '•••••••'`), `revealed_groups: []` ✓.
- **Cache-Control:** secret-bearing responses (`/diagnostics`, `/api/diagnostics/journal`) set `no-store, no-cache, must-revalidate` ✓.
- **Static path traversal:** `/static/../app.py` (+ encoded) → 404 ✓.
- **Log endpoint:** `/api/logs?level=INVALID'` → 400 (strict allowlist) ✓. SSE capped at 6 concurrent streams (`SSE_MAX_CONCURRENT_STREAMS`).
- **Error handling:** no traceback/stack leak on any malformed input probed.

---

## Findings

### F1 — Missing HTTP security-hardening headers (LOW–MEDIUM, pre-existing, defense-in-depth)
Responses carry no `X-Content-Type-Options: nosniff`, no `X-Frame-Options` / CSP `frame-ancestors`,
no `Referrer-Policy`, no `Permissions-Policy`. On a trusted-LAN unauthenticated PWA the risk is
bounded, but two are cheap and worth adding:
- **`X-Content-Type-Options: nosniff`** — universal, prevents MIME-sniffing.
- **`X-Frame-Options: DENY`** (or CSP `frame-ancestors 'none'`) — a malicious page on the same LAN
  could iframe the PWA and clickjack the owner into a state-changing control (Reset WiFi / power off),
  since the PWA is unauthenticated. DENY closes it.

CSP (script/style) is a larger effort (the PWA may use inline styles) — defer.
Fix location: `create_app()` / an `after_request` hook in `control_server`.

### F2 — `Server: waitress` banner (INFORMATIONAL)
Minor WSGI-server disclosure (no version). Optional to suppress.

### Accepted (already tracked / by design)
- No in-flight coalescing on the journal endpoint (LAN client can force cold `journalctl` forks) —
  filed TODO; consistent with the trusted-LAN posture (`/api/diagnostics` had the same surface pre-#436).
- SSID not scrubbed from journal tails — by design; a same-LAN client already knows the SSID.
- Server-side journalctl failure renders as "no recent log lines" (T3 accepted limit) — filed TODO.

## Recommendation
Add F1's `nosniff` + `X-Frame-Options: DENY` (a few lines in an `after_request`). Everything else is
clean. Re-run this pass if/when the remote-exposure tripwire is approached (auth + HTTPS + CSP become
required at that point).
