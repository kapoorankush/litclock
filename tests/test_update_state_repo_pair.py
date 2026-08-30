"""litclock-dev#728 — update_state derives its repo pair from origin, and
"available" means semver-newer.

The PWA's update card showed "Current v0.225.3 / Available v0.224.0" on a
dev Pi: ``update_state.py`` kept the hardcoded public pair after
litclock-dev#721 fixed the same defect in update.sh's resolver, and
``available`` was computed as *different-from-current*, which is how a
LOWER tag lit the badge. These tests execute the real Python
``origin_repo_pair()`` against real throwaway git checkouts (mirroring
tests/test_update_sh.py::TestOriginRepoPair case-for-case), pin the
wiring through ``build_check_payload``, and pin the semver-greater
semantics of ``_tag_is_newer``.
"""

from __future__ import annotations

import ast
import inspect
import logging
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from control_server import update_state  # noqa: E402


def _repo_with_origin(tmp_path: Path, url: str | None) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    if url is not None:
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
    return repo


class TestOriginRepoPair:
    """Case-for-case parity with the bash origin_repo_pair tests."""

    def _run(self, tmp_path, url):
        return update_state.origin_repo_pair(_repo_with_origin(tmp_path, url))

    def test_https_dev_origin_resolves_dev(self, tmp_path):
        assert self._run(tmp_path, "https://github.com/kapoorankush/litclock-dev.git") == (
            "kapoorankush",
            "litclock-dev",
        )

    def test_https_without_dot_git(self, tmp_path):
        assert self._run(tmp_path, "https://github.com/kapoorankush/litclock-dev") == (
            "kapoorankush",
            "litclock-dev",
        )

    def test_ssh_form(self, tmp_path):
        # DEV-shaped on purpose (see the bash twin): a public ssh URL's
        # correct answer coincides with the fallback pair, so dropping the
        # ssh parse arm would stay green against it.
        assert self._run(tmp_path, "git@github.com:kapoorankush/litclock-dev.git") == (
            "kapoorankush",
            "litclock-dev",
        )

    def test_public_https_resolves_public(self, tmp_path):
        assert self._run(tmp_path, "https://github.com/kapoorankush/litclock.git") == (
            "kapoorankush",
            "litclock",
        )

    def test_unparseable_falls_back_to_public(self, tmp_path):
        assert self._run(tmp_path, "ssh://weird.example/foo") == ("kapoorankush", "litclock")

    def test_missing_remote_falls_back_to_public(self, tmp_path):
        assert self._run(tmp_path, None) == ("kapoorankush", "litclock")

    def test_deep_path_falls_back_not_mangles(self, tmp_path):
        # /review litclock-dev#722 F3 twin: exactly-one-slash needs its own pin.
        assert self._run(tmp_path, "https://github.com/o/r/extra") == ("kapoorankush", "litclock")

    def test_trailing_slash_tolerated(self, tmp_path):
        assert self._run(tmp_path, "https://github.com/kapoorankush/litclock-dev/") == (
            "kapoorankush",
            "litclock-dev",
        )

    def test_no_git_repo_at_all_falls_back(self, tmp_path):
        # `git remote get-url` exits non-zero outside a work tree.
        assert update_state.origin_repo_pair(tmp_path / "nowhere") == ("kapoorankush", "litclock")

    def test_default_repo_dir_honors_litclock_dir_env(self, tmp_path, monkeypatch):
        repo = _repo_with_origin(tmp_path, "https://github.com/someone/elsewhere.git")
        monkeypatch.setenv("LITCLOCK_DIR", str(repo))
        assert update_state.origin_repo_pair() == ("someone", "elsewhere")


