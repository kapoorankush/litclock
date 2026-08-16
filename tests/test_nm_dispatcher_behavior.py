"""Behavioural tests for scripts/nm-dispatcher/99-litclock-ip-change.

tests/test_nm_dispatcher_install.py pins the dispatcher's *text* (guards
present, install modes correct). That is not enough for litclock-dev#645: the bug there
was ordering, and every string those tests assert on was still present while a
freshly provisioned clock showed a false "Connection issue" banner for up to an
hour.

So these tests RUN the real script. The harness copies it to a tmp tree,
rewrites its absolute path prefixes to live under that tree, and puts stub
`ip`, `logger`, `systemctl` and `litclock-mark-collected.sh` binaries on PATH
which record their invocations.

The contract under test:

  * the collected marker and /run/litclock/last-rendered-ip are written
    REGARDLESS of .handoff-complete — inert bookkeeping, and the one `up` event
    on a fresh provision always lands before the handoff marker exists (litclock-dev#645);
  * only `systemctl start litclock.service` — the render — stays gated;
  * an address we must not believe is never recorded at all: our own setup
    hotspot's gateway, or a DHCP-failure link-local. `_anomalies.py` treats any
    non-empty lan_ip as a healthy network, so recording one of those would mute
    the very fault this fix exists to report (litclock-dev#645 /review).

TECHNIQUE NOTE — every assertion here was mutation-checked, because /review
proved several earlier ones could not fail:

* Four tests passed against a dispatcher replaced with `#!/bin/sh\nexit 0`.
  They asserted only absences. Each now carries a returncode check and a
  POSITIVE CONTROL proving the script reached the code under test.
* The harness's own prefix assertion was `assert count` (truthiness) against a
  body that includes COMMENTS, so deleting the mark-collected invocation left
  it satisfied by prose. It now works on a comment-stripped body and also
  asserts that no absolute path escapes the rewrite.
* The end-to-end test injected the marker path explicitly, so pointing
  DEFAULT_LAST_RENDERED_IP_PATH at a nonexistent path left the suite green.
  Writer and reader are now asserted to agree, and the round-trip drives the
  anomaly computation the symptom actually lived in.
* The `|| true` protecting the whole fix (a failing collected-helper would
  abort before the marker write, under `set -e`) had no coverage at all.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = REPO_ROOT / "scripts" / "nm-dispatcher" / "99-litclock-ip-change"

# A routable address the owner's router would hand out.
LAN_IP = "192.168.2.80"
# Addresses the dispatcher must refuse to record. HOTSPOT_GATEWAY is our own
# AP; 169.254.x is what NM self-assigns when DHCP fails, which is the likely
# real-world case rather than the exotic one.
HOTSPOT_GATEWAY = "10.42.0.1"
LINK_LOCAL_IP = "169.254.11.22"

# Absolute prefixes the dispatcher hardcodes (it runs as root under NM, so it
# deliberately has no env seams). Each MUST still appear in CODE, or the
# harness would be pointing a rewritten script at real system paths.
PATH_PREFIXES = ("/etc/litclock", "/run/litclock", "/usr/local/lib/litclock")

# Absolute paths that are correct to leave alone — writing to the real one is
# the intent, not an escape from the harness.
ALLOWED_ABSOLUTE_PATHS = frozenset({"/dev/null"})

STUB_IP = """#!/bin/sh
# Stand-in for iproute2. Emits a realistic `ip -4 -o addr show wlan0` line so
# the dispatcher's awk field-split is exercised for real; FAKE_IP empty means
# "wlan0 has no v4 address".
[ -n "${FAKE_IP:-}" ] || exit 0
printf '2: wlan0    inet %s/24 brd 192.168.2.255 scope global dynamic noprefixroute wlan0\\n' "$FAKE_IP"
"""

STUB_LOGGER = """#!/bin/sh
printf '%s\\n' "$*" >> "$LOGGER_LOG"
"""

STUB_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
"""

STUB_MARK_COLLECTED = """#!/bin/sh
printf '%s\\n' "$*" >> "$MARK_COLLECTED_LOG"
"""

