#!/bin/bash
#
# Cut a LitClock release: promote the CHANGELOG heading, commit, tag — and stop.
#
# Usage: scripts/cut-release.sh vX.Y.Z [--expect-sha <sha>] [-m <summary>] [--yes]
#
# litclock-dev#687.
#
# WHY THIS EXISTS, given that CI already gates the CHANGELOG (litclock-dev#681):
#
# The gate in build-image.yml fails a tag build when CHANGELOG.md has no section
# for the tag. It is worth keeping, but it cannot prevent the defect it was
# written for:
#
#   1. It fires AFTER the tag is public. The PWA resolves updates from /tags, so
#      the blank update card reaches the fleet at `git push --tags` — the very
#      event that triggers the workflow.
#   2. The OTA path never touches the image asset. scripts/update.sh applies an
#      update with `git fetch --tags` + `git reset --hard <tag>`; it does not
#      download the .img. Failing the build stops the FRESH-FLASH artifact and
#      has no bearing on the OTA path at all.
#
# Together: the gate protects flashing, not updating. The blank card was an
# updating problem. So the fix has to move earlier — make the mistake unmakeable
# rather than merely detectable, by putting the promotion and the tag in one
# step that cannot do the second without the first.
#
# WHAT THIS DELIBERATELY DOES NOT DO: push. The final block prints the push
# command instead of running it. Pushing from inside here would reintroduce the
# "irreversible the instant it runs" property this exists to remove — a human
# beat has to remain between cutting a release and the fleet seeing it.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHANGELOG="$REPO_ROOT/CHANGELOG.md"
CHECKER="$REPO_ROOT/scripts/check-changelog-section.py"

die() {
    echo -e "${RED}error:${NC} $*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: $0 vX.Y.Z [options]

Promotes '## [Unreleased]' in CHANGELOG.md to the release heading, commits that,
and creates the tag. It does NOT push — the push command is printed for you.

Options:
  --expect-sha <sha>  Refuse unless HEAD is this commit. This is how you state
                      which commit you validated; without it you are asked to
                      confirm HEAD interactively.
  -m, --message <s>   Summary appended to the release commit subject, matching
                      the existing convention:
                        docs(changelog): v0.222.0 — settings QR quiet-zone fix
  --yes               Skip the interactive HEAD confirmation (for automation).
  --allow-version-jump
                      Permit a version that is not the immediate successor of the
                      highest existing release. Both the updater and the PWA pick
                      the HIGHEST semver, so a tag cut too high permanently hides
                      every release below it.
  -h, --help          This text.
EOF
}

TAG=""
EXPECT_SHA=""
SUMMARY=""
ASSUME_YES=false
ALLOW_VERSION_JUMP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --expect-sha)
            [[ $# -ge 2 ]] || die "--expect-sha needs a value"
            EXPECT_SHA="$2"; shift 2 ;;
        -m|--message)
            [[ $# -ge 2 ]] || die "$1 needs a value"
            SUMMARY="$2"; shift 2 ;;
        --yes) ASSUME_YES=true; shift ;;
        --allow-version-jump) ALLOW_VERSION_JUMP=true; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *)
            [[ -z "$TAG" ]] || die "expected exactly one tag, also got: $1"
            TAG="$1"; shift ;;
    esac
done

[[ -n "$TAG" ]] || { usage >&2; exit 1; }

# ---------------------------------------------------------------- preflight

# Release-shaped tags only, matching scripts/lib/github_api.sh's filter and the
# CI gate's. An RC or QA tag is never offered to the fleet, so it has no
# CHANGELOG section to promote and must not consume [Unreleased].
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "'$TAG' is not a release-shaped tag (vMAJOR.MINOR.PATCH).
Only releases the updater offers are cut with this script."

cd "$REPO_ROOT"

git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository: $REPO_ROOT"

[[ -f "$CHANGELOG" ]] || die "no CHANGELOG.md at $CHANGELOG"
[[ -f "$CHECKER" ]] || die "no scripts/check-changelog-section.py — the release gate must run here"

