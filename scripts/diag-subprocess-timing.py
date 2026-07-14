#!/usr/bin/env python3
"""Measure wall-clock latency of the diagnostics "fast" subprocess calls (#430).

Why this exists
---------------
`/diagnostics` and `/api/status` shell out to a handful of "fast" commands
(``nmcli``, ``ip``, ``iw``, ``systemctl is-active``, ``timedatectl``,
``git rev-parse``, ``uname``) and cache the result. They all currently share a single
3 s budget (``DIAG_SUBPROC_TIMEOUT_S``) / 2 s on the status path. If a call
exceeds its budget, ``cached_subprocess`` returns ``None`` → the value renders
empty → a false-positive anomaly banner fires on a perfectly healthy clock
(same class as the v0.214.2 ``journalctl`` bug).

#430 sizes a *dedicated* budget per call site. That sizing needs real numbers
from Pi Zero 2W hardware under load — which CI can't produce. This script is
that measurement tool. Run it on authorclock + the test Pi under each of the
load conditions below and paste the table into the #430 thread; the worst-case
p99 (plus headroom) sets each per-call constant.

It measures the SAME argv the code runs (see ``src/control_server/_network.py``,
``routes/diagnostics/_collectors.py``, ``routes/status.py``) and times each call
exactly the way ``cached_subprocess`` does: ``time.monotonic()`` around a single
``subprocess.run(...)``. No project deps — stock ``python3`` only, so it runs on
a bare Pi without the venv.

Load conditions to measure (run the script once per condition)
--------------------------------------------------------------
1. Idle Pi (baseline).
2. Concurrent paint cycle — run while ``litclock.service`` is rendering
   (e.g. ``watch -n1 systemctl start litclock.service`` in another shell, or
   just run across a couple of minute-ticks).
3. Memory pressure — e.g. ``stress-ng --vm 1 --vm-bytes 80% -t 60s`` if
   available, or open several large files.
4. Degraded SD card / slow IO — e.g. ``dd if=/dev/zero of=~/_io bs=1M count=512
   oflag=dsync`` running in another shell (then ``rm ~/_io``).
5. Wedged WiFi (``nmcli`` specifically) — bring the link down / move out of
   range while measuring; nmcli is the known hang risk.

Usage
-----
    python3 scripts/diag-subprocess-timing.py                 # 200 iters, all calls
    python3 scripts/diag-subprocess-timing.py -n 500          # more samples
    python3 scripts/diag-subprocess-timing.py --label "memory-pressure"
    python3 scripts/diag-subprocess-timing.py --json          # machine-readable

The script never trips the real timeout: every call is run with a generous
``--cap`` (default 15 s) so a genuine hang is *measured* (and reported as a
``HANG`` / capped sample) rather than silently truncated at today's 3 s budget.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
import time

# ── Resolve the dynamic bits (active iface, litclock units) the way the
#    collectors do, so the measured argv matches production as closely as a
#    standalone script can. Best-effort: fall back to sensible defaults.


def _active_iface(cap: float) -> str:
    """Parse the egress interface out of ``ip -4 route show default``.

    Mirrors how the diagnostics collector derives the iface it then feeds to
    ``iw dev <iface> link``. Falls back to wlan0 (the Pi Zero 2W's only stock
    interface) when the route can't be read.
    """
    try:
        out = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            check=False,
            capture_output=True,
            text=True,
            timeout=cap,
        ).stdout
        # "default via 192.168.2.1 dev wlan0 proto dhcp ..." → wlan0
        parts = out.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return "wlan0"


# The exact units production's `systemctl is-active` checks (mirror of
# control_server.routes.diagnostics._collectors.DIAG_UNITS). Hardcoded, not
# discovered: production queries this FIXED set every time (even units that
# don't exist on a given box still cost a D-Bus round trip), and `is-active`
# latency scales with the unit count — so measuring a discovered subset would
# under-report the real budget. Keep in sync with DIAG_UNITS if it changes.
DIAG_UNITS = (
    "litclock.service",
    "litclock-control.service",
    "litclock-firstboot.service",
    "litclock-update.timer",
    "litclock-reresolve-location.service",
)


def build_calls(cap: float) -> list[tuple[str, list[str]]]:
    """The seven diagnostics 'fast' calls from #430, with dynamic argv resolved.

    One row per per-call budget constant in `_collectors.py` so the table can
    size every constant (nmcli, ip route, iw, systemctl, timedatectl, git,
    uname). `iface` is resolved like the collector does; the systemctl unit set
    is the fixed production DIAG_UNITS above.
    """
    iface = _active_iface(cap)
    return [
        ("nmcli (ssid)", ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"]),
        ("ip route (default)", ["ip", "-4", "route", "show", "default"]),
        ("iw link (signal)", ["iw", "dev", iface, "link"]),
        ("systemctl is-active", ["systemctl", "is-active", *DIAG_UNITS]),
        ("timedatectl (tz)", ["timedatectl", "show", "-p", "Timezone", "--value"]),
        ("git rev-parse", ["git", "rev-parse", "--short", "HEAD"]),
        ("uname (kernel)", ["uname", "-r"]),
    ]


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100), no interpolation.

    Nearest-rank: the value at ``ceil(q/100 * n)`` (1-based), i.e. the smallest
    sample that at least q% of samples fall at or below. ``math.ceil`` is used
    directly rather than a ``round(x + 0.5)`` idiom — Python's ``round`` is
    banker's (half-to-even), which mis-ranks at integer boundaries (e.g. p99 of
    n=100 would land on rank 100, not 99). Robust for n==1.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = math.ceil((q / 100.0) * len(ordered))
    idx = max(0, min(len(ordered) - 1, rank - 1))
    return ordered[idx]


def measure(name: str, argv: list[str], iterations: int, cap: float) -> dict:
    samples: list[float] = []
    hangs = 0
    errors = 0
    for _ in range(iterations):
        start = time.monotonic()
        try:
            subprocess.run(argv, check=False, capture_output=True, text=True, timeout=cap)
        except subprocess.TimeoutExpired:
            hangs += 1
            samples.append(cap)  # record the cap as a floor for the hang
            continue
        except (OSError, ValueError):
            errors += 1
            continue
        samples.append(time.monotonic() - start)
    ms = [s * 1000.0 for s in samples]
    return {
        "name": name,
        "argv": argv,
        "n": len(ms),
        "hangs": hangs,
        "errors": errors,
        "min_ms": min(ms) if ms else float("nan"),
        "median_ms": statistics.median(ms) if ms else float("nan"),
        "p95_ms": _pct(ms, 95),
        "p99_ms": _pct(ms, 99),
        "max_ms": max(ms) if ms else float("nan"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--iterations", type=int, default=200, help="samples per call (default 200)")
    parser.add_argument(
        "--cap",
        type=float,
        default=15.0,
        help="per-call hard cap in seconds — generous so real hangs get measured, not truncated (default 15)",
    )
    parser.add_argument("--label", default="", help="free-text label for the load condition (echoed in output)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    args = parser.parse_args()

    calls = build_calls(args.cap)
    # Drop calls whose binary isn't installed (e.g. iw on an ethernet-only box)
    # so the run doesn't report spurious errors for an absent tool.
    runnable = [(name, argv) for name, argv in calls if shutil.which(argv[0])]
    missing = [argv[0] for name, argv in calls if not shutil.which(argv[0])]

    results = [measure(name, argv, args.iterations, args.cap) for name, argv in runnable]

    if args.json:
        print(json.dumps({"label": args.label, "missing_binaries": missing, "results": results}, indent=2))
        return 0

    print(f"# diag-subprocess timing — {args.iterations} iters/call, cap={args.cap}s", end="")
    print(f", label={args.label!r}" if args.label else "")
    if missing:
        print(f"# (skipped, binary not found: {', '.join(missing)})")
    header = f"{'call':<22} {'n':>4} {'min':>8} {'med':>8} {'p95':>8} {'p99':>8} {'max':>8} {'hang':>5}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['name']:<22} {r['n']:>4} "
            f"{r['min_ms']:>7.1f}m {r['median_ms']:>7.1f}m {r['p95_ms']:>7.1f}m "
            f"{r['p99_ms']:>7.1f}m {r['max_ms']:>7.1f}m {r['hangs']:>5}"
        )
    print("-" * len(header))
    print("# Budget rule of thumb: per-call timeout >= observed worst-case p99 across")
    print("# ALL load conditions, then add headroom (v0.214.2 journal used ~1.5x).")
    print("# 'hang' counts samples that hit the 15s cap — those are the wedge cases")
    print("# the per-call budget must NOT misclassify as a healthy empty result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