# Records the argv the dispatcher uses to create /run/litclock, then does the
# real work minus the ownership flags (the suite does not run as root). The
# ownership is the whole point of the call, and it cannot be asserted any other
# way from an unprivileged test.
STUB_INSTALL = """#!/bin/sh
printf '%s\\n' "$*" >> "$INSTALL_LOG"
for arg in "$@"; do
    case "$arg" in -*) ;; *) mkdir -p "$arg" ;; esac
done
"""


def _code_only(body: str) -> str:
    """The dispatcher with comment lines stripped.

    All three path prefixes appear in prose as well as code, so any assertion
    made against the raw file is satisfiable by a comment alone — which is how
    an earlier version of this harness would have survived deletion of the
    mark-collected invocation.
    """
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


class Harness:
    """A rewritten dispatcher plus the fake system it talks to."""

    def __init__(self, tmp_path: Path, *, helper_exit: int = 0, helper_present: bool = True):
        self.root = tmp_path
        self.etc = tmp_path / "etc" / "litclock"
        self.run = tmp_path / "run" / "litclock"
        self.libexec = tmp_path / "usr" / "local" / "lib" / "litclock"
        self.bin = tmp_path / "bin"
        for d in (self.etc, self.run, self.libexec, self.bin):
            d.mkdir(parents=True, exist_ok=True)

        self.marker = self.run / "last-rendered-ip"
        self.handoff_complete = self.etc / ".handoff-complete"
        self.logger_log = tmp_path / "logger.log"
        self.systemctl_log = tmp_path / "systemctl.log"
        self.mark_collected_log = tmp_path / "mark-collected.log"
        self.install_log = tmp_path / "install.log"

        body = DISPATCHER.read_text()
        code = _code_only(body)
        for prefix in PATH_PREFIXES:
            assert prefix in code, (
                f"dispatcher no longer uses {prefix!r} in CODE (comments do not count) — this "
                f"harness rewrites that prefix to a tmp tree, so a rename would silently point "
                f"the test at the real system, or at nothing. Update PATH_PREFIXES."
            )
            body = body.replace(prefix, f"{tmp_path}{prefix}")

        self.script = tmp_path / "99-litclock-ip-change"
        self.script.write_text(body)
        self.script.chmod(0o755)

        self._write_stub("ip", STUB_IP)
        self._write_stub("logger", STUB_LOGGER)
        self._write_stub("systemctl", STUB_SYSTEMCTL)
        self._write_stub("install", STUB_INSTALL)
        # The dispatcher only invokes the helper if it is executable, and
        # deliberately has no repo-copy fallback (#387 C1).
        if helper_present:
            self._write_stub(
                str(self.libexec / "litclock-mark-collected.sh"),
                STUB_MARK_COLLECTED if helper_exit == 0 else f"#!/bin/sh\nexit {helper_exit}\n",
            )

    def _write_stub(self, name: str, body: str) -> None:
        path = Path(name) if os.path.isabs(name) else self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def break_logger(self) -> None:
        self._write_stub("logger", "#!/bin/sh\nexit 1\n")

    def complete_handoff(self) -> None:
        self.handoff_complete.write_text("")

    def run_dispatcher(
        self,
        interface: str = "wlan0",
        action: str = "up",
        ip: str = LAN_IP,
        connection_id: str = "MyHomeWiFi",
    ):
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin}:{env.get('PATH', '')}",
            FAKE_IP=ip,
            CONNECTION_ID=connection_id,
            LOGGER_LOG=str(self.logger_log),
            SYSTEMCTL_LOG=str(self.systemctl_log),
            MARK_COLLECTED_LOG=str(self.mark_collected_log),
            INSTALL_LOG=str(self.install_log),
        )
        # Exec the file directly rather than `sh <file>`, so the script's own
        # shebang chooses the interpreter — which is what NetworkManager does.
        return subprocess.run(
            [str(self.script), interface, action],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    # ── observations ────────────────────────────────────────────────────
    def _read(self, path: Path) -> str:
        return path.read_text() if path.exists() else ""

    @property
    def recorded_ip(self) -> str | None:
        return self.marker.read_text().strip() if self.marker.exists() else None

    @property
    def rendered(self) -> bool:
        return "litclock.service" in self._read(self.systemctl_log)

    @property
    def systemctl_invocations(self) -> str:
        return self._read(self.systemctl_log)

    @property
    def collected(self) -> bool:
        return "network" in self._read(self.mark_collected_log)

    @property
    def journal(self) -> str:
        return self._read(self.logger_log)

    @property
    def install_invocations(self) -> str:
        return self._read(self.install_log)


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# ─── The harness must not be able to test nothing ───────────────────────────


def test_shebang_is_posix_sh():
    # NM execs the file; the harness must not be the only thing choosing the
    # interpreter, and a bash-ism would be invisible to `sh -n` too.
    assert DISPATCHER.read_text().splitlines()[0] == "#!/bin/sh"


def test_path_prefixes_cover_every_absolute_path_the_script_executes():
    """A path outside PATH_PREFIXES runs against the REAL filesystem.

    As root in a container CI that would create or mutate real system paths,
    and the test would still look like it passed.
    """
    code = _code_only(DISPATCHER.read_text())
    stray = {
        p
        for p in re.findall(r"(?<![\w.])/(?:[\w.-]+/)*[\w.-]+", code)
        if not p.startswith(PATH_PREFIXES) and p not in ALLOWED_ABSOLUTE_PATHS
    }
    assert not stray, f"absolute paths the harness does not rewrite: {sorted(stray)}"


def test_writer_and_reader_agree_on_the_marker_path():
    """litclock-dev#645 was a write that never happened; the mirror image is a read
    pointed somewhere else.

    Three independent literals name this file — the dispatcher's `MARKER=`,
    control_server._network, and routes/diagnostics/_collectors — and nothing
    pinned them together. Repointing either reader left the whole suite green.
    """
    from control_server import _network
    from control_server.routes.diagnostics import _collectors

    m = re.search(r"^MARKER=(\S+)$", DISPATCHER.read_text(), re.M)
    assert m, "dispatcher no longer assigns MARKER= on its own line"
    written = m.group(1)

    assert written == _network.DEFAULT_LAST_RENDERED_IP_PATH
    assert written == _collectors.DEFAULT_LAST_RENDERED_IP_PATH


def test_hotspot_constants_match_the_python_source_of_truth():
    """The shell cannot import them, so assert the parity instead.

    If wifi_provision ever renames the connection or moves the gateway, the
    dispatcher would silently start recording the hotspot address as the LAN
    IP again.
    """
    import setup_server
    import wifi_provision

    body = DISPATCHER.read_text()
    con = re.search(r"^HOTSPOT_CON_NAME=(\S+)$", body, re.M)
    gw = re.search(r"^HOTSPOT_GATEWAY=(\S+)$", body, re.M)
    assert con and gw, "dispatcher must declare HOTSPOT_CON_NAME and HOTSPOT_GATEWAY on their own lines"

    assert con.group(1) == wifi_provision.HOTSPOT_CON_NAME
    assert gw.group(1) == wifi_provision.HOTSPOT_GATEWAY
    assert gw.group(1) == setup_server.HOTSPOT_GATEWAY_IP


# ─── litclock-dev#645: the fresh-provision window ────────────────────────────────────


class TestPreHandoff:
    """On a fresh provision the ordering is fixed: wlan0's `up` onto the
    user's SSID fires BEFORE .handoff-complete is written. That one event has
    to record the address, or the marker stays absent until the next DHCP
    renewal (7200s lease observed on hardware) and the diagnostics Network
    section reports a fault on a perfectly healthy clock.
    """

    def test_records_the_ip_before_handoff_completes(self, harness):
        result = harness.run_dispatcher(ip=LAN_IP)

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip == LAN_IP, (
            "the pre-handoff `up` event must record the LAN IP — this is litclock-dev#645: "
            "gating the marker write meant the only event that could write it was skipped"
        )

    def test_refreshes_the_collected_marker_before_handoff_completes(self, harness):
        # Without this, network_never_collected stays true while SSID is
        # populated — and _anomalies.py only mutes to the grey "Not yet
        # collected" tier when SSID is EMPTY, so the section renders as a
        # fault rather than as uncollected.
        result = harness.run_dispatcher()

        assert result.returncode == 0, result.stderr
        assert harness.collected, "the collected marker must refresh regardless of the handoff gate"

    def test_does_not_render_before_handoff_completes(self, harness):
        # The half of the gate that must survive: litclock.service is not
        # ready to paint real quotes during hotspot teardown.
        result = harness.run_dispatcher()

        assert result.returncode == 0, result.stderr
        # Positive control. Without it this assertion is satisfied by a script
        # that did nothing at all — verified: it passed against `exit 0`.
        assert harness.collected, "script never reached the bookkeeping; the assertion below would be vacuous"
        assert not harness.rendered, "the render must stay gated on .handoff-complete"

    def test_says_why_it_did_not_render(self, harness):
        # litclock-dev#645 hid in a silent exit; litclock-dev#646 is the same lesson on the
        # Python side. Leave journal evidence.
        harness.run_dispatcher(ip=LAN_IP)

        assert "handoff not complete" in harness.journal
        assert LAN_IP in harness.journal
        assert "re-rendering" not in harness.journal, "must not claim a render it did not perform"

    def test_does_not_claim_a_record_it_did_not_make(self, harness):
        """`up` fires before DHCP hands out a lease, and a wrong-password retry
        loop emits repeated address-less `up` events. Saying "recorded as
        unknown" there asserts a write the CURRENT_IP guard skipped — the same
        defect as litclock-dev#646's timer claiming a completion it did not perform."""
        result = harness.run_dispatcher(ip="")

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip is None
        assert "nothing recorded" in harness.journal
        assert "recorded as unknown" not in harness.journal

    def test_second_event_for_the_same_ip_is_deliberately_silent(self, harness):
        """NM fires `up` then `dhcp4-change` back to back, so the same-IP
        short-circuit is the common path, not a corner. It exits above the
        gate's logger, so repeat events are quiet in BOTH handoff states —
        pinned here so the "no more silent exits" claim stays scoped to events
        that actually changed something."""
        harness.run_dispatcher(action="up", ip=LAN_IP)
        harness.logger_log.write_text("")

        result = harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip == LAN_IP
        assert not harness.rendered
        assert harness.journal == "", "an unchanged IP must not add journal volume"


class TestAddressesThatMustNotBeBelieved:
    """litclock-dev#645 /review.

    _anomalies.py:134 is `if not values.get("lan_ip")` — ANY non-empty string
    suppresses the missing-IP anomaly, with no check that it is routable. So
    recording the wrong address is worse than recording none: a clock that
    never got a lease reports itself healthy, which is the exact fault class
    this fix exists to surface.
    """

    def test_does_not_record_our_own_hotspot_gateway(self, harness):
        result = harness.run_dispatcher(ip=HOTSPOT_GATEWAY, connection_id="litclock-hotspot")

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip is None, "the setup hotspot's own address is not the clock's LAN IP"
        assert not harness.rendered

    def test_does_not_mark_network_collected_while_on_our_own_hotspot(self, harness):
        """The collected marker is PERSISTENT (/var/lib) and reset-setup.sh
        does not remove it. Writing it during the first hotspot of the first
        boot would retire the grey "Not yet collected" tier for `network` on
        every device, permanently, on a false premise."""
        harness.run_dispatcher(ip=HOTSPOT_GATEWAY, connection_id="litclock-hotspot")

        assert not harness.collected

    def test_recognises_the_hotspot_by_address_when_connection_id_is_absent(self, harness):
        # CONNECTION_ID is the primary signal; the address is the backstop.
        result = harness.run_dispatcher(ip=HOTSPOT_GATEWAY, connection_id="")

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip is None

    def test_does_not_record_a_dhcp_failure_link_local(self, harness):
        """The likely case, not the exotic one: DHCP fails on the owner's
        network and NM self-assigns 169.254.x. Recording it would suppress the
        missing-IP anomaly at every uptime, forever."""
        harness.complete_handoff()

        result = harness.run_dispatcher(ip=LINK_LOCAL_IP)

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip is None
        assert LINK_LOCAL_IP not in harness.journal

    @pytest.mark.parametrize("addr", [LINK_LOCAL_IP, ""], ids=["link-local", "no-address-at-all"])
    def test_a_repeated_unusable_address_coalesces_like_a_real_one(self, harness, addr):
        """litclock-dev#667 — the guard that was missing, and why the bug shipped.

        The test above asserts `recorded_ip is None` and never looks at
        `rendered`, so its observation window did not contain the behaviour
        that regressed. Blanking the address removed it from the same-IP
        short-circuit's coverage (that gate reads `[ -n "$CURRENT_IP" ]`), so
        every repeat event fell through to a full e-ink render.

        A clock whose DHCP is permanently broken sits in exactly this state and
        NM keeps retrying, so this is the #309 /review A4 failure -- unbounded
        litclock.service starts, each holding the SPI bus and burning panel
        cycles -- on a device with a finite panel budget and no UPS.

        Three identical events must render at most once, the same as three
        identical events at a real address.
        """
        harness.complete_handoff()

        for _ in range(3):
            result = harness.run_dispatcher(action="dhcp4-change", ip=addr)
            assert result.returncode == 0, result.stderr

        # `rendered` is derived from the systemctl log, so count invocations
        # across the three events rather than resetting a flag.
        n = harness.systemctl_invocations.count("litclock.service")
        assert n == 1, (
            f"litclock-dev#667: three identical events at {addr!r} queued {n} renders. "
            "0 means coalescing swallowed the transition (the first event after a "
            "change must repaint); >1 means it did not coalesce at all."
        )
        assert harness.recorded_ip is None, "and it still must not be recorded as a believable address"

    @pytest.mark.parametrize("addr", [LINK_LOCAL_IP, ""], ids=["link-local", "no-address-at-all"])
    def test_recovering_to_the_same_address_still_repaints(self, harness, addr):
        """litclock-dev#667 — regaining the address is as much news as losing it.

        The panel spent the outage advertising a QR for an address that did not
        work; the dispatcher render exists to close that window rather than
        wait up to 60s for the next minute tick. An earlier version of this fix
        skipped the observed write on an empty address, so the marker kept the
        stale pre-outage IP and the recovery event matched it and was swallowed
        — for the no-address flavour only, while link-local recovered correctly.
        """
        harness.complete_handoff()
        harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)
        harness.run_dispatcher(action="dhcp4-change", ip=addr)
        before = harness.systemctl_invocations.count("litclock.service")

        harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)

        after = harness.systemctl_invocations.count("litclock.service")
        assert after == before + 1, "recovering to the previous address must repaint"

    def test_a_planted_observed_marker_cannot_suppress_the_diagnostics_write(self, harness):
        """litclock-dev#667 — /run/litclock is 0755 pi pi, so an unprivileged writer can
        drop a plain (non-symlink) file at the coalescing marker. The #387 C2
        symlink guards do not address CONTENT, and this file gates an early
        exit that now sits above the $MARKER write.

        Without the second clause in that exit, planting the current LAN IP
        here suppresses the last-rendered-ip write for the life of the boot,
        read_lan_ip returns nothing, and the clock shows a permanent false
        "Connection issue" — litclock-dev#645's exact symptom, reachable by a file write.
        """
        harness.complete_handoff()
        (harness.run / "last-observed-ip").write_text(f"{LAN_IP}\n")

        for _ in range(3):
            harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)

        assert harness.recorded_ip == LAN_IP, (
            "a pi-writable coalescing file must never gate the diagnostics write"
        )

    def test_a_deleted_marker_self_heals_on_the_next_event(self, harness):
        """The same clause, without an attacker. Losing $MARKER while the
        observed marker survives must not leave diagnostics blind until the
        address happens to change."""
        harness.complete_handoff()
        harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)
        harness.marker.unlink()

        harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)

        assert harness.recorded_ip == LAN_IP, "the believable-address marker must self-heal"

    def test_drops_a_symlink_planted_at_the_observed_marker(self, harness, tmp_path):
        """#387 C2 applies to both markers; only $MARKER had a test."""
        victim = tmp_path / "victim"
        victim.write_text("do not touch\n")
        observed = harness.run / "last-observed-ip"
        observed.symlink_to(victim)
        harness.complete_handoff()

        harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)

        assert victim.read_text() == "do not touch\n", "root must not follow a planted symlink"
        assert not observed.is_symlink(), "the symlink must be dropped"

    def test_a_link_local_after_a_real_address_still_renders_once(self, harness):
        """Coalescing must not swallow the transition. Losing the real address
        IS news -- the panel is showing a QR for an address that no longer
        works -- so the first blanked event after a change still repaints."""
        harness.complete_handoff()
        harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)
        before = harness.systemctl_invocations.count("litclock.service")

        result = harness.run_dispatcher(action="dhcp4-change", ip=LINK_LOCAL_IP)

        assert result.returncode == 0, result.stderr
        after = harness.systemctl_invocations.count("litclock.service")
        assert after == before + 1, "the first event after losing a routable address must repaint"

    def test_a_link_local_still_counts_as_network_data_collected(self, harness):
        """Deliberate asymmetry: unlike the hotspot, this IS a real event on a
        real SSID, so the section is collected — it just has no believable
        address, which is what makes the anomaly fire rather than mute."""
        harness.complete_handoff()

        harness.run_dispatcher(ip=LINK_LOCAL_IP)

        assert harness.collected

    def test_does_not_record_loopback(self, harness):
        harness.complete_handoff()

        harness.run_dispatcher(ip="127.0.0.1")

        assert harness.recorded_ip is None


