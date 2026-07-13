# TODOS

## Active

### Runtime enforcement of "no 204/3xx on /api/*" — follow-up to #254

**What:** Add an `after_request` hook that raises (in DEBUG) or logs+converts to 200 (in production) when any `/api/*` response is 204 or a 3xx. Currently this is enforced at lint level via `tests/test_api_contract_lint.py` (a static walk over registered view functions).

**Why:** Lint catches what's expressible at registration time, but a route that conditionally returns `Response(status=204)` based on input would slip past. Runtime enforcement closes the loophole.

**Pros:** True invariant. No way for a route author to violate the contract without a loud failure.

**Cons:** Adds a global `after_request` hook for one rule. Marginal value if the lint is already catching realistic cases. May produce false positives on streaming/chunked responses if those ever land.

**Context:** Tension 7 / T7A in #254 review banned 204/3xx on /api/*. Test #12 in the test plan ships as a static lint. Promote to runtime when more routes exist (M3-M5).

**Depends on / blocked by:** #254 shipped + 5+ /api/* routes in production.

## Completed

### Harden coordinate redaction value-group — #498 — SHIPPED

**Filed as #498; merged to master 2026-07-10 (bundles into v0.217.0) via PR #500.** Follow-up to #497. `_COORD_KEYED_RE`'s lead-in became a zero-width negative lookbehind (`(?<![A-Za-z])`), which keeps the #497 compound-key fix, also catches adjacent no-separator coords (`lat=11.1lon=22.2`), and is safer against over-redaction (a *letter* before the keyword still blocks `belong=`/`along=`/`flat=`). The value group also rounds signed + scientific-notation spellings and JSON quoted-key forms. Comma-decimals were deliberately left un-matched (the validator's `float()` rejects them → unreachable, and a comma is ambiguous with a list separator — Codex `/review` P2). All shapes were verified non-reachable in-tree; defense-in-depth for the share-safety contract. Codex `/review` caught a scientific-int-mantissa gap (`331494e-4`) and the comma-list-corruption risk, both fixed before merge. See https://github.com/kapoorankush/litclock/issues/498.

### LKG auto-revert (bootcheck.service) — follow-up to #209 — SHIPPED

**Filed as #493; merged to master 2026-07-10 (bundles into v0.217.0); hardware-QA-validated on the test Pi.** `litclock-bootcheck.service` consumes the #209 `lkg-sha` writer: per-boot "did the clock paint a frame since boot?" via the tmpfs render heartbeat (network-independent). Fail 1-2 auto-reboot to retry; fail 3 pins the last-known-good SHA and routes recovery back through `update.sh` rollback mode (full install — git + submodules + venv + units + sudoers + smoke — not a code-only `git reset`), then a terminal `bootcheck-gave-up` marker + re-flash splash bounds the sequence to ~4 reboots. Runs as `pi` on the existing `020_litclock-control` grants, so it works after #387 drops `010`. The end-to-end auto-reboot self-heal was proven unattended on real hardware (2 reboots → rollback → quotes returned). Closes the #82 brick-recovery prerequisite. See https://github.com/kapoorankush/litclock/issues/209.

### Text-fit row triage (P3 under #211) — shipped

**Completed:** 2026-04-24, shipped as PR #218 squash `fa7b68fd`.
**Result:** Failed_nofit 33 → 0. Corpus gap 33 → 0 (first complete coverage). All 33 previously-failing rows trimmed to fit 800×400 @ 18pt while preserving each timestring; mid-sentence cuts use `...` prefix/suffix per user rule. PHP warnings enhanced (idx + quote excerpt) in the same PR for future triage. Release `litclock-images-v3` published (130 MB). Two quotes hand-trimmed (Proust 19:00, Infinite Jest 22:16) since they had single long sentences with no natural split boundaries. Pre-existing timestring-parse gap at 23:25 ("eleven o'clock and twenty-five minutes") flagged in PR body as follow-up — bypassed via `--skip-validate`; PHP's literal stristr match renders the image correctly, so the gate is the only concern.
**Reference:** PR #218.

### Issue #216 — PHP `$generated` counter reports honest writes

**Completed:** 2026-04-24, shipped on branch `fix/php-generated-counter-issue-216`.
**Result:** `TurnQuoteIntoImage()` now returns one of four status strings (`written`, `failed_nostring`, `failed_nofit`, `failed_write`). Both `imagepng()` calls have their return values checked. The caller switch-dispatches the status into 4 counters. New summary block reports Written / Failed (with per-category breakdown) / Images on disk / Gap (CSV rows without image). Old code would have called a 34-attempt run "Generated new: 34" — now it honestly says "Written: 1, Failed: 33, could not fit text: 33, Gap: 33". Scope was reduced via plan-eng-review (original issue body wrongly assumed corpus_edit.py had a parser that inherited the miscount — reading the code showed capture=False, no parser; scope narrowed to PHP-only). Orphan handling on partial writes defers to the existing skip-if-both-exist self-healing check at lines 58-61 per D2 decision.
**Reference:** issue #216.

### Issue #214 — pip/gcc fix via venv-apt parity

