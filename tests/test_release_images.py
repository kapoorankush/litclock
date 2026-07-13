"""Tests for scripts/release_images.sh.

The script calls `gh` and `git`. We stub `gh` with a fake that records its
args to a file, so we can assert what got called without needing a real
GitHub account. `git` runs against a real tmp repo.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_images.sh"


def _write_fake_gh(bin_dir: Path, log_file: Path, view_exits: int = 1, create_exits: int = 0) -> None:
    """Write a fake gh executable. view exit 1 = release does not exist (the happy case)."""
    fake = textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> "{log_file}"
        case "$1" in
            release)
                case "$2" in
                    view) exit {view_exits} ;;
                    create) exit {create_exits} ;;
                esac
                ;;
        esac
        exit 0
    """)
    gh = bin_dir / "gh"
    gh.write_text(fake)
    gh.chmod(0o755)


def _write_fake_gh_capturing(bin_dir: Path, log_file: Path, capture_dir: Path) -> None:
    """Like _write_fake_gh but additionally copies any positional file args
    (the tarball, sidecar, manifest) passed to `gh release create` into
    `capture_dir` so the test can introspect what would have been uploaded.
    """
    fake = textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> "{log_file}"
        if [[ "$1" == "release" && "$2" == "create" ]]; then
            shift 2
            while [[ "$#" -gt 0 ]]; do
                if [[ -f "$1" ]]; then cp "$1" "{capture_dir}/"; fi
                shift
            done
        fi
        case "$1$2" in
            releaseview) exit 1 ;;
        esac
        exit 0
    """)
    gh = bin_dir / "gh"
    gh.write_text(fake)
    gh.chmod(0o755)


def _make_repo_with_images(
    tmp_path: Path,
    dirty: bool = False,
    with_images: bool = True,
    with_manifest: bool = True,
    manifest_files: dict[str, str] | None = None,
    extra_pngs: dict[str, bytes] | None = None,
) -> Path:
    """Real git repo with scripts/ + optionally images/ + optionally manifest.json.

    The default manifest claims `{quote_0000_0.png: <hash>}` to match the
    default seeded PNG pair — release_images.sh's #313 completeness gate
    refuses an empty `files` map, so the default fixture must mirror a real
    publisher state (every seeded main PNG appears in manifest, every claimed
    main PNG has its credits sibling on disk).

    `manifest_files` overrides the default {name: hash} map (use {} to test
    the empty-manifest refuse-to-publish path).
    `extra_pngs` adds additional PNGs to images/ on top of the default pair
    (keys are paths relative to images/; values are bytes).
    """
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)

    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts" / "release_images.sh")
    (root / "scripts" / "release_images.sh").chmod(0o755)

    if with_images:
        (root / "images").mkdir()
        (root / "images" / "quote_0000_0.png").write_bytes(b"a")
        (root / "images" / "metadata").mkdir()
        (root / "images" / "metadata" / "quote_0000_0_credits.png").write_bytes(b"b")
        if extra_pngs:
            for rel, body in extra_pngs.items():
                p = root / "images" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(body)
        if with_manifest:
            # Post-#299 release_images.sh refuses to publish without a manifest.
            # Post-#313 it also refuses an empty `files` map; default seeds a
            # claim matching the seeded main PNG.
            if manifest_files is None:
                files_map = {"quote_0000_0.png": hashlib.sha256(b"a").hexdigest()}
            else:
                files_map = manifest_files
            manifest = {
                "corpus_hash": "0",
                "generator_hash": "0",
                "files": files_map,
            }
            (root / "images" / "manifest.json").write_text(json.dumps(manifest) + "\n")

    (root / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

    if dirty:
        (root / "README.md").write_text("dirty\n")

    return root


def _run(repo: Path, version: str, bin_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(repo),  # keep away from the real gh config
        "PATH": f"{bin_dir}:/usr/bin:/bin" if bin_dir else "/usr/bin:/bin",
    }
    # sha256sum and tar need to be findable.
    for extra in ("/usr/local/bin", "/usr/sbin"):
        if Path(extra).is_dir():
            env["PATH"] += f":{extra}"
    return subprocess.run(
        [str(repo / "scripts" / "release_images.sh"), version],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


class TestValidation:
    def test_missing_version_arg_errors(self, tmp_path):
        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        result = subprocess.run(
            [str(repo / "scripts" / "release_images.sh")],
            cwd=repo,
            capture_output=True,
            text=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            timeout=10,
            check=False,
        )
        assert result.returncode == 1
        assert "usage" in result.stderr.lower()

    def test_bad_version_format_errors(self, tmp_path):
        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        result = _run(repo, "2.0", bin_dir=bin_dir)
        assert result.returncode == 1
        assert "must match" in result.stderr.lower()

    def test_dirty_tree_aborts(self, tmp_path):
        repo = _make_repo_with_images(tmp_path, dirty=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        assert "uncommitted" in result.stderr.lower()
        # gh must not have been called.
        assert not log.exists() or log.read_text() == ""

    def test_missing_images_dir_aborts(self, tmp_path):
        repo = _make_repo_with_images(tmp_path, with_images=False)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        assert "no images" in result.stderr.lower() or "no images" in result.stdout.lower()

    def test_missing_manifest_aborts(self, tmp_path):
        """#299: refuse to publish a release without images/manifest.json — the
        corpus-integrity CI gate would later fail PRs against the resulting
        tag, but the tag would already be occupied, blocking a clean rerun."""
        repo = _make_repo_with_images(tmp_path, with_manifest=False)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        assert "manifest.json missing" in result.stderr.lower() or "refuse to release" in result.stderr.lower()
        # gh release create must NOT have been called (the pre-flight `gh release view`
        # is allowed; what we're guarding is the actual publish).
        log_contents = log.read_text() if log.exists() else ""
        assert "release create" not in log_contents


class TestPreflight:
    def test_existing_release_aborts(self, tmp_path):
        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log, view_exits=0)  # 0 = release exists
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        assert "already exists" in result.stderr.lower() or "already exists" in result.stdout.lower()
        lines = log.read_text().splitlines() if log.exists() else []
        assert any("release view litclock-images-v1" in line for line in lines)
        # No create call should have happened.
        assert not any("release create" in line for line in lines)


class TestHappyPath:
    def test_creates_release_with_both_assets(self, tmp_path):
        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log, view_exits=1, create_exits=0)
        result = _run(repo, "v2", bin_dir=bin_dir)
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        log_text = log.read_text()
        # Must view-check first.
        assert log_text.splitlines()[0].startswith("release view litclock-images-v2")
        # Then create with the tag and both asset paths somewhere in the captured
        # args (multi-line release notes make per-line matching fragile).
        assert "release create litclock-images-v2" in log_text
        assert "litclock-images.tar.gz" in log_text
        assert ".sha256" in log_text
        # User-facing output must tell them what to do next.
        assert ".images-version" in result.stdout

    def test_release_notes_include_git_sha(self, tmp_path):
        """Release notes must embed the git commit SHA for provenance."""
        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        expected_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 0
        log_text = log.read_text()
        assert expected_sha in log_text, "release notes must include the git SHA"

    def test_rejects_staged_changes(self, tmp_path):
        """Staged changes to tracked files block the release."""
        repo = _make_repo_with_images(tmp_path)
        (repo / "README.md").write_text("staged change\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        combined = (result.stderr + result.stdout).lower()
        assert "uncommitted" in combined or "staged" in combined

    def test_allows_untracked_files(self, tmp_path):
        """Untracked files (backups, scratch) don't affect the tarball — don't block."""
        repo = _make_repo_with_images(tmp_path)
        (repo / "scratch.txt").write_text("untracked\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_prints_tarball_size(self, tmp_path):
        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 0
        assert "tarball size" in result.stdout.lower()

    def test_generates_byte_integrity_manifest(self, tmp_path):
        """#293: release_images.sh must produce a files.sha256 covering every
        PNG inside the published tarball. Post-staging-refactor the sidecar
        lives in TMPDIR_ROOT (not the working tree), so we inspect it via the
        published tarball.
        """

        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        # Fake gh that captures the tarball passed to `release create`.
        fake = textwrap.dedent(f"""\
            #!/bin/bash
            echo "$@" >> "{log}"
            if [[ "$1" == "release" && "$2" == "create" ]]; then
                shift 2
                while [[ "$#" -gt 0 ]]; do
                    if [[ -f "$1" ]]; then cp "$1" "{capture}/"; fi
                    shift
                done
            fi
            case "$1$2" in
                releaseview) exit 1 ;;
            esac
            exit 0
        """)
        gh = bin_dir / "gh"
        gh.write_text(fake)
        gh.chmod(0o755)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        # Sidecar lives inside the published tarball, not in the working tree
        # (post-staging refactor for #293).
        assert not (repo / "images" / "files.sha256").exists(), (
            "files.sha256 should NOT be in the working tree — staging refactor freezes "
            "it to TMPDIR_ROOT to close the release-time TOCTOU"
        )
        tarball = capture / "litclock-images.tar.gz"
        assert tarball.exists(), "tarball was never passed to gh release create"
        with tarfile.open(tarball) as tf:
            names = tf.getnames()
            sidecar_member = tf.extractfile("images/files.sha256")
            assert sidecar_member is not None, f"files.sha256 not in tarball: {names[:10]}..."
            sidecar_text = sidecar_member.read().decode()
            png_members = {n: tf.extractfile(n).read() for n in names if n.endswith(".png")}
        lines = [ln for ln in sidecar_text.splitlines() if ln.strip()]
        # _make_repo_with_images seeds 2 PNGs: quote_0000_0.png + metadata/quote_0000_0_credits.png
        assert len(lines) == 2, f"expected 2 PNG entries, got {len(lines)}: {lines}"
        # Verify each entry's hash actually matches the PNG bytes IN the tarball.
        for line in lines:
            digest, _, rel_path = line.partition("  ")
            rel_path = rel_path.lstrip("./")
            tarball_path = f"images/{rel_path}"
            assert tarball_path in png_members, f"sidecar references {rel_path} but it's not in the tarball"
            actual = hashlib.sha256(png_members[tarball_path]).hexdigest()
            assert digest == actual, f"sidecar lists wrong hash for {rel_path}"
        # User-facing log mentions the count so an operator sees what got covered.
        assert "files.sha256" in result.stdout.lower()

    def test_files_sha256_is_bundled_inside_tarball(self, tmp_path):
        """#293: files.sha256 must be INSIDE the published tarball, not just
        left as a side-effect file in the working tree. Otherwise consumers
        fall through to the legacy-release branch and the verify is a no-op.
        Future refactor that moves the sidecar generation outside the tar
        coverage path would silently break the entire fix.
        """

        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        # Modified fake gh that copies positional file args (the tarball) to
        # a capture dir so we can inspect the artifact that *would* have been
        # uploaded.
        capture = tmp_path / "capture"
        capture.mkdir()
        fake = textwrap.dedent(f"""\
            #!/bin/bash
            echo "$@" >> "{log}"
            if [[ "$1" == "release" && "$2" == "create" ]]; then
                shift 2
                while [[ "$#" -gt 0 ]]; do
                    if [[ -f "$1" ]]; then cp "$1" "{capture}/"; fi
                    shift
                done
            fi
            case "$1$2" in
                releaseview) exit 1 ;;
            esac
            exit 0
        """)
        gh = bin_dir / "gh"
        gh.write_text(fake)
        gh.chmod(0o755)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        tarball = capture / "litclock-images.tar.gz"
        assert tarball.exists(), "tarball was never passed to gh release create"
        with tarfile.open(tarball) as tf:
            names = tf.getnames()
        assert "images/files.sha256" in names, (
            f"files.sha256 not found inside the published tarball — consumers will "
            f"fall through to legacy-release branch and skip verification. "
            f"Members: {names[:20]}..."
        )

    def test_refuses_release_when_images_dir_has_no_pngs(self, tmp_path):
        """#293: release_images.sh must refuse to publish if find returns no
        PNGs. Without the `xargs -r` guard + count check, an empty images/
        would produce a sidecar with `e3b0...  -` (sha of empty stdin) — a
        corrupt entry that fails verification on every consumer install.
        """
        repo = _make_repo_with_images(tmp_path)
        # Strip out the seeded PNGs but keep manifest.json (release_images.sh
        # also requires manifest.json — both gates should be checked).
        for png in (repo / "images").rglob("*.png"):
            png.unlink()
        # Commit so the working tree is clean (release_images.sh refuses dirty).
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "strip pngs"], cwd=repo, check=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        combined = (result.stdout + result.stderr).lower()
        assert "no pngs found" in combined or "files.sha256 ended up empty" in combined


class TestNoNetworkLeaks:
    """Regression: make sure the script doesn't require or use any real network."""

    def test_no_real_gh_invocation(self, tmp_path, monkeypatch):
        # Simulate "gh" being unavailable (bin dir empty) — the script should
        # fail early and cleanly, not hang or hit the network.
        repo = _make_repo_with_images(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode != 0
        # Either "command not found" surfaces or the script errors earlier.
        # The point is: no hang, finishes in < 30s (our timeout).


class TestManifestCompleteness:
    """Issue #313: release_images.sh must refuse to publish when the byte
    sidecar doesn't cover every PNG referenced by manifest.json. files.sha256
    verifies each entry's hash but says nothing about completeness — a
    partial-emission bug in quote_to_image.php would otherwise ship a release
    whose runtime would crash on the missing minutes.
    """

    def test_completeness_gate_catches_missing_credits(self, tmp_path):
        """Manifest claims a second main PNG; the main is on disk but the
        paired credits sibling is not. The byte sidecar will be internally
        consistent (it only hashes files that exist) but incomplete vs the
        manifest claim — gate must refuse the publish.
        """
        repo = _make_repo_with_images(
            tmp_path,
            extra_pngs={"quote_1200_0.png": b"second main only"},
            manifest_files={
                "quote_0000_0.png": hashlib.sha256(b"a").hexdigest(),
                "quote_1200_0.png": hashlib.sha256(b"second main only").hexdigest(),
            },
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        combined = (result.stdout + result.stderr).lower()
        assert "manifest completeness" in combined
        assert "quote_1200_0_credits.png" in combined
        # `gh release create` must NOT have been called.
        log_contents = log.read_text() if log.exists() else ""
        assert "release create" not in log_contents

    def test_completeness_gate_catches_missing_main(self, tmp_path):
        """Manifest claims `quote_1200_0.png` but neither the main nor credits
        PNG exists on disk. Gate must refuse.
        """
        repo = _make_repo_with_images(
            tmp_path,
            manifest_files={
                "quote_0000_0.png": hashlib.sha256(b"a").hexdigest(),
                "quote_1200_0.png": "deadbeef",  # claimed but no PNGs exist
            },
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        combined = (result.stdout + result.stderr).lower()
        assert "manifest completeness" in combined
        assert "quote_1200_0.png" in combined
        log_contents = log.read_text() if log.exists() else ""
        assert "release create" not in log_contents

    def test_completeness_gate_refuses_empty_manifest(self, tmp_path):
        """Manifest's `files` map is empty — no completeness claim made, but
        we still refuse because shipping against an empty manifest is a
        degenerate state (publisher likely forgot to regenerate).
        """
        repo = _make_repo_with_images(tmp_path, manifest_files={})
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        combined = (result.stdout + result.stderr).lower()
        assert "empty" in combined and "manifest" in combined
        log_contents = log.read_text() if log.exists() else ""
        assert "release create" not in log_contents

    def test_completeness_warns_on_orphan_pngs_but_proceeds(self, tmp_path):
        """Extra PNGs on disk that aren't claimed by manifest.json → warn but
        don't refuse. The publisher may have manually removed a corrupt entry
        from the manifest mid-edit; forcing this fatal blocks legitimate work.
        """
        repo = _make_repo_with_images(
            tmp_path,
            # Orphan: PNG present on disk, manifest doesn't claim it.
            extra_pngs={
                "quote_0830_0.png": b"orphan main",
                "metadata/quote_0830_0_credits.png": b"orphan credits",
            },
            manifest_files={"quote_0000_0.png": hashlib.sha256(b"a").hexdigest()},
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        combined = (result.stdout + result.stderr).lower()
        assert "orphan" in combined
        # Publish proceeded.
        log_contents = log.read_text()
        assert "release create litclock-images-v1" in log_contents

    def test_completeness_gate_logs_at_most_ten_missing(self, tmp_path):
        """When > 10 files are missing, the gate truncates the listing so the
        operator log stays scannable. The summary "... +N more" line is shown.
        """
        manifest_files = {"quote_0000_0.png": hashlib.sha256(b"a").hexdigest()}
        for i in range(15):
            manifest_files[f"quote_01{i:02d}_0.png"] = "deadbeef"
        repo = _make_repo_with_images(tmp_path, manifest_files=manifest_files)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        _write_fake_gh(bin_dir, log)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "more main" in combined or "more credits" in combined

    def test_completeness_pass_proceeds_to_release(self, tmp_path):
        """End-to-end: a manifest that claims exactly what's on disk passes
        the gate and the tarball is built. Verify the tarball includes both
        manifest.json and files.sha256.
        """
        repo = _make_repo_with_images(tmp_path)  # default = claim quote_0000_0.png
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "gh.log"
        capture = tmp_path / "capture"
        capture.mkdir()
        _write_fake_gh_capturing(bin_dir, log, capture)
        result = _run(repo, "v1", bin_dir=bin_dir)
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "manifest completeness" not in (result.stderr + result.stdout).lower() or (
            "ok:" in result.stdout.lower()
        )
        tarball = capture / "litclock-images.tar.gz"
        assert tarball.exists()
        with tarfile.open(tarball) as tf:
            names = tf.getnames()
        assert "images/files.sha256" in names
        assert "images/manifest.json" in names
