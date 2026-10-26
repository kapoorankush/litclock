# TODOS

## Active

### Runtime enforcement of "no 204/3xx on /api/*" — follow-up to litclock-dev#254

**What:** Add an `after_request` hook that raises (in DEBUG) or logs+converts to 200 (in production) when any `/api/*` response is 204 or a 3xx. Currently this is enforced at lint level via `tests/test_api_contract_lint.py` (a static walk over registered view functions).

**Why:** Lint catches what's expressible at registration time, but a route that conditionally returns `Response(status=204)` based on input would slip past. Runtime enforcement closes the loophole.

**Pros:** True invariant. No way for a route author to violate the contract without a loud failure.

**Cons:** Adds a global `after_request` hook for one rule. Marginal value if the lint is already catching realistic cases. May produce false positives on streaming/chunked responses if those ever land.

**Context:** Tension 7 / T7A in litclock-dev#254 review banned 204/3xx on /api/*. Test #12 in the test plan ships as a static lint. Promote to runtime when more routes exist (M3-M5).

**Depends on / blocked by:** litclock-dev#254 shipped + 5+ /api/* routes in production.

### Literal corpus format: text between pipes is the text — owner decision 2026-10-13, deferred

**What:** Read every quote corpus (`image-gen/litclock_annotated.csv`, `languages/<code>/quotes.csv`) as a LITERAL format: whatever sits between two `|` is displayed exactly as typed, with no CSV quoting and no `""` escapes. Requires one-time decode/rewrite of the English corpus (no row contains a `|`, measured 2026-10-13), and updating every reader and writer: `src/quote_corpus.py`, `src/quote_renderer.py`, `tools/validate_measurement.py`, `image-gen/quote_to_image.php` (`fgetcsv`), `image-gen/corpus_edit.py` (writer + content hashes/manifest). Replace render-invariants L6 (strict-CSV) with a literal-format check: exactly 6 fields per line, plus a warning for a field that looks CSV-quoted (`"""` or doubled `""`), which is what a spreadsheet export produces.

**Why:** The owner prefers that volunteers and the maintainer never need to know CSV escaping. Under CSV a leading `"` is syntax, so `"Come at seven," she said.` silently loses its opening mark on the panel. PR litclock-dev#928 (2026-10-13) hand-fixed 41 rows of exactly this class.

**Cons:** Spreadsheet exports (Excel/Sheets) add CSV quoting, so that workflow then needs the warning above. `quote_renderer.py` and `tools/validate_measurement.py` are both runtime-render proof inputs, so the change costs every device one ~3 min re-validation (one revoke per update however many inputs change).

