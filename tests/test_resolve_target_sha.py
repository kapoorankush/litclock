"""Tests for resolve_target_sha() in scripts/update.sh (issue #209).

The resolver reads the latest Release tag via github_api_latest_release_tag
then hits local git to produce an authoritative SHA. All failure modes
emit empty stdout with exit 0 (graceful-offline) so an offline Pi keeps
ticking on its pinned SHA.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from textwrap import dedent

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
LIB = REPO_ROOT / "scripts" / "lib" / "github_api.sh"


def _make_sandbox_repo(tmp_path: Path, tag: str | None = "v1.0.0") -> tuple[Path, str | None]:
    """Build a tmp git repo with ≥1 commit and (optionally) a tag, return
    (repo_dir, tagged_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, env=env, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()
    if tag:
        subprocess.run(["git", "tag", tag], cwd=repo, env=env, check=True)
        return repo, sha
    return repo, None


def _run_resolver(
    repo: Path,
    *,
    stub_tag_script: str,
) -> subprocess.CompletedProcess[str]:
    """Source the lib + update.sh's resolve_target_sha in a way that lets us
    stub github_api_latest_release_tag so the test doesn't need a server.

    We don't source update.sh wholesale (it has pre-flight checks that run
    on import). Instead we extract the function body and feed it its own
    stubbed prerequisites.
    """
    # Stub: override github_api_latest_release_tag via the loaded lib — but
    # since the real function reads HTTP, we replace the definition entirely.
    driver = dedent(
        f"""
        set -o pipefail
        # Load the real lib (for shape) then override the tag fetcher.
        . {LIB}
        {stub_tag_script}
        # Now source update.sh's resolver — but update.sh runs setup at the
        # top. Extract the function bytes by awk and eval them here.
        resolver_body=$(awk '/^resolve_target_sha\\(\\) \\{{/,/^}}/' {UPDATE_SH})
        eval "$resolver_body"
        cd "$1"
        resolve_target_sha
        """
    )
    return subprocess.run(
        ["bash", "-c", driver, "_", str(repo)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo.parent), "TMPDIR": str(repo.parent)},
        timeout=10,
        check=False,
    )


class TestHappyPath:
    def test_resolves_tag_to_local_sha(self, tmp_path):
        repo, sha = _make_sandbox_repo(tmp_path, tag="v1.0.0")
        stub = 'github_api_latest_release_tag() { printf "v1.0.0\\n"; }'
        r = _run_resolver(repo, stub_tag_script=stub)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert r.stdout.strip() == sha


