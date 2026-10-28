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

# ─── Durability barriers (litclock-dev#893) ──────────────────────────
#
# rename(2) is atomic for concurrent READERS, not a durability barrier: after
# `mv` returns the bytes can still be in page cache, and a power cut can bring
# the file back zero-length. ext4's auto_da_alloc heuristic covers a rename
# over an EXISTING file, which is why env.sh has not been seen doing it — but
# not a brand-new file, which is what most /var/lib/litclock state is
# (rollback-target, blocked-sha and the self-test pass record are all written
# fresh). So every staged file is fsynced BEFORE its rename and its directory
# after, the same two barriers src/config.py and src/wifi_provision.py use.
#
# `sync FILE` is fsync(2) in coreutils (>= 8.24; Pi OS ships 9.1). It REOPENS
# the file rather than syncing the descriptor that wrote it, so a writeback
# error from a failing card can in principle be lost if the kernel evicts the
# inode in between (Codex, litclock-dev#893 review) — a known residual, accepted: closing it
# means staging every body through `dd conv=fsync` instead of a redirect.
# `sync` absent entirely (the busybox/CI sandboxes the no-flock fallback exists
# for) degrades to the old unsynced write rather than refusing.

# _sync_staged PATH — flush a staged file before it is renamed into place.
# rc 1 means the flush FAILED and the caller must not rename: publishing bytes
# the kernel just said it could not write is the zero-length hazard itself.
_sync_staged() {
    command -v sync >/dev/null 2>&1 || return 0
    sync "$1" 2>/dev/null
}