class TestPostHandoff:
    def test_renders_and_records_after_handoff(self, harness):
        harness.complete_handoff()

        result = harness.run_dispatcher(ip="192.168.2.90")

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip == "192.168.2.90"
        assert harness.rendered
        assert "re-rendering" in harness.journal

    def test_uses_no_block_so_nm_does_not_kill_it(self, harness):
        harness.complete_handoff()

        harness.run_dispatcher()

        # Via the accessor, so a regressed render fails on the assertion rather
        # than dying with FileNotFoundError on a log the stub never created.
        assert "--no-block" in harness.systemctl_invocations

    def test_same_ip_does_not_re_render(self, harness):
        # #309 adversarial finding A4: every lease renewal fires dhcp4-change,
        # and each render holds the SPI bus.
        harness.complete_handoff()
        harness.marker.write_text(f"{LAN_IP}\n")

        result = harness.run_dispatcher(action="dhcp4-change", ip=LAN_IP)

        assert result.returncode == 0, result.stderr
        assert not harness.rendered, "unchanged IP must not queue a render"
        assert harness.collected, "...but the collected marker still refreshes on a renewal (#445)"

    @pytest.mark.parametrize("action", ["up", "dhcp4-change", "dhcp6-change"])
    def test_every_ip_change_action_records_and_renders(self, harness, action):
        # dhcp6-change was accepted by the case arm but exercised by no test:
        # deleting it from the arm passed the whole suite.
        harness.complete_handoff()

        harness.run_dispatcher(action=action, ip="192.168.2.99")

        assert harness.recorded_ip == "192.168.2.99"
        assert harness.rendered


