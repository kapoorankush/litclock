"""Tests for the build-image GitHub Actions workflow.

Validates workflow structure, triggers, and build configuration.
"""

import os

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "build-image.yml")


def _load_workflow():
    with open(WORKFLOW_PATH) as f:
        wf = yaml.safe_load(f)
    # PyYAML parses the YAML keyword `on:` as boolean True.
    # Normalize so tests can use wf["on"] regardless.
    if True in wf and "on" not in wf:
        wf["on"] = wf.pop(True)
    return wf


class TestWorkflowTriggers:
    def test_triggers_on_version_tags(self):
        wf = _load_workflow()
        tags = wf["on"]["push"]["tags"]
        assert "v*" in tags

    def test_supports_manual_dispatch(self):
        wf = _load_workflow()
        assert "workflow_dispatch" in wf["on"]

    def test_manual_dispatch_has_ref_input(self):
        wf = _load_workflow()
        inputs = wf["on"]["workflow_dispatch"]["inputs"]
        assert "litclock_ref" in inputs


class TestWorkflowBuildJob:
    def test_build_job_exists(self):
        wf = _load_workflow()
        assert "build" in wf["jobs"]

    def test_runs_on_ubuntu(self):
        wf = _load_workflow()
        assert "ubuntu" in wf["jobs"]["build"]["runs-on"]

    def test_has_write_permissions(self):
        """Needs contents:write to create GitHub Releases."""
        wf = _load_workflow()
        perms = wf["jobs"]["build"]["permissions"]
        assert perms["contents"] == "write"

    def test_frees_disk_space(self):
        """pi-gen needs ~10 GB; runner must free space first."""
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        step_names = [s.get("name", "") for s in steps]
        assert any("disk" in name.lower() for name in step_names)

    def test_clones_pi_gen_at_pinned_tag(self):
        """pi-gen should be cloned at a specific tag, not HEAD."""
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        clone_step = next(s for s in steps if s.get("name") == "Clone pi-gen")
        assert "--branch" in clone_step["run"]

    def test_compresses_with_xz(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        compress_step = next(s for s in steps if s.get("name") == "Compress image")
        assert "xz" in compress_step["run"]

    def test_generates_sha256_checksum(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        compress_step = next(s for s in steps if s.get("name") == "Compress image")
        assert "sha256sum" in compress_step["run"]

    def test_creates_release_on_tag(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        release_step = next(s for s in steps if s.get("name") == "Create release")
        assert "gh release create" in release_step["run"]
        assert release_step["if"] == "github.ref_type == 'tag'"

    def test_uploads_dev_build_on_dispatch(self):
        wf = _load_workflow()
        steps = wf["jobs"]["build"]["steps"]
        dev_step = next(s for s in steps if s.get("name") == "Upload dev build")
        assert dev_step["if"] == "github.ref_type != 'tag'"
        assert "gh release create" in dev_step["run"]
        assert "--prerelease" in dev_step["run"]
