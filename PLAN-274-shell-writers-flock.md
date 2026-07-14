# PLAN — Issue #274: shell writers bypass env.sh flock

## Problem statement

Three shell writers mutate `/home/pi/litclock/env.sh` outside the `fcntl.flock` sidecar that `config.atomic_update` introduced in PR #272 (#253). A concurrent PWA Settings save and weekly auto-update, gift-prep, or reset-setup can interleave: shell does non-atomic `>>` / `cat >`, Python does read+`os.replace`. Last-writer-wins either silently drops a user save or drops the new sample-merged var, and a power loss mid-`>>` can leave env.sh half-written.

## Current state

PWA / Python writer (correct):
- `src/config.py:382-417` — `_exclusive_lock(target)` contextmanager: opens `<target>.name + ".lock"` sibling with `O_RDWR|O_CREAT 0o644`, blocking `fcntl.flock(fd, LOCK_EX)`, unlock+close in `finally`. Sidecar (not target) chosen because `os.replace` swaps the inode (`config.py:386-391`).
- `src/config.py:420-462` — `atomic_update` wraps the read-modify-write+`os.replace` in `with _exclusive_lock(p):`. Validation runs *before* lock acquisition (`config.py:455-460`).
- Default `ENV_FILE_DEFAULT = /home/pi/litclock/env.sh` (`src/config.py:46`) → lock path `/home/pi/litclock/env.sh.lock`. Overridable via `$LITCLOCK_ENV_FILE`.
- Concurrency test that pinned the contract: `tests/test_config.py:666-707`.

Unprotected shell writers:
- `scripts/update.sh:484-512` — Phase 3 sample merge. `echo ... >> "$INSTALL_DIR/env.sh"` per missing var. Multiple appends per run. `set -e` deliberately omitted (`tests/test_update_sh.py:105`, MEMORY.md).
- `scripts/reset-setup.sh:209-221` — `cat > "$INSTALL_DIR/env.sh" <<'EOF' … EOF` + `chown pi:pi`. Runs as root (`reset-setup.sh:64-68`), no `set -e`.
- `scripts/prepare-for-cloning.sh:54-66` — same heredoc overwrite + `chown`. Runs as root with `set -e` (line 12).

Reference flock pattern already in the repo: `scripts/download_images.sh:167-171` (`exec 200>"$LOCK_FILE"; flock -n 200 || …`).

## Proposed approach

### Architecture — one writer pattern, three callers

Goal: every shell mutation of `env.sh` holds the *same* `fcntl`/`flock` sidecar lock the Python writer uses. `fcntl.flock` and `flock(1)` both call the `flock(2)` syscall against the open fd — they interoperate. Sidecar inode is stable across the heredoc + mv (we are introducing a `mv` for shell atomicity); fine.

Shared helper added to `scripts/lib/state.sh` (already sourced by `update.sh`; sourced by the two reset scripts via a new `. "$_THIS_SCRIPT_DIR/lib/state.sh"`):

```bash
# scripts/lib/state.sh — addendum
ENV_FILE_DEFAULT="${LITCLOCK_ENV_FILE:-/home/pi/litclock/env.sh}"

# atomic_write_env_sh DEST CONTENT — overwrite env.sh atomically under flock.
# CONTENT is the full file body (used by reset-setup + prepare-for-cloning).
atomic_write_env_sh() {
    local dest="$1" content="$2"
    local lock="${dest}.lock"
    local tmp
    : > "$lock" 2>/dev/null || sudo touch "$lock" 2>/dev/null || true
    tmp=$(mktemp "${dest}.XXXXXX" 2>/dev/null) || tmp="${dest}.tmp.$$"
    printf '%s' "$content" > "$tmp" || { rm -f "$tmp"; return 1; }
    # Subshell scope; fd 200 closes (and the flock releases) on `)`.
    (
        flock -w 30 -E 75 200 || { rm -f "$tmp"; exit 75; }
        # Preserve ownership: stat the existing file BEFORE replace.
        if [[ -e "$dest" ]]; then
            local owner; owner=$(stat -c '%U:%G' "$dest")
            chown "$owner" "$tmp" 2>/dev/null || true
            local mode; mode=$(stat -c '%a' "$dest")
            chmod "$mode" "$tmp" 2>/dev/null || true
        fi
        mv -f "$tmp" "$dest"
    ) 200>"$lock"
    local rc=$?
    [[ $rc -ne 0 ]] && rm -f "$tmp" 2>/dev/null
    return $rc
}

# with_env_lock CMD... — run CMD holding the env.sh flock. Use for update.sh
# Phase 3 where we APPEND multiple lines and must not stage a full file.
with_env_lock() {
    local lock="${ENV_FILE_DEFAULT}.lock"
    : > "$lock" 2>/dev/null || sudo touch "$lock" 2>/dev/null || true
    (
        flock -w 30 -E 75 200 || exit 75
        "$@"
    ) 200>"$lock"
}
```

