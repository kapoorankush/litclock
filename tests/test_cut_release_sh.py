"""Tests for scripts/cut-release.sh (litclock-dev#687).

These EXECUTE the script against throwaway git repositories rather than
asserting on its source. A release script's whole value is in what it refuses,
and a `assert "git status --porcelain" in src` check passes just as happily when
the guard is commented out (see the litclock-dev#673 lesson: a guard can be green and
protect nothing). Every refusal below is driven by building the repository state
that should trigger it.
"""

import datetime
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CUT_RELEASE = REPO_ROOT / "scripts" / "cut-release.sh"

CHANGELOG_WITH_ENTRIES = """# Changelog

## [Unreleased]

### Fixed
- a real entry (litclock-dev#1)

## [v0.1.0] - 2026-01-01

### Added
- the first one
"""


# The script runs its own `git commit` / `git tag -a` with whatever git config
# the machine has. On a box or CI container with no identity, or with
# commit.gpgsign / tag.gpgSign set and no key, every happy-path test here would
# fail for reasons that have nothing to do with the code under test. Pin it.
HERMETIC_GIT = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "LC_ALL": "C",
}


def _env(**overrides):
    env = {**os.environ, **HERMETIC_GIT}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
        env=_env(),
    )


@pytest.fixture
def repo(tmp_path):
    """A throwaway repo laid out like the real one: the script, the CI gate it
    calls, and the production extractor that gate loads by path."""
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "src" / "control_server").mkdir(parents=True)

    for rel in (
        "scripts/cut-release.sh",
        "scripts/check-changelog-section.py",
        "src/control_server/update_state.py",
    ):
        dest = r / rel
        dest.write_bytes((REPO_ROOT / rel).read_bytes())
        dest.chmod(0o755)

    (r / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (r / "CHANGELOG.md").write_text(CHANGELOG_WITH_ENTRIES, encoding="utf-8")

    _git(r, "init", "-q", "-b", "master", ".")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _run(repo, *args, expect_rc=None, stdin="", env=None):
    proc = subprocess.run(
        ["./scripts/cut-release.sh", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        env=env if env is not None else _env(),
    )
    if expect_rc is not None:
        assert proc.returncode == expect_rc, f"rc={proc.returncode}\nout={proc.stdout}\nerr={proc.stderr}"
    return proc


def _tags(repo):
    return _git(repo, "tag", "-l").stdout.split()


def _push_line_tokens(stdout):
    """Parse the printed push command the way a shell would.

    Asserting on a substring proves nothing about safety: an unescaped
    `release;echo pwned` still contains the branch name. Round-tripping through
    a POSIX word splitter is the actual claim — the line the operator pastes
    must parse back to exactly the refs it names.
    """
    line = next(ln for ln in stdout.splitlines() if "git push" in ln)
    return shlex.split(line)


class TestTheHappyPath:
    def test_it_promotes_the_heading_and_opens_a_fresh_unreleased(self, repo):
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

        headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
        assert headings[0] == "## [Unreleased]", "the next change must have somewhere to land"
        assert headings[1].startswith("## [v0.2.0] - "), headings
        assert headings[2] == "## [v0.1.0] - 2026-01-01", "older releases must be untouched"

        # The entries moved under the release heading, not under the new empty one.
        assert text.index("- a real entry") > text.index("## [v0.2.0]")

    def test_the_date_is_today_in_the_existing_format(self, repo):
        """Shape alone is not enough: hardcoding DATE=1970-01-01 in the script
        left the whole suite green."""
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        lines = (repo / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        line = next(ln for ln in lines if ln.startswith("## [v0.2.0]"))
        assert re.fullmatch(r"## \[v0\.2\.0\] - \d{4}-\d{2}-\d{2}", line), line

        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        assert line.split(" - ", 1)[1] in {today.isoformat(), yesterday.isoformat()}, (
            f"{line} is not today's date (midnight-tolerant)"
        )

    def test_the_release_commit_touches_only_the_changelog(self, repo):
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _run(repo, "v0.2.0", "--yes", expect_rc=0)

        changed = _git(repo, "diff", "--name-only", f"{before}", "HEAD").stdout.split()
        assert changed == ["CHANGELOG.md"], (
            "the tag must differ from the validated commit by the CHANGELOG alone, "
            f"otherwise it is not the tree that was QA'd: {changed}"
        )
        assert _git(repo, "rev-parse", "HEAD~1").stdout.strip() == before

    def test_it_creates_an_annotated_tag_on_the_release_commit(self, repo):
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert _tags(repo) == ["v0.2.0"]
        assert _git(repo, "cat-file", "-t", "v0.2.0").stdout.strip() == "tag", "releases are annotated tags"
        assert (
            _git(repo, "rev-parse", "v0.2.0^{commit}").stdout.strip() == _git(repo, "rev-parse", "HEAD").stdout.strip()
        )
        message = _git(repo, "tag", "-l", "--format=%(contents:subject)", "v0.2.0").stdout.strip()
        assert message == "LitClock v0.2.0", message

    def test_the_commit_subject_follows_the_existing_convention(self, repo):
        _run(repo, "v0.2.0", "--yes", "-m", "settings QR quiet-zone fix", expect_rc=0)
        subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
        assert subject == "docs(changelog): v0.2.0 — settings QR quiet-zone fix"

    def test_the_changelog_at_the_tag_satisfies_the_ci_gate(self, repo, tmp_path):
        """What the fleet consumes is CHANGELOG.md AT THE TAG — fetch_release_notes
        reads it at the tag ref, not from anyone's working tree. Running the gate
        on the working tree would be near-tautological (the script already dies if
        its own gate call fails), so extract the tagged blob and gate that."""
        _run(repo, "v0.2.0", "--yes", expect_rc=0)

        tagged = _git(repo, "show", "v0.2.0:CHANGELOG.md").stdout
        assert "## [v0.2.0] - " in tagged, "the tag does not carry the promoted heading"
        assert "- a real entry (litclock-dev#1)" in tagged

        at_tag = tmp_path / "at-tag.md"
        at_tag.write_text(tagged, encoding="utf-8")
        gate = subprocess.run(
            ["python3", "scripts/check-changelog-section.py", "v0.2.0", "--changelog", str(at_tag)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
            env=_env(),
        )
        assert gate.returncode == 0, f"the script and the CI gate disagree at the tag: {gate.stdout}{gate.stderr}"

    def test_the_working_tree_is_clean_afterwards(self, repo):
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""


class TestItRefuses:
    def test_an_unreleased_section_with_no_entries(self, repo):
        """Releasing nothing is the other half of the blank-update-card mistake."""
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [v0.1.0] - 2026-01-01\n\n- old\n")
        _git(repo, "commit", "-qam", "empty unreleased")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "no entries" in proc.stderr
        assert _tags(repo) == []

    def test_headings_alone_do_not_count_as_entries(self, repo):
        """A bare '### Changed' renders as a category heading and nothing else
        on the update card. This rule is deliberately stricter than
        check-changelog-section.py's; the gate runs second and is authoritative."""
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Changed\n\n## [v0.1.0] - 2026-01-01\n\n- old\n"
        )
        _git(repo, "commit", "-qam", "headings only")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "no entries" in proc.stderr
        assert _tags(repo) == []

    def test_a_dirty_working_tree(self, repo):
        (repo / "scripts" / "stray.sh").write_text("echo unreviewed\n")
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "dirty" in proc.stderr
        assert _tags(repo) == []

    def test_a_tag_that_already_exists(self, repo):
        """Tags are never moved: the fleet resolves updates from /tags, so
        moving one changes what already-updated devices fetched."""
        _git(repo, "tag", "v0.2.0")
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        # Specifically the PREFLIGHT refusal, not the `git tag` call failing later:
        # dropping this guard still ends in a refusal, but only after the CHANGELOG
        # has been rewritten and rolled back. Refusing before touching anything is
        # the behaviour under test.
        assert "already exists locally, pointing at" in proc.stderr
        assert "git tag failed" not in proc.stderr
        # The CHANGELOG must be left alone — no half-promoted state.
        assert "## [Unreleased]\n\n### Fixed" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    def test_a_detached_head(self, repo):
        """`git push origin <branch>` from a detached HEAD silently no-ops, so
        the tag would point at a commit no branch contains."""
        _git(repo, "checkout", "-q", "--detach", "HEAD")
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "detached" in proc.stderr
        assert _tags(repo) == []

    @pytest.mark.parametrize("tag", ["v0.2", "v0.2.0-rc1", "vfoo", "0.2.0", "dev-20260818-abc1234"])
    def test_a_tag_the_updater_would_never_offer(self, repo, tag):
        """RC and QA tags must not consume [Unreleased] — github_api.sh filters
        the update list to vMAJOR.MINOR.PATCH, so nothing else is ever offered."""
        proc = _run(repo, tag, "--yes", expect_rc=1)
        assert "release-shaped" in proc.stderr
        assert _tags(repo) == []

    def test_two_unreleased_headings(self, repo):
        """A bad merge can leave two. Promoting 'the first one' would then
        release the wrong notes, silently."""
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n- one (litclock-dev#1)\n\n## [Unreleased]\n\n- two (litclock-dev#2)\n"
        )
        _git(repo, "commit", "-qam", "double unreleased")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "2 '## [Unreleased]' headings" in proc.stderr
        assert _tags(repo) == []

    def test_a_changelog_with_no_unreleased_heading(self, repo):
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [v0.1.0] - 2026-01-01\n\n- old\n")
        _git(repo, "commit", "-qam", "no unreleased")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "no '## [Unreleased]' heading" in proc.stderr
        assert _tags(repo) == []


class TestExpectSha:
    def test_it_refuses_when_head_is_not_the_named_commit(self, repo):
        """How the operator states which commit they validated. The methodology
        is validate-first-tag-last, so tagging a different commit than the one
        that was QA'd is the failure this prevents."""
        (repo / "note.txt").write_text("later work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "work after the validated commit")
        validated = _git(repo, "rev-parse", "HEAD~1").stdout.strip()

        proc = _run(repo, "v0.2.0", "--expect-sha", validated, expect_rc=1)
        assert "HEAD is not the commit you named" in proc.stderr
        assert _tags(repo) == []

    def test_it_proceeds_when_head_matches(self, repo):
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _run(repo, "v0.2.0", "--expect-sha", head, expect_rc=0)
        assert _tags(repo) == ["v0.2.0"]

    def test_an_unresolvable_sha_is_refused(self, repo):
        proc = _run(repo, "v0.2.0", "--expect-sha", "deadbeef", expect_rc=1)
        assert "does not resolve" in proc.stderr
        assert _tags(repo) == []


class TestItStopsBeforePushing:
    def test_nothing_reaches_the_remote(self, repo, tmp_path):
        """Pushing from inside would reintroduce the 'irreversible the instant
        it runs' property this script exists to remove. Asserted against a real
        remote rather than by grepping for 'git push'."""
        remote = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=60)
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "master")
        before = _git(repo, "ls-remote", str(remote)).stdout

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)

        assert _git(repo, "ls-remote", str(remote)).stdout == before, "cut-release.sh must not push"
        assert "git push --atomic origin" in proc.stdout, "it must print the push it declined to run"
        assert _push_line_tokens(proc.stdout) == ["git", "push", "--atomic", "origin", "master", "v0.2.0"]

    # git rejects spaces in branch names, but every one of these it accepts —
    # verified with `git check-ref-format --branch`.
    @pytest.mark.parametrize("branch", ["rel'ease", "x$(id)", "a;echo", "a|b", "a&b", "`id`", 'a"b'])
    def test_the_printed_push_survives_a_hostile_branch_name(self, repo, branch):
        """That line is meant to be pasted into a shell. Wrapping the refs in
        literal single quotes is not enough — a name containing a single quote
        breaks straight back out of them (/review, Codex)."""
        _git(repo, "checkout", "-q", "-b", branch)

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)

        assert _push_line_tokens(proc.stdout) == ["git", "push", "--atomic", "origin", branch, "v0.2.0"], (
            "the printed command does not parse back to the refs it names"
        )

    def test_it_names_the_remote_it_checked(self, repo, tmp_path):
        """`origin` is not necessarily the namespace the fleet resolves from —
        update.sh and update_state.py read releases from the public repo, so a
        tag cut in a clone whose origin is the dev repo was checked against the
        wrong tag list. Printing the URL makes that visible."""
        remote = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=60)
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "master")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert str(remote) in proc.stdout
        assert "could not reach origin" not in proc.stderr, "a reachable origin must not warn"

    def test_it_warns_loudly_when_it_cannot_see_the_fleets_tags(self, repo):
        """The fixture repo has no remote in the fleet's namespace, so neither the
        duplicate-tag nor the version-order check can see the tags that matter. A
        silent skip would read as 'checked, clean'."""
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert "no configured remote matches the namespace the fleet resolves from" in proc.stderr
        assert "kapoorankush/litclock" in proc.stderr, "it must name the namespace it looked for"

    def test_it_warns_when_a_configured_remote_is_unreachable(self, repo, tmp_path):
        _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert "could not reach 'origin'" in proc.stderr

    def test_it_prints_how_to_undo(self, repo):
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        undo = next(ln for ln in proc.stdout.splitlines() if "git tag -d" in ln)
        assert shlex.split(undo)[:4] == ["git", "tag", "-d", "v0.2.0"], undo
        assert "git reset --hard" in undo


