# Plan: LKG auto-revert (`litclock-bootcheck.service`) — TODOS.md follow-up to #209

## Goal
Close the "smoke passed but runtime crashes" brick class. The #209 writer records the last-known-good SHA to `/var/lib/litclock/lkg-sha`; this ships the consumer that self-heals a bricked device with no SSH / no SD reflash. Hard prerequisite for #82 (public release: SSH is off, recipients have no other recovery path).

## What exists (from recon)
- `litclock-lkg.service` + `.timer` run `scripts/litclock-lkg-record.sh`, writing a raw 40-char SHA to `/var/lib/litclock/lkg-sha` only when 3 gates pass: post-update grace expired (`post-update-grace-until` mtime > 900s), heartbeat fresh (`/run/litclock/heartbeat` mtime < 180s), and HEAD != recorded.
- **Health signal = `/run/litclock/heartbeat`** (tmpfs), touched by `literary_clock.py::_write_heartbeat()` after every successful `epd.display()`. Network-independent (weather is optional), so freshness genuinely means "render path works."
- `/var/lib/litclock/` is persistent (pi:pi 0755). journald is volatile on flashed images → counter must live here, not in journald.
- `update.sh`: single-flight flock on `/var/lib/litclock/update.lock`; Phase 1 stops `litclock.timer` + **clears `lkg-sha`**; captures `OLD_SHA` for its own smoke-fail rollback (`git reset --hard $OLD_SHA`, line ~726); Phase 7 touches the grace marker + restarts services.
- Test pattern: `tests/test_lkg_writer.py` drives the shell script via `subprocess.run` with env-injected paths (`LITCLOCK_DIR`, state dir, heartbeat file) over a throwaway `git init` sandbox. No systemd-unit tests.

## The DOA-update gap (central design problem)
`update.sh` clears `lkg-sha` at Phase 1 and it is only re-armed after the NEW code renders healthy for 15 min. So if the new code is dead-on-arrival (never renders), `lkg-sha` is **empty** exactly when bootcheck needs it. The pre-update SHA (`OLD_SHA`, known-good — LKG had blessed it) is not persisted durably past update.sh's own run.

**Fix:** `update.sh` persists the pre-update known-good SHA to `/var/lib/litclock/rollback-sha` at the point it resets to new code (Phase 2). `litclock-lkg-record.sh` clears `rollback-sha` at the moment it arms a fresh `lkg-sha` (new code proven healthy → old rollback target no longer wanted). Bootcheck's revert target = `lkg-sha` if present, else `rollback-sha`.

## Counting semantics (chosen: natural-boot counting, no autonomous retry-reboot loop)
Per the TODO wording ("reverts … after 3 consecutive failed boots, then reboots once"), bootcheck does NOT reboot on each failed boot. It counts **natural** boots (the user power-cycling a blank clock — the "turn it off and on again" reflex) and reboots exactly ONCE, after the revert. This avoids the greenboot-style autonomous reboot loop and its transient-failure / dead-lkg reboot-loop hazards. Trade-off: self-heal needs the user to power-cycle ~3×; if they never do, the device sits blank (no regression vs today).

## Design

### State files (`/var/lib/litclock/`, persistent, pi:pi, atomic writes via `lib/state.sh`)
- `boot-fail-count` — integer, consecutive failed boots.
- `bootcheck-reverted` — marker: a revert already happened this cycle (bounds the loop: if reverted code ALSO fails 3×, give up instead of reverting again).
- `rollback-sha` — pre-update known-good SHA, written by `update.sh` (covers the DOA-update empty-`lkg-sha` case).

### Units
- `litclock-bootcheck.timer`: `OnBootSec=8min` (must exceed worst-case first render incl. first-boot image sync; sibling LKG timer uses 10min). One decisive check per boot. `WantedBy=timers.target`.
- `litclock-bootcheck.service`: `Type=oneshot`, `User=pi`, `WorkingDirectory=/home/pi/litclock`, `ConditionPathExists=/etc/litclock/.handoff-complete` (no heartbeat is expected before setup completes — never count pre-setup boots), runs `scripts/litclock-bootcheck.sh`.