class TestFailuresThatMustNotStarveTheRecord:
    """The `|| true`s are load-bearing under `set -e`, and none had coverage.

    Each of these is litclock-dev#645's shape one token away: an early abort that skips
    the marker write, bringing the false "Connection issue" straight back.
    """

    def test_a_failing_collected_helper_still_records_the_ip(self, tmp_path):
        h = Harness(tmp_path, helper_exit=1)

        result = h.run_dispatcher(ip=LAN_IP)

        assert result.returncode == 0, result.stderr
        assert h.recorded_ip == LAN_IP, "a failing helper must not abort the script before the marker write"

    def test_a_missing_collected_helper_still_records_the_ip(self, tmp_path):
        # #387 C1: deliberately no repo-copy fallback, so an image that never
        # installed the helper must still record the IP.
        h = Harness(tmp_path, helper_present=False)

        result = h.run_dispatcher(ip=LAN_IP)

        assert result.returncode == 0, result.stderr
        assert h.recorded_ip == LAN_IP
        assert not h.collected

    def test_a_failing_logger_does_not_suppress_the_render(self, harness):
        # An unguarded `logger` under `set -e` aborts before systemctl. That is
        # also the reachable end of the oversized-marker path: an execve too
        # large fails E2BIG, which is why LAST_IP is read bounded.
        harness.complete_handoff()
        harness.break_logger()

        result = harness.run_dispatcher(ip="192.168.2.93")

        assert result.returncode == 0, result.stderr
        assert harness.rendered, "a logging hiccup must never swallow the render"

    def test_an_oversized_marker_does_not_abort_the_script(self, harness):
        # $MARKER is in a pi-writable dir but read and re-emitted by root.
        harness.complete_handoff()
        harness.marker.write_text("9" * 200_000)

        result = harness.run_dispatcher(ip=LAN_IP)

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip == LAN_IP
        assert harness.rendered

    def test_a_newline_in_the_marker_cannot_forge_a_journal_line(self, harness):
        # journald is the only diagnostic channel here, and this script is one
        # of its writers — a planted newline would render as a standalone
        # dispatcher entry under `journalctl -o cat`.
        harness.complete_handoff()
        harness.marker.write_text("1.2.3.4\nAug 15 00:00:00 pi litclock-dispatcher: forged\n")

        harness.run_dispatcher(ip=LAN_IP)

        # The payload survives as inert text on the real line (`tr -d` folds it
        # in) — what must NOT survive is its own line. A standalone entry is
        # what reads as a genuine dispatcher log during triage.
        lines = [ln for ln in harness.journal.splitlines() if ln.strip()]
        assert len(lines) == 1, f"planted newline produced a second journal line: {lines}"
        assert not lines[0].startswith("Aug ")