class TestBuildCheckPayloadWiring:
    """The parser alone can't fix litclock-dev#728 — the payload builder must
    actually query the derived pair (the litclock-dev#721 F6 lesson: a
    correct helper the caller never uses)."""

    def test_derived_pair_reaches_both_fetchers(self):
        with (
            patch.object(update_state, "origin_repo_pair", return_value=("devowner", "devrepo")),
            patch.object(update_state, "fetch_latest_release_tag", return_value="v9.9.9") as tag_fn,
            patch.object(update_state, "fetch_release_notes", return_value=None) as notes_fn,
        ):
            update_state.build_check_payload("v0.225.3")
        assert tag_fn.call_args.args == ("devowner", "devrepo"), (
            "build_check_payload queried a pair other than the origin-derived one — "
            "the litclock-dev#728 defect (dev Pi rendering public's tags)"
        )
        assert notes_fn.call_args.args == ("v9.9.9", "devowner", "devrepo")


class TestAvailableMeansNewer:
    def _payload(self, current: str, latest: str) -> dict:
        with (
            patch.object(update_state, "origin_repo_pair", return_value=("o", "r")),
            patch.object(update_state, "fetch_latest_release_tag", return_value=latest),
            patch.object(update_state, "fetch_release_notes", return_value=None),
        ):
            return update_state.build_check_payload(current)

    def test_lower_tag_is_not_available(self):
        # The observed lie: v0.224.0 offered to a v0.225.3 device.
        assert self._payload("v0.225.3", "v0.224.0")["available"] is False

    def test_higher_tag_is_available(self):
        assert self._payload("v0.224.0", "v0.225.3")["available"] is True

    def test_equal_tag_is_not_available(self):
        assert self._payload("v0.225.3", "v0.225.3")["available"] is False

    def test_describe_suffix_past_the_tag_is_not_available(self):
        # `git describe` on an untagged commit: still "up to date" — the
        # updater only ever installs blessed tags.
        assert self._payload("v0.225.3-4-gabc1234", "v0.225.3")["available"] is False

    def test_describe_suffix_vs_lower_tag_not_available(self):
        """The discriminating case (/review litclock-dev#729 testing pass): the plain
        equal-tag suffix test is also satisfied by the legacy fallback, so
        only THIS one goes red if _DESCRIBE_VERSION_RE's suffix arm breaks —
        the dev-Pi lie (lower tag offered) relit on the common
        git-describe-with-suffix state."""
        assert self._payload("v0.225.3-4-gabc1234", "v0.224.0")["available"] is False

    def test_describe_suffix_lower_current_vs_higher_tag_available(self):
        assert self._payload("v0.224.0-4-gabc1234", "v0.225.3")["available"] is True

    def test_dirty_suffix_both_directions(self):
        assert self._payload("v0.225.3+dirty", "v0.224.0")["available"] is False
        assert self._payload("v0.224.0+dirty", "v0.225.3")["available"] is True

    def test_double_digit_components_compare_numerically(self):
        # String comparison would call v0.9.0 > v0.10.0.
        assert self._payload("v0.9.0", "v0.10.0")["available"] is True
        assert self._payload("v0.10.0", "v0.9.0")["available"] is False

    def test_unparseable_current_keeps_legacy_behavior(self):
        # No tag reachable / hand-built checkout: a differing blessed tag
        # is still an update worth offering.
        assert self._payload("unknown", "v0.225.3")["available"] is True

    def test_network_failure_still_reports_unknown(self):
        with patch.object(update_state, "origin_repo_pair", return_value=("o", "r")), patch.object(
            update_state, "fetch_latest_release_tag", return_value=None
        ):
            payload = update_state.build_check_payload("v0.225.3")
        assert payload["available"] is None

    def test_unparseable_tag_is_not_available(self):
        assert self._payload("v0.225.3", "nightly-2026")["available"] is False


