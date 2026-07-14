# Contributing to LitClock

This document outlines the development workflow and practices for this project.

## Current Practices (Tier 1)

### Branching Strategy

All changes go through pull requests. Never push directly to `master`.

**Branch naming convention:**
- `feat/description-issue-N` - New functionality (e.g., `feat/guardian-quotes-issue-11`)
- `fix/description-issue-N` - Bug fixes (e.g., `fix/venv-stale-paths`)
- `chore/description-issue-N` - Maintenance tasks (e.g., `chore/simplify-cleanup-issue-98`)
- `docs/description` - Documentation only (e.g., `docs/improve-readme`)

Include the issue number in the branch name when one exists.

**Workflow:**
```bash
# 1. Create a branch from master
git checkout master
git pull origin master
git checkout -b feat/my-feature-issue-42

# 2. Make changes and commit (see commit conventions below)
git add <files>
git commit -m "feat: add new feature"

# 3. Push and create PR
git push -u origin feat/my-feature-issue-42
gh pr create
```

### Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
type(scope): description

[optional body]

[optional footer]
```

**Types:**
- `feat` - New feature
- `fix` - Bug fix
- `docs` - Documentation changes
- `style` - Code style (formatting, no logic change)
- `refactor` - Code restructuring (no feature/fix)
- `test` - Adding or updating tests
- `chore` - Maintenance (dependencies, build, etc.)

**Examples:**
```
feat(display): add weather icon support
fix(wifi): prevent disconnect on Pi Zero W
docs(readme): update installation instructions
chore(deps): update Pillow to 10.0
```

**Scope** is optional but helpful - it indicates which part of the codebase is affected:
- `display` - E-ink display rendering
- `wifi` - Network connectivity
- `weather` - Weather fetching
- `quotes` - Quote database/processing
- `install` - Installation scripts
- `update` - Update mechanism (`scripts/update.sh`)
- `server` - Setup/captive portal server
- `guardian` - Guardian quotes pipeline
- `tests` - Test suite
- `deps` - Dependencies

### Pull Requests

- Fill out the PR template when creating PRs
- Link to related issues using `Closes #123` or `Fixes #123`
- Keep PRs focused - one feature/fix per PR
- Write a clear description of what changed and why

### Issues

- Use issues to track bugs, features, and tasks
- Reference issues in commits and PRs
- Close issues via PR merge when possible

---

## Quality Gates (Tier 2)

- [x] **CI Pipeline** - GitHub Actions (`.github/workflows/lint.yml`) runs on every push/PR to master:
  - Ruff (Python linting)
  - ShellCheck (shell script linting)
  - pytest (Python unit tests)
  - vitest (JS unit tests for the control PWA — see "JavaScript tests" below)
  - pip-audit (dependency vulnerability scanning)
- [ ] **Required Reviews** - Enforce PR reviews before merge (requires GitHub Pro for private repos)
- [ ] **Branch Protection** - Prevent direct pushes to master (requires GitHub Pro for private repos)

## Future Practices

### Tier 3: Release Management (Not yet implemented)

- [ ] **Semantic Versioning** - Version tags (v1.0.0, v1.1.0, etc.)
- [ ] **Changelog** - Track changes between versions
- [ ] **GitHub Releases** - Tagged releases with notes

---

## Local Development

### Prerequisites

- Raspberry Pi Zero WH (or similar) for hardware testing
- Python 3.11+
- Waveshare 7.5" e-Paper display (for full testing)

### Setup

```bash
# Clone the repository
git clone https://github.com/kapoorankush/litclock.git
cd litclock

# Create virtual environment. --system-site-packages lets the venv see
# apt-provisioned GPIO libs (gpiozero / spidev / lgpio etc.) without
# pip-recompiling them. Mirrors the image-build + update.sh pattern.
python3 -m venv --system-site-packages venv
source venv/bin/activate

# Install dependencies. requirements-apt.txt lists packages that come from
# apt on the Pi (don't pip-install them into the venv — it either fails
# to compile on a gcc-less image, or shadows the apt version with a binary
# that gpiozero may then pick as its pin_factory backend — see #214).
pip install -r requirements.txt
```

