"""Tests for scripts/nm-dispatcher/99-litclock-ip-change + its install paths.

The NetworkManager dispatcher (#309) re-renders the e-ink corner QR
when wlan0's IP changes, so the displayed address doesn't lag behind
reality after DHCP churn. Mode 0755 root:root is mandatory — NM
silently skips dispatcher scripts that are group/world-writable.

These tests guard:

  1. The dispatcher script exists, parses cleanly under `sh -n`, and
     has executable mode bits in the repo (the install paths use
     `install -m 0755`, but a bare `cp` somewhere would inherit the
     repo mode, so keep it +x here).
  2. The gate on `.handoff-complete` is present (regression guard —
     without it, the dispatcher storms during first-boot hotspot
     teardown and queues litclock.service before the clock is ready).
  3. The interface filter (`wlan0`) is present (regression guard —
     dispatcher should only fire for the WiFi adapter).
  4. install.sh, update.sh, and pi-gen all install the file with the
     correct mode + ownership.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = REPO_ROOT / "scripts" / "nm-dispatcher" / "99-litclock-ip-change"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
PI_GEN_SERVICES = REPO_ROOT / "pi-gen" / "stage3" / "03-install-services" / "00-run.sh"


# ─── File contents ──────────────────────────────────────────────────────────


class TestDispatcherFile:
    def test_file_exists(self):
        assert DISPATCHER.is_file(), f"missing {DISPATCHER}"

    @pytest.mark.skipif(
        shutil.which("sh") is None,
        reason="sh not in PATH (unlikely)",
    )
    def test_passes_shell_syntax_check(self):
        result = subprocess.run(
            ["sh", "-n", str(DISPATCHER)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"sh -n rejected the dispatcher: {result.stdout}{result.stderr}"

    def test_repo_copy_is_executable(self):
        # The install paths use `install -m 0755`, but a future `cp` somewhere
        # would inherit the repo mode. Keep +x here as defense in depth.
        mode = DISPATCHER.stat().st_mode
        assert mode & stat.S_IXUSR, "dispatcher should be executable in repo"

    def test_gates_on_handoff_complete_marker(self):
        # Without this gate, the dispatcher would fire during first-boot
        # hotspot teardown and queue litclock.service before the clock is
        # ready to render real quotes. Regression guard.
        body = DISPATCHER.read_text()
        assert "/etc/litclock/.handoff-complete" in body, "dispatcher must check handoff-complete marker before firing"

    def test_filters_to_wlan0_interface(self):
        # Pin the exact shell-test form the dispatcher uses. Previous test
        # had an OR-cascade with a final `"wlan0" in body` clause that
        # matched any mention of wlan0 anywhere (including comments), so
        # deleting the actual guard would have silently passed. Caught by
        # /review (3x cross-confirm: testing + maintainability + adversarial
        # specialists). Tighten to the verbatim guard so a regression fails.
        body = DISPATCHER.read_text()
        assert '[ "$INTERFACE" = "wlan0" ] || exit 0' in body, (
            "dispatcher must contain the verbatim wlan0 guard; any other form risks a silently-defanged regression"
        )

    def test_reacts_to_ip_change_actions(self):
        body = DISPATCHER.read_text()
        # NM dispatcher actions that signal an IP change: `up` covers
        # reconnect-after-drop, `dhcp4-change` covers in-place lease renewal
        # with a new address, `dhcp6-change` the IPv6 equivalent.
        for action in ("up", "dhcp4-change"):
            assert action in body, f"dispatcher must react to NM action {action!r}"

    def test_uses_systemctl_no_block(self):
        # NM dispatcher must return promptly. `--no-block` lets us queue
        # the litclock.service start without waiting on the render.
        body = DISPATCHER.read_text()
        assert "--no-block" in body, "dispatcher must use systemctl --no-block so NM doesn't kill it for slowness"

    def test_same_ip_short_circuit_present(self):
        # /review adversarial finding A4: NM fires dhcp4-change on every
        # DHCP lease renewal, even when the IP is unchanged. Without a
        # same-IP guard, a flapping AP can queue unbounded litclock.service
        # starts, each holding the SPI bus and burning panel cycles. Guard
        # writes the rendered IP to /run/litclock/last-rendered-ip (tmpfs)
        # and skips when the current wlan0 IP matches.
        body = DISPATCHER.read_text()
        assert "/run/litclock/last-rendered-ip" in body, (
            "dispatcher must persist the last-rendered IP under /run/litclock "
            "so flapping dhcp4-change events are coalesced"
        )
        assert "ip -4 -o addr show wlan0" in body, (
            "dispatcher must read the current wlan0 IPv4 address to compare against the marker"
        )

    def test_logs_real_ip_changes_to_journal(self):
        # /review my-critical-pass finding #2: original dispatcher swallowed
        # every error with `2>/dev/null || true` so operators had no
        # journalctl evidence the dispatcher fired (or what happened).
        # One `logger -t litclock-dispatcher` line per real IP change keeps
        # the volume tiny — most days zero entries thanks to the same-IP
        # short-circuit above.
        body = DISPATCHER.read_text()
        assert "logger -t litclock-dispatcher" in body, (
            "dispatcher must log to journalctl so operators have visibility into when it fired"
        )


# ─── Install path coverage ──────────────────────────────────────────────────


class TestInstallPaths:
    """The dispatcher must be installed by every code path that provisions
    a LitClock: first-flash via pi-gen, post-flash setup via install.sh,
    and in-place upgrade via update.sh. Missing any one of these silently
    leaves the #309 UX bug in place on that install path."""

    def test_install_sh_copies_dispatcher(self):
        body = INSTALL_SH.read_text()
        assert "nm-dispatcher/99-litclock-ip-change" in body, "install.sh must install the NM dispatcher (#309)"
        assert "/etc/NetworkManager/dispatcher.d" in body, "install.sh must target the NM dispatcher directory"

    def test_install_sh_uses_correct_mode(self):
        # NM silently skips dispatcher scripts that aren't 0755 root:root.
        body = INSTALL_SH.read_text()
        # Look for the install line specifically (not just any 0755 in the file).
        lines = body.splitlines()
        nm_lines = [line for line in lines if "nm-dispatcher" in line or "NetworkManager/dispatcher" in line]
        # At least one of the install commands targeting the dispatcher must
        # specify mode 0755 and root:root ownership.
        joined = "\n".join(nm_lines + body.split("# #309")[1].splitlines()[:20])
        assert "0755" in joined and "root" in joined, (
            "install.sh dispatcher install must use mode 0755 root:root (NM silently rejects anything else)"
        )

    def test_update_sh_syncs_dispatcher(self):
        body = UPDATE_SH.read_text()
        assert "nm-dispatcher/99-litclock-ip-change" in body, "update.sh must sync the NM dispatcher on upgrades (#309)"
        assert "/etc/NetworkManager/dispatcher.d" in body, "update.sh must target the NM dispatcher directory"

    def test_update_sh_is_idempotent(self):
        # Mirror the sudoers Phase 5b pattern: cmp before install so we don't
        # log a noisy reinstall every weekly update.timer run when nothing
        # changed. Look for `cmp -s` near the dispatcher install.
        body = UPDATE_SH.read_text()
        # Slice the file from the #309 Phase 5c marker forward.
        marker = "# ─── Phase 5c"
        assert marker in body, "update.sh should have a Phase 5c section header for #309"
        phase_5c = body[body.index(marker) : body.index(marker) + 1500]
        assert "cmp -s" in phase_5c, "Phase 5c should diff against installed copy before reinstall (idempotency)"

    def test_pi_gen_installs_dispatcher(self):
        body = PI_GEN_SERVICES.read_text()
        assert "nm-dispatcher/99-litclock-ip-change" in body, "pi-gen stage3 must install the NM dispatcher (#309)"
        assert "/etc/NetworkManager/dispatcher.d" in body, "pi-gen stage3 must target the NM dispatcher directory"

    def test_pi_gen_uses_correct_mode(self):
        body = PI_GEN_SERVICES.read_text()
        # Find the #309 block and assert mode + ownership are correct.
        assert "# #309" in body, "pi-gen should have a #309 comment marker"
        block_start = body.index("# #309")
        block = body[block_start : block_start + 700]
        assert "0755" in block and "root" in block, "pi-gen dispatcher install must use mode 0755 root:root"