class TestNoHardcodedPairInPayloadPath:
    """AST-level backstop (a comment or docstring must not satisfy a
    behavior assertion — /review litclock-dev#729 testing pass): build_check_payload's
    body must CALL origin_repo_pair, and every fetch_latest_release_tag call
    must pass arguments (the defaults are the public pair)."""

    def test_payload_builder_routes_through_derivation(self):
        src = textwrap.dedent(inspect.getsource(update_state.build_check_payload))
        calls = [node for node in ast.walk(ast.parse(src)) if isinstance(node, ast.Call)]

        def _name(call: ast.Call) -> str:
            func = call.func
            if isinstance(func, ast.Name):
                return func.id
            if isinstance(func, ast.Attribute):
                return func.attr
            return ""

        assert any(_name(c) == "origin_repo_pair" for c in calls), (
            "build_check_payload no longer calls origin_repo_pair (litclock-dev#728)"
        )
        for call in calls:
            if _name(call) == "fetch_latest_release_tag":
                assert call.args or call.keywords, (
                    "argument-free fetch call is back — defaults are the public pair "
                    "(litclock-dev#728)"
                )


class TestParseOriginUrl:
    """Pure-parser pins for the /review litclock-dev#729 hardening: slug charset, dot
    components, userinfo, and host-spoof shapes."""

    def test_credentialed_https_resolves_its_own_pair(self):
        # The origin shape a hand-provisioned PAT produces. Falling back
        # here would reproduce the litclock-dev#721 pinning.
        assert update_state._parse_origin_url(
            "https://oauth2:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/kapoorankush/litclock-dev.git"
        ) == ("kapoorankush", "litclock-dev")

    def test_credentialed_public_https(self):
        assert update_state._parse_origin_url(
            "https://user:pass@github.com/kapoorankush/litclock.git"
        ) == ("kapoorankush", "litclock")

    def test_empty_owner_rejected(self):
        assert update_state._parse_origin_url("https://github.com//litclock.git") is None

    def test_space_in_component_rejected(self):
        # Unvalidated, this reached TAGS_URL_TEMPLATE and raised
        # http.client.InvalidURL — a repeating 500 on /api/update/check
        # and a burned confirm token on apply (/review litclock-dev#729, proved live).
        assert update_state._parse_origin_url("https://github.com/o wner/repo") is None

    def test_query_string_rejected(self):
        assert update_state._parse_origin_url("https://github.com/o/r?x=y.git") is None

    def test_dot_dot_component_rejected(self):
        # '..' is charset-legal but path-traverses api.github.com
        # (/repos/../notifications) with the device PAT attached.
        assert update_state._parse_origin_url("https://github.com/../repo") is None
        assert update_state._parse_origin_url("https://github.com/owner/..") is None

    def test_ssh_deep_path_rejected(self):
        assert update_state._parse_origin_url("git@github.com:o/r/extra") is None

    def test_host_spoof_via_userinfo_rejected(self):
        # "github.com@evil.example" — the userinfo strip must not let a
        # non-github host masquerade.
        assert update_state._parse_origin_url("https://github.com@evil.example/o/r") is None