### Per-caller wiring

1. **`scripts/update.sh:484-512` (Phase 3 merge — append idiom).** Wrap the existing while-read block in `with_env_lock <<'EOF' … EOF` is not viable (heredoc into subshell loses outer var state). Cleanest: hoist Phase 3 body into a `_phase3_merge_sample()` function and call it via `with_env_lock _phase3_merge_sample`. On rc==75 (timeout): `log_warn "env.sh locked by another writer; skipping sample merge this run"` and continue — Phase 3 is opportunistic; the next weekly tick will retry. Compatible with the no-`set -e` invariant: `with_env_lock` is one statement, `_phase3_merge_sample` returns rc preserved.

2. **`scripts/reset-setup.sh:209-221`.** Replace the heredoc-into-file with: read default body into a bash var, call `atomic_write_env_sh "$INSTALL_DIR/env.sh" "$DEFAULTS"`. On rc!=0: log + continue (reset-setup is best-effort across many steps; aborting halfway is worse than a config we re-write on next boot). Also add a `systemctl stop litclock-control.service 2>/dev/null || true` ahead of all env.sh mutation (per issue body) so the contention window collapses to "shell writer only".

3. **`scripts/prepare-for-cloning.sh:54-66`.** Same `atomic_write_env_sh` substitution. Also stop `litclock-control.service` before the write. NOTE: this script has `set -e` (line 12), so the helper MUST return 0 on the happy path and we may want a `|| true` on the call site to keep parity with the existing `chown` tolerance.

### Lockfile path and bootstrap

- Single path: `${LITCLOCK_ENV_FILE:-/home/pi/litclock/env.sh}.lock` — identical to what Python computes in `_exclusive_lock` (`config.py:401`). Override via `$LITCLOCK_ENV_FILE` works on both sides.
- If env.sh does not exist yet (first-boot, before `cp env.sh.sample env.sh` at `update.sh:511`): create the lockfile anyway via `: > "$lock"` / `sudo touch`. The lock has no contents; only its inode matters. Owner can be `pi` or `root`; flock works regardless.
- Lockfile parent dir is `$INSTALL_DIR` (always exists on a provisioned Pi). No `mkdir -p` needed.

### Self-modifying update.sh interaction (PR #94)

The Phase 3 sample merge runs at `update.sh:484`, *after* the self-reexec checksum guard at `update.sh:404-408`. So by the time we acquire the env.sh flock, we're already executing from the new on-disk `update.sh` bytes — no risk of a stale-fd interaction with the flock subshell.

The other flock that update.sh holds is `/var/lib/litclock/update.lock` (`update.sh:70-110`) — a *different* lockfile guarding the whole script. Holding both is fine; they protect orthogonal contention surfaces and there is no acquisition cycle (update.sh always acquires update.lock first, then env.sh.lock inside Phase 3).

## Edge cases + decisions