# _sync_dir DIR — make a completed rename in DIR durable. Always rc 0: the new
# file is already in place and its bytes are already on the card, so the worst
# a lost rename brings back is the OLD file, whole (or, on a first write, no
# file). Warns rather than fails —
# an owner decision on the litclock-dev#893 review, which weighed it against gift/clone
# prep: there "the old file" is the previous owner's env.sh, so a failure here
# on a card that is already returning I/O errors is worth a line in the log,
# but not worth a distinct return code threaded through every caller.
_sync_dir() {
    command -v sync >/dev/null 2>&1 || return 0
    sync "$1" 2>/dev/null \
        || echo "[WARN] could not sync directory $1 — the file just renamed into it may not survive a power cut (litclock-dev#893)" >&2
    return 0
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
        # Flush ONCE, with the privilege that wrote it (a restrictive sudo umask
        # leaves it 0600 root, unopenable by pi). Never an unprivileged try then
        # a sudo retry: a retried fsync can report success after the kernel has
        # already dropped the failed pages, which would publish the very file a
        # flush just said was bad (litclock-dev#893 review, Codex; reproduced).
        if command -v sync >/dev/null 2>&1 && ! sudo sync "$tmp" 2>/dev/null; then
            sudo rm -f "$tmp" 2>/dev/null
            return 1
        fi
        sudo mv "$tmp" "$dest" 2>/dev/null || return 1
        _sync_dir "$parent"
        return 0
    }
    printf "%s" "$content" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    _sync_staged "$tmp" || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$dest" 2>/dev/null || { rm -f "$tmp"; return 1; }
    _sync_dir "$parent"
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
# VALUES may differ from the sample and three deliberately do: the sample
# documents WEATHER_LATITUDE/LONGITUDE with real Austin coordinates as an
# example, while a seeded device must leave them EMPTY. With
# WEATHER_LOCATION_MODE=auto the IP-geo resolver fills them on a good boot;
# on the ip-api.com-blocked path a seeded coordinate would render Austin
# weather on a device that is not in Austin — worse than an honest empty.
# The third is LITCLOCK_RUNTIME_RENDER, which is a PARAMETER here rather than
# a constant: `true` when first-boot.sh asks for it (a fresh flash renders text
# from its first paint, against a marker stamped at image-build time), `false`
# for every other caller and for the sample. The sample is what update.sh
# Phase 3 BACKFILLS onto existing devices, so a `true` there would switch an
# old clock on with none of litclock-dev#871 Stage B's guards run. Fresh-flash
# default and existing-device backfill are different jobs and this is the only
# key where they disagree; do not "fix" them into agreement.
# tests/test_first_boot_flow.py compares the KEY SET and comment status, not
# values, and tests/test_runtime_render_autostamp.py pins the divergence.
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
    # RUNTIME_RENDER ($2, optional) — litclock-dev#871 Stage B. Defaults to
    # `false`, and the default is the fail-safe direction ON PURPOSE: this
    # helper is NOT a fresh-flash-only seeder. reset-setup.sh and
    # prepare-for-cloning.sh both call it on an EXISTING device, and neither
    # removes `.runtime-render-validated` (they clear the self-test record, not
    # the marker). So a `true` default would hand a reset clock runtime render
    # with none of Stage B's five guards run — including a clock whose
    # self-test had FAILED, which is precisely the device the guards exist to
    # hold back. Those callers therefore never ask for `true` outright: they
    # pass the device's CURRENT value, read by env_sh_current_runtime_render
    # below. A clock already on text keeps it (a fresh flash seeded it, or
    # Stage B migrated it through its guards); a clock still on images, or
    # held back by a failed self-test, stays there for update.sh to migrate on
    # its next tick. Before that, every reset and every clone dropped a text
    # clock back to PNGs until its next weekly tick.
    # `first-boot.sh` passes `true` explicitly, because a fresh flash carries a
    # marker stamped at image-build time against the shipped renderer.
    local runtime_render="${2-false}"
    if [[ "$runtime_render" != "true" && "$runtime_render" != "false" ]]; then
        echo "[state] env_sh_defaults: runtime_render '$runtime_render' is not true/false; seeding false" >&2
        runtime_render="false"
    fi
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
export LITCLOCK_CONTENT_MODE=quotes
export LITCLOCK_FORTUNE_CATEGORIES=
export LITCLOCK_FORTUNE_DWELL_MINUTES=1
export GIFT_MODE_MESSAGE=
export LITCLOCK_RUNTIME_RENDER=$runtime_render
# export DISPLAY_CLEAR_HOUR=2
# export LITCLOCK_RENDER_LEAD_S=4
# export LITCLOCK_PARTIAL_REFRESH=true
# export WEATHER_API_TIMEOUT=15
# export LOG_LEVEL=WARNING
EOF
}