class TestItLeavesNoHalfState:
    def test_a_gate_failure_restores_the_changelog_and_tags_nothing(self, repo):
        """The script's own pre-check scans the whole [Unreleased] span for a
        bullet; the gate only sees the first 10 non-empty lines the extractor
        returns. So a section that buries its entries under ten lines of
        headings passes the pre-check and fails the gate — the one case where
        the CHANGELOG is already rewritten when the refusal lands. It must fail
        closed: file restored, nothing committed, nothing tagged.
        """
        filler = "\n".join(f"### Category {i}" for i in range(10))
        (repo / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## [Unreleased]\n\n{filler}\n\n- buried entry (litclock-dev#1)\n\n"
            "## [v0.1.0] - 2026-01-01\n\n- old\n"
        )
        _git(repo, "commit", "-qam", "buried entries")
        before_text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "restored" in proc.stderr
        assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before_text
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
        assert _tags(repo) == []
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""


class TestUsage:
    def test_no_arguments_is_an_error_not_a_release(self, repo):
        proc = _run(repo, expect_rc=1)
        assert "Usage:" in proc.stderr
        assert _tags(repo) == []

    def test_two_tags_are_refused(self, repo):
        proc = _run(repo, "v0.2.0", "v0.3.0", "--yes", expect_rc=1)
        assert "exactly one tag" in proc.stderr
        assert _tags(repo) == []

    def test_an_unknown_option_is_refused(self, repo):
        """Not silently ignored: --push would be a very bad thing to swallow."""
        proc = _run(repo, "v0.2.0", "--push", "--yes", expect_rc=1)
        assert "unknown option" in proc.stderr
        assert _tags(repo) == []

    def test_help_exits_zero_without_touching_anything(self, repo):
        proc = _run(repo, "--help", expect_rc=0)
        assert "Usage:" in proc.stdout
        assert _tags(repo) == []


