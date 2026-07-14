"""P2 memory sentinel for control_server (issue #262).

PLAN-LitClock-Control-PWA.md P2 specifies: "<40MB RSS for control_server.
CI perf-sentinel test fails on regression to >50MB."

Why this test spawns a subprocess instead of measuring in-process
------------------------------------------------------------------
An in-process `resource.getrusage(RUSAGE_SELF).ru_maxrss` check is useless
as a sentinel: the pytest interpreter has the dev venv loaded (and may
have many earlier test modules' imports lingering), which dwarfs the
actual control_server footprint. Issue #262 explicitly calls this out.

So we shell out to a clean ``python3 -m control_server.app`` subprocess,
wait for it to bind its port (warm + serve /api/health), exercise
/api/status to stretch the M2 quote-corpus + status template import path,
and then read ``/proc/<pid>/status`` ``VmRSS`` from the subprocess —
which sees only control_server's own working set, not pytest's.

Why the cap is 50_000 KB
------------------------
PLAN target is <40MB steady-state; the sentinel sits at 50MB (10MB
headroom) so M3+M4+M5 route additions have room to grow without
tripping the alarm, while still failing loudly before the Pi Zero 2W's
512MB RAM gets squeezed under load.

Cap matches PLAN P2 spec. Measured headroom on x86_64 GitHub Actions
ubuntu-latest is 33-34 MB (~16 MB margin). Pi Zero 2W (ARMv7, glibc)
may behave differently — re-verify on hardware if margins matter.

Skipped on non-Linux (``/proc/<pid>/status`` is Linux-specific). CI
runs ubuntu-latest so this always exercises.
"""

from __future__ import annotations

import http.client
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Memory sentinel: hard fail when control_server's resident set exceeds
# this many KB. See module docstring for the rationale. Boundary is
# inclusive — exactly 50_000 KB passes; >50_000 fails. Mirrors PLAN
# wording "fails at >50MB".
RSS_CAP_KB = 50_000

# Leak-detection delta cap. After the warm-up phase, RSS should be
# steady-state — any further growth across N additional requests means
# an allocator is holding per-request state. 1024 KB is forgiving enough
# that allocator arena rounding does not trip it, but tight enough that
# a real linear leak ~100KB/request will fire within a handful of hits.
RSS_DELTA_CAP_KB = 1024

# Warm-up hits (lazy-import realization) and leak-probe hits (delta).
WARMUP_HITS = 5
LEAK_PROBE_HITS = 15

# Boot timeout: how long we wait for the spawned server to start serving
# /api/health. Locally this takes ~0.5-1s; CI cold-imports may be slower.
BOOT_TIMEOUT_S = 15.0

REPO_ROOT = Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="/proc/<pid>/status VmRSS is Linux-only",
)


def _find_free_port() -> int:
    """Bind-and-release trick to grab a port the kernel currently considers
    free on 127.0.0.1. Pairs with LITCLOCK_CONTROL_BIND=127.0.0.1 in the
    spawned subprocess so the probe scope matches the waitress bind scope.

    A TOCTOU window remains between releasing here and waitress binding;
    that window is closed defensively by _wait_for_health detecting an
    early subprocess exit (e.g., "address already in use") and surfacing
    stderr immediately instead of waiting for BOOT_TIMEOUT_S.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(port: int, proc: subprocess.Popen, timeout_s: float) -> bool:
    """Poll /api/health until 200, or the subprocess dies, or we time out.

    Returns True if the server is ready, False otherwise. An early
    subprocess exit (e.g., bind contention from a TOCTOU loser) short-
    circuits the wait so the fixture surfaces stderr promptly.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return True
        except (ConnectionError, OSError, http.client.HTTPException):
            time.sleep(0.1)
    return False


