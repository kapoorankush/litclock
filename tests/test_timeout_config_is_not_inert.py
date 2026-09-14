"""The per-test timeout must not be able to go inert (litclock-dev#769).

`timeout` is an option **pytest-timeout adds**. Without the plugin, pytest
prints `Unknown config option: timeout` as one warning among several and runs
with no per-test timeout at all — the exact inversion of what litclock-dev#662
wanted, since a hang then hangs forever on a developer machine and is caught
only in CI, where requirements-dev.txt is always installed.

It went unnoticed for months. A warning is not a signal.

`required_plugins` in pyproject makes pytest refuse to start instead. These
tests pin the coupling itself, because a future edit that drops `timeout`,
drops the requirement, or lets the pin drift from requirements-dev.txt would
otherwise restore the silent-degradation state with the suite green.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PLUGIN = "pytest-timeout"


def _pytest_ini() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["tool"]["pytest"]["ini_options"]


def test_a_configured_timeout_is_backed_by_a_required_plugin():
    """The coupling, in the direction that matters: configuring `timeout`
    without requiring the plugin is the inert state."""
    ini = _pytest_ini()
    assert "timeout" in ini, (
        "the per-test timeout is gone. If that is deliberate, delete this test and the "
        "required_plugins entry together — leaving the requirement without the setting is "
        "a dependency nobody uses (litclock-dev#662, litclock-dev#769)"
    )
    required = " ".join(ini.get("required_plugins", []))
    assert PLUGIN in required, (
        f"pyproject configures `timeout = {ini['timeout']}` but does not require "
        f"{PLUGIN}. Without the plugin that setting is a WARNING and the suite runs with "
        "no per-test timeout — a hang becomes a PASS-shaped failure that only CI catches"
    )


def test_the_pin_matches_requirements_dev():
    """Two files naming the same version is a drift surface. Pin them together
    so a bump in one goes red rather than quietly disagreeing.

    Compared as SPECIFIERS, not as scraped version numbers. /review measured
    the numeric version passing `==2.4.0` against `>=2.4.0` — which is the very
    scenario the message below describes, because the day 2.5.0 ships
    `pip install -r requirements-dev.txt` installs it and pytest then refuses to
    start. A version-less pair passed too, on `None == None`, so the pin could
    be deleted from both files with the suite green.
    """
    from packaging.requirements import Requirement  # noqa: PLC0415

    required = _pytest_ini().get("required_plugins", [])
    spec = next((r for r in (Requirement(x) for x in required) if r.name == PLUGIN), None)
    assert spec, f"{PLUGIN} is not in required_plugins"

    dev_reqs = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    declared = [
        Requirement(ln.strip())
        for ln in dev_reqs.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and Requirement(ln.strip()).name == PLUGIN
    ]
    assert len(declared) == 1, f"expected exactly one {PLUGIN} line in requirements-dev.txt, got {declared}"

    assert str(spec.specifier), f"{PLUGIN} in required_plugins carries no version at all"
    assert str(declared[0].specifier), f"{PLUGIN} in requirements-dev.txt carries no version at all"
    assert spec.specifier == declared[0].specifier, (
        f"pyproject requires {str(spec)!r} but requirements-dev.txt declares "
        f"{str(declared[0])!r}. A developer installing requirements-dev could then still "
        "fail required_plugins, which turns the loud failure into a confusing one"
    )


def test_pytest_refuses_to_run_without_the_plugin():
    """Executed, not asserted from config. `required_plugins` is an inert
    string unless pytest actually enforces it — and the whole point of this
    issue is a config option that looked configured and did nothing.

    `-p no:timeout` disables the plugin for a child run without uninstalling
    anything.
    """
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:timeout", "--collect-only", "-q", "tests/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode != 0, (
        "pytest ran with pytest-timeout disabled. required_plugins is not being enforced, "
        f"so the per-test timeout can silently go inert again.\n{r.stdout[-2000:]}"
    )
    assert PLUGIN in (r.stdout + r.stderr), (
        "it failed, but not for the missing plugin — the failure must NAME what is missing "
        f"or the next person cannot act on it.\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    )


def test_the_timeout_is_bounded_on_both_sides():
    """A ceiling has two ways to be useless, and the first version of this test
    only checked one.

    Too LOW and it fires on a loaded machine instead of a genuine hang, turning
    a green suite flaky. Too HIGH and it never fires at all — /review measured
    `timeout = 100000` passing a bare `>= 120`, which is the litclock-dev#662
    failure restored: the job burns to its wall-clock limit and reports a
    timeout rather than the regression.

    The upper bound is what makes this more than arithmetic against a constant.
    The whole suite runs in ~235s (pyproject's own figure; measured 237s on this
    branch), and the slowest single test is a small fraction of that, so a
    per-test ceiling has to sit well clear of the slowest test and well under
    the job budget.
    """
    timeout = _pytest_ini()["timeout"]
    assert timeout >= 120, (
        f"timeout = {timeout}s is close enough to real test durations to fire on a loaded "
        "machine rather than on a genuine hang"
    )
    assert timeout <= 900, (
        f"timeout = {timeout}s is above any plausible CI job budget, so a hang is killed by "
        "the runner before the per-test timeout can report WHICH test hung — which is the "
        "litclock-dev#662 failure this setting exists to prevent"
    )


def test_every_workflow_that_runs_pytest_installs_the_required_plugins():
    """`required_plugins` is a START-UP gate, so it breaks any CI job that
    installs a NARROWER dependency set than requirements-dev.txt.

    That is not hypothetical — it happened in this PR. `build-image.yml` runs a
    deliberately minimal subset (`pip install pytest pyyaml`, no Pillow, no
    hardware deps, so the image build cannot be gated by an unrelated flaky
    test), and adding `required_plugins` made that job exit 4 before collecting
    anything. Reproduced in a clean venv, then fixed.

    The coupling is invisible from either side: pyproject does not know who runs
    pytest, and a workflow author has no reason to read pyproject. This test is
    the only thing joining them.

    Asserted on EXECUTED lines. The first version searched the whole file and
    was satisfied by the COMMENT explaining why the plugin is in the install
    list — so reverting the install line itself stayed green. Measured.
    """
    import re as _re

    required = _pytest_ini().get("required_plugins", [])
    assert required, "no required_plugins — this test has nothing to enforce"
    names = [_re.split(r"[><=~!]", spec)[0].strip() for spec in required]

    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found — the glob is broken"

    runs_pytest = _re.compile(r"(^|\s|&&\s*)(python3?\s+-m\s+)?pytest(\s|$)")
    offenders = []
    for wf in workflows:
        lines = [
            ln for ln in wf.read_text(encoding="utf-8").splitlines()
            if not ln.strip().startswith("#")
        ]
        body = "\n".join(lines)
        pytest_lines = [
            (i, ln.strip()) for i, ln in enumerate(lines, 1) if runs_pytest.search(ln.strip())
        ]
        if not pytest_lines:
            continue
        # `-r requirements-dev.txt` covers the plugins by definition; only a
        # hand-rolled install list can under-specify.
        if "requirements-dev.txt" in body:
            continue
        missing = [n for n in names if n not in body]
        if missing:
            lineno, text = pytest_lines[0]
            offenders.append(f"{wf.name}:{lineno}: runs pytest but never installs {missing}: {text}")

    assert not offenders, (
        "a workflow runs pytest without installing every plugin pyproject REQUIRES. "
        "pytest refuses to start, so the job fails with exit 4 before collecting a single "
        "test:\n" + "\n".join(offenders)
    )