class TestTheInteractiveConfirmation:
    """The DEFAULT path — taken whenever neither --expect-sha nor --yes is given,
    which is what docs/building-image.md's short form does. It had zero coverage:
    replacing the reply check with `:` (cut the release no matter what the
    operator typed) left the whole suite green.
    """

    def test_y_proceeds(self, repo):
        _run(repo, "v0.2.0", stdin="y\n", expect_rc=0)
        assert _tags(repo) == ["v0.2.0"]

    def test_yes_also_proceeds(self, repo):
        """'yes' is what people type. Aborting on it is a trap, not a safeguard."""
        _run(repo, "v0.2.0", stdin="yes\n", expect_rc=0)
        assert _tags(repo) == ["v0.2.0"]

    def test_n_aborts_and_changes_nothing(self, repo):
        before = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        proc = _run(repo, "v0.2.0", stdin="n\n", expect_rc=1)
        assert "aborted" in proc.stderr
        assert _tags(repo) == []
        assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before

    def test_anything_else_aborts(self, repo):
        proc = _run(repo, "v0.2.0", stdin="maybe\n", expect_rc=1)
        assert "aborted" in proc.stderr
        assert _tags(repo) == []

    def test_closed_stdin_fails_with_a_message(self, repo):
        """Under `set -e` a bare `read` hitting EOF exits the script before its
        own die can run, so a non-tty caller got a silent exit 1 — indistinguishable
        from a real refusal, on the default path."""
        proc = _run(repo, "v0.2.0", stdin="", expect_rc=1)
        assert proc.stderr.strip(), "a refusal with no message is not a refusal"
        assert "--yes" in proc.stderr, "it must say how to run non-interactively"
        assert _tags(repo) == []


class TestTheRemoteTagGuard:
    def test_a_tag_that_already_exists_on_origin(self, repo, tmp_path):
        """The fleet resolves OTA updates from /tags. Reusing a tag origin
        already carries changes what already-updated devices fetched. This whole
        branch was unreachable in the suite — every other test either has no
        origin (rc=128) or an origin without the tag (rc=2)."""
        remote = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=60)
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "tag", "v0.2.0")
        _git(repo, "push", "-q", "origin", "v0.2.0")
        _git(repo, "tag", "-d", "v0.2.0")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "already exists on 'origin'" in proc.stderr
        assert _tags(repo) == []
        assert "## [Unreleased]\n\n### Fixed" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def _stub_the_gate_to_run(repo, shell_snippet):
    """Replace the CI gate the script shells out to with a stub that perturbs the
    repository first. The gate runs inside exactly the window between the HEAD
    snapshot and the commit, so this drives the race deterministically — no
    sleeps, no background process, no flake."""
    (repo / "scripts" / "check-changelog-section.py").write_text(
        "import subprocess, sys\n"
        f"subprocess.run({shell_snippet!r}, check=True)\n"
        "print('stub gate: OK')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-qam", "stub the gate so it perturbs the repo mid-run")