| # | Case | Decision |
|---|------|----------|
| 1 | env.sh missing (first-boot) | Lockfile is independent; create it. The pre-existing fallback at `update.sh:509-512` (`cp env.sh.sample env.sh`) stays outside the lock — no concurrent writer exists at that point in the boot sequence (litclock-control gated by `.setup-complete`, see `systemd/litclock-control.service:10`). |
| 2 | Lockfile dir missing | `$INSTALL_DIR` always exists once we are inside any of these scripts (they all reference `$INSTALL_DIR/env.sh`). No extra `mkdir`. |
| 3 | `set -e` semantics | `update.sh` has no `set -e` — keep helper return-code-based, never let an inner `chown`/`stat` failure abort. `prepare-for-cloning.sh` has `set -e` — wrap the helper call in an explicit `|| log_warn …` so a lock timeout doesn't kill the whole prep flow. |
| 4 | update.sh self-modification (#94) | Phase 3 is post-checksum-reexec; safe. |
| 5 | Failure mode — block forever? | NO. `update.sh` runs in systemd weekly; blocking on a stuck PWA lock would defer the update indefinitely. **Use `flock -w 30 -E 75` (30s wait, exit 75 on timeout)** for all three callers. Rationale: a healthy `atomic_update` holds the lock for ~5ms; 30s is 6000× headroom. `update.sh` on timeout: warn + skip Phase 3 (resume next tick). `reset-setup.sh` + `prepare-for-cloning.sh` on timeout: warn + abort the env.sh step but continue the rest (user can rerun; the WiFi-wipe / .setup-complete clear is the more important part of those flows). |
| 6 | Ownership preservation | `atomic_write_env_sh` does `stat -c '%U:%G' "$dest"` + `chown` on the staged tmp BEFORE `mv`. Matches Python side (`config.py` uses fchown on tmp fd). |
| 7 | Atomicity on shell side | Move from `>>` / `cat >` to write-tmp-then-`mv -f` so a power loss mid-write leaves either pre- or post-state, never torn. (`mv` within the same filesystem is `rename(2)` — atomic on ext4.) Matches the `os.replace` guarantee (issue Acceptance criterion 3). |
| 8 | flock binary missing | `command -v flock` already gated in `update.sh:71`. Mirror that guard in the helper: if `flock` unavailable, fall back to no-lock write + `log_warn` (preserves current behavior in sandbox/CI). |
| 9 | Holding flock across `sudo` | reset-setup + prepare-for-cloning run as root already (`EUID -ne 0` guard); no sudo escalation inside the lock body. update.sh runs as pi; the lock body in Phase 3 does no `sudo`. |
| 10 | sibling Python `setup_server.save_settings` issue | Out of scope (issue body confirms — separate sibling). |

## Data flow

```
                       /home/pi/litclock/env.sh.lock  (sidecar inode)
                       ┌──────────────────────────┐
                       │   fcntl.flock LOCK_EX    │  ← all writers contend here
                       └──────────────────────────┘
                                  ▲
   ┌──────────────────────────────┼──────────────────────────────────────┐
   │                              │                                      │
   │ src/config.py:                                                      │
   │   atomic_update()            │                                      │
   │     _exclusive_lock(p)       │  Python flock(fd, LOCK_EX)           │
   │       read+modify+os.replace │                                      │
   │                              │                                      │
   │ scripts/update.sh Phase 3:                                          │
   │   with_env_lock _phase3_merge_sample      flock -w 30 -E 75 200    │
   │     while read line; do echo >> env.sh                              │
   │                                                                     │
   │ scripts/reset-setup.sh step 3:                                      │
   │   atomic_write_env_sh "$ENV" "$DEFAULTS"  flock -w 30 -E 75 200    │
   │     mktemp → printf → chown → mv -f                                 │
   │                                                                     │
   │ scripts/prepare-for-cloning.sh step 2:                              │
   │   atomic_write_env_sh "$ENV" "$DEFAULTS"  flock -w 30 -E 75 200    │
   │     mktemp → printf → chown → mv -f                                 │
   └─────────────────────────────────────────────────────────────────────┘
```

## Test plan

Add `tests/test_envsh_shell_flock.py` (new file, in scope of CLAUDE.md test command):

1. **Structural (grep) — fast, runs in CI sandbox.**
   - `test_update_sh_phase3_runs_under_flock`: assert `with_env_lock` or `flock -w 30 -E 75` appears in the line range of Phase 3 (between `Phase 3: Merge new env vars` and `Phase 4`).
   - `test_reset_setup_uses_atomic_write_env_sh`: grep `atomic_write_env_sh "$INSTALL_DIR/env.sh"` in `reset-setup.sh`.
   - `test_prepare_for_cloning_uses_atomic_write_env_sh`: same on `prepare-for-cloning.sh`.
   - `test_lockfile_path_matches_python`: grep `${LITCLOCK_ENV_FILE:-/home/pi/litclock/env.sh}` in `scripts/lib/state.sh` so the path stays in lockstep with `src/config.py:46`.
   - `test_helper_uses_mv_not_redirect`: assert `atomic_write_env_sh` body contains `mv -f` and does NOT contain a bare `>` writing directly to `$dest`.

2. **Cross-process flock integration — the regression test that would have caught this.** New test using `subprocess.Popen` to actually exercise shell + Python contention:
   - Set `LITCLOCK_ENV_FILE=tmp_path/env.sh` in both processes.
   - Background a bash process holding `flock -x` on the sidecar via `flock $ENV.lock sleep 0.5`.
   - From the foreground Python, call `config.atomic_update({"WEATHER_UNITS": "metric"}, env)` and time it.
   - Assert the call duration is ≥ 0.4s (it was forced to wait for the shell lock) — proves the locks interoperate.
   - Reverse direction: hold the lock from Python (`with config._exclusive_lock(env):`), shell `atomic_write_env_sh` with `flock -w 1` must exit 75. Assert exit code.

3. **No-flock fallback path**: monkey-patch `PATH` to drop `flock`, assert `atomic_write_env_sh` still writes and emits the warn (mirrors the `download_images.sh:167-171` and `update.sh:71` pattern).

JS tests: N/A.

## Rollout / risk

- **Single PR, file-disjoint at directory level from active Triangle D candidates** (#264 src/literary_clock, #355 tests/test_wifi_retry_flow) per MEMORY.md — safe to ship in parallel.
- **No release tag needed for the fix itself** — pure shell + helper addition. Will land via auto-update once a v0.211.x bump fires.
- **Risk vectors**:
  1. Holding the flock longer than expected in `update.sh` Phase 3 would defer concurrent PWA Settings saves. Phase 3 loops over ~10 lines of env.sh.sample; bounded ms. The 30s timeout on the *opposite* side (PWA) protects user UX.
  2. `mv -f` over an env.sh that another reader (`runtheclock.sh` → `source env.sh`) has open: bash's `source` reads the whole file once and closes — same race exists today against Python's `os.replace`; not a regression.
  3. lockfile ownership drift: first writer creates it `pi`-owned, later writers run as root and append (lockfile body is never written, but `fcntl.flock` against a root-owned file from pi requires only the file be readable — `0o644` from `_exclusive_lock` is fine). No churn.
- **Reversibility**: revert is a single PR; the helper additions to `scripts/lib/state.sh` are dead code without the call-site changes.

## Critical files for implementation

- `src/config.py`
- `scripts/lib/state.sh`
- `scripts/update.sh`
- `scripts/reset-setup.sh`
- `scripts/prepare-for-cloning.sh`
- `tests/test_envsh_shell_flock.py` (new file)

## Top decisions / open questions for reviewer

1. **Timeout-and-skip, not block.** All three callers use `flock -w 30 -E 75`; on timeout `update.sh` skips Phase 3 (next weekly tick retries), reset/prepare warn-and-continue. Blocking-forever from a systemd-driven `update.sh` is the wrong default.
2. **Two helpers, not one.** `atomic_write_env_sh` (full-body overwrite + `mv`) for reset/prepare matches Python's `os.replace` atomicity. `with_env_lock` (lock-then-run-callback) for `update.sh` Phase 3's per-var `>>` append pattern, hoisted into `_phase3_merge_sample()` to preserve the existing logic with minimal diff.
3. **Open question for the user**: should I also stop `litclock-control.service` in `update.sh` (not just reset/prepare)? Issue body asks for it on reset/prepare only, but if update.sh and the PWA both write the same minute, even a 30s flock means the PWA UI sees a confusing latency. Defensible to defer to a follow-up.