class TestFailureModes:
    def test_no_tag_returned_empty_stdout(self, tmp_path):
        repo, _ = _make_sandbox_repo(tmp_path, tag="v1.0.0")
        stub = "github_api_latest_release_tag() { return 0; }"  # empty stdout
        r = _run_resolver(repo, stub_tag_script=stub)
        assert r.returncode == 0
        assert r.stdout.strip() == ""
        assert "no latest release" in r.stderr.lower() or "warn" in r.stderr.lower()

    def test_tag_not_present_locally_empty_stdout(self, tmp_path):
        repo, _ = _make_sandbox_repo(tmp_path, tag="v1.0.0")
        stub = 'github_api_latest_release_tag() { printf "v99.0.0\\n"; }'  # not tagged locally
        r = _run_resolver(repo, stub_tag_script=stub)
        # Note: resolve_target_sha does `git fetch --tags origin`; our sandbox has no
        # origin remote, so fetch fails → resolver treats as offline → empty stdout.
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_malformed_tag_never_reaches_resolver(self, tmp_path):
        """The lib whitelists tag_name; a shell-metachar tag is filtered at the
        lib boundary so the resolver sees empty input."""
        repo, _ = _make_sandbox_repo(tmp_path, tag="v1.0.0")
        # Stub returns empty (simulating lib rejection of bad tag).
        stub = "github_api_latest_release_tag() { return 0; }"
        r = _run_resolver(repo, stub_tag_script=stub)
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestStructural:
    def test_resolver_rejects_target_commitish(self):
        """Guard: resolver MUST NOT use target_commitish (unreliable per eng review A1)."""
        body = UPDATE_SH.read_text()
        # Extract the function; grep for target_commitish only inside it.
        import re

        fn = re.search(r"resolve_target_sha\(\)\s*\{(.*?)^\}", body, re.DOTALL | re.MULTILINE)
        assert fn, "resolve_target_sha not found in update.sh"
        assert "target_commitish" not in fn.group(1), (
            "target_commitish is unreliable — resolver must use tag_name + git rev-list"
        )

    def test_resolver_returns_zero_always(self):
        """Graceful-offline: every return path inside resolve_target_sha() is `return 0`
        OR the implicit return of a command that emits to stdout (printf)."""
        body = UPDATE_SH.read_text()
        import re

        fn = re.search(r"resolve_target_sha\(\)\s*\{(.*?)^\}", body, re.DOTALL | re.MULTILINE)
        assert fn
        fn_body = fn.group(1)
        for rc in re.findall(r"\breturn\s+(\S+)", fn_body):
            assert rc == "0", f"graceful-offline violated: found `return {rc}`"

    def test_resolver_uses_single_tag_fetch_with_timeout(self):
        """Regression for #209 hardware-found defect: pi-gen ships a shallow
        clone, so `git fetch --tags origin` backfills 30k+ historical
        objects on first run and exceeds the service's 120s
        TimeoutStartSec on a Pi Zero 2W. Resolver must scope the fetch
        to the one tag it already named, and wrap it in a hard `timeout`
        ceiling so a stuck fetch can't wedge the resolver."""
        body = UPDATE_SH.read_text()
        import re

        fn = re.search(r"resolve_target_sha\(\)\s*\{(.*?)^\}", body, re.DOTALL | re.MULTILINE)
        assert fn, "resolve_target_sha not found"
        fn_body = fn.group(1)

        # The blanket `git fetch --tags origin` must NOT be present anymore.
        assert "git fetch --tags" not in fn_body, (
            "resolver must not do a blanket `git fetch --tags` — that backfills "
            "all historical objects for every release tag, killing the Pi-side "
            "120s TimeoutStartSec on shallow clones (#209 regression)."
        )
        # Must do a single-tag refspec fetch with --no-tags scope.
        assert "--no-tags" in fn_body, "resolver must scope fetch with --no-tags"
        assert "refs/tags/${tag}:refs/tags/${tag}" in fn_body, (
            "resolver must fetch the explicitly-named tag via a refspec"
        )
        # Must wrap with `timeout` so a stuck fetch can't exceed
        # TimeoutStartSec on systemd-driven runs.
        assert re.search(r"timeout\s+\d+\s+git fetch", fn_body), (
            "git fetch must be wrapped in `timeout N` for a hard ceiling"
        )
        # rev-list still resolves the tag to an authoritative SHA.
        assert "git rev-list -n 1" in fn_body


def test_update_sh_sources_the_lib():
    """Wiring check — update.sh must source scripts/lib/github_api.sh."""
    body = UPDATE_SH.read_text()
    assert "lib/github_api.sh" in body
    # Must tolerate the lib being missing (for fresh-image pre-#209 updates)
    assert "[[ -f" in body or "[ -f" in body


def test_rev_list_invocation_quotes_tag():
    """Injection defense: the tag argument must be quoted when passed to git rev-list."""
    body = UPDATE_SH.read_text()
    import re

    assert re.search(r'git rev-list -n 1 "\$tag"', body), (
        "tag must be double-quoted in rev-list invocation to prevent word-splitting"
    )


# Keep os imported for the rare case someone extends this module with
# env-manipulation helpers — pytest doesn't complain if unused.
_ = os
