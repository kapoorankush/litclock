# shellcheck shell=bash
#
# LitClock shared GitHub REST API helpers.
#
# Sourced by:
#   scripts/update.sh         (resolve_target_sha via github_api_latest_release_tag)
#   scripts/download_images.sh (all metadata + asset fetches via github_api_curl)
#
# Design goals:
#   * No dependence on `gh` CLI — stock Pi OS ships curl + python3, not gh.
#   * Graceful-offline — network/HTTP/parse failures exit 0 and emit an empty
#     stdout so callers can treat them as "nothing to do" without branching.
#   * Auth optional — honors GH_TOKEN, GITHUB_TOKEN, then ~/.git-credentials
#     for private-repo flows. Public repos work with no auth at all.
#   * Safe parsing — python3 json module, never shell string-munging.
#
# Environment overrides (test hooks):
#   LITCLOCK_API_BASE_URL       default https://api.github.com
#   LITCLOCK_GITHUB_API_TIMEOUT default 10 (seconds, curl --max-time)
#   LITCLOCK_GIT_CREDENTIALS    default ${HOME}/.git-credentials

# Build auth args exactly once per process. Resolution order:
#   1. $GH_TOKEN (explicit env)
#   2. $GITHUB_TOKEN (set automatically inside GitHub Actions)
#   3. ~/.git-credentials (the same file `git config credential.helper=store`
#      writes to — gives us a single source of truth shared with `git pull`)
# Empty array when none are set; fine for public-repo reads. The
# .git-credentials fallback exists so the timer-driven update path works
# under systemd's clean env (no ~/.profile sourcing) without needing an
# extra EnvironmentFile= and a duplicate token. Same token already
# satisfies `git pull` is reused for REST API calls.
_LITCLOCK_GITHUB_AUTH_ARGS_BUILT=0
_litclock_token_from_git_credentials() {
    local creds_file="${LITCLOCK_GIT_CREDENTIALS:-${HOME:-/home/pi}/.git-credentials}"
    [ -r "$creds_file" ] || return 1
    # Match the first https://user:token@github.com line. The credential
    # helper writes one URL per line; we only handle the github.com host
    # since that's the only host LitClock REST calls target.
    sed -nE 's|^https://[^:/[:space:]]+:([^@[:space:]]+)@github\.com(/.*)?$|\1|p' \
        "$creds_file" | head -1
}
github_api_auth_args() {
    if [ "${_LITCLOCK_GITHUB_AUTH_ARGS_BUILT:-0}" -eq 1 ]; then
        return 0
    fi
    LITCLOCK_GITHUB_AUTH_ARGS=()
    local token=""
    if [ -n "${GH_TOKEN:-}" ]; then
        token="$GH_TOKEN"
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        token="$GITHUB_TOKEN"
    else
        token=$(_litclock_token_from_git_credentials 2>/dev/null || true)
    fi
    if [ -n "$token" ]; then
        LITCLOCK_GITHUB_AUTH_ARGS=(-H "Authorization: Bearer ${token}")
    fi
    _LITCLOCK_GITHUB_AUTH_ARGS_BUILT=1
}

# github_api_curl <url> <accept-header> <out-file>
#
# Authenticated GET against the GitHub REST API. Returns curl's exit code
# so the caller can decide how to react to the three failure modes:
#   * 0                  — success
#   * 22 (from --fail)   — HTTP 4xx/5xx
#   * other              — network / DNS / timeout
# Uses `--max-time` so a dead connection doesn't wedge the caller.
github_api_curl() {
    local url="$1" accept="$2" out="$3"
    local timeout="${LITCLOCK_GITHUB_API_TIMEOUT:-10}"
    github_api_auth_args
    curl -fsSL --max-time "$timeout" \
        "${LITCLOCK_GITHUB_AUTH_ARGS[@]}" \
        -H "Accept: ${accept}" \
        -o "$out" \
        "$url"
}