**Completed:** 2026-04-23, shipped on branch `fix/venv-apt-parity-issue-214`.
**Result:** Fixed the real root cause — `scripts/update.sh` and `scripts/install.sh` diverged from `pi-gen/stage3/01-setup-app/00-run.sh`'s `--system-site-packages + grep -v` pattern, causing pip-compile failures on Pi hardware. Introduced `requirements-apt.txt` as single source of truth for apt-provisioned pip names. All three scripts now build the filter regex from that file. Dropped `RPi.GPIO==0.7.1` from `requirements.txt` (proven unused at runtime via import-blocker probe on clean Pi — driver chain binds to gpiozero's lgpio pin factory only). Added piwheels `extra-index-url` to pi-gen's `/etc/pip.conf`. 5 new tests (2 in test_update_sh.py, new test_install_sh.py, new test_apt_provisioned_drift.py with 6 tests including drift guard + RPi.GPIO reintroduction guard). Fresh-image QA verified 2026-04-24 on clean litclock Pi built from `24866727267`.
**Reference:** issue #214.

### Verify Phase 2c end-to-end on real Pi hardware when first v2 quote-release happens

**Completed:** 2026-04-22, verified on test Pi `litclock` (192.168.2.67).
**Result:** Full flow ran end-to-end. Pi fast-forwarded 86e4932 → 338eb1a3, `.images-version` v1 → v2, `download_images.sh` fetched the v2 tarball + SHA256, atomic swap landed correctly. Post-state: 2110 bucket 7→6, 2210 bucket 3→4, `quote_2110_6.png` removed, `quote_2210_3.png` present with sha256 `315ec525ca85d2fa54a82b9f3a309507cbc03663fa6d85b949d460eb61cb9743` matching the dev-box original byte-for-byte. Clock timer active, per-minute renders clean.
**Reference:** issue #82, PR #213.

## From /plan-eng-review 2026-06-11 (v0.214.3 narrowed-scope)

- **Pi Zero 2W latency-simulator fixture** — Filed as **#444** (2026-06-13). See https://github.com/kapoorankush/litclock/issues/444 for the full spec.

- **Lazy-tail degraded-case observation** (P3 follow-up, observation only) — **RESOLVED in v0.216.0 (#436/#475).** The multi-failure "each service gets a serial ≤8s journalctl call → blows the client 10s budget" case is gone: `/diagnostics` now hydrates each unit's tail independently via `GET /api/diagnostics/journal?unit=`, so one slow/failed unit can't abort another's. Kept here for history; no action left. Original Codex OV-7 finding (2026-06-11).

## From /plan-eng-review 2026-06-12 (v0.214.4 — #432 grey tier + #431 paper cuts)

### Persistent uncollected-marker file (v0.215 follow-up to #432) — SHIPPED

**Filed as #445 (2026-06-13); implemented 2026-06-15.** Persistent JSON marker at `/var/lib/litclock/.last-collected-marker.json` replaces the reboot-wiped tmpfs check in `_compute_uncollected`, killing the ~5-10s post-reboot grey flicker. Cross-language writers (`scripts/litclock-mark-collected.sh` for the root NM dispatcher; `src/collected_marker.py` for the pi IP-geo resolvers) share one file + lock + format; read side falls back to the legacy tmpfs check when the marker is absent. No sudoers change (`/var/lib/litclock` is already `0755 pi pi`). Merged to master under `[Unreleased]`; bundles into the next tag. See https://github.com/kapoorankush/litclock/issues/445.

## From /plan-eng-review 2026-07-03 (#436 — journal tails off the SSR critical path)

> NOTE: the "Lazy-tail degraded-case observation" TODO above (multi-failure blows the 10s budget)
> is RESOLVED by the #436 plan's per-unit hydration (each unit's tail fetched independently, so one
> slow/failed unit can't abort another's). Close it when #436 lands.

### Distinguish empty logs from failed journalctl (collector contract) — P3 follow-up to #436

- **What:** `_read_journal_tail` (`_collectors.py:543`) returns `[]` for BOTH "no recent logs" and
  "journalctl failed/timed out" (it routes through `cached_subprocess_or_empty` then `if not raw:
  return []`). Change it to return a failure sentinel distinct from empty, and thread that through the
  new `/api/diagnostics/journal` endpoint response + JSON schema + redaction, so the client can show
  "couldn't load logs" for a SERVER-side journalctl failure too.
- **Why:** #436 ships an error affordance that only fires on CLIENT-side fetch failure (abort/timeout
  of the per-unit request). A server-side journalctl timeout still arrives as an empty tail,
  indistinguishable from a genuinely quiet unit. This closes that honesty gap.
- **Pros:** The "couldn't load logs" state becomes fully honest; helps the exact debug moment.
- **Cons:** Changes the collector contract + several call sites + tests. The `[]`-means-empty
  assumption is load-bearing in a few readers — audit before changing.
- **Context:** Codex outside-voice tension T3 in the #436 /plan-eng-review (2026-07-03). Accepted the
  limit for the #436 PR; this is the fuller fix. The new per-unit endpoint from #436 is the natural
  place to surface the sentinel (HTTP status or an `ok:false` field).
- **Depends on / blocked by:** ~~#436 (the per-unit endpoint) landing first.~~ **UNBLOCKED** — #436/#475 shipped in v0.216.0 (2026-07-07); the `GET /api/diagnostics/journal?unit=` endpoint now exists as the natural place to surface the failure sentinel. Available to pick up (still P3).
