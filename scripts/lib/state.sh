# shellcheck shell=bash
#
# LitClock — shared state-file helpers.
#
# Both update.sh and litclock-lkg-record.sh write to /var/lib/litclock with
# the same idiom: stage to .tmp, then mv. ext4 rename is atomic, so a power-
# cycle mid-write leaves either the old contents or the new contents — never
# a half-written file. Sourced (not executed); no shebang.
#
# Marker files used by callers (all under /var/lib/litclock):
#   lkg-sha                    — recorded HEAD SHA after a healthy soak
#                                (litclock-lkg-record.sh writes; bootcheck reads)
#   update-failed              — set by update.sh on smoke-test failure
#   post-update-grace-until    — mtime-only marker; reader treats
#                                `now - mtime < 900s` as "still in grace"
#                                (issue #241, decision D2)
#   ── LKG auto-revert (litclock-bootcheck, #209 follow-up) ──
#   boot-fail-count            — consecutive failed boots (bootcheck writes;
#                                update.sh Phase 7 clears on a successful apply)
#   bootcheck-recovering       — a rollback was already triggered this cycle;
#                                bounds a re-rollback loop (bootcheck writes on
#                                trigger; cleared on a healthy boot / normal update)
#   bootcheck-gave-up          — recovery exhausted (LKG also bad / hardware
#                                dead); terminal until a healthy boot clears it
#   rollback-target            — LKG SHA pinned for update.sh rollback mode
#                                (bootcheck writes; update.sh consumes + clears)
#   blocked-sha                — release SHA update.sh must NOT reinstall until a
#                                newer release supersedes it (survives a rollback)

# read_sha_file <path> — echo a validated 40-char lowercase hex SHA from a state
# file, or empty string if the file is absent/unreadable/malformed. Single
# source of truth for the "read a persisted SHA" contract shared by
# litclock-lkg-record.sh, litclock-bootcheck.sh, and update.sh (rollback-target
# / blocked-sha), so SHA validation can't drift between them.
read_sha_file() {
    local path="$1" val
    [[ -s "$path" ]] || { printf ''; return 0; }
    val=$(tr -cd '0-9a-f' < "$path" 2>/dev/null || printf '')
    if [[ "$val" =~ ^[0-9a-f]{40}$ ]]; then
        printf '%s' "$val"
    else
        printf ''
    fi
}

atomic_write_file() {
    # $1 = destination, $2 = content (may be empty for marker files)
    local dest="$1" content="${2-}"
    local parent tmp
    # Symlink guard (mirrors _atomic_write_env_sh_finalize): refuse to write
    # through a symlink at dest. The mktemp+mv path is symlink-safe, but the
    # sudo-tee fallback below would otherwise follow a pi-planted link and write
    # as root through it. Defense-in-depth — /var/lib/litclock is 0755 pi:pi so
    # planting a link already needs pi shell — but the codebase has a history of
    # symlink-follow bugs on this dir, so fail closed.
    if [[ -L "$dest" ]]; then
        echo "[state] refusing to write through symlink at $dest" >&2
        return 1
    fi
    parent=$(dirname "$dest")
    if [[ ! -d "$parent" ]]; then
        sudo mkdir -p "$parent" 2>/dev/null || mkdir -p "$parent" 2>/dev/null || return 1
    fi
    tmp=$(mktemp "${dest}.XXXXXX" 2>/dev/null) || {
        # Fall back to sudo for /var/lib/litclock on a fresh device where
        # the pi user doesn't yet own the state dir.
        tmp="${dest}.tmp.$$"
        printf "%s" "$content" | sudo tee "$tmp" >/dev/null 2>&1 || return 1
        sudo mv "$tmp" "$dest" 2>/dev/null || return 1
        return 0
    }
    printf "%s" "$content" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$dest" 2>/dev/null || { rm -f "$tmp"; return 1; }
    return 0
}