class TestGatesThatMustNotMove:
    """Moving the handoff gate must not have widened the other two filters."""

    def test_ignores_other_interfaces(self, harness):
        harness.complete_handoff()

        result = harness.run_dispatcher(interface="eth0")

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip is None
        assert not harness.collected
        assert not harness.rendered
        assert harness.journal == "", "a filtered event must be indistinguishable from silence, not from a crash"

    def test_ignores_irrelevant_actions(self, harness):
        harness.complete_handoff()

        result = harness.run_dispatcher(action="down")

        assert result.returncode == 0, result.stderr
        assert harness.recorded_ip is None
        assert not harness.collected
        assert not harness.rendered
        assert harness.journal == ""

    def test_an_addressless_event_leaves_a_previously_recorded_ip_alone(self, harness):
        """Deliberately NOT cleared. `up` can fire before DHCP lands, and
        ANOMALY_LAN_IP_SETTLE_S only protects the first 300s of uptime — so
        clearing on every addressless event would trip the anomaly on a
        long-running clock over a transient. That is litclock-dev#645 inverted."""
        harness.complete_handoff()
        harness.marker.write_text(f"{LAN_IP}\n")

        result = harness.run_dispatcher(ip="")

        assert result.returncode == 0, result.stderr
        assert harness.collected, "positive control: the script ran"
        assert harness.recorded_ip == LAN_IP

    def test_drops_a_symlink_planted_at_the_marker(self, harness):
        # #387 C2: /run/litclock is 0755 pi pi and this script runs as root.
        harness.complete_handoff()
        victim = harness.root / "victim"
        victim.write_text("do not touch\n")
        harness.marker.symlink_to(victim)

        harness.run_dispatcher(ip=LAN_IP)

        assert victim.read_text() == "do not touch\n", "dispatcher must not write through a planted symlink"
        assert not harness.marker.is_symlink()
        assert harness.recorded_ip == LAN_IP