# The promotion is written with `cp`, which follows a symlink at the
# destination. A CHANGELOG.md replaced by a symlink (a PR can do that) would
# have the release heading written THROUGH it, into a file outside the
# repository, and the staged-path guard below would then refuse to commit --
# after the out-of-repo write had already happened (/review, security pass).
[[ ! -L "$CHANGELOG" ]] || die "CHANGELOG.md is a symlink to $(readlink "$CHANGELOG").
Refusing to write the promotion through it."

# `timeout` bounds the network probe below. Absent (a stripped container), run
# unwrapped rather than skipping the probe entirely.
if command -v timeout >/dev/null 2>&1; then
    _bounded() { timeout "$@"; }
else
    _bounded() { shift; "$@"; }
fi

# A dirty tree means the release commit could carry work that was never
# reviewed, and the tag would then not be the tree anyone validated.
if [[ -n "$(git status --porcelain)" ]]; then
    die "working tree is dirty. Commit or stash first — a release commit must
contain the CHANGELOG promotion and nothing else:

$(git status --short)"
fi

# A release commit on a detached HEAD is orphaned, and `git push origin <branch>`
# from there silently no-ops (it pushes nothing and still exits 0), so the tag
# would point at a commit no branch contains.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$BRANCH" != "HEAD" ]] || die "HEAD is detached. Check out the release branch first —
a tag cut here would point at a commit no branch contains."

# Do NOT move an existing tag. The methodology is build-once-promote /
# validate-first-tag-last and the fleet resolves updates from /tags: moving a
# tag changes what already-updated devices fetched.
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    die "tag $TAG already exists locally, pointing at $(git rev-parse --short "$TAG^{commit}").
Tags are never moved — the fleet resolves updates from /tags, so moving one
changes what devices fetch. Pick the next version."
fi

# Advisory only: the local check above catches the ordinary case, and `git push`
# itself refuses to overwrite an existing remote tag. Being offline must not
# block cutting a release, but a silent skip would read as "checked, clean".
# WHICH remote the fleet actually resolves from. `origin` is not it in every
# clone: this repository's origin is the DEV repo, while update.sh and
# update_state.py resolve releases from the PUBLIC one. Probing only origin
# meant the fleet-safety guard consulted a tag list that does not contain the
# fleet's tags at all -- re-cutting a version that has been live for days passed
# both the local and the remote check (/review, red team; demonstrated against
# the real remotes). Resolve the namespace from the same constants the device
# uses, and probe every remote that could be it.
UPDATE_STATE="$REPO_ROOT/src/control_server/update_state.py"
FLEET_OWNER=""
FLEET_REPO=""
if [[ -f "$UPDATE_STATE" ]]; then
    # Anchored on the whole constant name: a bare `DEFAULT_OWNER[^=]*=` also
    # matches DEFAULT_OWNER_BACKUP and would silently resolve the wrong
    # namespace (/review, Codex).
    # No `| head -1`: the same pipefail+SIGPIPE trap as the bullet check below.
    # Take the first line in bash instead, where there is no pipeline to fail.
    FLEET_OWNER="$(sed -n 's/^DEFAULT_OWNER\([[:space:]]*:[^=]*\)\?[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\2/p' "$UPDATE_STATE")"
    FLEET_OWNER="${FLEET_OWNER%%$'\n'*}"
    FLEET_REPO="$(sed -n 's/^DEFAULT_REPO\([[:space:]]*:[^=]*\)\?[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\2/p' "$UPDATE_STATE")"
    FLEET_REPO="${FLEET_REPO%%$'\n'*}"
fi

# Identity, not a suffix glob. `*"$OWNER/$REPO"` has no boundary before the
# owner and no host check, so it matched https://github.com.evil.test/o/r,
# https://example.test/archive/o/r and notkapoorankush/litclock, while missing
# a trailing slash or a differently-cased owner (/review, Codex). Compare the
# host and the last two path components, case-insensitively.
_remote_is_fleet() {
    local url="$1" host path owner repo
    url="${url%/}"
    shopt -s nocasematch
    [[ "$url" != *.git ]] || url="${url%.*}"
    url="${url%/}"
    shopt -u nocasematch

    if [[ "$url" == *"://"* ]]; then
        host="${url#*://}"; host="${host%%/*}"; host="${host##*@}"; host="${host%%:*}"
        path="${url#*://}"; path="${path#*/}"
    elif [[ "$url" == *"@"*":"* ]]; then          # scp syntax: git@host:owner/repo
        host="${url#*@}"; host="${host%%:*}"
        path="${url#*:}"
    else
        return 1                                   # a local path is never the fleet
    fi

    repo="${path##*/}"
    owner="${path%/*}"; owner="${owner##*/}"

    [[ "${host,,}" == "github.com" ]] || return 1
    [[ "${owner,,}" == "${FLEET_OWNER,,}" ]] || return 1
    [[ "${repo,,}" == "${FLEET_REPO,,}" ]] || return 1
    return 0
}

