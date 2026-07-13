"""Per-row reader tests for ``control_server.routes.diagnostics._collectors``.

Pre-#419 the readers were tested indirectly via :func:`collect_diagnostics`
which depended on the real ``ip``/``nmcli``/``iw``/``journalctl``/
``systemctl``/``timedatectl`` binaries being installed on the test host.
This file isolates each reader by monkey-patching ``cached_subprocess``
at every binding site (``_collectors``, ``_network``) and feeding it
canned outputs per cache key.

Three cases per reader (T6 from issue body):

- happy: cached_subprocess returns a valid output → reader extracts the
  expected typed value.
- empty: cached_subprocess returns ``""`` (binary missing / non-zero exit)
  → reader returns ``None`` (or an empty mapping for batched readers).
- malformed: cached_subprocess returns garbage → reader returns ``None``
  without raising.
"""

from __future__ import annotations

import pytest
from flask import Flask

from control_server.routes.diagnostics import _collectors


@pytest.fixture()
def app():
    """Minimal Flask app so readers calling ``current_app.config.get(...)``
    don't blow up on the empty context."""
    app = Flask(__name__)
    return app


@pytest.fixture()
def fake_subprocess(monkeypatch):
    """Install a configurable fake at every cached_subprocess binding site.

    Tests set ``fake_subprocess.responses[key] = stdout`` to control what
    each cache-keyed call returns. An unset key returns ``""`` (binary
    missing). Patching BOTH _collectors and _network namespaces is the
    D8-honest pattern — Python binds names at import time per module, so
    a single monkeypatch can miss a binding the reader actually uses.

    #428 PR1a: readers at the call sites now go through
    ``cached_subprocess_or_empty`` (CQ-1 helper). Patch BOTH names at
    BOTH bindings so a test setting ``responses[key]`` hits whichever
    path the reader takes. Pre-existing tests don't care about the
    None-vs-empty distinction; their unset-key default of ``""`` is the
    same shape ``cached_subprocess_or_empty`` would have coerced anyway.
    """

    class _Fake:
        def __init__(self):
            self.responses: dict[str, str] = {}
            self.calls: list[tuple[str, list[str]]] = []
            # Parallel record of the keyword contract per call (#444): the
            # per-call ``timeout`` and ``ttl`` the reader passed. Kept in a
            # separate list so the existing ``key, argv = calls[-1]`` unpack
            # sites stay untouched. ``calls`` and ``calls_kw`` are appended in
            # lockstep, so ``calls_kw[i]`` describes ``calls[i]``.
            self.calls_kw: list[dict[str, float]] = []

        def __call__(self, key: str, argv: list[str], *, timeout: float, ttl: float) -> str:
            # timeout/ttl are required keyword-only (#444 /review): every
            # diagnostics reader passes them explicitly, so a reader that
            # drops one should raise TypeError here rather than silently
            # recording a fake default that masks the regression.
            self.calls.append((key, argv))
            self.calls_kw.append({"timeout": timeout, "ttl": ttl})
            return self.responses.get(key, "")

        def kw_for(self, key: str) -> dict[str, float]:
            """Return the timeout/ttl kwargs of the single call for ``key``.

            Asserts EXACTLY ONE call was recorded for ``key`` (#444 /review,
            Codex): "most recent call" would let a wrong-then-retried-right
            call site pass while production still paid the bad first call.
            Raises ``AssertionError`` on zero or duplicate calls so a test
            asserting a per-call contract can't silently pass on a stale or
            ambiguous record.
            """
            matches = [kw for (k, _argv), kw in zip(self.calls, self.calls_kw, strict=True) if k == key]
            assert matches, f"no cached_subprocess call recorded for key {key!r}"
            assert len(matches) == 1, f"expected exactly 1 call for key {key!r}, got {len(matches)}"
            return matches[0]

    fake = _Fake()
    # Patch wherever cached_subprocess is bound today. _collectors imports
    # it directly; _network imports it for the shared helpers; tests will
    # break loudly if a new binding site appears without showing up here.
    monkeypatch.setattr(
        "control_server.routes.diagnostics._collectors.cached_subprocess",
        fake,
    )
    monkeypatch.setattr(
        "control_server.routes.diagnostics._collectors.cached_subprocess_or_empty",
        fake,
    )
    monkeypatch.setattr("control_server._network.cached_subprocess", fake)
    monkeypatch.setattr("control_server._network.cached_subprocess_or_empty", fake)
    return fake