class TestOriginRepoPairFailureBranches:
    """The subprocess exception arm and the unreadable-vs-unparsed warning
    split (/review litclock-dev#729: replacing the except body with `pass` left `url`
    unbound and shipped green — these pin the arm)."""

    def _ns(self, run):
        # Patch update_state's *view* of the subprocess module so the test
        # runner's own subprocess use is untouched.
        return SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError)

    def test_git_binary_missing_falls_back(self, tmp_path):
        with patch.object(update_state, "subprocess", self._ns(Mock(side_effect=FileNotFoundError("git")))):
            assert update_state.origin_repo_pair(tmp_path) == ("kapoorankush", "litclock")

    def test_git_timeout_falls_back(self, tmp_path):
        exc = subprocess.TimeoutExpired(cmd=["git"], timeout=10)
        with patch.object(update_state, "subprocess", self._ns(Mock(side_effect=exc))):
            assert update_state.origin_repo_pair(tmp_path) == ("kapoorankush", "litclock")

    def test_unreadable_and_unparsed_warnings_are_distinct(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="control_server.update_state"):
            update_state.origin_repo_pair(tmp_path / "nowhere")
        assert any("unreadable" in rec.getMessage() for rec in caplog.records)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="control_server.update_state"):
            update_state.origin_repo_pair(_repo_with_origin(tmp_path, "ssh://weird.example/foo"))
        assert any("unparsed" in rec.getMessage() for rec in caplog.records)

    def test_unparsed_warning_redacts_userinfo(self, tmp_path, caplog):
        # An unparseable CREDENTIALED origin must never echo the credential:
        # the raw record reaches stderr -> persistent journald BEFORE the
        # log-buffer RedactingFilter runs (/review litclock-dev#729 security pass).
        url = "https://gituser:supersecretvalue@github.com/o/r/extra"
        with caplog.at_level(logging.WARNING, logger="control_server.update_state"):
            pair = update_state.origin_repo_pair(_repo_with_origin(tmp_path, url))
        assert pair == ("kapoorankush", "litclock")
        joined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "supersecretvalue" not in joined
        assert "<redacted>" in joined

    def test_empty_string_litclock_dir_falls_through(self, tmp_path, monkeypatch):
        # `os.environ.get(...) or ...`: empty LITCLOCK_DIR must resolve via
        # the parents[2] fallback, not Path("").
        monkeypatch.setenv("LITCLOCK_DIR", "")
        fake = SimpleNamespace(returncode=0, stdout="https://github.com/someone/elsewhere.git\n")
        recorded: list[list[str]] = []

        def _run(argv, **kwargs):
            recorded.append(argv)
            return fake

        ns = SimpleNamespace(run=_run, SubprocessError=subprocess.SubprocessError)
        with patch.object(update_state, "subprocess", ns):
            assert update_state.origin_repo_pair() == ("someone", "elsewhere")
        repo_dir = Path(recorded[0][2])
        assert repo_dir == Path(update_state.__file__).resolve().parents[2]


class TestFetchersNeverRaiseOnMalformedPair:
    """Belt behind the slug validation: even a malformed pair must degrade
    to None, never 500 the route (/review litclock-dev#729 — uncaught
    http.client.InvalidURL, proved live). No network happens: urlopen
    rejects the URL before any connection."""

    def test_tags_fetch_returns_none(self):
        assert update_state.fetch_latest_release_tag("o wner", "repo") is None

    def test_release_notes_fetch_returns_none(self):
        assert update_state.fetch_release_notes("v1.0.0", "o wner", "repo") is None


class TestRecomputeAvailable:
    """Serve-time re-derivation (/review litclock-dev#729 adversarial pass): a cached
    boolean written under the pre-litclock-dev#728 different-means-available semantics
    would serve the lie for up to 6h after the fix deploys."""

    def test_stale_pre_fix_cache_is_corrected(self):
        payload = {"latest_tag": "v0.224.0", "available": True, "current_version": "v0.224.0"}
        out = update_state.recompute_available(payload, "v0.225.3")
        assert out["available"] is False
        assert out["current_version"] == "v0.225.3"

    def test_newer_tag_stays_available(self):
        payload = {"latest_tag": "v0.226.0", "available": False, "current_version": "v0.225.0"}
        assert update_state.recompute_available(payload, "v0.225.3")["available"] is True

    def test_network_failure_payload_keeps_none(self):
        payload = {"latest_tag": None, "available": None, "current_version": "v0.225.3"}
        assert update_state.recompute_available(payload, "v0.225.3")["available"] is None


def _extract_bash_origin_repo_pair() -> str:
    body = (Path(__file__).resolve().parents[1] / "scripts" / "update.sh").read_text()
    start = body.index("origin_repo_pair() {")
    end = body.index("\n}\n", start) + len("\n}\n")
    span = body[start:end]
    assert "git remote get-url origin" in span, "span lost the origin read"
    assert '"kapoorankush" "litclock"' in span, "span lost the fallback pair"
    return span