FLEET_REMOTE=""
if [[ -n "$FLEET_OWNER" && -n "$FLEET_REPO" ]]; then
    # `git remote -v` prints the url.<x>.insteadOf-REWRITTEN url; the configured
    # identity is what we are matching on, so read it directly.
    while read -r rname; do
        [[ -n "$rname" ]] || continue
        if _remote_is_fleet "$(git config --get "remote.$rname.url" || true)"; then
            FLEET_REMOTE="$rname"
            break
        fi
    done < <(git remote)
fi

# `cmd && arr+=(x)` as a top-level statement returns non-zero when cmd fails,
# which under `set -e` exits the script -- in a clone with no origin, which is
# every test fixture. Use an if.
PROBE_REMOTES=()
if git remote get-url origin >/dev/null 2>&1; then
    PROBE_REMOTES+=("origin")
fi
if [[ -n "$FLEET_REMOTE" && "$FLEET_REMOTE" != "origin" ]]; then
    PROBE_REMOTES+=("$FLEET_REMOTE")
fi

# Bounded: an unroutable-but-answering remote (VPN up, GitHub blackholed) blocks
# on TCP connect for the kernel's full retry window -- measured still running at
# 45s, against a comment that promises being offline must not block a release
# (/review, performance pass). timeout's rc=124 is neither 0 nor 2, so it lands
# in the warning branch with no other change.
REMOTE_TAGS=""
FLEET_TAGS_SEEN=false
for remote in ${PROBE_REMOTES[@]+"${PROBE_REMOTES[@]}"}; do
    set +e
    _bounded 10 git ls-remote --exit-code --tags "$remote" "refs/tags/$TAG" >/dev/null 2>&1
    ls_remote_rc=$?
    set -e
    if [[ $ls_remote_rc -eq 0 ]]; then
        die "tag $TAG already exists on '$remote' ($(git remote get-url "$remote")).
Tags are never moved — the fleet resolves updates from /tags, so moving one
changes what devices fetch. Pick the next version."
    elif [[ $ls_remote_rc -ne 2 ]]; then
        # 2 is --exit-code's "no matching ref", i.e. the clean case. Anything
        # else (no network, auth) means we did not actually check.
        echo -e "${YELLOW}warning:${NC} could not reach '$remote' to check whether $TAG already exists (rc=$ls_remote_rc)." >&2
        echo -e "         The local check passed and the push will still refuse to move a remote tag." >&2
        continue
    fi
    set +e
    remote_tag_list="$(_bounded 10 git ls-remote --tags "$remote" 2>/dev/null)"
    tag_list_rc=$?
    set -e
    if [[ $tag_list_rc -ne 0 ]]; then
        # The single-ref probe above succeeded, so this is a partial read, not
        # an unreachable remote. Marking the fleet's tags "seen" here would let
        # the version-order check below report a clean pass on data it never
        # got (/review, Codex).
        echo -e "${YELLOW}warning:${NC} could not list tags on '$remote' (rc=$tag_list_rc); version-order check is running on partial data." >&2
        continue
    fi
    REMOTE_TAGS="$REMOTE_TAGS
$remote_tag_list"
    [[ "$remote" != "${FLEET_REMOTE:-}" ]] || FLEET_TAGS_SEEN=true
done

if [[ -z "$FLEET_REMOTE" ]]; then
    echo -e "${YELLOW}warning:${NC} no configured remote matches the namespace the fleet resolves from" >&2
    echo -e "         (${FLEET_OWNER:-?}/${FLEET_REPO:-?}, per src/control_server/update_state.py)." >&2
    echo -e "         The duplicate-tag and version-order checks below cannot see the fleet's tags." >&2