**More readers and writers than the list above** (2026-10-21 /review of litclock-dev#930): `image-gen/merge_quotes.py` WRITES the corpus with `csv.writer` and would silently re-add CSV quoting after the migration; readers also include `image-gen/generate_images.py`, `audit_quotes.py`, `apply_audit.py`, `process_reviewed.py`, `build_gold_set.py`, `gather_quotes.py`, `tools/render_invariants.py`, `tools/language_corpora.py`, and test fixtures built with `csv.writer` (`test_apply_audit.py`, `test_build_gold_set.py`). `languages/<code>/quotes.csv` is a convention for future volunteer corpora; none exists in the tree yet.

**Context:** Decoded text should be byte-identical, so the PNGs themselves should not change; verify with the manifest content hashes. **But the pipeline will still treat it as a full corpus change** (2026-10-21 /review of litclock-dev#930): editing `quote_to_image.php` changes `generator_hash`, so `corpus_edit.py`'s `_generator_out_of_sync()` forces a full 4,803-image regeneration; rewriting the CSV-quoted rows changes `corpus_hash`; and CLAUDE.md requires `corpus_edit.py ship`, which bumps `.images-version` and cuts a new images release every device downloads (~124MB) unless that is deliberately skipped. **Bundle it with a release that already touches a proof input**, so the fleet re-validates once rather than twice. The first such window is litclock-dev#947 (Pillow 13 / certifi, `requirements.txt`, queued for the next public port, 2026-10-21). Using it means holding a security port for an unstarted feature that needs full bench QA; not decided.

**Depends on / blocked by:** nothing; the owner said "later some time".

## Completed

### Harden coordinate redaction value-group — litclock-dev#498 — SHIPPED

**Filed as litclock-dev#498; merged to master 2026-07-10 (bundles into v0.217.0) via PR litclock-dev#500.** Follow-up to litclock-dev#497. `_COORD_KEYED_RE`'s lead-in became a zero-width negative lookbehind (`(?<![A-Za-z])`), which keeps the litclock-dev#497 compound-key fix, also catches adjacent no-separator coords (`lat=11.1lon=22.2`), and is safer against over-redaction (a *letter* before the keyword still blocks `belong=`/`along=`/`flat=`). The value group also rounds signed + scientific-notation spellings and JSON quoted-key forms. Comma-decimals were deliberately left un-matched (the validator's `float()` rejects them → unreachable, and a comma is ambiguous with a list separator — Codex `/review` P2). All shapes were verified non-reachable in-tree; defense-in-depth for the share-safety contract. Codex `/review` caught a scientific-int-mantissa gap (`331494e-4`) and the comma-list-corruption risk, both fixed before merge. See https://github.com/kapoorankush/litclock/issues/498.

### LKG auto-revert (bootcheck.service) — follow-up to litclock-dev#209 — SHIPPED

**Filed as litclock-dev#493; merged to master 2026-07-10 (bundles into v0.217.0); hardware-QA-validated on the test Pi.** `litclock-bootcheck.service` consumes the litclock-dev#209 `lkg-sha` writer: per-boot "did the clock paint a frame since boot?" via the tmpfs render heartbeat (network-independent). Fail 1-2 auto-reboot to retry; fail 3 pins the last-known-good SHA and routes recovery back through `update.sh` rollback mode (full install — git + submodules + venv + units + sudoers + smoke — not a code-only `git reset`), then a terminal `bootcheck-gave-up` marker + re-flash splash bounds the sequence to ~4 reboots. Runs as `pi` on the existing `020_litclock-control` grants, so it works after litclock-dev#387 drops `010`. (Written when litclock-dev#387 planned that drop; it was **reversed 2026-07-12** and `010` is kept. Note the original claim was always narrower than it reads: bootcheck's own `sudo mkdir -p "$STATE_DIR"` is not in `020` and degrades via a fallback, and the `update.sh` rollback it routes into copies unit files on the blanket grant.) The end-to-end auto-reboot self-heal was proven unattended on real hardware (2 reboots → rollback → quotes returned). Closes the litclock-dev#82 brick-recovery prerequisite. See https://github.com/kapoorankush/litclock/issues/209.

### Text-fit row triage (P3 under litclock-dev#211) — shipped

**Completed:** 2026-04-24, shipped as PR litclock-dev#218 squash `fa7b68fd`.
**Result:** Failed_nofit 33 → 0. Corpus gap 33 → 0 (first complete coverage). All 33 previously-failing rows trimmed to fit 800×400 @ 18pt while preserving each timestring; mid-sentence cuts use `...` prefix/suffix per user rule. PHP warnings enhanced (idx + quote excerpt) in the same PR for future triage. Release `litclock-images-v3` published (130 MB). Two quotes hand-trimmed (Proust 19:00, Infinite Jest 22:16) since they had single long sentences with no natural split boundaries. Pre-existing timestring-parse gap at 23:25 ("eleven o'clock and twenty-five minutes") flagged in PR body as follow-up — bypassed via `--skip-validate`; PHP's literal stristr match renders the image correctly, so the gate is the only concern.
**Reference:** PR litclock-dev#218.

### Issue litclock-dev#216 — PHP `$generated` counter reports honest writes

**Completed:** 2026-04-24, shipped on branch `fix/php-generated-counter-issue-216`.
**Result:** `TurnQuoteIntoImage()` now returns one of four status strings (`written`, `failed_nostring`, `failed_nofit`, `failed_write`). Both `imagepng()` calls have their return values checked. The caller switch-dispatches the status into 4 counters. New summary block reports Written / Failed (with per-category breakdown) / Images on disk / Gap (CSV rows without image). Old code would have called a 34-attempt run "Generated new: 34" — now it honestly says "Written: 1, Failed: 33, could not fit text: 33, Gap: 33". Scope was reduced via plan-eng-review (original issue body wrongly assumed corpus_edit.py had a parser that inherited the miscount — reading the code showed capture=False, no parser; scope narrowed to PHP-only). Orphan handling on partial writes defers to the existing skip-if-both-exist self-healing check at lines 58-61 per D2 decision.
**Reference:** issue litclock-dev#216.

### Issue litclock-dev#214 — pip/gcc fix via venv-apt parity

**Completed:** 2026-04-23, shipped on branch `fix/venv-apt-parity-issue-214`.
**Result:** Fixed the real root cause — `scripts/update.sh` and `scripts/install.sh` diverged from `pi-gen/stage3/01-setup-app/00-run.sh`'s `--system-site-packages + grep -v` pattern, causing pip-compile failures on Pi hardware. Introduced `requirements-apt.txt` as single source of truth for apt-provisioned pip names. All three scripts now build the filter regex from that file. Dropped `RPi.GPIO==0.7.1` from `requirements.txt` (proven unused at runtime via import-blocker probe on clean Pi — driver chain binds to gpiozero's lgpio pin factory only). Added piwheels `extra-index-url` to pi-gen's `/etc/pip.conf`. 5 new tests (2 in test_update_sh.py, new test_install_sh.py, new test_apt_provisioned_drift.py with 6 tests including drift guard + RPi.GPIO reintroduction guard). Fresh-image QA verified 2026-04-24 on clean litclock Pi built from `24866727267`.
**Reference:** issue litclock-dev#214.

### Verify Phase 2c end-to-end on real Pi hardware when first v2 quote-release happens

**Completed:** 2026-04-22, verified on test Pi `litclock` (192.168.2.67).
**Result:** Full flow ran end-to-end. Pi fast-forwarded 86e4932 → 338eb1a3, `.images-version` v1 → v2, `download_images.sh` fetched the v2 tarball + SHA256, atomic swap landed correctly. Post-state: 2110 bucket 7→6, 2210 bucket 3→4, `quote_2110_6.png` removed, `quote_2210_3.png` present with sha256 `315ec525ca85d2fa54a82b9f3a309507cbc03663fa6d85b949d460eb61cb9743` matching the dev-box original byte-for-byte. Clock timer active, per-minute renders clean.
**Reference:** issue litclock-dev#82, PR litclock-dev#213.

## From /plan-eng-review 2026-06-11 (v0.214.3 narrowed-scope)

- **Pi Zero 2W latency-simulator fixture** — Filed as **litclock-dev#444** (2026-06-13). See https://github.com/kapoorankush/litclock/issues/444 for the full spec.

- **Lazy-tail degraded-case observation** (P3 follow-up, observation only) — **RESOLVED in v0.216.0 (litclock-dev#436/litclock-dev#475).** The multi-failure "each service gets a serial ≤8s journalctl call → blows the client 10s budget" case is gone: `/diagnostics` now hydrates each unit's tail independently via `GET /api/diagnostics/journal?unit=`, so one slow/failed unit can't abort another's. Kept here for history; no action left. Original Codex OV-7 finding (2026-06-11).

## From /plan-eng-review 2026-06-12 (v0.214.4 — litclock-dev#432 grey tier + litclock-dev#431 paper cuts)

### Persistent uncollected-marker file (v0.215 follow-up to litclock-dev#432) — SHIPPED

**Filed as litclock-dev#445 (2026-06-13); implemented 2026-06-15.** Persistent JSON marker at `/var/lib/litclock/.last-collected-marker.json` replaces the reboot-wiped tmpfs check in `_compute_uncollected`, killing the ~5-10s post-reboot grey flicker. Cross-language writers (`scripts/litclock-mark-collected.sh` for the root NM dispatcher; `src/collected_marker.py` for the pi IP-geo resolvers) share one file + lock + format; read side falls back to the legacy tmpfs check when the marker is absent. No sudoers change (`/var/lib/litclock` is already `0755 pi pi`). Merged to master under `[Unreleased]`; bundles into the next tag. See https://github.com/kapoorankush/litclock/issues/445.

## From /plan-eng-review 2026-07-03 (litclock-dev#436 — journal tails off the SSR critical path)

> NOTE: the "Lazy-tail degraded-case observation" TODO above (multi-failure blows the 10s budget)
> is RESOLVED by the litclock-dev#436 plan's per-unit hydration (each unit's tail fetched independently, so one
> slow/failed unit can't abort another's). Close it when litclock-dev#436 lands.

### Distinguish empty logs from failed journalctl (collector contract) — P3 follow-up to litclock-dev#436

- **What:** `_read_journal_tail` (`_collectors.py:543`) returns `[]` for BOTH "no recent logs" and
  "journalctl failed/timed out" (it routes through `cached_subprocess_or_empty` then `if not raw:
  return []`). Change it to return a failure sentinel distinct from empty, and thread that through the
  new `/api/diagnostics/journal` endpoint response + JSON schema + redaction, so the client can show
  "couldn't load logs" for a SERVER-side journalctl failure too.
- **Why:** litclock-dev#436 ships an error affordance that only fires on CLIENT-side fetch failure (abort/timeout
  of the per-unit request). A server-side journalctl timeout still arrives as an empty tail,
  indistinguishable from a genuinely quiet unit. This closes that honesty gap.