class TestReadKernelRelease:
    """``_read_kernel_release`` shells out to ``uname -r``."""

    def test_happy(self, app, fake_subprocess):
        fake_subprocess.responses["diag-uname-r"] = "6.6.20+rpt-rpi-v8"
        with app.app_context():
            assert _collectors._read_kernel_release() == "6.6.20+rpt-rpi-v8"

    def test_empty(self, app, fake_subprocess):
        # uname missing → cached_subprocess returns "" → reader returns None.
        with app.app_context():
            assert _collectors._read_kernel_release() is None

    def test_malformed_whitespace(self, app, fake_subprocess):
        # Trailing whitespace gets stripped by cached_subprocess upstream;
        # an all-whitespace stdout would already have been stripped to "".
        # Verify a single-token output passes through unchanged.
        fake_subprocess.responses["diag-uname-r"] = "garbage_but_a_string"
        with app.app_context():
            # The reader trusts cached_subprocess; "garbage" is the value.
            assert _collectors._read_kernel_release() == "garbage_but_a_string"


class TestReadTimezone:
    """``_read_timezone`` shells out to ``timedatectl show ... Timezone``."""

    def test_happy(self, app, fake_subprocess):
        fake_subprocess.responses["diag-timezone"] = "America/Chicago"
        with app.app_context():
            assert _collectors._read_timezone() == "America/Chicago"

    def test_empty(self, app, fake_subprocess):
        with app.app_context():
            assert _collectors._read_timezone() is None

    def test_malformed_multiline(self, app, fake_subprocess):
        # timedatectl --value should produce single-line output; an
        # unexpected multi-line response is passed through as-is.
        fake_subprocess.responses["diag-timezone"] = "America/Chicago\nUnexpected"
        with app.app_context():
            # The reader returns the raw string; downstream is permissive.
            assert _collectors._read_timezone() == "America/Chicago\nUnexpected"


class TestReadIface:
    """``_read_iface`` shells out to ``ip -4 route show default``."""

    def test_happy(self, app, fake_subprocess):
        fake_subprocess.responses["diag-default-route"] = "default via 192.168.1.1 dev wlan0 proto dhcp metric 600"
        with app.app_context():
            assert _collectors._read_iface() == "wlan0"

    def test_empty(self, app, fake_subprocess):
        # No default route configured → empty stdout → None.
        with app.app_context():
            assert _collectors._read_iface() is None

    def test_malformed_no_dev(self, app, fake_subprocess):
        # Real ``ip`` output without "dev <name>"; reader handles gracefully.
        fake_subprocess.responses["diag-default-route"] = "blackhole default"
        with app.app_context():
            assert _collectors._read_iface() is None


class TestReadSsid:
    """``_read_ssid`` shells out to ``nmcli -t -f NAME,TYPE …``."""

    def test_happy(self, app, fake_subprocess):
        fake_subprocess.responses["diag-wifi-ssid"] = "Wired connection 1:802-3-ethernet\nMyHomeWiFi:802-11-wireless"
        with app.app_context():
            assert _collectors._read_ssid() == "MyHomeWiFi"

    def test_empty(self, app, fake_subprocess):
        # nmcli not installed OR no active connections → "" → None.
        with app.app_context():
            assert _collectors._read_ssid() is None

    def test_malformed_no_colon(self, app, fake_subprocess):
        # nmcli -t output should always contain NAME:TYPE; a malformed line
        # without a colon is silently skipped (loop continues, returns None).
        fake_subprocess.responses["diag-wifi-ssid"] = "garbage_no_colon"
        with app.app_context():
            assert _collectors._read_ssid() is None


class TestReadSignalDbm:
    """``_read_signal_dbm`` shells out to ``iw dev <iface> link``."""

    def test_happy(self, app, fake_subprocess):
        # The reader first resolves iface via _read_iface; supply both keys.
        fake_subprocess.responses["diag-default-route"] = "default via 1.1.1.1 dev wlan0"
        fake_subprocess.responses["diag-iw-signal-wlan0"] = (
            "Connected to ab:cd:ef:01:23:45 (on wlan0)\n\tSSID: MyHomeWiFi\n\tsignal: -49 dBm"
        )
        with app.app_context():
            assert _collectors._read_signal_dbm() == -49

    def test_empty(self, app, fake_subprocess):
        # iw not installed → empty stdout → None.
        with app.app_context():
            assert _collectors._read_signal_dbm() is None

    def test_malformed_signal_non_int(self, app, fake_subprocess):
        fake_subprocess.responses["diag-default-route"] = "default via 1.1.1.1 dev wlan0"
        fake_subprocess.responses["diag-iw-signal-wlan0"] = "signal: not-a-number dBm"
        with app.app_context():
            assert _collectors._read_signal_dbm() is None