# One URL table, both implementations. Parity maintained by manually
# mirrored test cases drifts silently on the next one-sided tweak
# (/review litclock-dev#729 adversarial pass) — this drives the REAL bash function and
# the REAL Python full path from the same inputs and requires byte-equal
# answers.
PARITY_URLS = [
    "https://github.com/kapoorankush/litclock-dev.git",
    "https://github.com/kapoorankush/litclock-dev",
    "https://github.com/kapoorankush/litclock.git",
    "git@github.com:kapoorankush/litclock-dev.git",
    "https://github.com/kapoorankush/litclock-dev/",
    "https://github.com/o/r/extra",
    "ssh://weird.example/foo",
    "https://oauth2:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/kapoorankush/litclock-dev.git",
    "https://user:pass@github.com/kapoorankush/litclock.git",
    "https://github.com//litclock.git",
    "https://github.com/o wner/repo",
    "https://github.com/../notifications",
    "https://github.com/o/r?x=y.git",
    "  https://github.com/kapoorankush/litclock-dev.git  ",
    "git@github.com:o/r/extra",
    "https://github.com/o/r.git ",
    "https://github.com@evil.example/o/r",
    "https://GITHUB.com/kapoorankush/litclock-dev.git",
    "",
]


class TestPythonBashParity:
    def _bash_pair(self, url: str) -> str:
        script = "git() { printf '%s\\n' " + shlex.quote(url) + "; }\n"
        script += _extract_bash_origin_repo_pair()
        script += "\norigin_repo_pair\n"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def _python_pair(self, url: str) -> str:
        fake = SimpleNamespace(returncode=0, stdout=url + "\n")
        ns = SimpleNamespace(run=lambda *a, **k: fake, SubprocessError=subprocess.SubprocessError)
        with patch.object(update_state, "subprocess", ns):
            owner, repo = update_state.origin_repo_pair(Path("/irrelevant"))
        return f"{owner} {repo}"

    def test_both_parsers_agree_on_every_table_row(self):
        disagreements = []
        for url in PARITY_URLS:
            bash_out = self._bash_pair(url)
            python_out = self._python_pair(url)
            if bash_out != python_out:
                disagreements.append(f"{url!r}: bash={bash_out!r} python={python_out!r}")
        assert not disagreements, (
            "the two resolvers named different repos — the exact litclock-dev#728 "
            "bug class:\n" + "\n".join(disagreements)
        )

    def test_table_contains_both_verdict_kinds(self):
        # Self-check: a table where everything falls back (or everything
        # parses) would vacuously pass. Require both kinds present.
        outs = {self._python_pair(url) for url in PARITY_URLS}
        assert "kapoorankush litclock-dev" in outs
        assert "kapoorankush litclock" in outs

    def test_bash_unparsed_warning_redacts_userinfo(self):
        url = "https://gituser:supersecretvalue@github.com/o/r/extra"
        script = "git() { printf '%s\\n' " + shlex.quote(url) + "; }\n"
        script += _extract_bash_origin_repo_pair()
        script += "\norigin_repo_pair\n"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0
        assert "supersecretvalue" not in result.stderr
        assert "<redacted>" in result.stderr


class TestVersionDescribeAnchorsReleaseTags:
    """The dev repo carries non-release tags (dev-YYYYMMDD-*). If one is
    nearest to HEAD, an unanchored `git describe` yields an unparseable
    current_version and the availability compare degrades to
    different-means-available — the litclock-dev#728 lying card resurrected
    (/review litclock-dev#729 adversarial pass). Pin the --match anchor."""

    def test_describe_argv_carries_release_match(self):
        from control_server import version as version_mod  # noqa: PLC0415

        recorded: list[list[str]] = []

        def _run(argv, **kwargs):
            recorded.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="v0.225.3\n")

        version_mod.get_version.cache_clear()
        try:
            with patch.object(version_mod.subprocess, "run", _run):
                assert version_mod.get_version() == "v0.225.3"
        finally:
            version_mod.get_version.cache_clear()
        argv = recorded[0]
        assert "--match" in argv, "git describe lost its release-tag anchor"
        assert argv[argv.index("--match") + 1] == "v[0-9]*"
