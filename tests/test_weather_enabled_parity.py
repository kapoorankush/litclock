"""WEATHER_ENABLED resolves identically on every surface (litclock-dev#790).

The defect this file exists to prevent is not one wrong line — it is TWO lines
that disagree. Before litclock-dev#790 there were three independent answers to
"is weather on?":

======================================  ==========  ==================
surface                                 key absent  ``1`` / ``yes``
======================================  ==========  ==================
``literary_clock.main()`` (the panel)   enabled     DISABLED
diagnostics ``_collectors``             DISABLED    enabled
``settings.html.j2`` (no-JS toggle)     enabled     DISABLED
======================================  ==========  ==================

So a device whose env.sh lacked the key painted weather while the support
bundle — read precisely to explain odd weather behaviour — reported it off.

The fix is structural: one function, ``config.weather_enabled()``. These tests
pin that structure (there is exactly one resolution site) AND the behaviour of
each surface by EXECUTION, because a test that only checked the collector would
not have caught the original bug.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config  # noqa: E402

# (raw env.sh value, expected resolution). ``None`` means the key is absent.
# The table is the contract; every surface below is checked against THIS list
# rather than against a hand-written expectation per surface.
CASES: list[tuple[str | None, bool]] = [
    (None, True),  # absent -> enabled (the renderer's answer; the visible one)
    ("", True),  # empty is indistinguishable from absent in a sourced env.sh
    ("true", True),
    ("TRUE", True),
    ("True", True),
    (" true ", True),  # a hand-edit with stray whitespace
    ("1", True),
    ("yes", True),
    ("false", False),
    ("FALSE", False),
    ("no", False),
    ("0", False),
    ("garbage", False),  # an unrecognised value fails toward the quiet panel
]


class TestSharedPredicate:
    def test_mapping_and_environ_agree(self, monkeypatch):
        """The two call shapes are the two consumers: the collector passes a
        dict read off env.sh, the renderer passes nothing and gets os.environ.
        They must not diverge, which is the whole point of the function."""
        for raw, expected in CASES:
            if raw is None:
                monkeypatch.delenv("WEATHER_ENABLED", raising=False)
                mapping: dict[str, str] = {}
            else:
                monkeypatch.setenv("WEATHER_ENABLED", raw)
                mapping = {"WEATHER_ENABLED": raw}
            assert config.weather_enabled(mapping) is expected, f"mapping form, raw={raw!r}"
            assert config.weather_enabled() is expected, f"os.environ form, raw={raw!r}"

    def test_default_constant_is_the_enabled_answer(self):
        # Pinned separately so a future edit that flips the constant has to
        # confront this assertion and the reasoning attached to it, rather
        # than quietly inverting every surface at once.
        assert config.WEATHER_ENABLED_DEFAULT == "true"
        assert config.weather_enabled({}) is True


class TestExactlyOneResolutionSite:
    """The anti-drift guard. A second inline read is how this bug happened."""

    # Any construct that decides truthiness from the raw key, in code that
    # runs. ``config.py`` is the one legitimate home.
    INLINE_READ = re.compile(
        r"""(?x)
        (
          os\.(?:getenv|environ)[^\n]*WEATHER_ENABLED   # os.getenv("WEATHER_ENABLED"...)
        | WEATHER_ENABLED[^\n]*==                       # s.get('WEATHER_ENABLED',...) == 'true'
        | get\(\s*["']WEATHER_ENABLED["'][^\n]*\)\s*(?:or|\.lower)  # (get(..) or "").lower()
        )
        """
    )

    def _python_and_template_sources(self) -> list[Path]:
        files = [p for p in (SRC).rglob("*.py")]
        files += list((SRC / "control_server" / "templates").rglob("*.j2"))
        return files

    def test_no_surface_resolves_the_key_on_its_own(self):
        offenders: list[str] = []
        for path in self._python_and_template_sources():
            if path.name == "config.py":
                continue  # the one home
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("{#"):
                    continue  # comments may name the key freely
                if self.INLINE_READ.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
        assert not offenders, (
            "WEATHER_ENABLED must be resolved through config.weather_enabled() only "
            "(litclock-dev#790). Independent reads found:\n  " + "\n  ".join(offenders)
        )

    def test_the_guard_can_actually_fail(self):
        # A guard that cannot fail is the litclock-dev#782 shape. Prove the
        # regex catches each of the three original expressions verbatim.
        originals = [
            'weather_enabled = os.getenv("WEATHER_ENABLED", "true").lower() == "true"',
            '"weather_enabled": (env_settings.get("WEATHER_ENABLED") or "").lower() in ("true", "1", "yes"),',
            "{% if s.get('WEATHER_ENABLED', 'true') == 'true' %}checked{% endif %}",
        ]
        for original in originals:
            assert self.INLINE_READ.search(original), f"regex missed a real pre-fix line: {original}"


class TestRendererSurface:
    """The panel — and the surface the whole litclock-dev#790 default is anchored on
    ("the default is TRUE because that is the answer the owner can SEE").

    The first version of this test was VACUOUS and /review caught it: it
    monkeypatched `literary_clock.config.weather_enabled`, then asserted on its
    OWN call to the patched function without ever entering renderer code. It
    proved monkeypatch works. Mutation-verified at the time: replacing
    `weather_enabled = config.weather_enabled()` with `weather_enabled = True`
    left this file and 238 neighbouring tests green — the panel could stop
    consulting the shared predicate entirely and nothing went red.

    So this drives the REAL renderer, as a subprocess, through `--dry-run`, and
    discriminates on which branch it takes. The two branches log distinguishable
    lines, which is what makes the decision observable without a display.
    """

    def _branch(self, env_value):
        """Run the shipped renderer and report which weather branch it took."""
        env = {k: v for k, v in os.environ.items() if k != "WEATHER_ENABLED"}
        env["LOG_LEVEL"] = "INFO"
        if env_value is not None:
            env["WEATHER_ENABLED"] = env_value
        r = subprocess.run(
            [sys.executable, "src/literary_clock.py", "--dry-run"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
        out = r.stdout + r.stderr
        if "WEATHER_ENABLED=false, skipping weather" in out:
            return "disabled"
        # Anything past the toggle means the toggle said ON. On a dev box with no
        # location this lands on the location check, which is the next gate.
        if "No location configured" in out or "Weather" in out:
            return "enabled"
        return f"indeterminate: {out[-300:]!r}"

    @pytest.mark.parametrize("raw,expected", [(None, True), ("false", False), ("true", True), ("1", True)])
    def test_the_renderer_branches_on_the_shared_table(self, raw, expected):
        got = self._branch(raw)
        want = "enabled" if expected else "disabled"
        assert got == want, (
            f"WEATHER_ENABLED={raw!r}: renderer took the {got!r} branch, expected {want!r}. "
            "This executes the shipped painter — if it stops calling "
            "config.weather_enabled(), this goes red."
        )


class TestDiagnosticsSurface:
    @pytest.mark.parametrize("raw,expected", [(None, True), ("false", False), ("1", True)])
    def test_collector_matches_the_table(self, tmp_path, raw, expected):
        from control_server import create_app

        env = tmp_path / "env.sh"
        lines = ["WEATHER_UNITS=imperial"]
        if raw is not None:
            lines.append(f"WEATHER_ENABLED={raw}")
        env.write_text("\n".join(lines) + "\n")

        app = create_app({"TESTING": True, "ENV_FILE": str(env)})
        from control_server.routes import diagnostics

        with app.app_context():
            values = diagnostics.collect_diagnostics()
        assert values["weather_enabled"] is expected


class TestDownstreamConsumers:
    """The surface the anti-drift regex structurally cannot reach.

    `TestExactlyOneResolutionSite` scans for the uppercase env key
    `WEATHER_ENABLED`. `_anomalies.py` consumes the COLLECTED lowercase key
    `weather_enabled`, so its own truthiness literal was invisible to the scan —
    and it had drifted, accepting `(True, "true", "1")` while config accepts
    `{"true", "1", "yes"}`. Found by /review, not by the guard.
    """

    def test_the_anomaly_detector_agrees_with_config_on_every_string(self):
        import sys as _sys

        _sys.path.insert(0, str(SRC))
        from control_server.routes.diagnostics import _anomalies

        for raw, expected in CASES:
            if raw in (None, ""):
                # Deliberate divergence, asserted separately below: an absent or
                # empty env KEY means enabled (config's rule), but an absent or
                # empty value in a collected PAYLOAD means malformed, and muting
                # a diagnostics section on malformed input is the wrong default.
                continue
            got = _anomalies._weather_is_enabled({"weather_enabled": raw})
            want = config.weather_enabled({"WEATHER_ENABLED": raw})
            assert got is want is expected, (
                f"raw={raw!r}: anomaly detector says {got}, config says {want}. "
                "These must not diverge — that is what litclock-dev#790 was for."
            )

    def test_yes_is_accepted_here_too(self):
        """The specific drift: config accepts "yes", the old literal did not."""
        from control_server.routes.diagnostics import _anomalies

        assert _anomalies._weather_is_enabled({"weather_enabled": "yes"}) is True

    def test_a_bool_payload_short_circuits(self):
        """The live path — the collector emits a bool."""
        from control_server.routes.diagnostics import _anomalies

        assert _anomalies._weather_is_enabled({"weather_enabled": True}) is True
        assert _anomalies._weather_is_enabled({"weather_enabled": False}) is False

    def test_a_malformed_payload_stays_falsy(self):
        """Deliberately NOT config's rule. An absent env KEY means enabled; a
        payload MISSING the key is malformed, and muting a diagnostics section
        on malformed input is the wrong default."""
        from control_server.routes.diagnostics import _anomalies

        assert _anomalies._weather_is_enabled({}) is False
        assert _anomalies._weather_is_enabled({"weather_enabled": None}) is False
        assert _anomalies._weather_is_enabled({"weather_enabled": ""}) is False
        # ...and config deliberately says the OPPOSITE for the same inputs,
        # because there the key is absent from env.sh rather than from a payload.
        assert config.weather_enabled({}) is True
        assert config.weather_enabled({"WEATHER_ENABLED": ""}) is True


class TestNoJsToggleSurface:
    @pytest.mark.parametrize("raw,expected", [(None, True), ("false", False), ("1", True)])
    def test_template_checkbox_matches_the_table(self, tmp_path, raw, expected):
        """The no-JS fallback is a real user surface (it is in the first-boot QA
        checklist), and it was the third divergent implementation."""
        from control_server import create_app

        env = tmp_path / "env.sh"
        lines = ["WEATHER_UNITS=imperial"]
        if raw is not None:
            lines.append(f"WEATHER_ENABLED={raw}")
        env.write_text("\n".join(lines) + "\n")

        app = create_app({"TESTING": True, "ENV_FILE": str(env)})
        # Render the REAL page, not the Jinja global in isolation — the bug was
        # in the template expression, so the template is what has to be executed.
        html = app.test_client().get("/settings").get_data(as_text=True)
        i = html.find('id="weather_enabled"')
        assert i != -1, "the weather toggle must render on the no-JS settings page"
        control = html[i : html.find(">", i) + 1]
        assert f'aria-checked="{str(expected).lower()}"' in control, control
        # Strip aria-checked before looking for the bare boolean attribute --
        # otherwise the substring "checked" is found inside "aria-checked" and
        # the assertion is true for every input, which is a guard that cannot
        # fail (the litclock-dev#782 shape). Caught by running it.
        bare = re.sub(r'aria-checked="[^"]*"', "", control)
        assert ("checked" in bare) is expected, control