def test_fresh_provision_event_sequence(harness):
    """The literal litclock-dev#645 timeline, in order, against one tree.

    Every other test runs a single event against a clean tree; the change is
    about ordering, so at least one test has to be a sequence.
    """
    # 1. Setup hotspot is up on wlan0. Not our LAN address, not collected.
    harness.run_dispatcher(action="up", ip=HOTSPOT_GATEWAY, connection_id="litclock-hotspot")
    assert harness.recorded_ip is None
    assert not harness.collected

    # 2. The owner's SSID comes up. Pre-handoff — this is the event litclock-dev#645
    #    skipped, and the whole bug.
    harness.run_dispatcher(action="up", ip=LAN_IP)
    assert harness.recorded_ip == LAN_IP
    assert not harness.rendered, "still pre-handoff"

    # 3. Handoff completes, then the lease changes. Now the render fires.
    harness.complete_handoff()
    harness.run_dispatcher(action="dhcp4-change", ip="192.168.2.81")
    assert harness.recorded_ip == "192.168.2.81"
    assert harness.rendered


def test_no_false_network_anomaly_after_a_fresh_provision(harness):
    """The symptom, end to end.

    litclock-dev#645 surfaced in the PWA, not the shell: the Network section read
    lan_ip from this marker and raised "Connection issue" on a healthy clock.
    Asserting the file round-trips through read_lan_ip is not enough — that
    passed while the anomaly logic still tripped. Drive the anomaly computation
    itself, past the 300s settling grace, which is where the banner appeared.
    """
    from control_server.routes.diagnostics._anomalies import ANOMALY_LAN_IP_SETTLE_S, _compute_anomalies

    # Fresh provision: wlan0 up on the owner's SSID, handoff not yet complete.
    harness.run_dispatcher(ip=LAN_IP)

    values = {
        "lan_ip": _read_lan_ip_like_production(harness),
        "ssid": "MyHomeWiFi",
        "signal_dbm": -48,
        "uptime_s": ANOMALY_LAN_IP_SETTLE_S + 1719,  # the bench observation
    }
    assert "network" not in _compute_anomalies(values)