class TestCacheKeyContract:
    """Pin the cache_key / ttl / timeout triple per reader (#419 F6 + D8).

    The whole point of the cache_key parameterization in _network.py is
    that status (default 5 s / 2 s) and diagnostics (20 s / 3 s with
    ``diag-`` prefix) share the helper implementation WITHOUT sharing
    cache entries. A regression that collapsed the keys would still
    pass every reader test above — but would silently break the
    isolation contract. This class spends the assertion.
    """

    def test_read_iface_uses_diag_default_route_with_20s_ttl(self, app, fake_subprocess):
        fake_subprocess.responses["diag-default-route"] = "default via 1.1.1.1 dev wlan0"
        with app.app_context():
            _collectors._read_iface()
        # The reader (which now flows through _read_default_route per F4)
        # must have called cached_subprocess with diag-prefixed key + 20s TTL.
        assert fake_subprocess.calls, "expected at least one cached_subprocess call"
        # The most recent call into cached_subprocess is the one we made.
        key, argv = fake_subprocess.calls[-1]
        assert key == "diag-default-route"
        assert argv == ["ip", "-4", "route", "show", "default"]

    def test_read_ssid_uses_diag_wifi_ssid_key(self, app, fake_subprocess):
        with app.app_context():
            _collectors._read_ssid()
        key, argv = fake_subprocess.calls[-1]
        assert key == "diag-wifi-ssid"
        assert argv[0] == "nmcli"

    def test_read_signal_dbm_uses_diag_iw_signal_prefix(self, app, fake_subprocess):
        fake_subprocess.responses["diag-default-route"] = "default via 1.1.1.1 dev wlan0"
        with app.app_context():
            _collectors._read_signal_dbm()
        # _read_signal_dbm first resolves iface (diag-default-route call),
        # then issues the iw query (diag-iw-signal-wlan0 call).
        keys = [c[0] for c in fake_subprocess.calls]
        assert "diag-default-route" in keys
        assert any(k.startswith("diag-iw-signal-") for k in keys)


class TestBatchedIsActive:
    """``_batched_is_active`` shells out to ``systemctl is-active u1 u2 …``."""

    UNITS = ("a.service", "b.timer", "c.service")

    def test_happy(self, app, fake_subprocess):
        # systemctl is-active emits one line per unit in argv order.
        key = "diag-systemctl-is-active-a.service+b.timer+c.service"
        fake_subprocess.responses[key] = "active\ninactive\nfailed"
        with app.app_context():
            assert _collectors._batched_is_active(self.UNITS) == {
                "a.service": "active",
                "b.timer": "inactive",
                "c.service": "failed",
            }

    def test_empty(self, app, fake_subprocess):
        # systemctl missing → every unit reports "unknown".
        with app.app_context():
            result = _collectors._batched_is_active(self.UNITS)
        assert result == {u: "unknown" for u in self.UNITS}

    def test_malformed_short_output(self, app, fake_subprocess):
        # Fewer lines than units → missing units fall back to "unknown".
        key = "diag-systemctl-is-active-a.service+b.timer+c.service"
        fake_subprocess.responses[key] = "active"  # only 1 line
        with app.app_context():
            assert _collectors._batched_is_active(self.UNITS) == {
                "a.service": "active",
                "b.timer": "unknown",
                "c.service": "unknown",
            }


class TestIsObviouslyHealthy:
    """#443 — the lazy-tail filter (#433) must stay in lockstep with the
    ``_compute_anomalies`` oneshot carve-out: oneshot units cycling through
    ``activating``/``deactivating`` during the per-minute quote paint are
    NOT anomalies and so should not pull a journal tail either. A ``failed``
    oneshot, and any transient state on a non-oneshot unit, are NOT healthy.
    """

    ONESHOT = "litclock.service"  # member of DIAG_ONESHOT_UNITS
    NON_ONESHOT = "litclock-control.service"

    def test_active_always_healthy(self):
        assert _collectors._is_obviously_healthy("active", self.ONESHOT) is True
        assert _collectors._is_obviously_healthy("active", self.NON_ONESHOT) is True

    def test_oneshot_lifecycle_states_healthy(self):
        for state in ("inactive", "activating", "deactivating"):
            assert _collectors._is_obviously_healthy(state, self.ONESHOT) is True

    def test_oneshot_failed_not_healthy(self):
        # A failed oneshot still needs its journal tail for debugging.
        assert _collectors._is_obviously_healthy("failed", self.ONESHOT) is False

    def test_non_oneshot_transient_not_healthy(self):
        # Only DIAG_ONESHOT_UNITS get the lifecycle pass.
        for state in ("inactive", "activating", "deactivating", "failed", "unknown"):
            assert _collectors._is_obviously_healthy(state, self.NON_ONESHOT) is False


