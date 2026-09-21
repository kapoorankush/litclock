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
#                                (issue litclock-dev#241, decision D2)
#   ── LKG auto-revert (litclock-bootcheck, litclock-dev#209 follow-up) ──
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

# ─── shell-history wipe + write-back lock (litclock-dev#834, litclock-dev#868) ──
#
# clear_and_lock_bash_history <path>... — empty each history path, then replace
# it with an empty DIRECTORY so that shells still open when the device powers
# off cannot write their history back. Shared by prepare-for-cloning.sh (Step
# 6) and reset-setup.sh's two handoff arms (gift mode and the --poweroff
# factory reset), because all three exist to pass the device to someone else
# and the previous owner's typed commands — an `nmcli … password …` line
# included — must not go with it. Before litclock-dev#868 only clone prep did this.
#
# `history -c` reaches only the calling script's own non-interactive shell.
# The console or SSH shell the operator ran the script FROM writes its history
# back on exit, during the power-off — bash's save_history() APPENDS the
# session's lines, then truncates to HISTFILESIZE — so on the bench the file
# `rm` had removed was back eight seconds later holding the operator's last
# command. A directory at the path fails open(O_WRONLY|O_APPEND), rename() over
# it (`history -w` writes a sibling temp file and renames) and the truncate's
# read with EISDIR, which no capability bypasses (root's DAC override beats a
# mode-0 file; a /dev/null symlink loses to the rename), and one `rmdir`
# removes it. scripts/first-boot.sh removes both directories on the
# not-yet-set-up path (restore_bash_history_after_clone_prep), which every
# clone AND every reset lands on, so the next owner's first login gets an
# ordinary history file.
#
# Reports, does not decide: the caller owns fatality. On return,
#   _HIST_DIRTY     paths whose CONTENTS survived (could not be removed — an
#                   immutable file, a read-only parent, a NON-EMPTY directory
#                   left by an earlier aborted run — or were refused because
#                   removing the NAME would not remove the contents: a symlink,
#                   or a file with a second hard link);
#   _HIST_UNLOCKED  paths that were emptied but could not be locked;
#   _HIST_LOCKED    paths now holding the lock — a caller that aborts can tell
#                   the operator which locks stay until the next boot.
# Returns 0 when DIRTY and UNLOCKED are both empty, 1 otherwise. The `rmdir`
# before `rm -f` takes the lock a previous run left (and refuses a non-empty
# one); an absent path satisfies both harmlessly.
#
# What the lock is, and is not (litclock-dev#873 review): a barrier against bash's own
# exit-time write-back and `history -w`, which is the measured leak. It is a
# root-owned directory in a pi-writable home, so the pi user can `rmdir` it —
# that user is the device's current owner, and a hostile owner racing their
# own reset is outside the threat model (the previous owner's history reaching
# the NEXT owner). Only the two default paths are covered; a shell with its own
# HISTFILE saves wherever that points.
clear_and_lock_bash_history() {
    history -c 2>/dev/null || true
    _HIST_DIRTY=()
    _HIST_UNLOCKED=()
    _HIST_LOCKED=()
    local _h _links
    for _h in "$@"; do
        # Refuse to follow (litclock-dev#873 review, Codex): `rm -f` on a SYMLINK unlinks
        # the link and leaves its target — contents included — on the device;
        # a file with a second hard link survives the same way. Both are the
        # owner's own arrangement, and the honest verdict is "could not clear",
        # never a green line over a file that is still there. Checked BEFORE
        # anything is removed, so the evidence of what survived is intact.
        # The one symlink that is fine is the standard "disable history" idiom,
        # `ln -sf /dev/null ~/.bash_history` (litclock-dev#873 red team): a character
        # device holds nothing, so the link is simply removed and locked over.
        # `-c` follows the link; `-L` does not.
        if [[ -L "$_h" && ! -c "$_h" ]]; then
            _HIST_DIRTY+=("$_h")
            continue
        fi
        if [[ -f "$_h" ]]; then
            _links=$(stat -c %h "$_h" 2>/dev/null || echo 0)
            if [[ "$_links" != 1 ]]; then
                _HIST_DIRTY+=("$_h")
                continue
            fi
        fi
        # `chattr +a` / `+i` on .bash_history is a common hardening idiom, and
        # on the --poweroff arm a DIRTY verdict lands after env.sh is wiped and
        # the key rotated, with no PWA left to retry from (litclock-dev#873 review, Claude
        # adversarial). Every caller runs as root, so clear the attributes
        # first; on a non-ext4 path or a missing chattr this is a no-op and the
        # verdict below still tells the truth. Only reached for a regular file
        # or a directory — the symlink case was refused above.
        chattr -ia "$_h" 2>/dev/null || true
        rmdir "$_h" 2>/dev/null || rm -f "$_h" 2>/dev/null || true
        if [[ -e "$_h" || -L "$_h" ]]; then
            _HIST_DIRTY+=("$_h")
            continue
        fi
        # mkdir IS the lock, and its status is the verdict: it either created
        # OUR empty directory or something landed at the path between the rm
        # and here. A failure is reported as UNLOCKED, not swallowed and then
        # re-tested with `-d`, which follows a symlink and would have called a
        # pi-planted link "locked" (litclock-dev#873 review, Codex + security specialist).
        if mkdir "$_h" 2>/dev/null && [[ -d "$_h" && ! -L "$_h" ]]; then
            _HIST_LOCKED+=("$_h")
        else
            _HIST_UNLOCKED+=("$_h")
        fi
    done
    (( ${#_HIST_DIRTY[@]} == 0 && ${#_HIST_UNLOCKED[@]} == 0 ))
}

# ─── env.sh writer-lock helpers (issue litclock-dev#274) ─────────────────────────
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

# env_sh_defaults [LANGUAGE] — print the canonical env.sh body every
# whole-file seeder writes (litclock-dev#840).
#
# THE SINGLE SOURCE. This block was hand-copied into first-boot.sh,
# reset-setup.sh and prepare-for-cloning.sh, and the copies drifted: measured
# at litclock-dev#783, they seeded 9, 10 and 8 of the sample's keys, so a
# device was born missing knobs depending on which path created it. The
# highest-fanout copy (prepare-for-cloning, the "SD Cards for Friends &
# Family" flow) was the worst. One function means a new env.sh.sample key is
# added in two places — the sample and here — not five.
#
# CONTRACT, all three parts load-bearing:
#
#   1. KEY SET. Must cover EVERY key in env.sh.sample.
#      tests/test_first_boot_flow.py::test_env_sh_defaults_helper_matches_sample
#      executes this function and compares against the sample.
#   2. COMMENT STATUS is copied from the sample and is NOT cosmetic. An
#      ACTIVE `LITCLOCK_RENDER_LEAD_S` hard-pins 4 into the field and makes a
#      later retune leave every device rendering the wrong minute (litclock-dev#762);
#      an active-but-EMPTY value is parsed at import above the litclock-dev#531
#      `except BaseException` guard and kills the painter every minute.
#   3. TRAILING NEWLINE. The body ends with one. update.sh Phase 3 APPENDS
#      missing sample keys with `>>`, so a body without it would splice the
#      first appended key onto `# export LOG_LEVEL=WARNING`. Command
#      substitution STRIPS trailing newlines, so every caller re-adds one:
#      `DEFAULTS=$(env_sh_defaults)$'\n'`. Do not "simplify" that away.
#
# VALUES may differ from the sample and two deliberately do: the sample
# documents WEATHER_LATITUDE/LONGITUDE with real Austin coordinates as an
# example, while a seeded device must leave them EMPTY. With
# WEATHER_LOCATION_MODE=auto the IP-geo resolver fills them on a good boot;
# on the ip-api.com-blocked path a seeded coordinate would render Austin
# weather on a device that is not in Austin — worse than an honest empty.
#
# LANGUAGE ($1, optional) seeds LITCLOCK_LANGUAGE. Empty (the default, used by
# first-boot and prepare-for-cloning) keeps Accept-Language negotiation alive
# on the next first boot — the litclock-dev#743 empty-seed contract. reset-setup.sh passes
# the gift language so every env-reading surface on the recipient's device
# boots in the gifter's chosen language (litclock-dev#532).
#
# The shape gate travels WITH the interpolation point (Codex 5b /review): this
# value is interpolated into a root-written env.sh, so anything outside the
# language-tag alphabet is dropped rather than shipped. reset-setup.sh gates
# and WARNS before calling, so in practice this never fires — it is here so
# that moving the interpolation into this file did not leave the belt behind.
env_sh_defaults() {
    local language="${1-}"
    if [[ -n "$language" && ! "$language" =~ ^[a-z][a-z0-9-]{0,16}$ ]]; then
        echo "[state] env_sh_defaults: language '$language' failed the shape check; seeding empty" >&2
        language=""
    fi
    cat <<EOF
# export OPENWEATHERMAP_APIKEY=
export WEATHER_ENABLED=true
export WEATHER_LATITUDE=
export WEATHER_LONGITUDE=
export WEATHER_LOCATION_NAME=
export WEATHER_UNITS=imperial
export WEATHER_LOCATION_MODE=auto
export WEATHER_IP_COUNTRY=
export WEATHER_LAST_IP_GEO_AT=
export WEATHER_TTL=3600
export ALLOW_NSFW_QUOTES=false
export LITCLOCK_LANGUAGE=$language
export SHOW_DIAGNOSTICS_SHORTCUT=false
export GIFT_MODE_MESSAGE=
export LITCLOCK_RUNTIME_RENDER=false
# export DISPLAY_CLEAR_HOUR=2
# export LITCLOCK_RENDER_LEAD_S=4
# export WEATHER_API_TIMEOUT=15
# export LOG_LEVEL=WARNING
EOF
}

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