atomic_remove_file() {
    # Best-effort removal; never fatal.
    local target="$1"
    [[ -e "$target" ]] || return 0
    rm -f "$target" 2>/dev/null || sudo rm -f "$target" 2>/dev/null || true
}

# ─── env.sh writer-lock helpers (issue #274) ─────────────────────────
#
# Three shell writers mutate env.sh (update.sh Phase 3, reset-setup.sh,
# prepare-for-cloning.sh). The Python PWA writer in src/config.py holds
# fcntl.flock on a sidecar `<env.sh>.lock`. Without these helpers, shell
# writers can interleave with the Python writer and either silently drop
# a user save or leave a half-written file after a power loss.
#
# Both helpers contend on the same sidecar lock path the Python writer
# uses (`<ENV_FILE_DEFAULT>.lock`), and respect `$LITCLOCK_ENV_FILE`
# overrides so tests can point at a tmpdir.
#
# `flock -w 30 -E 75` semantics:
#   * Wait up to 30s for the lock (healthy atomic_update holds ~5ms;
#     30s is 6000x headroom for a stuck writer).
#   * On timeout, exit 75 from the subshell — caller distinguishes
#     lock-timeout (75) from any other failure.
# Holding a flock indefinitely from a systemd-driven update.sh would
# defer the weekly update on a stuck PWA lock; timeout-and-skip is the
# correct default.
#
# If `flock(1)` is not on PATH (sandbox / CI without util-linux), both
# helpers fall back to an unlocked write + warn, mirroring the guard at
# scripts/update.sh:71 and scripts/download_images.sh:167-171.

ENV_FILE_DEFAULT="${LITCLOCK_ENV_FILE:-/home/pi/litclock/env.sh}"

# atomic_write_env_sh DEST CONTENT — overwrite env.sh atomically under
# the sidecar flock. CONTENT is the full file body (used by
# reset-setup.sh + prepare-for-cloning.sh). Preserves ownership + mode
# of an existing destination so the file remains pi:pi 0644 across the
# replace.
#
# Returns:
#   0   wrote successfully (or fallback wrote with warn).
#   75  flock timed out — destination untouched. Wait defaults to 30s,
#       overridable via $LITCLOCK_ENV_LOCK_WAIT (seconds, integer).
#   1   any other failure (mktemp, printf, mv) — destination untouched.
#
# Callers MUST tolerate rc!=0 explicitly; we never abort the caller's
# script.
atomic_write_env_sh() {
    local dest="$1" content="$2"
    local lock="${dest}.lock"
    local tmp rc
    local wait_seconds="${LITCLOCK_ENV_LOCK_WAIT:-30}"

    # Best-effort lockfile creation. If the parent dir is unwritable
    # AND sudo isn't available, fall through — flock below will fail
    # to open and we'll degrade to the no-flock path.
    : > "$lock" 2>/dev/null || sudo touch "$lock" 2>/dev/null || true

    # Stage the new body in a sibling tmp file so the lock body only
    # does the rename(2). Falls back to a deterministic name if mktemp
    # is unavailable (busybox sandboxes).
    tmp=$(mktemp "${dest}.XXXXXX" 2>/dev/null) || tmp="${dest}.tmp.$$"
    if ! printf '%s' "$content" > "$tmp" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null
        return 1
    fi

    # No-flock fallback: degrade to unlocked write + warn. Matches the
    # pattern in scripts/update.sh:71 — production Pis have flock; CI
    # sandboxes may not, and refusing to write would break those tests.
    if ! command -v flock >/dev/null 2>&1; then
        echo "[WARN] flock(1) unavailable — writing env.sh without lock (sandbox/CI fallback)" >&2
        _atomic_write_env_sh_finalize "$tmp" "$dest"
        return $?
    fi

    # Subshell scope; fd 200 closes (and the flock releases) on `)`.
    (
        flock -w "$wait_seconds" -E 75 200 || { rm -f "$tmp" 2>/dev/null; exit 75; }
        _atomic_write_env_sh_finalize "$tmp" "$dest" || exit 1
    ) 200>"$lock"
    rc=$?
    [[ $rc -ne 0 ]] && rm -f "$tmp" 2>/dev/null
    return $rc
}