class TestFastReaderTimeoutContract:
    """#444 — pin that the FAST diagnostics readers pass the short
    ``DIAG_SUBPROC_TIMEOUT_S`` (3 s) budget at their call sites.

    This is the converse of the journalctl-outlier coverage that already
    exists. ``tests/test_control_server_perf.py`` pins the journalctl side:

    - ``test_read_journal_tail_uses_journal_timeout_not_fast_timeout`` — the
      journalctl call site uses ``DIAG_JOURNAL_TIMEOUT_S`` (8 s) + 20 s ttl
      + exactly one fork (the #427 regression guard).
    - ``test_journal_timeout_exceeds_fast_call_timeout`` — the constant
      relationship ``DIAG_JOURNAL_TIMEOUT_S > DIAG_SUBPROC_TIMEOUT_S``.

    What nothing asserted before this class: that the fast readers' call
    sites actually pass the 3 s budget. A reader hardcoding a wrong timeout
    (or dropping the kwarg and falling back to a slower default) would let a
    wedged ``nmcli``/``systemctl``/``timedatectl`` stall a page render, and
    no test would catch it. The ``fake_subprocess`` fixture records each
    call's ``timeout``/``ttl`` (via ``kw_for``), so this asserts the
    *argument value* — no wall-clock race, no latency model.

    The other two motivating #444 hotfix classes are also already covered:
    #428 failure-TTL cap by ``TestFailureTtl`` (``test_subprocess_helper.py``,
    fake-clock), #433 lazy-tail forks by ``TestLazyTailFilter``
    (``test_control_server_diagnostics.py``, call-count spy).
    """

    def test_fast_readers_use_short_subproc_timeout(self, app, fake_subprocess):
        # Every non-journalctl reader uses a short per-call budget so a wedged
        # nmcli/systemctl/timedatectl can't stall a page render. Two
        # representative call sites: one single-shot (timedatectl), one
        # batched (systemctl is-active). #430 split the single shared budget
        # into per-call constants; assert each site reads ITS own constant.
        units = ("litclock.service", "litclock-control.service")
        with app.app_context():
            _collectors._read_timezone()
            _collectors._batched_is_active(units)
        tz_kw = fake_subprocess.kw_for("diag-timezone")
        active_kw = fake_subprocess.kw_for("diag-systemctl-is-active-" + "+".join(units))
        assert tz_kw["timeout"] == _collectors.DIAG_TIMEDATECTL_TIMEOUT_S
        assert active_kw["timeout"] == _collectors.DIAG_SYSTEMCTL_TIMEOUT_S
        for kw in (tz_kw, active_kw):
            assert kw["ttl"] == _collectors.DIAG_SUBPROC_TTL_S

    # #430 — every fast reader reads its OWN per-call timeout constant.
    # Parametrized + sentinel-monkeypatched so the assertion BITES even while
    # the constants are seeded equal (DIAG_*_TIMEOUT_S == DIAG_SUBPROC_TIMEOUT_S
    # today): a call site still wired to the shared base would record the base
    # value, not the patched sentinel, and fail. Each reader runs in its own
    # parametrization so the fixture's exactly-one-call-per-key contract holds
    # (e.g. _read_signal_dbm internally calls the ip-route reader too, but under
    # a DIFFERENT cache key, so the iw key still sees exactly one call).
    _PER_CALL_WIRING = [
        ("DIAG_UNAME_TIMEOUT_S", "diag-uname-r", lambda: _collectors._read_kernel_release()),
        ("DIAG_GIT_HEAD_TIMEOUT_S", "diag-git-head", lambda: _collectors._read_git_head()),
        ("DIAG_IP_ROUTE_TIMEOUT_S", "diag-default-route", lambda: _collectors._read_default_route()),
        ("DIAG_NMCLI_TIMEOUT_S", "diag-wifi-ssid", lambda: _collectors._read_ssid()),
        ("DIAG_TIMEDATECTL_TIMEOUT_S", "diag-timezone", lambda: _collectors._read_timezone()),
        (
            "DIAG_SYSTEMCTL_TIMEOUT_S",
            "diag-systemctl-is-active-litclock.service+litclock-control.service",
            lambda: _collectors._batched_is_active(("litclock.service", "litclock-control.service")),
        ),
        ("DIAG_IW_LINK_TIMEOUT_S", "diag-iw-signal-wlan0", lambda: _collectors._read_signal_dbm()),
    ]

    @pytest.mark.parametrize("const_name, cache_key, reader", _PER_CALL_WIRING)
    def test_each_fast_reader_reads_its_own_per_call_constant(
        self, app, fake_subprocess, monkeypatch, const_name, cache_key, reader
    ):
        sentinel = 41.7  # a value no real budget would hold
        monkeypatch.setattr(_collectors, const_name, sentinel)
        with app.app_context():
            reader()
        assert fake_subprocess.kw_for(cache_key)["timeout"] == sentinel, (
            f"{cache_key} did not read {const_name} — it's likely still wired to the shared "
            f"DIAG_SUBPROC_TIMEOUT_S base, which defeats the #430 per-call tuning."
        )