# ─── #387: 020-completion hardening (C1 + C2) ───────────────────────────────


class TestPrivilegeHardening387:
    """The dispatcher runs as root. Two escalation vectors closed in #387:
    C1 (it must invoke the root-owned mark-collected copy, not the pi-writable
    repo one) and C2 (root writes into pi-owned dirs must not follow symlinks)."""

    def test_c1_invokes_root_owned_mark_collected(self):
        body = DISPATCHER.read_text()
        assert "/usr/local/lib/litclock/litclock-mark-collected.sh" in body, (
            "dispatcher (root) must call the root-owned mark-collected copy (#387 C1)"
        )

    def test_c1_does_not_run_pi_writable_helper_as_root(self):
        body = DISPATCHER.read_text()
        # No unguarded call to the pi-writable repo path (that would be pi->root).
        assert "/home/pi/litclock/scripts/litclock-mark-collected.sh" not in body, (
            "dispatcher must NOT execute the pi-writable helper as root (#387 C1)"
        )

    def test_c2_last_rendered_ip_symlink_guard(self):
        body = DISPATCHER.read_text()
        assert '[ -L "$MARKER" ]' in body, (
            "dispatcher must refuse to follow a symlink at /run/litclock/last-rendered-ip (#387 C2)"
        )

    def test_install_paths_ship_root_owned_helpers(self):
        # All three provisioning paths must install both helpers root-owned to
        # /usr/local/lib/litclock so pi cannot rewrite what runs as root.
        for src, name in ((INSTALL_SH, "install.sh"), (UPDATE_SH, "update.sh"), (PI_GEN_SERVICES, "pi-gen")):
            body = src.read_text()
            assert "/usr/local/lib/litclock" in body, f"{name} must install the #387 helpers dir"
            assert "litclock-set-timezone" in body, f"{name} must install the tz-wrapper root-owned"
            assert "litclock-mark-collected.sh" in body, f"{name} must install mark-collected root-owned"
            assert "-o root -g root" in body, f"{name} must install the helpers root:root"


class TestMarkCollectedHardening387:
    """C2 symlink guard + mktemp staging in the mark-collected writer itself."""

    MARK = REPO_ROOT / "scripts" / "litclock-mark-collected.sh"

    def test_symlink_guard_present(self):
        body = self.MARK.read_text()
        assert '[ -L "$MARKER" ]' in body, "mark-collected must guard against a symlink marker (#387 C2)"

    def test_uses_mktemp_not_predictable_tmp(self):
        body = self.MARK.read_text()
        assert "mktemp" in body, "mark-collected must stage via mktemp (O_EXCL), not a guessable $$ path"
        assert 'tmp="$MARKER.tmp.$$"' not in body, "the guessable tmp path must be gone (#387 C2)"