# Internal: preserve dest's ownership/mode on the staged tmp, then
# atomically replace. Split out so both the locked and no-lock paths
# share the same finalize semantics. ext4 rename(2) is atomic — readers
# see either pre- or post-state, never a torn intermediate.
#
# Refuses to proceed when dest is a symlink: `stat -c '%a'` on a symlink
# returns the symlink's own mode (always 0777), which would then be
# chmod'd onto the staged tmp — leaving env.sh world-writable after the
# rename. The parent directory is pi-owned so an attacker would already
# need pi-shell access to create the link, but reset-setup.sh and
# prepare-for-cloning.sh both run as root and writing the
# OPENWEATHERMAP_APIKEY into a world-writable file is a defense-in-depth
# leak we don't accept.
#
# When dest doesn't exist (first-boot path), explicitly chmod 0644 so
# the new env.sh doesn't inherit mktemp's 0600 (which would block the
# pi-user `source env.sh` in runtheclock.sh from reading it).
#
# WARNING: callers must NEVER `rm -f` the sidecar `${dest}.lock` while
# any writer might hold it. Unlinking creates a new inode on the next
# `: > "$lock"` and the flock interlock silently breaks (both writers
# proceed against fresh, unrelated locks). No production code path
# unlinks the sidecar today; keep it that way.
_atomic_write_env_sh_finalize() {
    local tmp="$1" dest="$2"
    local owner mode
    if [[ -L "$dest" ]]; then
        echo "[ERROR] atomic_write_env_sh: refusing to overwrite symlink at $dest" >&2
        return 1
    fi
    if [[ -e "$dest" ]]; then
        owner=$(stat -c '%U:%G' "$dest" 2>/dev/null) || owner=""
        mode=$(stat -c '%a' "$dest" 2>/dev/null) || mode=""
        if [[ -n "$owner" ]]; then
            chown "$owner" "$tmp" 2>/dev/null \
                || sudo chown "$owner" "$tmp" 2>/dev/null \
                || true
        fi
        if [[ -n "$mode" ]]; then
            chmod "$mode" "$tmp" 2>/dev/null || true
        fi
    else
        # First-boot path: mktemp staged the file at 0600. env.sh must be
        # world-readable so the pi-user `source env.sh` in runtheclock.sh
        # works after a fresh install.
        chmod 0644 "$tmp" 2>/dev/null || true
    fi
    mv -f "$tmp" "$dest" 2>/dev/null || return 1
    return 0
}

# with_env_lock CMD... — run CMD holding the env.sh sidecar flock.
# Used by update.sh Phase 3 where the writer APPENDs multiple `>>` lines
# per missing var and we can't stage a full body. CMD runs in a subshell,
# so any state mutated by CMD (arrays, exported vars) does NOT propagate
# to the caller — pack everything CMD needs to log into CMD itself.
#
# Returns:
#   75  flock timed out (caller should warn + continue). Wait defaults
#       to 30s, overridable via $LITCLOCK_ENV_LOCK_WAIT.
#   *   CMD's own exit code otherwise.
#
# No-flock fallback: invoke CMD directly + warn. Same rationale as
# atomic_write_env_sh.
with_env_lock() {
    local lock="${ENV_FILE_DEFAULT}.lock"
    local wait_seconds="${LITCLOCK_ENV_LOCK_WAIT:-30}"
    : > "$lock" 2>/dev/null || sudo touch "$lock" 2>/dev/null || true

    if ! command -v flock >/dev/null 2>&1; then
        echo "[WARN] flock(1) unavailable — running env.sh writer without lock (sandbox/CI fallback)" >&2
        "$@"
        return $?
    fi

    (
        flock -w "$wait_seconds" -E 75 200 || exit 75
        "$@"
    ) 200>"$lock"
}