def test_a_clock_that_never_got_a_lease_still_reports_the_fault(harness):
    """The other half of the same contract, and the regression the address
    filter exists to prevent: suppressing the false alarm must not suppress
    the true one."""
    from control_server.routes.diagnostics._anomalies import ANOMALY_LAN_IP_SETTLE_S, _compute_anomalies

    harness.run_dispatcher(ip=LINK_LOCAL_IP)

    values = {
        "lan_ip": _read_lan_ip_like_production(harness),
        "ssid": "MyHomeWiFi",
        "signal_dbm": -48,
        "uptime_s": ANOMALY_LAN_IP_SETTLE_S + 60,
    }
    assert "network" in _compute_anomalies(values)


def _read_lan_ip_like_production(harness: Harness) -> str | None:
    from control_server._network import read_lan_ip

    return read_lan_ip(str(harness.marker))


@pytest.mark.parametrize(
    "addr", [LAN_IP, LINK_LOCAL_IP, ""], ids=["real", "link-local", "no-address-at-all"]
)
def test_creates_the_run_dir_pi_owned_not_root_owned(tmp_path, addr):
    """tmpfiles.d declares /run/litclock as 0755 pi pi at sysinit, so this call
    is defensive — but the script runs as ROOT and, since the gate moved, runs
    EARLIER in boot. On any boot where the dir is genuinely absent, a bare
    `mkdir -p` would create it root:root and every pi-side writer would fail
    silently for the rest of that boot: the current-quote file and LKG
    heartbeat, update_state, the weather cache, the gift-message write.

    litclock-dev#667 parameterised this. It previously ran only with a believable
    address, so its observation window contained just one of the two creation
    sites — and on a clock with permanently broken DHCP the believable-address
    path never runs at all, making the other site the ONLY creator for the life
    of the boot.
    """
    h = Harness(tmp_path)
    for child in sorted(h.run.iterdir()):
        child.unlink()
    h.run.rmdir()

    result = h.run_dispatcher(ip=addr)

    assert result.returncode == 0, result.stderr
    assert h.run.is_dir(), "the dispatcher must create /run/litclock when it is missing"
    assert "-o pi" in h.install_invocations and "-g pi" in h.install_invocations, (
        "the run dir must be created pi-owned; a root-owned /run/litclock breaks every pi-side writer"
    )
