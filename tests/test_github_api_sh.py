"""Tests for scripts/lib/github_api.sh (issues #209 + #247).

Covers `github_api_latest_release_tag` semantics against a local mock
HTTP server. The resolver hits /tags and selects the highest-semver
release-shaped tag (vX.Y.Z). All failure modes exit 0 with empty stdout
(graceful-offline) so an offline Pi never breaks mid-update.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib" / "github_api.sh"


# ── Mock HTTP server ──────────────────────────────────────────────────


class _ReleaseResponse:
    """Declarative response for the mock — status code, headers, body."""

    def __init__(self, status: int = 200, body: Any = None, raw_body: bytes | None = None, delay: float = 0.0):
        self.status = status
        self.body = body
        self.raw_body = raw_body
        self.delay = delay


class _Handler(BaseHTTPRequestHandler):
    # Each test fixture sets .response on the class via _MockServer.
    response: _ReleaseResponse = _ReleaseResponse()
    last_auth_header: str | None = None

    def do_GET(self):  # noqa: N802
        # Capture Authorization header so tests can assert auth plumbing.
        _Handler.last_auth_header = self.headers.get("Authorization")
        if _Handler.response.delay:
            import time

            time.sleep(_Handler.response.delay)
        self.send_response(_Handler.response.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if _Handler.response.raw_body is not None:
            self.wfile.write(_Handler.response.raw_body)
        elif _Handler.response.body is not None:
            self.wfile.write(json.dumps(_Handler.response.body).encode())

    def log_message(self, format, *args):  # silence stderr spew
        pass


class _MockServer:
    def __init__(self, response: _ReleaseResponse):
        self.response = response
        _Handler.response = response
        _Handler.last_auth_header = None
        # Port 0 — let the OS pick a free one.
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# ── Runner ────────────────────────────────────────────────────────────


def _run_tag_resolver(
    *,
    base_url: str,
    owner: str = "kapoorankush",
    repo: str = "litclock",
    timeout: int = 2,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke github_api_latest_release_tag against the given base URL."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "LITCLOCK_API_BASE_URL": base_url,
        "LITCLOCK_GITHUB_API_TIMEOUT": str(timeout),
    }
    if env_extra:
        env.update(env_extra)
    cmd = [
        "bash",
        "-c",
        # Reset the auth-args cache so each test run sees a fresh GH_TOKEN/GITHUB_TOKEN.
        f". {LIB}; _LITCLOCK_GITHUB_AUTH_ARGS_BUILT=0; github_api_latest_release_tag '{owner}' '{repo}'",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15, check=False)


# ── Tests ─────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_200_ok_returns_tag(self):
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.2.3"}]))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == "v1.2.3"

    def test_picks_highest_semver_when_multiple(self):
        """/tags is commit-date-ordered, not semver. The resolver must sort."""
        server = _MockServer(
            _ReleaseResponse(
                200,
                body=[
                    {"name": "v0.9.99"},
                    {"name": "v1.0.0"},
                    {"name": "v0.10.5"},
                    {"name": "v1.2.3"},
                    {"name": "v1.10.0"},
                    {"name": "v1.2.4"},
                ],
            )
        )
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        # 1.10.0 > 1.2.4 (semver, not lex). Critical regression check.
        assert r.stdout.strip() == "v1.10.0"

    def test_excludes_prerelease_suffixes(self):
        """vX.Y.Z-rcN, -alpha, -beta must not be selected as latest."""
        server = _MockServer(
            _ReleaseResponse(
                200,
                body=[
                    {"name": "v0.209.0-rc2"},
                    {"name": "v0.209.0-rc1"},
                    {"name": "v0.208.0"},
                ],
            )
        )
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == "v0.208.0"

    def test_excludes_non_release_tags(self):
        """Asset tags (litclock-images-v3), QA bridge tags (qa-209-rc1.x),
        and rollback markers (safe-before-issue-N) must not be considered."""
        server = _MockServer(
            _ReleaseResponse(
                200,
                body=[
                    {"name": "litclock-images-v3"},
                    {"name": "qa-209-rc1.3"},
                    {"name": "safe-before-issue-160"},
                    {"name": "v0.207.1"},
                ],
            )
        )
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == "v0.207.1"


class TestFailureModes:
    def test_404_no_releases_empty_stdout(self):
        server = _MockServer(_ReleaseResponse(404, body={"message": "Not Found"}))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url, env_extra={"GH_TOKEN": "ghp_x"})
        finally:
            server.stop()
        assert r.returncode == 0  # graceful-offline
        assert r.stdout.strip() == ""
        assert "warn" in r.stderr.lower()
        # Auth must still be plumbed even on failure paths — regression
        # guard against "auth was sent" silently decoupling from "parsing
        # succeeded" if the request-construction layer ever changes.
        assert _Handler.last_auth_header == "Bearer ghp_x"

    def test_500_server_error_empty_stdout(self):
        server = _MockServer(_ReleaseResponse(500, body={"message": "boom"}))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_network_timeout_empty_stdout(self):
        """Server delays longer than the resolver's --max-time."""
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}], delay=3.0))
        server.start()
        try:
            # timeout=1 → curl --max-time 1 < server delay 3
            r = _run_tag_resolver(base_url=server.base_url, timeout=1)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_malformed_json_empty_stdout(self):
        server = _MockServer(_ReleaseResponse(200, raw_body=b"{not valid json"))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_empty_response_body_empty_stdout(self):
        server = _MockServer(_ReleaseResponse(200, raw_body=b""))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_response_is_object_not_array_empty_stdout(self):
        """/tags returns an array. If GitHub ever returns an object shape
        (or we're pointed at the wrong endpoint), reject gracefully."""
        server = _MockServer(_ReleaseResponse(200, body={"message": "not an array"}))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_empty_array_empty_stdout(self):
        """Repo with no tags at all (fresh init) — graceful-offline."""
        server = _MockServer(_ReleaseResponse(200, body=[]))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_no_release_shaped_tags_empty_stdout(self):
        """All tags exist but none match vX.Y.Z (e.g. only QA bridge tags)."""
        server = _MockServer(
            _ReleaseResponse(
                200,
                body=[
                    {"name": "litclock-images-v3"},
                    {"name": "qa-209-rc1.3"},
                    {"name": "v0.209.0-rc1"},  # prerelease, excluded
                ],
            )
        )
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""
        # Operator must be able to tell this from "json parse error" or
        # "response is not a JSON array" — the warn surfaces the python
        # detail so misconfigurations are debuggable from the journal.
        assert "no release-shaped tags" in r.stderr

    def test_warn_surfaces_python_reason_for_object_response(self):
        """When the response is the wrong shape, the bash warn must include
        the python detail rather than collapsing all parse failures to one
        generic message."""
        server = _MockServer(_ReleaseResponse(200, body={"message": "not an array"}))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""
        assert "not a JSON array" in r.stderr

    def test_warn_surfaces_python_reason_for_malformed_json(self):
        server = _MockServer(_ReleaseResponse(200, raw_body=b"{not valid json"))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""
        assert "json parse error" in r.stderr

    def test_shell_metachars_excluded_by_strict_regex(self):
        """A tag like `v1.0; rm -rf /` doesn't match ^v\\d+\\.\\d+\\.\\d+$,
        so it is silently dropped at the filter stage. The whitelist would
        also catch it as a second line of defense."""
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0; rm -rf /"}]))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_leading_dash_excluded_by_strict_regex(self):
        """Defense in depth: a tag starting with `-` would be interpreted as
        a flag by `git rev-list -n 1 <tag>`. The strict semver regex
        already excludes these (must start with `v`), but verify."""
        for bad in ["-rf", "--exec=foo", "-version"]:
            server = _MockServer(_ReleaseResponse(200, body=[{"name": bad}]))
            server.start()
            try:
                r = _run_tag_resolver(base_url=server.base_url)
            finally:
                server.stop()
            assert r.returncode == 0
            assert r.stdout.strip() == "", f"leading-dash tag {bad!r} should be rejected"

    def test_403_rate_limited_empty_stdout(self):
        server = _MockServer(
            _ReleaseResponse(
                403,
                body={"message": "API rate limit exceeded"},
            )
        )
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_unreachable_host_empty_stdout(self):
        """Point at a port nothing is listening on — must not hang."""
        # Grab a port, bind+close so nothing is listening.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = _run_tag_resolver(base_url=f"http://127.0.0.1:{port}", timeout=2)
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestAuth:
    def test_gh_token_forwarded_to_api(self):
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url, env_extra={"GH_TOKEN": "ghp_test_123"})
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == "v1.0.0"
        assert _Handler.last_auth_header == "Bearer ghp_test_123"

    def test_github_token_fallback(self):
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url, env_extra={"GITHUB_TOKEN": "ghs_test_456"})
        finally:
            server.stop()
        assert r.returncode == 0
        assert _Handler.last_auth_header == "Bearer ghs_test_456"

    def test_no_auth_header_when_no_token(self):
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0
        assert _Handler.last_auth_header is None

    def test_git_credentials_fallback_used_when_env_empty(self, tmp_path):
        """Regression for #209 hardware-found defect (PR #237): the systemd
        timer-driven update path runs with clean env (no ~/.profile), so
        GH_TOKEN is empty even when the token is present in the standard
        ~/.git-credentials file. The helper must fall back to parsing
        that file so the REST API call uses the same auth `git pull`
        already uses."""
        creds = tmp_path / ".git-credentials"
        creds.write_text("https://kapoorankush:ghp_credentials_fallback@github.com\n")
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(
                base_url=server.base_url,
                env_extra={"LITCLOCK_GIT_CREDENTIALS": str(creds)},
            )
        finally:
            server.stop()
        assert r.returncode == 0
        assert r.stdout.strip() == "v1.0.0"
        assert _Handler.last_auth_header == "Bearer ghp_credentials_fallback"

    def test_env_tokens_take_precedence_over_credentials_file(self, tmp_path):
        """Resolution order: GH_TOKEN > GITHUB_TOKEN > ~/.git-credentials.
        An env override must NOT be silently shadowed by a stale token in
        the credentials file."""
        creds = tmp_path / ".git-credentials"
        creds.write_text("https://kapoorankush:ghp_FILE_TOKEN@github.com\n")
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(
                base_url=server.base_url,
                env_extra={
                    "GH_TOKEN": "ghp_ENV_WINS",
                    "LITCLOCK_GIT_CREDENTIALS": str(creds),
                },
            )
        finally:
            server.stop()
        assert r.returncode == 0
        assert _Handler.last_auth_header == "Bearer ghp_ENV_WINS"

    def test_credentials_file_only_matches_github_com(self, tmp_path):
        """A token entry for a non-github.com host (e.g. a self-hosted
        Gitea) must not be picked up — we only know how to talk to
        github.com here."""
        creds = tmp_path / ".git-credentials"
        creds.write_text("https://user:tok_GITLAB@gitlab.example.com\nhttps://user:tok_GITEA@gitea.internal\n")
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(
                base_url=server.base_url,
                env_extra={"LITCLOCK_GIT_CREDENTIALS": str(creds)},
            )
        finally:
            server.stop()
        assert r.returncode == 0
        # No github.com line → no token → no Authorization header
        assert _Handler.last_auth_header is None

    def test_first_github_com_entry_wins(self, tmp_path):
        """If multiple github.com entries somehow exist, take the first."""
        creds = tmp_path / ".git-credentials"
        creds.write_text("https://kapoorankush:tok_FIRST@github.com\nhttps://otheruser:tok_SECOND@github.com\n")
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(
                base_url=server.base_url,
                env_extra={"LITCLOCK_GIT_CREDENTIALS": str(creds)},
            )
        finally:
            server.stop()
        assert r.returncode == 0
        assert _Handler.last_auth_header == "Bearer tok_FIRST"

    def test_missing_credentials_file_no_auth_no_error(self, tmp_path):
        """When ~/.git-credentials doesn't exist (public-repo install,
        anon REST works fine), helper must stay silent."""
        missing = tmp_path / "does-not-exist"
        server = _MockServer(_ReleaseResponse(200, body=[{"name": "v1.0.0"}]))
        server.start()
        try:
            r = _run_tag_resolver(
                base_url=server.base_url,
                env_extra={"LITCLOCK_GIT_CREDENTIALS": str(missing)},
            )
        finally:
            server.stop()
        assert r.returncode == 0
        assert _Handler.last_auth_header is None


class TestStructural:
    """Grep invariants — defense-in-depth against someone silently removing
    the graceful-offline discipline from the library."""

    @pytest.fixture(scope="class")
    def lib_content(self):
        return LIB.read_text()

    def test_return_zero_on_all_failures(self, lib_content):
        # Every `return` inside github_api_latest_release_tag must be `return 0`.
        import re

        fn = re.search(r"github_api_latest_release_tag\(\)\s*\{(.*?)^\}", lib_content, re.DOTALL | re.MULTILINE)
        assert fn, "function body not found"
        body = fn.group(1)
        returns = re.findall(r"\breturn\s+(\S+)", body)
        assert returns, "no return statements at all — that's also wrong"
        for rc in returns:
            assert rc == "0", f"graceful-offline violated: found `return {rc}`"

    def test_curl_uses_max_time(self, lib_content):
        assert "--max-time" in lib_content, "curl must use --max-time for graceful offline"

    def test_curl_uses_fail(self, lib_content):
        # -fsSL includes --fail
        assert "-fsSL" in lib_content, "curl must use --fail (via -fsSL) so 4xx/5xx exit non-zero"