class TestHeadMovingUnderneathIt:
    """Everything is checked against a snapshot taken before the confirmation
    prompt, which an operator can sit at indefinitely. If HEAD moves in that
    window the commit and tag land on a commit nobody validated, while the
    summary still prints the OLD sha as the contents.

    The two guards are tested separately on purpose: a scenario that trips both
    lets either one satisfy the assertion, and dropping the HEAD check then stays
    green.
    """

    def test_it_refuses_when_head_moved_on_the_same_branch(self, repo):
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _stub_the_gate_to_run(repo, ["git", "commit", "-q", "--allow-empty", "-m", "someone else's commit"])

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "HEAD moved" in proc.stderr, proc.stderr
        assert _tags(repo) == [], "a tag here would point at a commit nobody validated"
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() != before_head
        assert "docs(changelog)" not in _git(repo, "log", "--format=%s", "-3").stdout

    def test_it_refuses_when_the_branch_changed_underneath_it(self, repo):
        """Same commit, different branch — HEAD is unchanged, so only the branch
        guard can catch this. The printed `git push origin <branch>` would
        otherwise name a branch the operator is no longer on."""
        _stub_the_gate_to_run(repo, ["git", "checkout", "-q", "-b", "sibling"])

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "branch changed" in proc.stderr, proc.stderr
        assert _tags(repo) == []

    def test_it_refuses_when_head_moved_to_another_branch(self, repo):
        """Everything is checked against a snapshot taken before the confirmation
        prompt, which an operator can sit at indefinitely. If HEAD moves in that
        window the commit and tag would land on a commit nobody validated, while
        the summary still printed the OLD sha as the contents.

        Driven deterministically by replacing the CI gate the script shells out
        to with a stub that moves HEAD first — the gate runs inside exactly the
        window being tested, so there is no race in the test itself.
        """
        _git(repo, "checkout", "-q", "-b", "other")
        (repo / "unvalidated.txt").write_text("work nobody reviewed\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "unvalidated work on another branch")
        _git(repo, "checkout", "-q", "master")

        _stub_the_gate_to_run(repo, ["git", "checkout", "-q", "other"])
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "HEAD moved" in proc.stderr, proc.stderr
        assert _tags(repo) == [], "a tag here would point at an unvalidated commit"
        assert _git(repo, "rev-parse", "other").stdout.strip() != before_head
        assert "unvalidated work" not in _git(repo, "log", "-1", "--format=%s", "master").stdout


class TestFailuresAfterThePromotion:
    """Once the CHANGELOG is rewritten, a failure has to roll all the way back.
    The worst shape is a release commit that consumed [Unreleased] with no tag:
    the obvious retry then dies with 'has no entries' and the operator is stuck.
    """

    def test_a_failing_commit_rolls_back(self, repo):
        _git(repo, "config", "user.useConfigOnly", "true")
        _git(repo, "config", "--unset-all", "user.email", check=False)
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        before_text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

        env = _env(GIT_AUTHOR_EMAIL=None, GIT_COMMITTER_EMAIL=None)
        proc = _run(repo, "v0.2.0", "--yes", env=env, expect_rc=1)

        assert "git commit failed" in proc.stderr, proc.stderr
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
        assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before_text
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""
        assert _tags(repo) == []

    def test_a_failing_tag_rolls_the_release_commit_back(self, repo):
        _git(repo, "config", "tag.gpgSign", "true")
        _git(repo, "config", "gpg.program", "/nonexistent/gpg-binary")
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        before_text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "git tag failed" in proc.stderr, proc.stderr
        assert _tags(repo) == []
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head, (
            "a release commit with [Unreleased] consumed and no tag makes the retry refuse"
        )
        assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before_text
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    def test_the_retry_after_a_rolled_back_failure_works(self, repo):
        """The point of rolling back: the obvious next action succeeds."""
        _git(repo, "config", "tag.gpgSign", "true")
        _git(repo, "config", "gpg.program", "/nonexistent/gpg-binary")
        _run(repo, "v0.2.0", "--yes", expect_rc=1)

        _git(repo, "config", "--unset", "tag.gpgSign")
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert _tags(repo) == ["v0.2.0"]


class TestTheChangelogPath:
    def test_a_symlinked_changelog_is_refused_before_anything_is_written(self, repo, tmp_path):
        """`cp` follows a symlink at the destination, so a CHANGELOG.md replaced
        by a symlink had the release heading written THROUGH it into a file
        outside the repository — and the staged-path guard then refused to commit,
        after the out-of-repo write had already happened."""
        outside = tmp_path / "outside.md"
        outside.write_text("# Not the changelog\n\n## [Unreleased]\n\n- bait (litclock-dev#1)\n", encoding="utf-8")
        before = outside.read_text(encoding="utf-8")

        (repo / "CHANGELOG.md").unlink()
        (repo / "CHANGELOG.md").symlink_to(outside)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "changelog is now a symlink")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "symlink" in proc.stderr
        assert outside.read_text(encoding="utf-8") == before, "wrote outside the repository"
        assert _tags(repo) == []


class TestExpectShaTakesAnObjectId:
    @pytest.mark.parametrize("ref", ["HEAD", "master", "@"])
    def test_a_ref_name_is_refused(self, repo, ref):
        """`--expect-sha HEAD` resolves to HEAD by definition, so it can never
        fail — and it takes the same branch, so it skips the interactive
        confirmation too. A guard that reads in shell history as 'I pinned the
        commit' while checking nothing is worse than no guard."""
        proc = _run(repo, "v0.2.0", "--expect-sha", ref, expect_rc=1)
        assert "not a ref name" in proc.stderr
        assert _tags(repo) == []

    def test_a_real_sha_still_works(self, repo):
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _run(repo, "v0.2.0", "--expect-sha", head[:12], expect_rc=0)
        assert _tags(repo) == ["v0.2.0"]


class TestChangelogShapes:
    def test_a_bullet_with_no_content_is_not_an_entry(self, repo):
        """A section holding only '- ' passes the CI gate's rule and produces an
        update card with an empty bullet."""
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- \n\n## [v0.1.0] - 2026-01-01\n\n- old\n",
            encoding="utf-8",
        )
        _git(repo, "commit", "-qam", "empty bullet")
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "no entries" in proc.stderr
        assert _tags(repo) == []

    def test_unreleased_as_the_last_section(self, repo):
        """The body extractor's `inside && /^## / { exit }` never fires here."""
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- the only entry (litclock-dev#1)\n", encoding="utf-8"
        )
        _git(repo, "commit", "-qam", "unreleased is last")
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        assert text.index("## [Unreleased]") < text.index("## [v0.2.0]")
        assert "- the only entry (litclock-dev#1)" in _git(repo, "show", "v0.2.0:CHANGELOG.md").stdout

    def test_a_changelog_with_no_trailing_newline(self, repo):
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n- an entry (litclock-dev#1)", encoding="utf-8")
        _git(repo, "commit", "-qam", "no trailing newline")
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert (repo / "CHANGELOG.md").read_text(encoding="utf-8").endswith("- an entry (litclock-dev#1)\n")

    def test_non_ascii_entries_survive_the_round_trip(self, repo):
        """This project's CHANGELOG is full of em dashes, and the suite pins
        LC_ALL=C, so a locale-sensitive regression in the awk pass would
        otherwise be invisible."""
        body = "- em dash — and CJK 時計 and an accent café (litclock-dev#1)\n"
        (repo / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## [Unreleased]\n\n### Fixed\n{body}\n## [v0.1.0] - 2026-01-01\n\n- old\n",
            encoding="utf-8",
        )
        _git(repo, "commit", "-qam", "non-ascii entries")
        _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert body in _git(repo, "show", "v0.2.0:CHANGELOG.md").stdout


class TestPreflightFileChecks:
    def test_a_missing_ci_gate_is_refused(self, repo):
        """This check is what guarantees the CI gate actually runs here."""
        (repo / "scripts" / "check-changelog-section.py").unlink()
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)
        assert "release gate must run here" in proc.stderr
        assert _tags(repo) == []

    @pytest.mark.parametrize("flag", ["--expect-sha", "-m", "--message"])
    def test_an_option_with_no_value_is_refused(self, repo, flag):
        proc = _run(repo, "v0.2.0", flag, expect_rc=1)
        assert "needs a value" in proc.stderr
        assert _tags(repo) == []