### Dev Dependencies

```bash
pip install -r requirements-dev.txt  # ruff linter
```

### JavaScript tests (control PWA)

The control_server's `src/control_server/static/js/*.js` files are tested
with vitest + jsdom (#338). Dev/CI only — never installed on the Pi.

```bash
# One-time setup. Requires Node 20+.
npm install

# Run the JS test suite (matches the CI gate):
npm run test:js

# Or watch mode for iterative dev:
npm run test:js:watch
```

Tests live in `tests/js/`. The framework re-evaluates each IIFE-wrapped
script against a fresh jsdom DOM per test file via the helpers in
`tests/js/helpers/loadScript.js`. See that file's docstrings for the
URL-pattern fetch mock + dialog stub conventions.

### Diagnostics subprocess-timing regressions (#444)

The diagnostics route shells out to `journalctl`/`systemctl`/`nmcli`/
`timedatectl` via `control_server._subprocess.cached_subprocess`. On a Pi
Zero 2W with a few weeks of journal storage and SD-card IO contention these
calls are *slow*, and three v0.214.x hotfixes (#427, #428, #433) each fixed
a per-call *timing* bug that the instant-return mocks in the suite couldn't
see — they only surfaced in hardware QA.

We protect those classes with **targeted, deterministic regression tests**
rather than a synthetic latency simulator (decided in #444 — a simulator
keyed on guessed p50/p95/p99 numbers tests against a fiction, and still
wouldn't assert the crisp argument-value invariants below). Each class has
a direct guard:

- **#428 — failure-TTL cap** (`min(ttl, FAILURE_TTL_CAP_S)`): a failing
  subprocess (`None`) must rotate out of the cache in 5 s, not the caller's
  full 20 s. Covered by `TestFailureTtl` in `tests/test_subprocess_helper.py`,
  which drives `_time.monotonic` with a fake `_MockClock` (no real sleeps),
  including the "born-stale" case where the subprocess outlasts the cap.
- **#433 — lazy-tail journal forks**: a healthy clock must fork `journalctl`
  zero times; only not-obviously-healthy units get a tail. Covered by
  `TestLazyTailFilter` in `tests/test_control_server_diagnostics.py`, which
  spies on `_batched_journal_tails` and asserts the exact `units` tuple.
- **#427 — journalctl per-call timeout outlier** (8 s vs the 3 s fast
  readers use): covered by `tests/test_control_server_perf.py` —
  `test_read_journal_tail_uses_journal_timeout_not_fast_timeout` pins the
  journalctl call site at `DIAG_JOURNAL_TIMEOUT_S` + 20 s ttl + one fork,
  and `test_journal_timeout_exceeds_fast_call_timeout` pins the constant
  relationship. The **converse** — that the *fast* readers' call sites pass
  the short `DIAG_SUBPROC_TIMEOUT_S` budget — is pinned by
  `TestFastReaderTimeoutContract` in
  `tests/test_control_server_diagnostics_readers.py`, whose `fake_subprocess`
  fixture records each call's `timeout`/`ttl` (via `.kw_for(key)`) so the
  test asserts the *argument value*, no wall-clock race.

- **#430 — per-call fast budgets**: each fast call (nmcli, iw, systemctl,
  timedatectl, git, ip route, uname) reads its OWN timeout constant
  (`DIAG_NMCLI_TIMEOUT_S`, `DIAG_IW_LINK_TIMEOUT_S`, …) instead of the single
  shared `DIAG_SUBPROC_TIMEOUT_S`, so a bump for one slow-under-load call
  doesn't loosen the cheap kernel calls. The seeds are behaviour-preserving
  (all == the shared base) until tuned from real Pi Zero 2W data — run
  `scripts/diag-subprocess-timing.py` on authorclock + the test Pi under each
  load condition (idle / paint contention / memory pressure / degraded SD /
  wedged WiFi) and size each budget at the worst-case p99 + headroom. The
  call-site wiring is pinned by `TestFastReaderTimeoutContract`'s
  `test_each_fast_reader_reads_its_own_per_call_constant` (sentinel
  monkeypatch — bites even while the seeds are equal), and the value
  invariants (positive, < cache TTL, ≤ the journal outlier, < SSE heartbeat,
  and the seed tripwire) by `TestFastCallBudgets` in
  `tests/test_control_server_perf.py`. **When you tune a constant from
  measured data: update `TestFastCallBudgets.test_seeded_at_shared_base_until_measured`
  and cite the measurement** in the commit + the constant's comment — never
  ship a guessed p99.

When adding a new diagnostics reader, assert its `timeout`/`ttl` contract
(in `TestFastReaderTimeoutContract` for a fast reader, or alongside the
journalctl tests in `test_control_server_perf.py` for a slow one) rather
than relying on the latency showing up in hardware QA.

### Updating Python dependencies

`requirements.in` is the source of truth for direct (human-edited) pins.
`requirements.txt` is the **generated lockfile** with every transitive
explicitly pinned — do not hand-edit `requirements.txt`.

This split closes a real failure mode (issue #323): LitClock is
release-gated (`update.sh` only pulls tagged releases), but transitive
Python deps that aren't pinned in `requirements.txt` resolve against
live PyPI at install time on each Pi. A future Werkzeug release that
broke Flask 3.1.3 would land silently on every Pi via the weekly
auto-update, pass the Phase 4.5 smoke test (which doesn't import
Flask), and kill `litclock-control.service` later. Locking transitives
at release-cut time eliminates that drift.

**Adding or bumping a direct dependency:**

```bash
# 1. Edit requirements.in to add/bump a direct pin.
# 2. Install pip-tools if you don't have it:
pip install pip-tools

# 3. Regenerate the lockfile. --upgrade re-resolves every transitive
#    against current PyPI so security bumps land at the same time:
pip-compile --upgrade requirements.in

# 4. Review the diff. Anything surprising should get a comment.
git diff requirements.txt

# 5. Test on a Pi (the test Pi at minimum — running update.sh against
#    the new lock is the cheapest fleet-fidelity check we have):
git commit -am "chore(deps): <what changed>"
git push  # on a branch — DO NOT cut a release yet

# 6. SSH to the test Pi, sync to master, run update.sh, verify the
#    clock keeps rendering AND litclock-control.service is healthy.
#    The Flask transitive chain (werkzeug/click/itsdangerous/blinker/
#    markupsafe/jinja2) is the one that historically breaks silently —
#    confirm /api/status returns 200 from the PWA.

# 7. Only after the test Pi is happy: merge and cut the release.
```

**Only list packages your code directly imports in `requirements.in`.**
Transitive deps (e.g. `certifi`, `idna`, `charset-normalizer`, `jinja2`,
`werkzeug`, …) are resolved and pinned automatically by `pip-compile`,
and they appear in `requirements.txt` with a `# via <parent>` comment.
Pinning a pure transitive in `requirements.in` makes it a top-level
constraint that `pip-compile --upgrade` will refuse to bump — that re-
introduces the same transitive security-bump gap this workflow exists
to close (#323).

**Exception: CVE-floor constraints.** When a transitive is held at a
minimum version for a documented security reason (e.g. `urllib3>=2.7.0`
to keep CVE-2026-44431 / CVE-2026-44432 closed — PR #322), list it in
`requirements.in` as a **range** (`>=X.Y.Z`), not an exact pin, with a
comment explaining the CVE / PR. The range preserves the floor while
still letting `pip-compile --upgrade` pick newer compatible releases.

The 5 apt-provisioned names (`gpiozero`, `lgpio`, `pigpio`, `spidev`,
`colorzero`) are listed in `requirements.in` so the resolver has the
full constraint graph AND the lockfile is self-documenting. The install
paths (`scripts/install.sh`, `scripts/update.sh`,
`pi-gen/stage3/01-setup-app/00-run.sh`) filter these names out of
the generated lock before pip-install via `requirements-apt.txt` —
they come from apt at runtime via `--system-site-packages` (issue #214).

**Why not eager `--upgrade-strategy`?** PR #322 narrowed `update.sh` to
plain `--upgrade` (no eager) because Phase 4.5's smoke test never
imports Flask, so a transitive break would ship to the fleet
undetected. The lockfile is the correct place to land transitive
security bumps — verified manually on a test Pi before the release
tag, not silently on every Sunday auto-update.

### Testing Changes

For display changes without hardware, use `eink_display.py` with `--save` to write a PNG instead of sending to the display:
```bash
python3 src/eink_display.py status "Test" --message "Hello" --save test.png
```

For full testing, deploy to a Raspberry Pi with the display connected.

### Shell helpers (`scripts/lib/`)

Shared bash helpers live under `scripts/lib/`. Source them with the script-relative pattern:

```bash
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"
```

**`scripts/lib/state.sh`** exposes two helpers any shell script that mutates `/home/pi/litclock/env.sh` MUST use to interoperate with `src/config.py:atomic_update`'s `fcntl.flock` on the PWA side. Skipping them re-introduces [#274](https://github.com/kapoorankush/litclock/issues/274):

- `atomic_write_env_sh DEST CONTENT` — full-body overwrite via `mktemp` + `mv -f` under the sidecar flock. Use for "rewrite env.sh from defaults" callers (`reset-setup.sh`, `prepare-for-cloning.sh`).
- `with_env_lock CMD...` — run CMD holding the env.sh sidecar flock. Use for `>>` append-per-var callers (`update.sh` Phase 3). CMD runs in a subshell so use a side-channel tempfile if you need to pass data back to the caller.

Both honor `LITCLOCK_ENV_LOCK_WAIT` (default 30s) and exit 75 on timeout. Cross-process interop is pinned by `tests/test_envsh_shell_flock.py`.

---

## Image Generation Pipeline

The `image-gen/` directory contains scripts for building and maintaining the quote database and generating display images. All scripts run from the `image-gen/` directory.

### Pipeline overview

```
scrape / gather → prepare review → manual review → merge → clean → detect NSFW → review NSFW → merge NSFW → validate → generate images
```

### Scripts

| # | Script | Purpose | Args |
|---|--------|---------|------|
| 1 | `gather_quotes.py` | Bulk-import quotes from multiple open-source literary clock repos | None |
| 2 | *(source-specific scraper)* | Write a new scraper for each new source. Output a pipe-delimited CSV matching the format of `prepare_review_csv.py`'s `INPUT_FILE` placeholder | — |
| 3 | `prepare_review_csv.py` | Flag scraped quotes for manual review, improve time phrases. Edit `INPUT_FILE` / `OUTPUT_FILE` placeholders at the top of the file before running | None |
| 4 | *(manual review)* | Edit the reviewed-quotes CSV in a spreadsheet | — |
| 5 | `merge_quotes.py` | Merge approved reviewed quotes into `litclock_annotated.csv`. Edit `REVIEW_FILE` placeholder at the top before running | None |
| 6 | `process_reviewed.py` | Handle partially-reviewed CSVs (split approved vs. needs-review). Edit path placeholders at the top before running | None |
| 7 | `clean_csv.py` | Remove duplicates and fix escape sequences in the annotated CSV (in place; backs up to `.pre_clean_csv_backup`) | None |
| 8 | `clean_quotes_csv.py` | Normalize Unicode (curly quotes, em-dashes, math italic) | None |
| 9 | `detect_nsfw.py` | Flag NSFW content via keywords and optional LLM | See below |
| 10 | `review_nsfw.py` | Interactive human review of NSFW flags, then merge decisions | See below |
| 11 | `validate_time_parser.py` | Validate every time phrase parses to its expected time | None |
| 12 | `generate_images.py` | Generate 800x400 PNG quote images with highlighted time phrase | None |

`time_parser.py` is a library module used by other scripts — not invoked directly.
`quote_to_image.php` is the primary image generator (produces better output than `generate_images.py`); run it from the `image-gen/` directory.

### Editing the quote corpus

Any change to `image-gen/litclock_annotated.csv` (add / delete / modify / retag a row) is a corpus edit. Ship it with one command:

```bash
# edit image-gen/litclock_annotated.csv in place, then:
python3 image-gen/corpus_edit.py ship "fix(corpus): retag X from HH:MM to HH:MM"
```

`ship` validates that every changed row's `timestring` actually parses to its HH:MM tag, detects the HH:MM buckets whose contents changed vs git HEAD, wipes the stale images in those buckets only, runs `quote_to_image.php`, bumps `.images-version` to the next integer, commits on a new branch, calls `scripts/release_images.sh`, pushes, and opens a PR.

This replaces the previous manual sequence and protects against three silent footguns:

1. **Filename drift** — images are keyed `quote_{HHMM}_{counter}.png` with the counter assigned by CSV row order, so any edit to a bucket renames every image in that bucket.
2. **Skip-if-exists staleness** — historically the PHP generator skipped on `file_exists` alone, silently preserving stale content under renamed slots. Post-#299 it now skips only when an `images/manifest.json` entry confirms the existing PNG was generated from the current row's content; reorders force a regen.
3. **Release-tag collision** — `scripts/release_images.sh` refuses to overwrite an existing release tag; forgetting to bump `.images-version` breaks the publish step.

**CI enforcement (post-#299):** `.github/workflows/corpus-integrity.yml` blocks any PR that edits `image-gen/litclock_annotated.csv` without bumping `.images-version` AND publishing a matching `litclock-images-vN` release whose `manifest.json` `corpus_hash` equals the SHA1 of the PR's CSV. There is no `--no-verify` escape hatch — running `corpus_edit.py ship` is the supported path.

Subcommands for debugging (all safe to run on the working tree):

| Subcommand | What it does |
|------------|--------------|
| `validate` | Assert every changed row's `timestring` parses to its HH:MM tag. Would catch a mistag like a `10.10pm.` row tagged `21:10`. |
| `diff` | List the HH:MM buckets whose contents differ from git HEAD, AND surface any drift between `images/manifest.json` and the current CSV (post-#299). |
| `regenerate` | Wipe dirty buckets, then run `quote_to_image.php`. Supports `--dry-run`. |
| `ship MSG` | Full end-to-end pipeline. Supports `--dry-run`, `--no-release`, `--no-push`, `--branch NAME`. |

See issue #211 for the original design and issue #299 for the manifest + CI integrity layer.

### detect_nsfw.py

```bash
python3 detect_nsfw.py [--use-llm] [--keywords-only] [--csv PATH] [--output PATH]
```

| Flag | Description |
|------|-------------|
| `--use-llm` | Enable Claude API classification (requires `ANTHROPIC_API_KEY`) |
| `--keywords-only` | Keyword matching only, skip LLM even if `--use-llm` is set |
| `--csv PATH` | Input CSV (default: `litclock_annotated.csv`) |
| `--output PATH` | Output file (default: `nsfw_flagged_for_review.csv`) |

### review_nsfw.py

```bash
python3 review_nsfw.py {interactive|merge|stats} [--reviewed PATH] [--csv PATH] [--output PATH]
```

| Subcommand | Description |
|------------|-------------|
| `interactive` | Terminal UI for reviewing flagged quotes one by one |
| `merge` | Apply NSFW decisions back into `litclock_annotated.csv` |
| `stats` | Show review progress statistics |