# github_api_latest_release_tag <owner> <repo>
#
# Emits the highest-semver release tag (vMAJOR.MINOR.PATCH) on stdout.
#
# Resolves via /repos/{owner}/{repo}/tags rather than /releases/latest
# because of a long-standing GitHub bug: fine-grained PATs return 404 on
# /releases/* for *private* repos even when Contents:Read is granted.
# See https://github.com/orgs/community/discussions/49276 (open since
# March 2023) and https://github.com/orgs/community/discussions/162365.
# Empirically on the LitClock test Pi: /releases/latest 4-of-5 404s,
# /tags is reliable. Issue #247 has the full diagnostic.
#
# Trade-offs vs /releases/latest:
#   - Loses GitHub's automatic draft/prerelease filtering. Mitigated by
#     the strict ^v\d+\.\d+\.\d+$ regex (excludes -rc, -alpha, etc.) and
#     by LitClock's tag-push-creates-release workflow (drafts have no
#     tag pushed, so they cannot appear in /tags).
#   - Gains correctness on private installs without depending on GitHub
#     fixing their bug.
#
# Shift in semantics worth flagging: the gate is now the *tag push*, not
# the *Release publication*. /releases/latest required a non-draft Release
# object to exist; /tags fires the moment a vX.Y.Z tag exists in the repo,
# even before CI finishes or a Release object is created. Practical
# implication: do not push a stable-shaped vX.Y.Z tag speculatively. If
# you need a holding tag for build/QA before commit, use a non-release
# shape (vX.Y.Z-rcN, qa-*, dev-*) — the regex excludes those by design.
#
# Validates the tag against a strict whitelist (letters, digits, _ . + -)
# as defense-in-depth so downstream git commands never see shell metachars
# even if the regex is ever loosened.
#
# Graceful-offline: any failure (network, HTTP, JSON parse, no candidate
# tags, whitelist) prints a warn to stderr and emits empty stdout.
# Always returns 0.
github_api_latest_release_tag() {
    local owner="$1" repo="$2"
    local api_base="${LITCLOCK_API_BASE_URL:-https://api.github.com}"
    # per_page=100 is the API max. LitClock has ~10 tags total today;
    # if the project ever exceeds 100 we'll need pagination, but a
    # release-shaped tag stays in the first page for years to come.
    local url="${api_base}/repos/${owner}/${repo}/tags?per_page=100"
    local tmp tag=""

    tmp=$(mktemp 2>/dev/null) || {
        printf "[github_api] warn: mktemp failed\n" >&2
        return 0
    }

    if ! github_api_curl "$url" "application/vnd.github+json" "$tmp" 2>/dev/null; then
        printf "[github_api] warn: failed to fetch %s\n" "$url" >&2
        rm -f "$tmp"
        return 0
    fi

    local py_stderr
    py_stderr=$(mktemp 2>/dev/null) || py_stderr=""
    tag=$(python3 -c '
import json, re, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception as e:
    sys.stderr.write("json parse error: " + str(e) + "\n")
    sys.exit(1)
if not isinstance(data, list):
    sys.stderr.write("response is not a JSON array\n")
    sys.exit(1)

# Strict release shape: vMAJOR.MINOR.PATCH. Excludes prerelease suffixes
# (vX.Y.Z-rc1, -alpha, -beta), build metadata (+sha), and unrelated tags
# (litclock-images-v3, qa-209-rc1.x, safe-before-issue-160).
release_re = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
candidates = []
for entry in data:
    if not isinstance(entry, dict):
        continue
    name = entry.get("name", "")
    if not isinstance(name, str):
        continue
    m = release_re.match(name)
    if m:
        candidates.append((tuple(int(p) for p in m.groups()), name))

if not candidates:
    sys.stderr.write("no release-shaped tags in response\n")
    sys.exit(1)

# /tags is documented as commit-date order, not semver. Be explicit.
candidates.sort(reverse=True)
tag = candidates[0][1]

# Defense-in-depth whitelist: the regex above already constrains the
# value, but keep this check so any future relaxation cannot accidentally
# let shell metachars through to git rev-list.
if not re.match(r"^[A-Za-z0-9._][A-Za-z0-9._+-]{0,127}$", tag):
    sys.stderr.write("tag contains disallowed characters: " + repr(tag) + "\n")
    sys.exit(1)
print(tag)
' "$tmp" 2>"${py_stderr:-/dev/null}")
    local rc=$?
    rm -f "$tmp"

    if [ "$rc" -ne 0 ] || [ -z "$tag" ]; then
        # Surface the python-side reason ("json parse error", "no release-
        # shaped tags in response", etc.) — without it the operator can't
        # tell auth/network/repo-state failures apart on a misconfigured Pi.
        local detail=""
        if [ -n "$py_stderr" ] && [ -s "$py_stderr" ]; then
            detail=$(tr '\n' ';' < "$py_stderr" | sed 's/;$//')
        fi
        if [ -n "$detail" ]; then
            printf "[github_api] warn: could not select latest release tag (%s)\n" "$detail" >&2
        else
            printf "[github_api] warn: could not select latest release tag from response\n" >&2
        fi
        rm -f "$py_stderr"
        return 0
    fi
    rm -f "$py_stderr"

    printf "%s\n" "$tag"
    return 0
}
