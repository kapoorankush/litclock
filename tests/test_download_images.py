"""Tests for scripts/download_images.sh.

We run the real shell script against a local HTTP server that mimics the
GitHub REST API release/asset endpoints. The script gets pointed at the
mock server via LITCLOCK_API_BASE_URL, so no network is needed.

Mock server routes:
    GET /repos/{slug}/releases/tags/{tag}  -> JSON with asset IDs
    GET /repos/{slug}/releases/assets/{id} -> raw asset bytes (with
                                              Accept: application/octet-stream)
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "download_images.sh"
SCRIPT_LIB_DIR = REPO_ROOT / "scripts" / "lib"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeApiHandler(BaseHTTPRequestHandler):
    """Serves the minimal slice of the GitHub REST API that download_images.sh uses."""

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _auth_ok(self) -> bool:
        if not getattr(self.server, "require_auth", False):
            return True
        return self.headers.get("Authorization", "").startswith("Bearer ")

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            self.send_response(404)
            self.end_headers()
            return

        server = self.server
        slug = getattr(server, "slug", "kapoorankush/litclock")
        release = getattr(server, "release", {})

        # Route 1: release metadata by tag
        meta_prefix = f"/repos/{slug}/releases/tags/"
        if self.path.startswith(meta_prefix):
            tag = self.path[len(meta_prefix) :]
            if tag != release.get("tag"):
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(
                {
                    "tag_name": release["tag"],
                    "assets": [{"id": aid, "name": name} for name, (aid, _bytes) in release["assets"].items()],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Route 2: asset bytes by ID
        asset_prefix = f"/repos/{slug}/releases/assets/"
        if self.path.startswith(asset_prefix):
            try:
                asset_id = int(self.path[len(asset_prefix) :])
            except ValueError:
                self.send_response(404)
                self.end_headers()
                return
            for _name, (aid, data) in release.get("assets", {}).items():
                if aid == asset_id:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()


class _MockServer:
    def __init__(self, release: dict, slug: str = "kapoorankush/litclock", require_auth: bool = False):
        self.port = _free_port()
        self.httpd = HTTPServer(("127.0.0.1", self.port), _FakeApiHandler)
        self.httpd.release = release
        self.httpd.slug = slug
        self.httpd.require_auth = require_auth
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _build_tarball(src_dir: Path, out_path: Path) -> bytes:
    """Tar up src_dir as `images/`. Return the raw bytes of the tarball."""
    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(src_dir, arcname="images")
    return out_path.read_bytes()


def _write_byte_manifest(staging: Path) -> None:
    """Write a `files.sha256` sidecar listing every PNG under staging/.

    Mirrors what scripts/release_images.sh generates pre-tarball: relative
    `./` paths sorted deterministically so `sha256sum -c` can verify from
    within the extracted images/ dir.
    """
    lines: list[str] = []
    for f in sorted(staging.rglob("*.png")):
        rel = f.relative_to(staging)
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{digest}  ./{rel}\n")
    (staging / "files.sha256").write_text("".join(lines))


def _make_repo(tmp_path: Path, version: str = "v1") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".images-version").write_text(f"{version}\n")
    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts" / "download_images.sh")
    (root / "scripts" / "download_images.sh").chmod(0o755)
    # download_images.sh sources scripts/lib/github_api.sh relative to itself;
    # mirror that layout into the sandbox.
    shutil.copytree(SCRIPT_LIB_DIR, root / "scripts" / "lib")
    return root


def _make_release(
    tmp_path: Path,
    version: str = "v1",
    contents: dict[str, bytes] | None = None,
    corrupt_sha: bool = False,
    empty_sha: bool = False,
    missing_sha_asset: bool = False,
    bundle_byte_manifest: bool = True,
    tamper_byte_manifest: bool = False,
    bundle_corpus_manifest: bool = False,
    manifest_files: dict[str, str] | None = None,
) -> dict:
    """Build a release dict the mock server can serve.

    bundle_byte_manifest: include `files.sha256` inside the tarball (new
        release flow as of #293). Set False to simulate a legacy release.
    tamper_byte_manifest: include `files.sha256` but with an entry whose
        hash does not match the tarballed PNG bytes. Simulates the M7-retro
        silent-failure mode where extracted content diverges from manifest.
    bundle_corpus_manifest: include `manifest.json` (the corpus manifest, not
        the byte sidecar) inside the tarball. Drives the #313 consumer-side
        completeness check.
    manifest_files: when set, populate manifest.json's `files` map with the
        given {name: hash} pairs (use to ship a manifest claiming files NOT
        present in contents, exercising the completeness gap).
    """
    if contents is None:
        contents = {
            "metadata/quote_0000_0_credits.png": b"fake credits",
            "quote_0000_0.png": b"fake quote",
        }
    staging = tmp_path / f"{version}_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    for rel, body in contents.items():
        f = staging / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body)
    if bundle_corpus_manifest:
        if manifest_files is None:
            # Default: claim every main PNG in contents.
            manifest_files = {
                rel: hashlib.sha256(body).hexdigest()
                for rel, body in contents.items()
                if rel.endswith(".png") and not rel.startswith("metadata/")
            }
        (staging / "manifest.json").write_text(
            json.dumps({"corpus_hash": "0", "generator_hash": "0", "files": manifest_files}) + "\n"
        )
    if bundle_byte_manifest:
        _write_byte_manifest(staging)
        if tamper_byte_manifest:
            # Replace the manifest with one entry pointing at the wrong hash.
            sidecar = staging / "files.sha256"
            lines = sidecar.read_text().splitlines()
            assert lines, "byte manifest unexpectedly empty"
            parts = lines[0].split("  ", 1)
            lines[0] = ("0" * 64) + "  " + parts[1]
            sidecar.write_text("\n".join(lines) + "\n")
    tarball_path = tmp_path / f"litclock-images-{version}.tar.gz"
    tar_bytes = _build_tarball(staging, tarball_path)
    sha_hex = hashlib.sha256(tar_bytes).hexdigest()
    if corrupt_sha:
        sha_hex = "0" * 64
    sha_content = "" if empty_sha else f"{sha_hex}  litclock-images.tar.gz\n"
    assets = {"litclock-images.tar.gz": (1001, tar_bytes)}
    if not missing_sha_asset:
        assets["litclock-images.tar.gz.sha256"] = (1002, sha_content.encode())
    return {"tag": f"litclock-images-{version}", "assets": assets}


def _run(
    script_repo: Path,
    *,
    base_url: str,
    slug: str = "kapoorankush/litclock",
    extra_args: list[str] | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "LITCLOCK_API_BASE_URL": base_url,
        "LITCLOCK_REPO_SLUG": slug,
    }
    if env_extra:
        env.update(env_extra)
    cmd = [str(script_repo / "scripts" / "download_images.sh"), "--repo-root", str(script_repo)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30, check=False)


# ── Tests ────────────────────────────────────────────────────────────────


class TestInputValidation:
    def test_missing_pin_file_exits_1(self, tmp_path):
        root = tmp_path / "empty_repo"
        root.mkdir()
        (root / "scripts").mkdir()
        shutil.copy2(SCRIPT, root / "scripts" / "download_images.sh")
        shutil.copytree(SCRIPT_LIB_DIR, root / "scripts" / "lib")
        (root / "scripts" / "download_images.sh").chmod(0o755)
        result = _run(root, base_url="http://unused")
        assert result.returncode == 1
        assert ".images-version not found" in result.stderr

    def test_empty_pin_file_exits_1(self, tmp_path):
        root = _make_repo(tmp_path)
        (root / ".images-version").write_text("")
        result = _run(root, base_url="http://unused")
        assert result.returncode == 1
        assert "empty" in result.stderr.lower()


class TestShortCircuit:
    def test_marker_matches_pin_is_noop(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v1\n")
        result = _run(root, base_url="http://127.0.0.1:1")
        assert result.returncode == 0
        assert "already at v1" in result.stdout.lower()

    def test_force_overrides_short_circuit(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v1\n")
        server = _MockServer(_make_release(tmp_path, version="v1"))
        server.start()
        try:
            result = _run(root, base_url=server.base_url, extra_args=["--force"])
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "installed images at v1" in result.stdout.lower()


class TestHappyPath:
    def test_fresh_install_downloads_and_marks(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"metadata/quote_0000_0_credits.png": b"A", "quote_0000_0.png": b"B"},
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert (root / "images" / "quote_0000_0.png").read_bytes() == b"B"
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"

    def test_version_bump_replaces_images_and_wipes_stale(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v1\n")
        (root / "images" / "stale_v1_file.png").write_bytes(b"stale")
        (root / ".images-version").write_text("v2\n")
        release = _make_release(tmp_path, version="v2", contents={"quote_2200_31.png": b"new v2"})
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (root / "images" / ".installed-version").read_text().strip() == "v2"
        assert (root / "images" / "quote_2200_31.png").read_bytes() == b"new v2"
        assert not (root / "images" / "stale_v1_file.png").exists()

    def test_idempotent_on_rerun(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1")
        server = _MockServer(release)
        server.start()
        try:
            r1 = _run(root, base_url=server.base_url)
            r2 = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert r1.returncode == 0
        assert r2.returncode == 0
        assert "already at v1" in r2.stdout.lower()


class TestFailureModes:
    def test_missing_release_tag_is_graceful(self, tmp_path):
        root = _make_repo(tmp_path, version="v99")  # server only has v1
        release = _make_release(tmp_path, version="v1")
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0
        assert "failed to fetch release metadata" in (result.stdout + result.stderr).lower()
        assert not (root / "images" / ".installed-version").exists()

    def test_release_missing_tarball_asset(self, tmp_path):
        """Release exists but the tarball asset is missing from it."""
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1")
        # Remove the tarball asset entry.
        del release["assets"]["litclock-images.tar.gz"]
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0
        assert "missing expected assets" in (result.stdout + result.stderr).lower()

    def test_release_missing_sha_asset(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1", missing_sha_asset=True)
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0
        assert "missing expected assets" in (result.stdout + result.stderr).lower()

    def test_sha_mismatch_preserves_existing_images(self, tmp_path):
        root = _make_repo(tmp_path, version="v2")
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v1\n")
        (root / "images" / "keep_me.png").write_bytes(b"original")
        release = _make_release(tmp_path, version="v2", corrupt_sha=True)
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0
        assert "mismatch" in result.stderr.lower() or "mismatch" in result.stdout.lower()
        assert (root / "images" / "keep_me.png").read_bytes() == b"original"
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"

    def test_empty_sha_file_exits_1(self, tmp_path):
        """An empty .sha256 asset is a broken release, not a network failure — exit 1."""
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1", empty_sha=True)
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1
        assert "empty sha file" in result.stderr.lower() or "malformed" in result.stderr.lower()

    def test_network_unreachable_is_graceful(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        result = _run(root, base_url="http://127.0.0.1:1")
        assert result.returncode == 0
        assert "failed to fetch" in (result.stdout + result.stderr).lower()


class TestAuth:
    def test_auth_passed_from_gh_token(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1")
        server = _MockServer(release, require_auth=True)
        server.start()
        try:
            # Without token: server returns 404, script exits 0 gracefully, no marker written.
            r_noauth = _run(root, base_url=server.base_url)
            assert r_noauth.returncode == 0
            assert not (root / "images" / ".installed-version").exists()
            # With token: download succeeds and marker is written.
            r_auth = _run(root, base_url=server.base_url, env_extra={"GH_TOKEN": "fake-token"})
            assert r_auth.returncode == 0, f"stderr: {r_auth.stderr}"
            assert (root / "images" / ".installed-version").read_text().strip() == "v1"
        finally:
            server.stop()

    def test_github_token_env_var_also_works(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1")
        server = _MockServer(release, require_auth=True)
        server.start()
        try:
            result = _run(root, base_url=server.base_url, env_extra={"GITHUB_TOKEN": "fake-token"})
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"


class TestConcurrency:
    def test_flock_prevents_overlap(self, tmp_path):
        """A second invocation while the first holds the lock should skip gracefully."""
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1")
        server = _MockServer(release)
        server.start()
        try:
            # Prime the lock file by holding flock externally, then try to run download_images.sh.
            lockfile = root / ".litclock-images.lock"
            lockfile.touch()
            # Use a Python-level blocking file lock via fcntl to simulate another run holding it.
            import fcntl

            fd = lockfile.open("w")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = _run(root, base_url=server.base_url)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
        finally:
            server.stop()
        assert result.returncode == 0
        assert "another" in (result.stdout + result.stderr).lower()
        assert "in progress" in (result.stdout + result.stderr).lower()
        # Must NOT have downloaded — lock blocked us before fetch.
        assert not (root / "images" / ".installed-version").exists()


class TestAtomicSwap:
    def test_swap_is_same_filesystem(self, tmp_path):
        """Verify the staging dir is under $REPO_ROOT, not /tmp."""
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1")
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0
        # After success, no staging dir should remain under repo root.
        staging_dirs = list(root.glob(".litclock-images-staging.*"))
        assert not staging_dirs, f"staging dir leaked: {staging_dirs}"

    def test_tar_extraction_does_not_clobber_files_outside_repo(self, tmp_path):
        """A tarball with a symlink pointing outside the repo must not allow
        the script to overwrite files outside REPO_ROOT. The previous version
        of this test asserted `not Path('/etc/passwd.litclock_escaped').exists()`
        which always passes regardless of script behavior (#293 testing
        review). Replace with a real canary outside the repo.
        """
        canary = tmp_path / "canary_outside_repo"
        canary.write_text("original")
        root = _make_repo(tmp_path, version="v1")
        evil_staging = tmp_path / "evil_staging"
        evil_staging.mkdir()
        (evil_staging / "normal.png").write_bytes(b"ok")
        evil_tar = tmp_path / "evil.tar.gz"
        with tarfile.open(evil_tar, "w:gz") as t:
            t.add(evil_staging, arcname="images")
            # Symlink pointing at the canary outside the repo.
            ti = tarfile.TarInfo("images/escape")
            ti.type = tarfile.SYMTYPE
            ti.linkname = str(canary)
            t.addfile(ti)
        tar_bytes = evil_tar.read_bytes()
        sha_hex = hashlib.sha256(tar_bytes).hexdigest()
        release = {
            "tag": "litclock-images-v1",
            "assets": {
                "litclock-images.tar.gz": (2001, tar_bytes),
                "litclock-images.tar.gz.sha256": (2002, f"{sha_hex}  litclock-images.tar.gz\n".encode()),
            },
        }
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        # Critical assertion: the canary file outside the repo MUST NOT have
        # been overwritten or modified through the symlink.
        assert canary.read_text() == "original", "tar extraction clobbered a file outside REPO_ROOT through a symlink"
        # The script either succeeded (symlink stored as a dangling/inert link)
        # or exited 0 gracefully — what matters is the canary stayed intact.
        assert result.returncode == 0


class TestArgumentParsing:
    def test_unknown_argument_errors(self, tmp_path):
        root = _make_repo(tmp_path, version="v1")
        result = _run(root, base_url="http://unused", extra_args=["--bogus"])
        assert result.returncode == 1
        assert "unknown argument" in result.stderr.lower()


class TestByteIntegrityVerification:
    """Issue #293: download_images.sh must verify on-disk PNG bytes match the
    bundled files.sha256 sidecar before trusting either the version marker
    (short-circuit) or a fresh install. The M7-retro production incident
    showed both the marker and `Installed images at vN` log can lie when
    something goes wrong during extraction/swap.
    """

    def test_post_install_verification_passes(self, tmp_path):
        """Happy path: tarball ships files.sha256, all PNGs match → install OK."""
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"clean v1 content", "quote_0001_0.png": b"another"},
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "byte-integrity verification ok" in result.stdout.lower()
        assert (root / "images" / "files.sha256").exists()
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"

    def test_post_install_verification_fails_rolls_back(self, tmp_path):
        """Tampered files.sha256 inside tarball → verify fails → previous
        images restored at $IMAGES_DIR, exit 1.
        """
        root = _make_repo(tmp_path, version="v2")
        # Seed with a working v1 install we want preserved on rollback.
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v1\n")
        (root / "images" / "keep_me.png").write_bytes(b"v1 content")
        release = _make_release(
            tmp_path,
            version="v2",
            contents={"quote_0000_0.png": b"v2 content"},
            tamper_byte_manifest=True,
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "byte-integrity verification failed" in (result.stdout + result.stderr).lower()
        assert "rolled back" in (result.stdout + result.stderr).lower()
        # Previous content restored:
        assert (root / "images" / "keep_me.png").read_bytes() == b"v1 content"
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"
        # New (broken) content should NOT be on disk:
        assert not (root / "images" / "quote_0000_0.png").exists()
        # No orphan dirs left behind:
        assert not list(root.glob("images.broken.*"))
        assert not list(root.glob("images.old.*"))

    def test_post_install_verification_fails_no_previous_content(self, tmp_path):
        """Tampered manifest on a fresh install (no previous IMAGES_DIR) →
        rollback can't restore anything, but $IMAGES_DIR ends up empty (not
        partial/corrupt) and exit 1.
        """
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"v1 content"},
            tamper_byte_manifest=True,
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1
        assert "byte-integrity verification failed" in (result.stdout + result.stderr).lower()
        # The corrupted PNGs must not be left in place as if install succeeded.
        assert not (root / "images" / "quote_0000_0.png").exists()
        assert not (root / "images" / ".installed-version").exists()
        # No stale staging or broken-aside dirs:
        assert not list(root.glob("images.broken.*"))
        assert not list(root.glob("images.old.*"))

    def test_legacy_release_without_sidecar_proceeds_with_warning(self, tmp_path):
        """Releases v1–v4 predate the byte-manifest. Skip verification with a
        warning rather than refuse — existing Pis must still update cleanly.
        """
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(tmp_path, version="v1", bundle_byte_manifest=False)
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "did not include files.sha256" in (result.stdout + result.stderr).lower()
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"
        assert not (root / "images" / "files.sha256").exists()

    def test_short_circuit_bypassed_when_pngs_mutated(self, tmp_path):
        """Marker says we're at the pinned version, but on-disk PNGs have
        been mutated since install (the M7-retro shape). The bundled
        files.sha256 catches the drift and forces a re-download.
        """
        root = _make_repo(tmp_path, version="v1")
        # Install v1 cleanly.
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"correct v1 content"},
        )
        server = _MockServer(release)
        server.start()
        try:
            result1 = _run(root, base_url=server.base_url)
            assert result1.returncode == 0
            # Tamper with the on-disk PNG out-of-band, simulating partial
            # extract / disk corruption / stale-PNG silent failure.
            (root / "images" / "quote_0000_0.png").write_bytes(b"wrong content")
            # Re-run: marker still says v1, but files.sha256 will catch the drift.
            result2 = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result2.returncode == 0, f"stderr: {result2.stderr}\nstdout: {result2.stdout}"
        assert "forcing re-download" in (result2.stdout + result2.stderr).lower()
        # After re-download, the tampered PNG is replaced with the correct content.
        assert (root / "images" / "quote_0000_0.png").read_bytes() == b"correct v1 content"

    def test_short_circuit_skips_verification_on_legacy_install(self, tmp_path):
        """If a Pi was provisioned from a legacy release (no files.sha256 on
        disk) and the marker still matches the pin, accept the marker rather
        than try to re-download every cron tick.
        """
        root = _make_repo(tmp_path, version="v3")
        # Simulate a legacy install: images/ present with marker but no sidecar.
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v3\n")
        (root / "images" / "quote_0000_0.png").write_bytes(b"legacy content")
        # Point base_url at an unreachable port — script must not attempt download.
        result = _run(root, base_url="http://127.0.0.1:1")
        assert result.returncode == 0
        assert "already at v3" in result.stdout.lower()
        assert "marker-only check" in result.stdout.lower()

    def test_force_runs_verification_too(self, tmp_path):
        """--force bypasses the short-circuit but post-install verification
        must still run.
        """
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"v1 content"},
            tamper_byte_manifest=True,
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url, extra_args=["--force"])
        finally:
            server.stop()
        assert result.returncode == 1
        assert "byte-integrity verification failed" in (result.stdout + result.stderr).lower()

    def test_failed_filenames_logged_on_verify_failure(self, tmp_path):
        """When verification fails, the operator log must list which files
        diverged — at least one FAILED line. Without this, #293 silently
        recreates the M7-retro debugging gap (operator can't tell what's
        wrong, just that something is).
        """
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"v1 content", "quote_0001_0.png": b"another"},
            tamper_byte_manifest=True,
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1
        combined = (result.stdout + result.stderr).lower()
        assert "failed" in combined, f"verify-fail log lacks FAILED detail: {combined}"

    def test_empty_sidecar_in_tarball_treated_as_mismatch(self, tmp_path):
        """A release that ships a zero-byte files.sha256 must not silently pass
        verification. `sha256sum -c` on an empty file returns 1 ('no properly
        formatted checksum lines') — we treat that as mismatch / rollback.
        """
        root = _make_repo(tmp_path, version="v1")
        staging = tmp_path / "v1_empty_sidecar"
        staging.mkdir()
        (staging / "quote_0000_0.png").write_bytes(b"x")
        (staging / "files.sha256").write_text("")  # empty sidecar
        tar_path = tmp_path / "litclock-images-v1.tar.gz"
        with tarfile.open(tar_path, "w:gz") as t:
            t.add(staging, arcname="images")
        tar_bytes = tar_path.read_bytes()
        sha_hex = hashlib.sha256(tar_bytes).hexdigest()
        release = {
            "tag": "litclock-images-v1",
            "assets": {
                "litclock-images.tar.gz": (3001, tar_bytes),
                "litclock-images.tar.gz.sha256": (
                    3002,
                    f"{sha_hex}  litclock-images.tar.gz\n".encode(),
                ),
            },
        }
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1
        assert "byte-integrity verification failed" in (result.stdout + result.stderr).lower()

    def test_malformed_sidecar_line_treated_as_mismatch(self, tmp_path):
        """Garbage / truncated sidecar lines must not silently pass."""
        root = _make_repo(tmp_path, version="v1")
        staging = tmp_path / "v1_malformed_sidecar"
        staging.mkdir()
        (staging / "quote_0000_0.png").write_bytes(b"x")
        digest = hashlib.sha256(b"x").hexdigest()
        (staging / "files.sha256").write_text(
            f"{digest}  ./quote_0000_0.png\nthis is not a valid checksum line at all\n"
        )
        tar_path = tmp_path / "litclock-images-v1.tar.gz"
        with tarfile.open(tar_path, "w:gz") as t:
            t.add(staging, arcname="images")
        tar_bytes = tar_path.read_bytes()
        sha_hex = hashlib.sha256(tar_bytes).hexdigest()
        release = {
            "tag": "litclock-images-v1",
            "assets": {
                "litclock-images.tar.gz": (4001, tar_bytes),
                "litclock-images.tar.gz.sha256": (
                    4002,
                    f"{sha_hex}  litclock-images.tar.gz\n".encode(),
                ),
            },
        }
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1

    def test_sidecar_references_missing_file(self, tmp_path):
        """Sidecar lists a file not present in the tarball → sha256sum -c
        returns 1 ('No such file or directory') → treat as mismatch + rollback.
        """
        root = _make_repo(tmp_path, version="v1")
        staging = tmp_path / "v1_missing_file"
        staging.mkdir()
        (staging / "quote_0000_0.png").write_bytes(b"x")
        digest = hashlib.sha256(b"x").hexdigest()
        # Sidecar lists a phantom second file that's not in the tarball.
        (staging / "files.sha256").write_text(f"{digest}  ./quote_0000_0.png\n{'0' * 64}  ./phantom.png\n")
        tar_path = tmp_path / "litclock-images-v1.tar.gz"
        with tarfile.open(tar_path, "w:gz") as t:
            t.add(staging, arcname="images")
        tar_bytes = tar_path.read_bytes()
        sha_hex = hashlib.sha256(tar_bytes).hexdigest()
        release = {
            "tag": "litclock-images-v1",
            "assets": {
                "litclock-images.tar.gz": (5001, tar_bytes),
                "litclock-images.tar.gz.sha256": (
                    5002,
                    f"{sha_hex}  litclock-images.tar.gz\n".encode(),
                ),
            },
        }
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1
        assert "byte-integrity verification failed" in (result.stdout + result.stderr).lower()


class TestManifestCompletenessVerification:
    """Issue #313: download_images.sh's defense-in-depth completeness check.
    release_images.sh is the primary gate, but if a publisher somehow bypasses
    it (forced asset re-upload, manifest edit between regen and release), the
    consumer must catch a release whose `files.sha256` is internally consistent
    but doesn't cover everything `manifest.json` claims.
    """

    def test_post_install_completeness_catches_partial_release(self, tmp_path):
        """Sidecar passes bytes, but manifest.json claims a file that's not in
        the sidecar — verify must fail and roll back to the previous content.
        """
        root = _make_repo(tmp_path, version="v2")
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v1\n")
        (root / "images" / "keep_me.png").write_bytes(b"v1 content")
        release = _make_release(
            tmp_path,
            version="v2",
            contents={"quote_0000_0.png": b"v2"},
            bundle_corpus_manifest=True,
            manifest_files={
                "quote_0000_0.png": hashlib.sha256(b"v2").hexdigest(),
                # Claimed but not in contents → not in sidecar → completeness gap.
                "quote_1200_0.png": "deadbeef",
            },
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 1, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        combined = (result.stdout + result.stderr).lower()
        assert "release is partial" in combined or "manifest.json references" in combined
        # Previous content restored.
        assert (root / "images" / "keep_me.png").read_bytes() == b"v1 content"
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"

    def test_completeness_passes_when_manifest_matches_sidecar(self, tmp_path):
        """Happy path: manifest.json claims exactly what's in the tarball,
        sidecar covers everything claimed → install proceeds."""
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={
                "quote_0000_0.png": b"main",
                "metadata/quote_0000_0_credits.png": b"credits",
            },
            bundle_corpus_manifest=True,
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"

    def test_completeness_skips_when_manifest_absent(self, tmp_path):
        """Legacy releases (no manifest.json bundled) must still install
        cleanly — the consumer can't verify a claim that wasn't made.
        """
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"v1"},
            bundle_corpus_manifest=False,
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert (root / "images" / ".installed-version").read_text().strip() == "v1"


class TestOfflineFailQuarantine:
    """Issue #314: when short-circuit verify-fail meets re-download-offline,
    the corrupt content must NOT keep rendering. Quarantine + degrade to
    time-only is the honest UX signal.
    """

    def _seed_clean_v1(self, tmp_path):
        """Helper: clean v1 install via the real script + mock server, then
        return the root + marker_file path."""
        root = _make_repo(tmp_path, version="v1")
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"correct"},
        )
        server = _MockServer(release)
        server.start()
        try:
            r = _run(root, base_url=server.base_url)
        finally:
            server.stop()
        assert r.returncode == 0, f"seed install failed: {r.stderr}"
        return root

    def test_verify_fail_plus_metadata_fetch_fail_quarantines(self, tmp_path):
        """Marker matches, on-disk verify fails, GitHub API unreachable.
        $IMAGES_DIR must be quarantined; update-failed marker must be set.
        """
        root = self._seed_clean_v1(tmp_path)
        (root / "images" / "quote_0000_0.png").write_bytes(b"tampered")
        marker_file = tmp_path / "update-failed"
        marker_file.parent.mkdir(exist_ok=True)
        result = _run(
            root,
            base_url="http://127.0.0.1:1",
            env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
        )
        assert result.returncode == 0, f"graceful-offline must still exit 0: {result.stderr}"
        assert not (root / "images" / "quote_0000_0.png").exists(), (
            "corrupt PNG must not remain at $IMAGES_DIR after quarantine"
        )
        assert list(root.glob("images.failed.*")), "$IMAGES_DIR should have been quarantined to images.failed.<ts>"
        assert marker_file.exists(), "update-failed marker must be set"

    def test_offline_with_clean_images_does_not_quarantine(self, tmp_path):
        """Marker matches AND on-disk verify passes — even with no network,
        the script must NOT touch $IMAGES_DIR. Confirms quarantine fires
        only when content is actually corrupt.
        """
        root = self._seed_clean_v1(tmp_path)
        marker_file = tmp_path / "update-failed"
        result = _run(
            root,
            base_url="http://127.0.0.1:1",
            env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
        )
        assert result.returncode == 0
        assert (root / "images" / "quote_0000_0.png").exists()
        assert not list(root.glob("images.failed.*"))
        assert not marker_file.exists()

    def test_offline_with_no_existing_images_still_exits_0(self, tmp_path):
        """Fresh Pi, no $IMAGES_DIR yet, network down — must NOT quarantine
        (nothing to quarantine), must still exit 0 with no marker set."""
        root = _make_repo(tmp_path, version="v1")
        marker_file = tmp_path / "update-failed"
        result = _run(
            root,
            base_url="http://127.0.0.1:1",
            env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
        )
        assert result.returncode == 0
        assert not list(root.glob("images.failed.*"))
        assert not marker_file.exists()

    def test_verify_fail_plus_sha_mismatch_quarantines(self, tmp_path):
        """Marker matches, on-disk verify fails, mock server returns a
        tarball with corrupt SHA. Today's behavior: log mismatch and exit 0.
        Under #314: quarantine the corrupt $IMAGES_DIR too."""
        root = self._seed_clean_v1(tmp_path)
        (root / "images" / "quote_0000_0.png").write_bytes(b"tampered")
        # Serve a release with corrupt SHA so the tarball download succeeds
        # but the SHA verify fails.
        release = _make_release(tmp_path, version="v1", corrupt_sha=True)
        server = _MockServer(release)
        server.start()
        try:
            marker_file = tmp_path / "update-failed"
            result = _run(
                root,
                base_url=server.base_url,
                env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
            )
        finally:
            server.stop()
        assert result.returncode == 0
        assert list(root.glob("images.failed.*"))
        assert marker_file.exists()

    def test_successful_install_clears_update_failed_marker(self, tmp_path):
        """After a quarantine event, the next successful install must clear
        the update-failed marker so the e-ink stops rendering the '!' glyph."""
        root = self._seed_clean_v1(tmp_path)
        marker_file = tmp_path / "update-failed"
        marker_file.touch()  # pretend a prior run quarantined.
        # Run a successful install (network up).
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"correct"},
        )
        server = _MockServer(release)
        server.start()
        try:
            result = _run(
                root,
                base_url=server.base_url,
                extra_args=["--force"],
                env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
            )
        finally:
            server.stop()
        assert result.returncode == 0
        assert not marker_file.exists(), "marker must be cleared on successful install"

    def test_quarantine_dirs_bounded_at_three(self, tmp_path):
        """The orphan-sweep keeps the newest 3 `.failed.*` dirs and prunes the rest.
        Stale dirs older than the newest 3 must be removed on next script entry.
        """
        root = _make_repo(tmp_path, version="v1")
        (root / "images").mkdir()
        (root / "images" / ".installed-version").write_text("v1\n")
        for ts in (
            "20260101T000000Z",
            "20260201T000000Z",
            "20260301T000000Z",
            "20260401T000000Z",
            "20260501T000000Z",
        ):
            d = root / f"images.failed.{ts}"
            d.mkdir()
            (d / "marker").write_text(ts)
        # Marker matches pinned version → short-circuit verify succeeds (no PNGs needed
        # since sidecar absent → rc=2 legacy path → exits cleanly before any download).
        result = _run(root, base_url="http://127.0.0.1:1")
        assert result.returncode == 0
        remaining = sorted(p.name for p in root.glob("images.failed.*"))
        assert remaining == [
            "images.failed.20260301T000000Z",
            "images.failed.20260401T000000Z",
            "images.failed.20260501T000000Z",
        ], f"expected the 3 newest, got: {remaining}"

    def test_double_verify_fail_does_not_restore_corrupt_old_dir(self, tmp_path):
        """The subtle interaction: verify-fail-at-short-circuit drives us
        into the download path, the freshly-downloaded content ALSO fails
        post-install verify. Today's rollback would restore OLD_DIR (the
        original corrupt content). Under #314 the rollback must NOT do that —
        quarantine OLD_DIR too, leave $IMAGES_DIR empty, set marker.
        """
        root = self._seed_clean_v1(tmp_path)
        # Corrupt the on-disk content so short-circuit verify fails.
        (root / "images" / "quote_0000_0.png").write_bytes(b"tampered")
        # Serve a release whose post-install verify ALSO fails.
        release = _make_release(
            tmp_path,
            version="v1",
            contents={"quote_0000_0.png": b"new_v1"},
            tamper_byte_manifest=True,
        )
        server = _MockServer(release)
        server.start()
        try:
            marker_file = tmp_path / "update-failed"
            result = _run(
                root,
                base_url=server.base_url,
                env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
            )
        finally:
            server.stop()
        assert result.returncode == 1
        combined = (result.stdout + result.stderr).lower()
        assert "byte-integrity verification failed" in combined
        assert "previous content was also corrupt" in combined, "double-verify-fail path must not silently roll back"
        # $IMAGES_DIR must be empty (not restored to the corrupt OLD_DIR).
        assert not (root / "images").exists() or not any((root / "images").iterdir())
        # Both old and new content quarantined.
        failed = sorted(p.name for p in root.glob("images.failed.*"))
        assert any(name.endswith(".prev") for name in failed), (
            f"expected a `.prev` quarantine dir for OLD_DIR, got: {failed}"
        )
        assert marker_file.exists(), "update-failed marker must be set after double-verify-fail"

    def test_verify_fail_plus_empty_sha_asset_quarantines(self, tmp_path):
        """#314 F2: when the publisher uploads an empty SHA file AND the
        on-disk content is already known-corrupt, the script must quarantine
        before exit 1. Without this, a single bad release would leave the
        fleet rendering corrupt PNGs with no "!" glyph signal until the
        publisher cuts a fix."""
        root = self._seed_clean_v1(tmp_path)
        (root / "images" / "quote_0000_0.png").write_bytes(b"tampered")
        # Publisher error: empty .sha256 asset.
        release = _make_release(tmp_path, version="v1", empty_sha=True)
        server = _MockServer(release)
        server.start()
        try:
            marker_file = tmp_path / "update-failed"
            result = _run(
                root,
                base_url=server.base_url,
                env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
            )
        finally:
            server.stop()
        # Empty SHA is a publisher error → exit 1 (unchanged contract).
        assert result.returncode == 1
        assert "empty sha" in (result.stdout + result.stderr).lower()
        # But the corrupt on-disk content must have been quarantined too.
        assert list(root.glob("images.failed.*")), "empty-SHA exit path must quarantine corrupt on-disk content"
        assert marker_file.exists()

    def test_marker_set_when_parent_dir_does_not_exist(self, tmp_path):
        """#314 F10: _set_update_failed_marker must create the parent dir
        if missing, not silently no-op. A botched install that didn't
        create /var/lib/litclock would otherwise swallow every glyph signal
        forever."""
        root = self._seed_clean_v1(tmp_path)
        (root / "images" / "quote_0000_0.png").write_bytes(b"tampered")
        # Point marker at a path whose parent dir does NOT exist yet.
        marker_dir = tmp_path / "litclock-state-nonexistent"
        marker_file = marker_dir / "update-failed"
        assert not marker_dir.exists()
        result = _run(
            root,
            base_url="http://127.0.0.1:1",
            env_extra={"LITCLOCK_UPDATE_FAILED_MARKER": str(marker_file)},
        )
        assert result.returncode == 0
        assert list(root.glob("images.failed.*")), "quarantine should have fired"
        assert marker_dir.exists(), "parent dir must have been created"
        assert marker_file.exists(), "marker must have been written even though parent dir was missing"