### `scripts/litclock-bootcheck.sh` logic (as pi; sudo only for `systemctl reboot`)
1. `flock -n` on `update.lock`; if held (update running) → exit 0 (retry next boot).
2. If `post-update-grace-until` fresh (< 900s) → exit 0 (new code still settling; don't judge yet).
3. Read heartbeat mtime.
   - **Fresh (< 180s) → healthy:** if `boot-fail-count` != 0, reset to 0; clear `bootcheck-reverted`; exit 0.
   - **Stale/missing → this boot failed:** continue.
4. Increment `boot-fail-count` atomically.
5. If `boot-fail-count` < 3 → log + exit 0 (wait for the next natural boot).
6. If `boot-fail-count` >= 3:
   - If `bootcheck-reverted` already set → reverted code still fails → **give up**: paint a "recovery failed" splash, exit 0 (no reboot loop).
   - Resolve revert target: `lkg-sha` if non-empty, else `rollback-sha`. If neither → **give up**: paint splash, exit 0.
   - `git reset --hard <target>` (holding `update.lock`); `systemctl daemon-reload` (revert may change unit files) + re-enable any changed units; touch `bootcheck-reverted`; reset `boot-fail-count` to 0 (fresh 3 tries on reverted code); `sudo systemctl reboot`.

### `update.sh` changes
- Phase 2: write `OLD_SHA` → `/var/lib/litclock/rollback-sha` (atomic) right before/after `git reset --hard $TARGET_SHA`.
- Phase 7 (success): clear `boot-fail-count` + `bootcheck-reverted` (a fresh good update wipes the failure streak). Leave `rollback-sha` for the LKG writer to clear once the new code is blessed.

### `litclock-lkg-record.sh` change
- When it arms a fresh `lkg-sha` (all gates pass, write succeeds): also clear `rollback-sha` (new code is now the known-good; old rollback target retired).

### Reboot authorization
`020_litclock-control` already authorizes `/usr/bin/systemctl reboot`. bootcheck runs as pi and uses the same scoped grant — no new sudoers entry, works after #387 drops `010`. Verify the exact authorized form (`reboot` vs `reboot --no-block`).

## Reboot-loop bound (safety)
Worst case: bad update → 3 natural power-cycles → revert + 1 reboot → reverted code also bad → 3 more cycles → give-up splash, no further reboots. Bounded. The give-up splash tells the user to reflash (last resort).

## Tests (`tests/test_bootcheck.py`, mirroring `test_lkg_writer.py`)
- healthy (heartbeat fresh) → count reset to 0, no reboot.
- stale heartbeat, count 0→1, 1→2 → increment only, no revert.
- count reaches 3, `lkg-sha` present → `git reset --hard` to it + reboot invoked (mock the reboot via injected command).
- count 3, `lkg-sha` empty but `rollback-sha` present → reverts to rollback-sha (DOA-update path).
- count 3, both empty → give-up, no reset.
- `bootcheck-reverted` already set + count 3 → give-up, no second revert.
- grace fresh → early exit, no count change.
- update.lock held → early exit, no count change.
- `.handoff-complete` missing → (ConditionPathExists guards; unit-level, plus a script-level guard test).
- update.sh writes `rollback-sha` at Phase 2; clears counters at Phase 7 (extend `test_update_*`).
- lkg-record clears `rollback-sha` on arm (extend `test_lkg_writer.py`).
- Structural invariant: bootcheck holds `update.lock`; reboot command is the scoped `systemctl reboot` form.

## FINALIZED DESIGN (post eng-review + codex outside voice, 2026-07-09)

The review reshaped this substantially. Codex showed a git-only `reset --hard` is an *incomplete* rollback (misses venv, submodules, and units already copied into `/etc`) and needs privileges bootcheck lacks; and that my `rollback-sha` file was the wrong fix. Finalized decisions:

### D1 — Recovery reuses the `update.sh` installer (complete). [USER APPROVED]
bootcheck is a **thin detector**. On confirmed failure it (a) pins the recovery target, (b) suppresses the bad SHA, (c) triggers `litclock-update.service`. `update.sh` grows a **rollback mode**: if `/var/lib/litclock/rollback-target` exists, it installs THAT SHA (complete: git + venv hash-gate + units→/etc + sudoers + dispatcher + daemon-reload + smoke + service restart) instead of the latest release. This reuses the tested, privileged installer and makes **`update.sh` the recovery island** (stable, installed) rather than bootcheck (part of the payload it distrusts). `020` already authorizes `systemctl start --no-block litclock-update.service` — no new sudoers surface.

### D2 — DOA-gap fix: STOP clearing `lkg-sha` (not `rollback-sha`). [adopted from codex]
The DOA gap is *created* by `update.sh` Phase 1 deleting `lkg-sha`. Fix: **never clear it**; the heartbeat-gated writer atomically replaces it with the new HEAD only once the new code actually paints. Elegant core invariant:

> Because the writer only blesses on a fresh heartbeat, and update.sh never clears `lkg-sha`, **`lkg-sha` always points at the last code that actually rendered.** A DOA update can never overwrite it → bootcheck always has a valid target. No second file, no power-loss window, no idempotency bug.

Inverts the existing `test_update_sh.py:524` invariant (Phase-1-clears-LKG) — regression test updated accordingly.

### D3 — Bounded auto-reboot (greenboot-style), autonomous. [USER APPROVED]
Auto-reboot is the mechanism that confirms *persistence* without user action (a single failed render may be transient; 3 confirms a real brick):
- fail 1 → auto-reboot to retry; fail 2 → auto-reboot to retry; fail 3 → trigger `update.sh` rollback (live, no extra reboot).
- post-rollback: if recovered code also fails 3× (marker `bootcheck-recovering` set) → **give-up**: best-effort splash, stop. Bounded to ~4 reboots.

### D4 — Health = "painted since THIS boot", grace ≥10 min. [validated]
`/run` is tmpfs (wiped each boot), so `/run/litclock/heartbeat` existing with mtime after boot-start means "the clock rendered at least once this boot." Removes sensitivity to the exact grace value. Grace `OnBootSec=12min` (> the LKG writer's 10min; a false positive here is destructive, so be more conservative, never less). Validated: image downloads happen only inside `update.sh`'s whole-body `update.lock` flock + 15min post-update grace, so a slow 130MB image sync can never trip bootcheck; images are persistent on SD, so normal reboots don't re-download.

### State files (final, `/var/lib/litclock/`)
- `boot-fail-count` — consecutive failed boots (write gated on change).
- `bootcheck-recovering` — marker: rollback already triggered this cycle (give-up bound).
- `rollback-target` — LKG SHA pinned for update.sh rollback mode (bootcheck writes, update.sh consumes+clears on success).
- `blocked-sha` — bad release SHA update.sh must not reinstall until a newer release exists.
- (`rollback-sha` from the original plan is DROPPED.)

### Codex fixes folded in (no longer open questions)
OnActiveSec on the timer; recovery routes through update.sh which already stops `litclock.timer` before mutating; full 40-char SHA (lkg-sha already is); install.sh + pi-gen/stage3/03-install-services wiring; bootcheck.sh installed to a stable path so a bad working-tree doesn't remove the running recovery script; best-effort give-up splash (documented as best-effort — shares the possibly-broken venv/display stack); `blocked-sha` suppression so the next weekly update won't reinstall the brick; invert `test_update_sh.py:524`.

### Documented residual limits
- If the release that *adds* bootcheck is itself so broken that bootcheck/systemd can't run, no in-tree agent can self-heal (SD reflash territory) — fundamental to any in-payload recovery.
- Corrupt/missing `images/` on a normal boot (no update pending) reads as a failed boot; reverting code won't fix missing data, but the revert is harmless and the case is rare.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_found | 4 arch/quality issues; design reshaped |
| Outside Voice | `codex exec` | Independent 2nd opinion | 1 | issues_found | 16 findings; 2 reshaped the design |

- **CODEX:** git-only reset is incomplete rollback (venv/submodules/installed-units); `rollback-sha` wrong fix → stop clearing `lkg-sha`; sudoers claim false for full rollback; recovery-agent-in-payload; bad-SHA re-install; install-path + timer + stop-timer gaps. All folded into the finalized design.
- **CROSS-MODEL:** Both reviewers agreed natural-boot counting is weak. Little tension — codex mostly extended the Claude review. The one reversal (`rollback-sha` → stop-clearing-`lkg-sha`) was adopted with user approval.
- **VERDICT:** ENG CLEARED (design reshaped + user-approved) — ready to implement.

NO UNRESOLVED DECISIONS