# env_sh_current_runtime_render FILE — print `true` if FILE turns runtime text
# rendering on for the painter, else `false`. Always exits 0.
#
# reset-setup.sh and prepare-for-cloning.sh call it on the OLD env.sh just
# before replacing it, and hand the answer to env_sh_defaults so the next owner
# (or every clone of this card) keeps the tier this device had already earned.
#
# What the painter sees: runtheclock.sh does `source ./env.sh` with no
# `set -a`, so only an EXPORTED assignment reaches it, and literary_clock.py
# accepts `1`/`true` in any case. This is NOT a `source`: reset-setup.sh runs as
# root and must not execute a pi-writable file. So instead of evaluating shell,
# it answers `true` only for a file whose shape leaves no room for doubt, and
# `false` for everything else (the value these callers seeded before it existed,
# which costs at most a week on PNGs until Stage B migrates the device):
#
#   * EVERY active line is a comment, blank, or one flat `[export ]NAME=VALUE`
#     with NAME in the key families the writers use (WEATHER_*, LITCLOCK_*, ...)
#     (spaces and tabs only, so a CRLF line, which the painter reads as
#     `true\r`, is not one) whose VALUE is built only from the shapes the
#     writers emit: unquoted safe characters, '...' segments, and "..."
#     segments without $ ` or \ (what config.py's shlex.quote and the
#     seeders produce). An `if`, a heredoc, an
#     `unset`, `export -n`, `readonly`, `declare`, `return`, `eval`, a `;` or a
#     trailing comment is not such a line, and makes the answer `false`;
#   * exactly ONE active line mentions the variable at all, and it is
#     `export LITCLOCK_RUNTIME_RENDER=` true/1, optionally quoted.
# (litclock-dev PR litclock-dev#929 review: a line counter alone said `true` for a CRLF
# file, an `unset` after the export, or an assignment inside a heredoc.)
env_sh_current_runtime_render() {
    local file="${1-}" body
    local var='LITCLOCK_RUNTIME_RENDER'
    local value="([A-Za-z0-9_@%+=:,./-]|'[^']*'|\"[^\"\$\`\\\\]*\")*"
    # Only the key families the writers emit (env.sh.sample, config.py, the
    # seeders). Any other name may be one bash or Python treats specially
    # (POSIXLY_CORRECT + a readonly UID ends the source early; PYTHONIOENCODING
    # kills the painter at startup: PR litclock-dev#929 re-review), so it reads false.
    local name='(WEATHER|LITCLOCK|OPENWEATHERMAP|ALLOW|SHOW|GIFT|DISPLAY|LOG|GH|GITHUB)_[A-Z0-9_]*'
    local flat="^[ 	]*(#.*)?\$|^[ 	]*(export[ 	]+)?${name}=${value}[ 	]*\$"
    local mention="(^|[^A-Za-z0-9_])${var}([^A-Za-z0-9_]|\$)"
    local on="^[ 	]*export[ 	]+${var}=([Tt][Rr][Uu][Ee]|1|\"([Tt][Rr][Uu][Ee]|1)\"|'([Tt][Rr][Uu][Ee]|1)')[ 	]*\$"
    if [[ -z "$file" || ! -f "$file" || ! -r "$file" ]]; then
        echo false
        return 0
    fi
    body=$(cat -- "$file" 2>/dev/null) || { echo false; return 0; }
    if printf '%s\n' "$body" | grep -vqE "$flat" \
        || [[ "$(printf '%s\n' "$body" | grep -vE '^[ 	]*(#|$)' | grep -cE "$mention" || true)" != "1" ]] \
        || ! printf '%s\n' "$body" | grep -qE "$on"; then
        echo false
    else
        echo true
    fi
    return 0
}