def test_the_release_tag_shape_agrees_across_all_three_definitions():
    """github_api.sh decides what the FLEET is offered, check-changelog-section.py
    decides what CI gates, and cut-release.sh decides what can be cut. Three
    copies of one rule; nothing but this test keeps them agreeing.
    """
    import re as _re

    def _semver_core(pattern):
        return pattern.replace("(", "").replace(")", "").replace("\\d", "[0-9]").replace("+", "+")

    gh = (REPO_ROOT / "scripts" / "lib" / "github_api.sh").read_text(encoding="utf-8")
    checker = (REPO_ROOT / "scripts" / "check-changelog-section.py").read_text(encoding="utf-8")
    cutter = (REPO_ROOT / "scripts" / "cut-release.sh").read_text(encoding="utf-8")

    gh_re = _re.search(r'release_re = re\.compile\(r"([^"]+)"\)', gh)
    checker_re = _re.search(r'_RELEASE_TAG_RE = re\.compile\(r"([^"]+)"\)', checker)
    cutter_re = _re.search(r'\[\[ "\$TAG" =~ (\S+) \]\]', cutter)
    assert gh_re and checker_re and cutter_re, "a tag-shape definition moved — this parity test went blind"

    normalised = {
        _semver_core(gh_re.group(1)),
        _semver_core(checker_re.group(1)),
        _semver_core(cutter_re.group(1)),
    }
    assert len(normalised) == 1, f"the three release-tag patterns have drifted: {normalised}"


class TestTheVersionMustBeTheNextOne:
    """Existence is not enough. Both resolvers pick the HIGHEST semver, not the
    newest tag — `github_api.sh` and `update_state.py` each sort(reverse=True)
    and take [0] — so one typo'd high tag buries every subsequent real release
    from the whole fleet, permanently, and the "tags are never moved" doctrine
    forbids the only remedy.
    """

    @pytest.fixture
    def repo_at_v1(self, repo):
        _git(repo, "tag", "-a", "v0.224.0", "-m", "LitClock v0.224.0")
        return repo

    @pytest.mark.parametrize("tag", ["v0.225.0", "v0.224.1", "v1.0.0"])
    def test_an_immediate_successor_is_allowed(self, repo_at_v1, tag):
        _run(repo_at_v1, tag, "--yes", expect_rc=0)
        assert tag in _tags(repo_at_v1)

    @pytest.mark.parametrize("tag", ["v0.324.0", "v0.226.0", "v2.0.0", "v0.224.5"])
    def test_a_version_that_skips_ahead_is_refused(self, repo_at_v1, tag):
        proc = _run(repo_at_v1, tag, "--yes", expect_rc=1)
        assert "skips past v0.224.0" in proc.stderr
        assert "--allow-version-jump" in proc.stderr, "it must name the escape hatch"
        assert _tags(repo_at_v1) == ["v0.224.0"]

    @pytest.mark.parametrize("tag", ["v0.223.0", "v0.100.0", "v0.224.0"])
    def test_a_version_at_or_below_the_highest_is_refused(self, repo_at_v1, tag):
        proc = _run(repo_at_v1, tag, "--yes", expect_rc=1)
        assert "not ahead of v0.224.0" in proc.stderr or "already exists locally" in proc.stderr
        assert _tags(repo_at_v1) == ["v0.224.0"]

    def test_the_escape_hatch_works_and_must_be_asked_for(self, repo_at_v1):
        _run(repo_at_v1, "v0.324.0", "--yes", expect_rc=1)
        _run(repo_at_v1, "v0.324.0", "--yes", "--allow-version-jump", expect_rc=0)
        assert "v0.324.0" in _tags(repo_at_v1)

    def test_the_fleets_tags_count_even_when_local_ones_are_behind(self, repo, tmp_path):
        """The case that made this necessary: this repository's local tags stop
        at v0.222.0 while the fleet has been on v0.224.0 for days. Cutting
        v0.223.0 must be refused on the strength of the REMOTE tag alone."""
        remote = tmp_path / "fleet.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=60)
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "tag", "-a", "v0.224.0", "-m", "x")
        _git(repo, "push", "-q", "origin", "v0.224.0")
        _git(repo, "tag", "-d", "v0.224.0")
        assert _tags(repo) == [], "the local clone must not know about it"

        proc = _run(repo, "v0.223.0", "--yes", expect_rc=1)
        assert "not ahead of v0.224.0" in proc.stderr, proc.stderr
        assert _tags(repo) == []