elif [[ "$FLEET_TAGS_SEEN" != true ]]; then
    echo -e "${YELLOW}warning:${NC} could not read tags from '$FLEET_REMOTE', the remote the fleet resolves from." >&2
    echo -e "         The version-order check below is running against local tags only." >&2
fi

# --------------------------------------------- the version must be the NEXT one

# Existence is not enough. Both resolvers pick the HIGHEST semver, not the newest
# tag (github_api.sh and update_state.py both sort(reverse=True) and take [0]),
# so one typo'd high tag -- v0.324.0 for v0.234.0 -- buries every subsequent real
# release from the entire fleet, permanently, and the doctrine three checks above
# forbids the only remedy (/review, red team; demonstrated). Refuse anything that
# is not the immediate successor.
all_release_tags="$(
    {
        git tag -l 'v*'
        printf '%s\n' "$REMOTE_TAGS" | sed -n 's#.*refs/tags/\(v[^^]*\)$#\1#p'
    } | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -u -V || true
)"
HIGHEST_TAG="$(printf '%s\n' "$all_release_tags" | tail -1)"

if [[ -n "$HIGHEST_TAG" ]]; then
    IFS=. read -r hi_major hi_minor hi_patch <<<"${HIGHEST_TAG#v}"
    IFS=. read -r new_major new_minor new_patch <<<"${TAG#v}"
    # Force base 10. The tag regex permits a leading zero, and bash reads 08 as
    # octal -- "value too great for base", a raw interpreter error rather than
    # a die, on a repo carrying a v0.08.0 tag (/review, Codex). The fleet's
    # resolvers use Python int(), which has no such quirk.
    hi_major=$((10#$hi_major)); hi_minor=$((10#$hi_minor)); hi_patch=$((10#$hi_patch))
    new_major=$((10#$new_major)); new_minor=$((10#$new_minor)); new_patch=$((10#$new_patch))
    successors=(
        "v$((hi_major + 1)).0.0"
        "v${hi_major}.$((hi_minor + 1)).0"
        "v${hi_major}.${hi_minor}.$((hi_patch + 1))"
    )
    is_successor=false
    for candidate in "${successors[@]}"; do
        [[ "$TAG" != "$candidate" ]] || { is_successor=true; break; }
    done

    if [[ "$is_successor" != true && "$ALLOW_VERSION_JUMP" == true ]]; then
        # Say what is being skipped. An override is only informed if the check
        # it overrides actually had its input (/review, Codex).
        echo -e "${YELLOW}warning:${NC} --allow-version-jump: cutting $TAG, which skips past $HIGHEST_TAG." >&2
        if [[ -z "$FLEET_REMOTE" || "$FLEET_TAGS_SEEN" != true ]]; then
            echo -e "         The fleet's own tags could NOT be read, so $HIGHEST_TAG may not be the real highest." >&2
        fi
    fi

    if [[ "$is_successor" != true && "$ALLOW_VERSION_JUMP" != true ]]; then
        if [[ "$new_major" -lt "$hi_major" ]] \
           || { [[ "$new_major" -eq "$hi_major" ]] && [[ "$new_minor" -lt "$hi_minor" ]]; } \
           || { [[ "$new_major" -eq "$hi_major" ]] && [[ "$new_minor" -eq "$hi_minor" ]] && [[ "$new_patch" -le "$hi_patch" ]]; }; then
            die "$TAG is not ahead of $HIGHEST_TAG, the highest release that exists.
Both the updater and the PWA pick the HIGHEST semver, not the newest tag, so this
would never be offered to anyone. Expected one of: ${successors[*]}"
        fi
        die "$TAG skips past $HIGHEST_TAG. Expected one of: ${successors[*]}

Both the updater and the PWA pick the HIGHEST semver, not the newest tag
(github_api.sh and update_state.py both sort and take the top), so a version
cut too high permanently hides every release numbered below it — and tags are
never moved, so there is no way back. If the jump is deliberate, re-run with
--allow-version-jump."
    fi
fi

ORIGIN_URL="$(git remote get-url origin 2>/dev/null || echo '(no origin configured)')"
REMOTE_CHECKED="${PROBE_REMOTES[*]:-(none)}"

HEAD_SHA="$(git rev-parse HEAD)"
HEAD_SHORT="$(git rev-parse --short HEAD)"

if [[ -n "$EXPECT_SHA" ]]; then
    # An object id, not any revision expression. `--expect-sha HEAD` resolves to
    # HEAD by definition, so it can never fail -- and it takes this branch, which
    # also skips the interactive confirmation. A guard that reads in shell
    # history as "I pinned the commit" while checking nothing is worse than no
    # guard (/review, security pass).
    [[ "$EXPECT_SHA" =~ ^[0-9a-fA-F]{7,40}$ ]] || die "--expect-sha takes a commit sha, not a ref name ('$EXPECT_SHA').
Naming a ref would let it resolve to whatever that ref points at, including HEAD itself."
    resolved="$(git rev-parse -q --verify "${EXPECT_SHA}^{commit}" 2>/dev/null)" \
        || die "--expect-sha '$EXPECT_SHA' does not resolve to a commit in this repository"
    # The shape test above is a test on the STRING. A ref can be named in hex --
    # `git branch deadbeef` is legal -- and would then resolve to whatever it
    # points at, pinning nothing (/review, red team; demonstrated). An object id
    # is a prefix of the object it names; a ref name is not.
    [[ "${resolved,,}" == "${EXPECT_SHA,,}"* ]] || die "--expect-sha '$EXPECT_SHA' is a ref name, not an object id.
It resolves to $resolved, which does not begin with it — so it would pin whatever
that ref happens to point at rather than the commit you validated."
    [[ "$resolved" == "$HEAD_SHA" ]] || die "HEAD is not the commit you named.

  --expect-sha  $resolved  $(git log -1 --format=%s "$resolved")
  HEAD          $HEAD_SHA  $(git log -1 --format=%s HEAD)

Check out the validated commit before cutting the release."
elif [[ "$ASSUME_YES" != true ]]; then
    echo -e "${BLUE}About to cut ${TAG} from:${NC}"
    echo "  $HEAD_SHORT  $(git log -1 --format=%s HEAD)   [$BRANCH]"
    echo
    # Under `set -e` a bare `read` that hits EOF exits the script immediately and
    # the die below never runs -- so a non-tty caller got a bare exit 1 with no
    # message at all, on the DEFAULT path (/review, maintainability pass).
    if ! read -r -p "Is that the commit you validated? (y/N) " reply; then
        die "no answer on stdin. Pass --yes or --expect-sha <sha> for non-interactive use."
    fi
    [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]] || die "aborted — nothing was changed."
fi

# ------------------------------------------------- the [Unreleased] section

# Anchor on the heading, but verify the span before splicing on it: a second
# [Unreleased] heading (from a bad merge) would make "the first one" the wrong
# one, and the promotion would silently release someone else's notes.
unreleased_lines="$(grep -n '^## \[Unreleased\][[:space:]]*$' "$CHANGELOG" | cut -d: -f1 || true)"
unreleased_count="$(printf '%s' "$unreleased_lines" | grep -c . || true)"

[[ "$unreleased_count" -ne 0 ]] || die "CHANGELOG.md has no '## [Unreleased]' heading to promote.
Was this release already cut?"
[[ "$unreleased_count" -eq 1 ]] || die "CHANGELOG.md has $unreleased_count '## [Unreleased]' headings (lines: $(echo "$unreleased_lines" | tr '\n' ' ')).
Fix that first — promoting the wrong one would release the wrong notes."

# Releasing nothing is the other half of the same mistake the blank update card
# was: a bare '### Changed' with nothing under it renders as a category heading
# and nothing else on the card, so headings alone do not count as entries.
#
# This rule is deliberately STRICTER than check-changelog-section.py's, in both
# directions, and the gate below stays authoritative. The gate accepts any line
# whose lstrip() starts with '-' or '*' (so '-nospace' and a bare '- ' both
# pass) but only sees the first 10 non-empty lines the extractor returns; this
# scans the whole section and requires a marker followed by actual content.
# Where they disagree the gate runs second and the promotion is rolled back, so
# the divergence fails closed (/review, maintainability + Codex).
unreleased_body="$(awk '
    /^## \[Unreleased\][[:space:]]*$/ { inside = 1; next }
    inside && /^## / { exit }
    inside { print }
' "$CHANGELOG")"

# A HERESTRING, not a pipe. `grep -q` exits at the first match and closes the
# pipe; the producer then takes SIGPIPE, and under `set -o pipefail` the
# PIPELINE reports 141 even though the match was found. The failure is
# size-dependent — on a small body the producer finishes before grep exits, so
# every test fixture passed while the REAL CHANGELOG, whose entries are long
# enough to fill the 64 KiB pipe buffer, reported "no entries" and blocked the
# release. Caught by running this script against the actual repo rather than
# against a fixture.
if ! grep -qE '^[[:space:]]*[-*][[:space:]]+[^[:space:]]' <<<"$unreleased_body"; then
    die "'## [Unreleased]' has no entries — there is nothing to release.

What is under it now:
$(head -20 <<<"$unreleased_body" | sed 's/^/    /')"
fi

# ------------------------------------------------------------- the promotion

DATE="$(date +%Y-%m-%d)"

# Rename the heading and open a fresh empty [Unreleased] above it, so the next
# change has somewhere to land and the file is never left without one.
tmp="$(mktemp)"
# Snapshot the original bytes rather than relying on `git checkout --` to undo
# the promotion. That command needs the index lock, so in the one failure mode
# where the lock is the problem the restore silently did nothing and the
# operator was told the file had been restored (/review, red team). A plain copy
# always works.
changelog_backup="$(mktemp)"
cp "$CHANGELOG" "$changelog_backup"
trap 'rm -f "$tmp" "$changelog_backup"' EXIT

restore_changelog() {
    cp "$changelog_backup" "$CHANGELOG"
}
awk -v tag="$TAG" -v date="$DATE" '
    !promoted && /^## \[Unreleased\][[:space:]]*$/ {
        print "## [Unreleased]"
        print ""
        print "## [" tag "] - " date
        promoted = 1
        next
    }
    { print }
' "$CHANGELOG" > "$tmp"
cp "$tmp" "$CHANGELOG"

# Run the CI gate against the result. The script and the gate agreeing is the
# whole point: this is the same check build-image.yml runs on the tag push, so
# a release that passes here cannot fail there for this reason.
if ! python3 "$CHECKER" "$TAG" --changelog "$CHANGELOG"; then
    restore_changelog
    die "the promoted CHANGELOG does not satisfy scripts/check-changelog-section.py.
CHANGELOG.md has been restored; nothing was committed or tagged."
fi

# ------------------------------------------------------- commit, tag, stop

# Undo everything this run created, back to the preflight state.
#
# The identity check is deliberately exact: OUR_COMMIT is the sha our own
# `git commit` produced, and nothing else is ever reset away. An earlier version
# accepted "any commit whose parent is the preflight sha", which a concurrent
# writer in the same worktree satisfies -- and `git reset --hard` then discarded
# a stranger's commit and its files while the script printed "restored"
# (/review, red team; demonstrated). This project has a recorded incident of
# exactly that concurrency (review subagents sharing one worktree), so it is not
# a theoretical actor.
OUR_COMMIT=""
rollback_to_preflight() {
    local head_now
    head_now="$(git rev-parse HEAD)"
    if [[ "$head_now" == "$HEAD_SHA" ]]; then
        # Nothing of ours is committed yet: only the index and worktree to undo.
        git reset -q --hard "$HEAD_SHA"
        return 0
    fi
    if [[ -n "$OUR_COMMIT" && "$head_now" == "$OUR_COMMIT" ]]; then
        git reset -q --hard "$HEAD_SHA"
        return 0
    fi
    return 1
}

# What to say when we could not undo. Never claim a restoration that did not
# happen: the whole point of the rollback is that the operator is not left with
# [Unreleased] consumed and no tag, so reporting that state as "restored" is
# worse than reporting the failure.
die_unrolled_back() {
    die "$1

Could NOT roll back automatically -- HEAD is $(git rev-parse --short HEAD), which is
neither the commit this run started from ($HEAD_SHORT) nor the commit it made.
Something else changed this repository while the script was running.

CHANGELOG.md may still be promoted, and if a release commit landed, a re-run will
refuse with \"has no entries\". Inspect 'git log --oneline -5' and 'git status'
before doing anything -- do NOT blindly reset, another process's work may be here."
}

# Everything above was checked against a snapshot taken before the confirmation
# prompt, which an operator can sit at indefinitely. Anything that moves HEAD in
# that window -- a concurrent checkout, another tool in the same worktree -- and
# the release commit and tag land on a commit nobody validated, while the
# summary below still prints the OLD sha as the contents. Re-assert against live
# state at the last possible moment (/review, security pass; demonstrated).
[[ "$(git rev-parse HEAD)" == "$HEAD_SHA" ]] || die "HEAD moved while this script was running
(was $HEAD_SHORT, now $(git rev-parse --short HEAD)). Nothing was committed or tagged;
CHANGELOG.md is promoted in the working tree -- 'git checkout -- CHANGELOG.md' to restore."
[[ "$(git rev-parse --abbrev-ref HEAD)" == "$BRANCH" ]] || die "the checked-out branch changed while this script was running
(was $BRANCH, now $(git rev-parse --abbrev-ref HEAD)). Nothing was committed or tagged."

# The one unguarded mutation in this sequence until now: a held .git/index.lock
# made this exit 128 with git's raw error and no rollback, leaving a promoted
# CHANGELOG whose obvious remedy ("commit or stash first") creates the very trap
# below (/review, red team; demonstrated).
if ! git add -- "$CHANGELOG"; then
    restore_changelog
    die "git add failed (another git process holding .git/index.lock?).
CHANGELOG.md has been restored; nothing was committed or tagged."
fi

# Compare against the path git actually reports. In a checkout nested inside a
# larger repository, --name-only emits a prefixed path and a literal
# "CHANGELOG.md" comparison would fire a false alarm that reads as corruption.
staged="$(git diff --cached --name-only)"
expected_path="$(git rev-parse --show-prefix)CHANGELOG.md"
if [[ "$staged" != "$expected_path" ]]; then
    git reset -q -- "$CHANGELOG" 2>/dev/null || true
    restore_changelog
    die "the release commit would have touched something other than $expected_path.
Staged instead: ${staged:-(nothing)}
Nothing was committed; the index and CHANGELOG.md have been restored."
fi

subject="docs(changelog): $TAG"
[[ -z "$SUMMARY" ]] || subject="$subject — $SUMMARY"

# A failure from here on used to leave half-state with no way back: a promoted
# CHANGELOG staged but uncommitted, or -- worse -- a release commit with
# [Unreleased] consumed and NO tag, which makes the obvious retry die with
# "has no entries" (/review, testing pass + Codex; reproduced with tag.gpgSign
# and with an absent git identity). Both arms now roll all the way back.
if ! git commit -q -m "$subject"; then
    if rollback_to_preflight; then
        die "git commit failed (a hook, or no git identity configured?).
Nothing was released; CHANGELOG.md and the index have been restored."
    fi
    die_unrolled_back "git commit failed (a hook, or no git identity configured?)."
fi

OUR_COMMIT="$(git rev-parse HEAD)"
RELEASE_SHA="$(git rev-parse --short HEAD)"

if ! git tag -a "$TAG" -m "LitClock $TAG"; then
    if rollback_to_preflight; then
        die "git tag failed (signing configured without a usable key?).
The release commit was rolled back and CHANGELOG.md restored. Nothing was released."
    fi
    die_unrolled_back "git tag failed AFTER the release commit landed."
fi

echo
echo -e "${GREEN}Cut $TAG.${NC}"
echo
echo "  release commit  $RELEASE_SHA  $subject"
echo "  tag             $TAG -> $RELEASE_SHA"
echo "  contents        $HEAD_SHORT plus this CHANGELOG-only commit"
echo "  remotes checked $REMOTE_CHECKED  ($ORIGIN_URL)"
echo
echo -e "${YELLOW}Not pushed.${NC} Nothing is visible to the fleet until you run:"
echo
# --atomic so the branch cannot land without the tag, and the refs go through
# %q because this line gets copy-pasted into a shell: a branch name may legally
# contain ';' and friends. Wrapping in literal single quotes is NOT enough --
# a branch name containing a single quote breaks straight back out of them
# (/review, Codex). %q escapes for exactly this.
printf "  git push --atomic origin %q %q\n" "$BRANCH" "$TAG"
echo
echo "To undo before pushing:"
echo
printf "  git tag -d %q && git reset --hard %s\n" "$TAG" "$HEAD_SHORT"
echo