def _read_vmrss_kb(pid: int) -> int:
    """Parse VmRSS (in KB) from /proc/<pid>/status. Returns -1 if not found."""
    with open(f"/proc/{pid}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                # Format: "VmRSS:\t   12345 kB"
                return int(line.split()[1])
    return -1


def _hit(port: int, path: str) -> int | None:
    """GET ``path`` against the spawned server. Returns status code or None
    on failure — failures are non-fatal because the sentinel cares about
    RSS, not response correctness."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status
    except Exception:
        return None


def _terminate_group(proc: subprocess.Popen) -> None:
    """Kill the subprocess's whole process group.

    Pairs with ``start_new_session=True`` at Popen time: the subprocess
    runs in its own session/process-group, so SIGKILL'ing pytest does
    NOT take the child with it. Teardown therefore must SIGTERM the
    group (not just the leader) and SIGKILL fallback if the leader is
    a slow shutdown.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


@pytest.fixture
def spawned_control_server():
    """Spawn ``python -m control_server.app`` as a subprocess with a free
    port and yield ``(proc, port)``. Teardown SIGTERMs the whole process
    group so a pytest SIGKILL (CI runner timeout, OOM killer) cannot
    orphan the subprocess.

    The fixture uses ``sys.executable`` so it inherits whichever venv
    pytest itself is running under — that's the same venv CI installs
    requirements.txt into, which mirrors the Pi's production venv.
    """
    port = _find_free_port()
    env = os.environ.copy()
    env["LITCLOCK_CONTROL_PORT"] = str(port)
    env["LITCLOCK_CONTROL_THREADS"] = "4"
    # Bind the spawned server to loopback only. Production default is
    # 0.0.0.0; the test scope is 127.0.0.1 so the probe is not exposed
    # on the LAN for the duration of the run.
    env["LITCLOCK_CONTROL_BIND"] = "127.0.0.1"
    # Ensure the spawned subprocess can import control_server. Match the
    # pyproject.toml ``pythonpath = ["src", "image-gen"]`` so the
    # subprocess sees the same module layout pytest itself sees.
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # Skip the get_version() git-describe fork at factory time so the boot
    # is deterministic and fast — irrelevant to RSS measurement.
    env["LITCLOCK_VERSION_OVERRIDE"] = "v0.test"

    proc = subprocess.Popen(
        [sys.executable, "-m", "control_server.app"],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # New session → independent process group; teardown via killpg
        # survives a pytest SIGKILL without orphaning the child.
        start_new_session=True,
    )
    try:
        if not _wait_for_health(port, proc, BOOT_TIMEOUT_S):
            # Surface stderr so a CI failure is debuggable — especially
            # the bind-contention case where the TOCTOU loser exits early
            # with "address already in use".
            try:
                out, err = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                _terminate_group(proc)
                out, err = proc.communicate(timeout=2.0)
            early_exit_code = proc.returncode
            pytest.fail(
                "control_server subprocess never became healthy.\n"
                f"early_exit_code: {early_exit_code}\n"
                f"stdout: {out.decode(errors='replace')}\n"
                f"stderr: {err.decode(errors='replace')}"
            )
        yield proc, port
    finally:
        _terminate_group(proc)


def test_control_server_rss_under_cap(spawned_control_server) -> None:
    """Steady-state control_server VmRSS must stay below the P2 cap, AND
    must not grow across additional requests (leak guard).

    Regression guard for M3/M4/M5 route additions silently bloating the
    process toward the 512MB Pi Zero 2W ceiling. See module docstring.

    Two angles in one test:
      1. Absolute cap: RSS <= RSS_CAP_KB after warm-up.
      2. Delta cap: RSS growth across LEAK_PROBE_HITS additional hits
         <= RSS_DELTA_CAP_KB. Catches a linear leak that would still
         clear the absolute cap on the first measurement.
    """
    proc, port = spawned_control_server

    # Warm the routes that carry the bulk of M2's import weight: status
    # touches the quote_corpus index + status template + update_state
    # helpers; index touches the PWA shell jinja chain. Hitting each
    # multiple times ensures lazy imports + thread-pool warm-up are
    # realized before we sample baseline RSS.
    for _ in range(WARMUP_HITS):
        assert _hit(port, "/api/health") == 200
        # /api/status returns 200 even when LITCLOCK_STATUS_FILE is absent —
        # the route degrades gracefully. We don't assert the body, just that
        # the import path is exercised.
        assert _hit(port, "/api/status") is not None
        assert _hit(port, "/") is not None

    baseline_kb = _read_vmrss_kb(proc.pid)
    assert baseline_kb > 0, "Failed to read VmRSS from /proc/<pid>/status"

    # Boundary is inclusive (see RSS_CAP_KB docstring): RSS == 50_000 KB
    # passes; RSS > 50_000 KB fails. Mirrors PLAN wording "fails at >50MB".
    assert baseline_kb <= RSS_CAP_KB, (
        f"control_server RSS regressed past the P2 cap: "
        f"{baseline_kb} KB ({baseline_kb / 1024:.1f} MB) > {RSS_CAP_KB} KB "
        f"({RSS_CAP_KB / 1024:.1f} MB). "
        "PLAN P2 budgets <40MB steady-state with 10MB headroom to 50MB. "
        "Investigate recent route/blueprint additions before raising the cap."
    )

    # Leak probe: drive LEAK_PROBE_HITS more requests through the same
    # routes and check that RSS did not climb. A linear leak ~100KB/hit
    # would gain ~1.5MB across this loop and trip RSS_DELTA_CAP_KB.
    for _ in range(LEAK_PROBE_HITS):
        _hit(port, "/api/health")
        _hit(port, "/api/status")
        _hit(port, "/")

    final_kb = _read_vmrss_kb(proc.pid)
    assert final_kb > 0, "Failed to re-read VmRSS from /proc/<pid>/status"

    delta_kb = final_kb - baseline_kb
    assert delta_kb <= RSS_DELTA_CAP_KB, (
        f"control_server RSS grew across {LEAK_PROBE_HITS} additional hits: "
        f"baseline={baseline_kb} KB, final={final_kb} KB, "
        f"delta={delta_kb} KB > cap {RSS_DELTA_CAP_KB} KB. "
        "Suggests a per-request allocation that's not being released — "
        "module-level list append, leaked file handle, cached request "
        "state, etc. Profile with tracemalloc before raising the cap."
    )

    # Re-check absolute cap after the leak probe — if a slow leak does
    # exist but is too small to trip the delta cap, the absolute cap
    # catches the cumulative drift.
    assert final_kb <= RSS_CAP_KB, (
        f"control_server RSS climbed past the P2 cap during leak probe: "
        f"{final_kb} KB ({final_kb / 1024:.1f} MB) > {RSS_CAP_KB} KB."
    )