class TestTheRollbackCannotDestroyOtherWork:
    """`git reset --hard` in a rollback is itself dangerous. An earlier version
    accepted "any commit whose parent is the preflight sha", which a concurrent
    writer in the same worktree satisfies — and it then discarded a stranger's
    commit and files while printing "restored". This project has a recorded
    incident of exactly that concurrency (review subagents sharing a worktree).
    """

    def _install_concurrent_writer(self, repo, count=1):
        hook = repo / ".git" / "hooks" / "pre-commit"
        lines = ["#!/bin/sh"]
        for i in range(count):
            lines += [
                f"echo work{i} > unvalidated{i}.txt",
                f"git add unvalidated{i}.txt",
                f"git commit --no-verify -q -m 'someone else commit {i}'",
            ]
        hook.write_text("\n".join(lines) + "\n", encoding="utf-8")
        hook.chmod(0o755)

    def test_a_strangers_commit_is_not_reset_away(self, repo):
        self._install_concurrent_writer(repo, count=1)

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        subjects = _git(repo, "log", "--format=%s", "-5").stdout
        assert "someone else commit 0" in subjects, (
            "the rollback destroyed a concurrent writer's commit — reflog-only recovery"
        )
        assert (repo / "unvalidated0.txt").exists(), "it destroyed their files too"
        assert _tags(repo) == []
        assert "Could NOT roll back automatically" in proc.stderr, proc.stderr

    def test_it_never_claims_a_restoration_that_did_not_happen(self, repo):
        """Reporting the un-rolled-back state as 'restored' is worse than
        reporting the failure: the operator is left with [Unreleased] consumed
        and a retry that refuses with 'has no entries'."""
        self._install_concurrent_writer(repo, count=2)

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "has been restored" not in proc.stderr, proc.stderr
        assert "Could NOT roll back automatically" in proc.stderr
        assert "do NOT blindly reset" in proc.stderr, "it must warn before the operator reaches for reset"


class TestGitAddFailure:
    def test_a_held_index_lock_is_reported_and_rolled_back(self, repo):
        """`git add` was the one unguarded mutation: a held .git/index.lock exited
        128 with git's raw error, no message and no rollback, leaving a promoted
        CHANGELOG whose obvious remedy creates the consumed-[Unreleased] trap."""
        before = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=1)

        assert "git add failed" in proc.stderr, proc.stderr
        assert "index.lock" in proc.stderr
        assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before
        assert _tags(repo) == []


class TestExpectShaCannotBeSatisfiedByAHexNamedRef:
    def test_a_branch_named_in_hex_is_refused(self, repo):
        """The shape test is a test on the STRING. `git branch deadbeef` is legal,
        and it resolved through `git rev-parse <name>^{commit}` pinning nothing —
        a loophole the ["HEAD", "master", "@"] parametrisation could never see."""
        _git(repo, "branch", "deadbeef", "master")
        proc = _run(repo, "v0.2.0", "--expect-sha", "deadbeef", expect_rc=1)
        assert "is a ref name, not an object id" in proc.stderr
        assert _tags(repo) == []

    def test_a_genuine_hex_prefix_of_head_still_works(self, repo):
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _run(repo, "v0.2.0", "--expect-sha", head[:10], expect_rc=0)
        assert _tags(repo) == ["v0.2.0"]


class TestTheFleetRemoteIsProbedEvenWhenItIsNotOrigin:
    """The finding that made this necessary: in the dev clone `origin` is the DEV
    repo while the fleet resolves from the PUBLIC one, so the duplicate-tag guard
    — the script's fleet-safety centrepiece — was consulting a tag list that does
    not contain the fleet's tags at all. Re-cutting a version that had been live
    for days passed both the local and the remote check.

    The namespace comes from the same constants the device uses
    (update_state.py's DEFAULT_OWNER / DEFAULT_REPO), not from a hardcoded name.
    """

    @pytest.fixture
    def fleet_owner_repo(self):
        text = (REPO_ROOT / "src" / "control_server" / "update_state.py").read_text(encoding="utf-8")
        owner = re.search(r'DEFAULT_OWNER[^=]*=\s*"([^"]+)"', text).group(1)
        name = re.search(r'DEFAULT_REPO[^=]*=\s*"([^"]+)"', text).group(1)
        return owner, name

    def _add_redirected_remote(self, repo, name, url, tmp_path, backing):
        """Configure a remote with a REAL github.com URL, transparently redirected
        to a local bare repo via url.<x>.insteadOf. The identity match reads the
        configured url (`git config --get remote.X.url`), while every network
        operation lands on disk — so the matching logic is exercised on genuine
        URL shapes with no network at all."""
        bare = tmp_path / backing
        if not bare.exists():
            subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=60)
        _git(repo, "remote", "add", name, url)
        _git(repo, "config", f'url.{bare}.insteadOf', url)
        return bare

    def _make_fleet_remote(self, repo, tmp_path, fleet_owner_repo):
        owner, name = fleet_owner_repo
        self._add_redirected_remote(repo, "origin", f"https://github.com/{owner}/litclock-dev.git", tmp_path, "dev.git")
        fleet = self._add_redirected_remote(
            repo, "pubref", f"https://github.com/{owner}/{name}.git", tmp_path, "fleet.git"
        )
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "push", "-q", "pubref", "master")
        return fleet

    def test_a_tag_live_on_the_fleet_is_refused_though_origin_is_clean(self, repo, tmp_path, fleet_owner_repo):
        self._make_fleet_remote(repo, tmp_path, fleet_owner_repo)
        _git(repo, "tag", "-a", "v0.224.0", "-m", "shipped to the fleet")
        _git(repo, "push", "-q", "pubref", "v0.224.0")
        _git(repo, "tag", "-d", "v0.224.0")

        proc = _run(repo, "v0.224.0", "--yes", expect_rc=1)

        assert "already exists on 'pubref'" in proc.stderr, proc.stderr
        assert _tags(repo) == []

    def test_it_reports_probing_the_fleet_remote(self, repo, tmp_path, fleet_owner_repo):
        self._make_fleet_remote(repo, tmp_path, fleet_owner_repo)
        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert "pubref" in proc.stdout, "the summary must name every remote it consulted"
        assert "no configured remote matches" not in proc.stderr