# env_staging_files DIR ARRAY — fill the caller's ARRAY (named, not printed)
# with every abandoned staging copy of DIR/env.sh. Each is a full unredacted env.sh (API key, coordinates, city):
# a writer died between staging and rename (SIGKILL, power loss — and since
# litclock-dev#893 the staged bytes are fsynced, so a power-loss orphan comes
# back FULL rather than mostly empty). Three shapes, one per writer:
#   env.sh.XXXXXX     the shell writers' `mktemp "${dest}.XXXXXX"`
#   env.sh.tmp.<pid>  their fallback when mktemp is unavailable
#   .env.sh.tmp.*     src/config.py's `tempfile.mkstemp(prefix=".env.sh.tmp.")`
# The third was missed by the litclock-dev#871 sweep, which matched only the
# first. env.sh.sample is six characters too and is tracked, so it is excluded
# by name. The single source for the gift wipe (reset-setup.sh) and the clone
# gate (prepare-for-cloning.sh), so a fourth writer is added in one place.
#
# It fills an array in the CALLER's shell instead of printing into a pipe or a
# `< <(...)`: a failure inside a process substitution is invisible to the
# reader, and the first version of this helper died there under the clone
# gate's `set -e` — on `shopt -p`, which exits 1 whenever the option is OFF —
# so the gate read an empty list and certified a card carrying the previous
# owner's key (litclock-dev#893 review, Codex; reproduced). Here a failure is the caller's.
env_staging_files() {
    local dir="$1" f restore
    local -n _env_staging_out="$2"
    local -A seen=()
    _env_staging_out=()
    restore=$(shopt -p nullglob) || true   # exit 1 just means "currently off"
    shopt -s nullglob
    for f in "$dir"/env.sh.?????? "$dir"/env.sh.tmp.* "$dir"/.env.sh.tmp.*; do
        [[ "$(basename "$f")" == "env.sh.sample" ]] && continue
        # `env.sh.tmp.NN` (a two-digit pid) matches the first glob too.
        [[ -n "${seen[$f]:-}" ]] && continue
        seen[$f]=1
        _env_staging_out+=("$f")
    done
    eval "$restore"
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
#   1   any other failure (mktemp, printf, the flush of the staged file,
#       mv) — destination untouched.
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
        rc=$?
        [[ $rc -ne 0 ]] && rm -f "$tmp" 2>/dev/null
        return $rc
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
# Atomic is not DURABLE (litclock-dev#893): the staged bytes are fsynced
# before the rename and the directory after it — see _sync_staged/_sync_dir.
# The file sync runs FIRST, while this user still owns the staging file; after
# a chown to a 0600 root:root destination's owner a pi caller could no longer
# open it to sync it. A failed file sync aborts with the destination untouched,
# as Python's raising fsync does; a failed directory sync warns and succeeds.
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
    # Flush FIRST — see the barrier notes above _sync_staged — and before the
    # symlink guard, so the guard stays adjacent to the `stat` it protects
    # (an fsync between them would widen that check-to-use gap to an SD-card
    # write, litclock-dev#893 review).
    if ! _sync_staged "$tmp"; then
        echo "[ERROR] atomic_write_env_sh: could not flush $tmp to disk; leaving $dest untouched" >&2
        return 1
    fi
    if [[ -L "$dest" ]]; then
        echo "[ERROR] atomic_write_env_sh: refusing to overwrite symlink at $dest" >&2
        return 1
    fi
    if [[ -e "$dest" ]]; then
        owner=$(stat -c '%U:%G' "$dest" 2>/dev/null) || owner=""
        mode=$(stat -c '%a' "$dest" 2>/dev/null) || mode=""
        # MODE FIRST, then ownership. The other order installs an unreadable
        # env.sh whenever the destination is root-owned and this runs as pi: the
        # mktemp staging file is pi-owned 0600, `sudo chown root:root` succeeds,
        # the subsequent unprivileged `chmod 644` then FAILS on a file pi no
        # longer owns, and the rename replaces a world-readable config with a
        # root-owned 0600 one that every pi-user service — the painter, the
        # control server — can no longer read. Both failures are swallowed, so
        # the caller reports success (review of litclock-dev#871 Stage B, which
        # is the first path to exercise this helper from update.sh).
        #
        # Setting the mode while pi still owns the temp file is the case that
        # matters and it succeeds there; it can still fail (a read-only mount,
        # an exotic ACL), which is why the sudo fallback and the `|| true` stay.
        # If the chown then fails the file lands pi:pi at whatever mode was set
        # — readable, degraded, not broken. That is the safe direction, and it
        # is why these stay best-effort rather than becoming a hard abort.
        # `$mode` is the DESTINATION's mode, not a constant 0644: a device whose
        # env.sh is 0600 keeps 0600.
        if [[ -n "$mode" ]]; then
            chmod "$mode" "$tmp" 2>/dev/null || sudo chmod "$mode" "$tmp" 2>/dev/null || true
        fi
        if [[ -n "$owner" ]]; then
            chown "$owner" "$tmp" 2>/dev/null \
                || sudo chown "$owner" "$tmp" 2>/dev/null \
                || true
        fi
    else
        # First-boot path: mktemp staged the file at 0600. env.sh must be
        # world-readable so the pi-user `source env.sh` in runtheclock.sh
        # works after a fresh install.
        chmod 0644 "$tmp" 2>/dev/null || true
    fi
    mv -f "$tmp" "$dest" 2>/dev/null || return 1
    _sync_dir "$(dirname "$dest")"
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