- **Pros:** The "couldn't load logs" state becomes fully honest; helps the exact debug moment.
- **Cons:** Changes the collector contract + several call sites + tests. The `[]`-means-empty
  assumption is load-bearing in a few readers — audit before changing.
- **Context:** Codex outside-voice tension T3 in the litclock-dev#436 /plan-eng-review (2026-07-03). Accepted the
  limit for the litclock-dev#436 PR; this is the fuller fix. The new per-unit endpoint from litclock-dev#436 is the natural
  place to surface the sentinel (HTTP status or an `ok:false` field).
- **Depends on / blocked by:** ~~litclock-dev#436 (the per-unit endpoint) landing first.~~ **UNBLOCKED** — litclock-dev#436/litclock-dev#475 shipped in v0.216.0 (2026-07-07); the `GET /api/diagnostics/journal?unit=` endpoint now exists as the natural place to surface the failure sentinel. Available to pick up (still P3).

## From /plan-eng-review 2026-10-08 (litclock-dev#532 D7–D11 re-review)

### Version-stamp the PWA's static URLs — P3 follow-up to the litclock-dev#532 re-review (R18)

- **What:** One template helper appends `?v=<release version>` to every static CSS/JS URL
  (`url_for('static', filename='css/settings.css')` today renders `/static/css/settings.css`; after,
  `/static/css/settings.css?v=0.232.0`), and `sw.js.j2`'s `PRECACHE_URLS` uses the same stamped URLs.
  Then raise the static `max-age` well above 15 minutes.
- **Why:** Static URLs are identical across releases, so for up to 15 minutes after an OTA a phone runs
  fresh HTML against the previous release's CSS/JS (`src/control_server/__init__.py:45-56` documents the
  compromise). A stamped URL changes every release, so the first load after an update fetches matching
  files, and the cache can then be long, which matters on iOS where no service worker runs.
- **Pros:** Closes the stale-asset window for good; makes the PWA's English `tr()` literal fallbacks
  key-only (the litclock-dev#532 R18 residual); enables a long cache on iOS.
- **Cons:** Touches ~55 static references across the templates plus the SW precache list (human ~4h /
  CC ~25min); a missed reference keeps the old behaviour for that file only.
- **Context:** Raised by Codex in the litclock-dev#532 D7–D11 eng re-review (Outside Voice O5). R18 chose to keep
  the literals for litclock-dev#532 because the language-side exposure was measured at 0 of 75 tags (no release
  ever removed a key that older JS used). This is the general fix that reasoning set aside.
- **Effort:** M
- **Priority:** P3
- **Depends on / blocked by:** None.