class TestFleetRemoteIdentityMatching:
    """The match must be an identity check, not a suffix glob. `*"$OWNER/$REPO"`
    has no boundary before the owner and no host check, so it accepted
    github.com.evil.test, a nested archive path and `notkapoorankush` — while
    missing a trailing slash or a differently-cased owner (/review, Codex).

    Every URL below is configured for real and transparently redirected to a
    local bare repo, so the matching runs on genuine URL shapes with no network.
    """

    @pytest.fixture
    def fleet(self):
        text = (REPO_ROOT / "src" / "control_server" / "update_state.py").read_text(encoding="utf-8")
        return (
            re.search(r'DEFAULT_OWNER[^=]*=\s*"([^"]+)"', text).group(1),
            re.search(r'DEFAULT_REPO[^=]*=\s*"([^"]+)"', text).group(1),
        )

    def _redirect(self, repo, name, url, tmp_path):
        bare = tmp_path / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=60)
        _git(repo, "remote", "add", name, url)
        _git(repo, "config", f"url.{bare}.insteadOf", url)
        _git(repo, "push", "-q", name, "master")

    @pytest.mark.parametrize(
        "shape",
        [
            "https://github.com/{o}/{r}.git",
            "https://github.com/{o}/{r}",
            "https://github.com/{o}/{r}/",
            "https://github.com/{O}/{R}.git",
            "git@github.com:{o}/{r}.git",
            "ssh://git@github.com/{o}/{r}.git",
        ],
    )
    def test_it_recognises_every_legitimate_url_shape(self, repo, tmp_path, fleet, shape):
        owner, name = fleet
        url = shape.format(o=owner, r=name, O=owner.upper(), R=name.upper())
        self._redirect(repo, "pubref", url, tmp_path)

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert "no configured remote matches" not in proc.stderr, f"{url} was not recognised"

    @pytest.mark.parametrize(
        "shape",
        [
            "https://github.com.evil.test/{o}/{r}.git",
            "https://example.test/archive/{o}/{r}.git",
            "https://github.com/not{o}/{r}.git",
            "https://github.com/{o}/{r}-dev.git",
            "https://gitlab.com/{o}/{r}.git",
        ],
    )
    def test_it_rejects_lookalike_urls(self, repo, tmp_path, fleet, shape):
        owner, name = fleet
        url = shape.format(o=owner, r=name)
        self._redirect(repo, "decoy", url, tmp_path)

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert "no configured remote matches" in proc.stderr, f"{url} was accepted as the fleet's remote"


class TestVersionArithmeticEdges:
    def test_a_leading_zero_tag_does_not_crash_the_arithmetic(self, repo):
        """The tag regex permits a leading zero and bash reads 08 as octal —
        'value too great for base', a raw interpreter error rather than a die.
        The fleet's resolvers use Python int(), which has no such quirk."""
        _git(repo, "tag", "-a", "v0.08.0", "-m", "x")

        proc = _run(repo, "v0.9.0", "--yes")

        assert "value too great for base" not in proc.stderr, proc.stderr
        assert "syntax error" not in proc.stderr
        # v0.08.0 parses as 0.8.0, so v0.9.0 is its minor successor.
        assert proc.returncode == 0, proc.stderr
        assert "v0.9.0" in _tags(repo)

    def test_a_leading_zero_request_is_refused_cleanly(self, repo):
        """v0.08.0 is refused — it is not the string v0.8.0, and a tag that sorts
        differently in `sort -V` than in the fleet's Python tuple sort is exactly
        what should not reach /tags. The claim under test is that it is refused by
        a die, not by a raw interpreter error."""
        _git(repo, "tag", "-a", "v0.7.0", "-m", "x")
        proc = _run(repo, "v0.08.0", "--yes", expect_rc=1)
        assert "value too great for base" not in proc.stderr, proc.stderr
        assert "syntax error" not in proc.stderr
        assert "v0.8.0" in proc.stderr, "the refusal must name the version it expected"
        assert _tags(repo) == ["v0.7.0"]


class TestResolvingTheFleetConstants:
    def test_a_similarly_named_constant_does_not_win(self, repo, tmp_path):
        """`^DEFAULT_OWNER[^=]*=` also matches DEFAULT_OWNER_BACKUP, and with
        `head -1` a decoy declared above the real constant silently resolves the
        wrong namespace — after which every remote check consults the wrong tag
        list while reporting a clean pass (/review, Codex)."""
        update_state = repo / "src" / "control_server" / "update_state.py"
        original = update_state.read_text(encoding="utf-8")
        # Inserted after the __future__ import (which must stay first) but before
        # the real constants, since the extraction takes the first match. Plain
        # assignments: `Final` is not in scope there, and the point is the NAME.
        decoys = 'DEFAULT_OWNER_BACKUP = "wrong-owner"\nDEFAULT_REPO_NAME = "wrong-repo"\n'
        future = "from __future__ import annotations\n"
        assert original.count(future) == 1
        update_state.write_text(original.replace(future, future + "\n" + decoys, 1), encoding="utf-8")
        _git(repo, "commit", "-qam", "add decoy constants above the real ones")

        owner = re.search(r'^DEFAULT_OWNER[^=]*=\s*"([^"]+)"', original, re.M).group(1)
        name = re.search(r'^DEFAULT_REPO[^=]*=\s*"([^"]+)"', original, re.M).group(1)
        url = f"https://github.com/{owner}/{name}.git"
        bare = tmp_path / "fleet.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=60)
        _git(repo, "remote", "add", "pubref", url)
        _git(repo, "config", f"url.{bare}.insteadOf", url)
        _git(repo, "push", "-q", "pubref", "master")

        proc = _run(repo, "v0.2.0", "--yes", expect_rc=0)
        assert "no configured remote matches" not in proc.stderr, (
            "the decoy constant won, so the real fleet remote went unrecognised"
        )
        assert "wrong-owner" not in proc.stderr


class TestAPartialRemoteRead:
    def test_it_does_not_report_the_fleets_tags_as_seen(self, repo, tmp_path):
        """The single-ref probe can succeed while the full tag listing fails —
        a partial read, not an unreachable remote. Marking the fleet's tags
        'seen' there lets the version-order check report a clean pass on data it
        never got.

        Driven with a `git` shim on PATH that fails only the bare
        `ls-remote --tags <remote>` call, leaving every other git call real.
        """
        owner_repo = (REPO_ROOT / "src" / "control_server" / "update_state.py").read_text(encoding="utf-8")
        owner = re.search(r'^DEFAULT_OWNER[^=]*=\s*"([^"]+)"', owner_repo, re.M).group(1)
        name = re.search(r'^DEFAULT_REPO[^=]*=\s*"([^"]+)"', owner_repo, re.M).group(1)
        url = f"https://github.com/{owner}/{name}.git"
        bare = tmp_path / "fleet.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=60)
        _git(repo, "remote", "add", "pubref", url)
        _git(repo, "config", f"url.{bare}.insteadOf", url)
        _git(repo, "push", "-q", "pubref", "master")

        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True, timeout=30).stdout.strip()
        shim = shim_dir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            '# Fail ONLY the bare `ls-remote --tags <remote>` (no refspec).\n'
            'if [ "$1" = "ls-remote" ] && [ "$2" = "--tags" ] && [ $# -eq 3 ]; then\n'
            "    echo 'shim: simulated partial read' >&2\n"
            "    exit 1\n"
            "fi\n"
            f'exec {real_git} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)

        env = _env(PATH=f"{shim_dir}:{os.environ['PATH']}")
        proc = _run(repo, "v0.2.0", "--yes", env=env, expect_rc=0)

        assert "could not list tags on 'pubref'" in proc.stderr, proc.stderr
        assert "partial data" in proc.stderr


class TestItWorksOnARealSizedChangelog:
    """The fixtures in this file are a few lines long. That is exactly why they
    all passed while the real CHANGELOG could not be released.

    `printf ... | grep -q` was the bullet check. `grep -q` exits at the FIRST
    match and closes the pipe; the producer then takes SIGPIPE, and under
    `set -o pipefail` the PIPELINE reports 141 even though the match was found.
    The failure is SIZE-DEPENDENT: on a small body the producer finishes writing
    before grep exits, so nothing fails. The real 68-line CHANGELOG, whose
    entries run to several thousand characters each, fills the pipe buffer —
    and the script reported "'## [Unreleased]' has no entries", refusing to cut
    any release at all.

    Caught by running the script against the actual repository instead of
    against a fixture, which is the only way this class of bug shows up.
    """

    # Linux's pipe buffer is 64 KiB. A body SMALLER than that is written in full
    # before `grep -q` can exit, so it never triggers the SIGPIPE — which is why
    # the first version of this test (~43 KB) passed against the restored bug
    # and only the real-CHANGELOG case caught it (/review, Codex). The fixture
    # must clear the buffer with room to spare, and the test asserts that it
    # does, so it cannot silently shrink back under the threshold.
    PIPE_BUFFER_BYTES = 64 * 1024

    def test_a_large_unreleased_section_still_cuts(self, repo):
        entries = "\n".join(
            f"- **Entry {i}** with enough prose to matter: " + ("lorem ipsum dolor sit amet " * 40)
            for i in range(300)
        )
        assert len(entries.encode()) > 4 * self.PIPE_BUFFER_BYTES, (
            f"fixture is only {len(entries.encode())} bytes; under ~{self.PIPE_BUFFER_BYTES} the producer "
            "finishes before grep exits and this test cannot see the bug it exists for"
        )
        (repo / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## [Unreleased]\n\n### Fixed\n{entries}\n\n## [v0.1.0] - 2026-01-01\n\n- old\n",
            encoding="utf-8",
        )
        _git(repo, "commit", "-qam", "a realistically large unreleased section")

        proc = _run(repo, "v0.2.0", "--yes")

        assert "has no entries" not in proc.stderr, (
            "a large [Unreleased] was reported empty — the bullet check is piping into `grep -q` "
            "again, and SIGPIPE under pipefail makes the pipeline fail on exactly the inputs that "
            "matter"
        )
        assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"
        assert _tags(repo) == ["v0.2.0"]

    def test_the_real_repository_changelog_can_be_released(self, repo):
        """The regression, stated as the thing a maintainer actually needs: this
        repo's own CHANGELOG must be releasable. A synthetic large body could
        drift from whatever the real file looks like."""
        real = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # Immediately after a release the real [Unreleased] is legitimately
        # EMPTY — cut-release.sh just promoted everything out of it — and the
        # script correctly refuses to cut nothing, so this test could never
        # pass on a freshly-cut tree (first seen cutting v0.225.0: the cut had
        # to be undone to fix this). The property under test is that a
        # REAL-SIZED file releases; every released section stays byte-real,
        # and one sentinel bullet is added ONLY when the section has none.
        unrel = real.index("## [Unreleased]")
        nxt = real.index("\n## [", unrel + 5)
        # The SCRIPT's own definition of an entry (cut-release.sh's check:
        # `-` or `*`, optionally indented, with non-space content) — not a
        # col-0 "- " scan, which /review showed diverges in both directions:
        # a bare "- " stub would skip injection while the script refuses
        # (test red, the v0.225.0 incident shape again), and a "* " bullet
        # would inject a sentinel the script never needed.
        if not re.search(r"^[ \t]*[-*][ \t]+\S", real[unrel:nxt], re.M):
            real = (
                real[:nxt]
                + "\n### Fixed\n- sentinel bullet: keeps the real-sized-file property testable post-release\n"
                + real[nxt:]
            )
        (repo / "CHANGELOG.md").write_text(real, encoding="utf-8")
        _git(repo, "commit", "-qam", "use the real CHANGELOG")

        proc = _run(repo, "v0.2.0", "--yes")

        assert "has no entries" not in proc.stderr, proc.stderr
        assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"

    def test_the_bullet_check_does_not_pipe(self):
        """Structural backstop for the two above: they would also pass if the
        pipeline were merely reordered into something that happens not to
        SIGPIPE today. The rule is simpler than the symptom — do not feed a
        `grep -q` from a pipe under pipefail."""
        body = (REPO_ROOT / "scripts" / "cut-release.sh").read_text(encoding="utf-8")
        executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        # Normalised, not a substring scan: `|grep -qE`, `| grep -E -q`,
        # `| grep --quiet`, `| command grep -q` and a newline after the pipe all
        # reintroduce the bug while reading nothing like "| grep -q"
        # (/review, Codex).
        flat = re.sub(r"\s+", " ", executed)
        early_exit_consumers = [
            (r"\|\s*(?:command\s+|/usr/bin/|/bin/)?grep\b[^|;&]*(?:\s-\w*q|\s--quiet)", "grep -q / --quiet"),
            (r"\|\s*(?:command\s+|/usr/bin/|/bin/)?grep\b[^|;&]*\s-\w*m\b", "grep -m"),
            (r"\|\s*(?:command\s+|/usr/bin/|/bin/)?head\b", "head"),
            (r"\|\s*(?:command\s+|/usr/bin/|/bin/)?sed\b[^|;&]*\bq\b", "sed ... q"),
            (r"\|\s*(?:command\s+|/usr/bin/|/bin/)?awk\b[^|;&]*\bexit\b", "awk ... exit"),
        ]
        for pattern, label in early_exit_consumers:
            hit = re.search(pattern, flat)
            assert hit is None, (
                f"{label!r} is being fed from a pipe ({hit.group(0)!r} ). Under `set -o pipefail` an "
                "early-exiting consumer SIGPIPEs the producer, and the pipeline reports 141 on exactly "
                "the large inputs that matter. Use a herestring or restructure."
            )
