#!/usr/bin/env python3
"""Environment gate for GD-exact measurement — runs WITHOUT PHP (litclock-dev#531 Stage 2).

src/gd_measure.py replicates libgd's hinted-FreeType metrics. That exactness
is environment-sensitive: freetype-py wheels BUNDLE their own libfreetype,
and hinted metrics change between FreeType versions. The dev box passes
because its wheel happens to bind FreeType 2.13.2 — the same version GD
links. The Pi's wheel (piwheels/aarch64) must be proven equivalent BEFORE
the runtime-render flag is flipped, and the Pi has no PHP to ask.

Two modes:

``generate`` (dev box; needs php-cli + php-gd)
    Samples strings (seeded corpus words + edge strings, each with a
    trailing-space variant — same recipe as the Stage-0 validator), runs
    tools/gd_measure_dump.php over the FULL production size range (18..110;
    the Stage-0 harness stopped at 44, but hinting drift is size-specific
    and the corpus fits up to fs 110), and writes a SELF-CONTAINED gzipped
    JSON dump: the strings themselves plus GD's brect corners per
    (face, size, string). Because the strings are embedded, later corpus
    edits do not invalidate the dump — regenerate only when the fonts or
    the measurement contract change.

``check`` (anywhere: Pi, CI, dev box; needs only freetype-py + fonts)
    Recomputes every measurement with src/gd_measure (gd_text_width AND
    gd_bbox — vertical metrics place the credits block) and requires 100%
    exactness. Any mismatch prints the offending environment versions and
    exits 1: DO NOT enable LITCLOCK_RUNTIME_RENDER where this fails.

The committed dump lives at tools/gd-expected-measurements.json.gz; the
development repo's render-invariants CI runs ``check`` against it so a
font swap or a gd_measure edit that breaks the recorded contract fails
loud there. In this repo, run ``check`` manually after any font change.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

DUMP_PATH = _REPO / "tools" / "gd-expected-measurements.json.gz"
FONTS = {
    "REG": "Literata72pt-ExtraLight.ttf",
    "BOLD": "Literata72pt-Black.ttf",
    "CRED": "Literata72pt-SemiBoldItalic.ttf",
}
EDGE_STRINGS = [" ", "  ", "a", "W.", "“quote”", "…", "fl", "ffi", "—", "’s", "A.M.local"]
SIZE_RANGE = range(18, 111)  # full production range: corpus fits 18..110, credits 18


def _freetype_version() -> str:
    import freetype

    return ".".join(map(str, freetype.version()))


def build_sample(csv_path: Path) -> list[str]:
    """400 seeded corpus words + up to 150 non-ASCII words + edge strings,
    each with a trailing-space variant (trailing spaces hit GD's
    xmax=advance rule). Words are pooled from quote AND title/author
    fields — the CRED face renders credits, and accented glyphs often
    appear ONLY there (Brontë, García; litclock-dev#537 review, finding 3).
    Non-ASCII words are included preferentially — all of them when the
    corpus has ≤150, a seeded sample of 150 otherwise — because per-glyph
    hinting drift is exactly where FreeType versions disagree first
    (docstring previously claimed ALL; litclock-dev#605 item 17). Seed
    pinned so regeneration is reproducible."""
    words: set[str] = set()
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter="|"):
            if len(row) < 5:
                continue
            for field in (row[2], row[3], row[4]):
                q = " ".join(field.replace("\\\\n", " ").replace("\\n", " ").split())
                words.update(w for w in q.split(" ") if w)
    rng = random.Random(531)
    non_ascii = sorted(w for w in words if any(ord(c) > 127 for c in w))
    if len(non_ascii) > 150:
        non_ascii = rng.sample(non_ascii, 150)
    sample = rng.sample(sorted(words), 400) + non_ascii + EDGE_STRINGS
    seen: set[str] = set()
    sample = [w for w in sample if not (w in seen or seen.add(w))]
    return [v for w in sample for v in (w, w + " ")]


def cmd_generate(args: argparse.Namespace) -> int:
    strings = build_sample(Path(args.csv))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("".join(s + "\n" for s in strings))
        wordfile = f.name
    dumper = _REPO / "tools" / "gd_measure_dump.php"
    try:
        proc = subprocess.run(
            ["php", str(dumper), wordfile],
            capture_output=True,
            text=True,
            check=False,
            env={"LITCLOCK_HOME": str(_REPO), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
    finally:
        Path(wordfile).unlink()
    if proc.returncode != 0:
        print(f"ERROR: gd_measure_dump.php failed (exit {proc.returncode}):\n{proc.stderr}", file=sys.stderr)
        return 1

    gd_version = "?"
    # boxes[tag][fs] = flat list of [xmin, xmax, ymin, ymax] per string index
    boxes: dict[str, dict[str, list[list[int]]]] = {t: {} for t in FONTS}
    for ln in proc.stdout.splitlines():
        if ln.startswith("#gd|"):
            gd_version = ln.split("|")[1]
            continue
        tag, fs, i, xmin, xmax, ymin, ymax = ln.split("\t")
        per_size = boxes[tag].setdefault(fs, [])
        assert int(i) == len(per_size), f"dump out of order at {tag}/{fs}/{i}"
        per_size.append([int(xmin), int(xmax), int(ymin), int(ymax)])

    expected_sizes = {str(fs) for fs in SIZE_RANGE}
    for tag in FONTS:
        assert set(boxes[tag]) == expected_sizes, f"missing sizes for {tag}"
        assert all(len(v) == len(strings) for v in boxes[tag].values())

    payload = {
        "meta": {
            "gd": gd_version,
            "freetype_py_binds": _freetype_version(),
            "fonts": FONTS,
            "sizes": [SIZE_RANGE.start, SIZE_RANGE.stop - 1],
            "measurements": len(strings) * len(FONTS) * len(expected_sizes),
            "recipe": "rng(531) 400 corpus words + edge strings, x trailing-space variant",
        },
        "strings": strings,
        "boxes": boxes,
    }
    with gzip.open(args.out, "wt", encoding="utf-8") as gz:
        json.dump(payload, gz, ensure_ascii=False, separators=(",", ":"))
    print(
        f"wrote {args.out}: {payload['meta']['measurements']} measurements "
        f"(GD {gd_version}, oracle FreeType {_freetype_version()})"
    )
    return 0


def _validate_dump_coverage(payload: dict) -> None:
    """Refuse a truncated/weakened dump BEFORE scoring against it — the
    check iterates whatever the dump contains, so without this a dump
    missing faces/sizes/strings would silently shrink coverage while
    still printing 100% (litclock-dev#537 review). Expectations come from this
    tool's OWN constants, never from the dump's self-description."""
    strings = payload["strings"]
    if len(set(strings)) < 800:
        raise SystemExit(f"ERROR: dump has only {len(set(strings))} distinct strings — expected the full sample")
    missing_edges = [e for e in EDGE_STRINGS if e not in strings]
    if missing_edges:
        raise SystemExit(f"ERROR: dump is missing edge strings {missing_edges!r} — regenerated from a weakened recipe?")
    if set(payload["boxes"]) != set(FONTS):
        raise SystemExit(f"ERROR: dump faces {sorted(payload['boxes'])} != expected {sorted(FONTS)}")
    expected_sizes = {str(fs) for fs in SIZE_RANGE}
    for tag in FONTS:
        if set(payload["boxes"][tag]) != expected_sizes:
            raise SystemExit(
                f"ERROR: dump sizes for {tag} do not cover the full {SIZE_RANGE.start}..{SIZE_RANGE.stop - 1} range"
            )
        for fs, per_size in payload["boxes"][tag].items():
            if len(per_size) != len(strings):
                raise SystemExit(f"ERROR: dump {tag}/fs{fs} has {len(per_size)} rows for {len(strings)} strings")


def _dump_is_the_committed_one(dump: Path) -> bool:
    """True when ``dump`` carries the same bytes as the committed ground
    truth the runtime reader validates against. Content-based, so a copy at
    another path still qualifies."""
    import hashlib

    from quote_renderer import EXPECTED_DUMP_PATH

    try:
        return hashlib.sha256(dump.read_bytes()).digest() == hashlib.sha256(EXPECTED_DUMP_PATH.read_bytes()).digest()
    except OSError:
        return False


def cmd_check(args: argparse.Namespace) -> int:
    if args.stamp and not _dump_is_the_committed_one(Path(args.dump)):
        # Refuse up front, before the (long) measurement pass: the runtime
        # reader recomputes the digest against the COMMITTED dump, so a
        # marker stamped against different bytes would print "may be
        # enabled" here and then be rejected on every start — a lie the
        # operator only discovers in journald (litclock-dev#611 review).
        print(
            "ERROR: refusing --stamp against a --dump that differs from the committed "
            "tools/gd-expected-measurements.json.gz — the runtime reader validates against "
            "the committed dump, so this marker could never be accepted. Run the check "
            "without --stamp to evaluate a candidate dump.",
            file=sys.stderr,
        )
        return 2

    from gd_measure import gd_bbox, gd_text_width

    with gzip.open(args.dump, "rt", encoding="utf-8") as gz:
        payload = json.load(gz)
    _validate_dump_coverage(payload)
    strings = payload["strings"]
    meta = payload["meta"]
    print(
        f"dump: {meta['measurements']} measurements, generated against GD {meta['gd']} "
        f"/ FreeType {meta['freetype_py_binds']}; this environment binds FreeType {_freetype_version()}"
    )

    t0 = time.time()
    n = exact = 0
    err: Counter = Counter()
    worst: list[tuple] = []
    for tag, font_name in payload["meta"]["fonts"].items():
        # Resolve through the renderer's own font lookup (honors
        # LITCLOCK_FONTS_DIR) so the check measures THE SAME bytes the
        # digest stamps and the runtime renders. A hardcoded repo path here
        # let `check` prove the repo fonts while an override made the
        # runtime render different ones (litclock-dev#611 review).
        from quote_renderer import _font_path

        font_path = _font_path(font_name)
        for fs_str, per_size in payload["boxes"][tag].items():
            fs = int(fs_str)
            for i, (xmin, xmax, ymin, ymax) in enumerate(per_size):
                s = strings[i]
                exp_w = xmax - xmin
                exp_bbox = (exp_w, ymax - ymin, abs(xmin) + exp_w, abs(ymin) + (ymax - ymin))
                got_w = gd_text_width(font_path, fs, s)
                got_bbox = gd_bbox(font_path, fs, s)
                n += 2
                if got_w == exp_w:
                    exact += 1
                else:
                    err[got_w - exp_w] += 1
                    if len(worst) < 10:
                        worst.append(("width", tag, fs, repr(s), exp_w, got_w))
                if got_bbox == exp_bbox:
                    exact += 1
                else:
                    err["bbox"] += 1
                    if len(worst) < 10:
                        worst.append(("bbox", tag, fs, repr(s), exp_bbox, got_bbox))

    print(f"{exact}/{n} exact ({exact * 100 / n:.2f}%) in {time.time() - t0:.0f}s")
    for w in worst:
        print("  mismatch:", w)
    if exact != n:
        print(
            f"FAIL: errdist={dict(err.most_common(5))}\n"
            "gd_measure is NOT byte-exact in this environment — do NOT enable\n"
            "LITCLOCK_RUNTIME_RENDER here. (freetype-py wheel binds a FreeType\n"
            "whose hinted metrics differ from the dump's GD.)"
        )
        if args.stamp:
            # A stale marker from a previously-passing environment must not
            # survive a now-failing check (e.g. after a freetype-py bump).
            try:
                Path(args.marker).unlink()
                print(f"removed stale validation marker {args.marker}")
            except FileNotFoundError:
                pass
        return 1
    print("PASS: this environment reproduces GD metrics exactly.")
    if args.stamp:
        write_validation_marker(Path(args.marker), Path(args.dump), meta["gd"], n)
        print(f"validation marker written: {args.marker} — LITCLOCK_RUNTIME_RENDER may be enabled")
    return 0


def write_validation_marker(marker: Path, dump: Path, gd_version: str, measurements: int) -> None:
    """Write the marker _runtime_render_enabled() accepts.

    Extracted from cmd_check so tests can exercise the ACTUAL writer against
    the ACTUAL reader (litclock-dev#605 item 1 — every prior test hand-wrote
    the format, so a drift on either side shipped green).

    The digest (litclock-dev#604) binds the stamp to its proof inputs —
    freetype version, font bytes, dump bytes — so a font swap or dump regen
    with the SAME freetype wheel invalidates the marker instead of leaving a
    stale proof in force.
    """
    import os
    import tempfile

    from quote_renderer import runtime_validation_digest

    content = (
        f"freetype={_freetype_version()} "
        f"digest={runtime_validation_digest(dump)} "
        f"dump_gd={gd_version} measurements={measurements}\n"
    )
    # tmp + rename, not open(O_TRUNC): a previous sudo-run stamp leaves a
    # root-owned marker that a pi-run re-stamp cannot truncate — but CAN
    # replace, because rename only needs write on the pi-owned parent dir
    # (litclock-dev#611 review).
    fd, tmp = tempfile.mkstemp(dir=str(marker.parent), prefix=marker.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.replace(tmp, marker)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_gen = sub.add_parser("generate", help="build the expected dump (dev box, needs PHP+GD)")
    p_gen.add_argument("--csv", default=str(_REPO / "image-gen" / "litclock_annotated.csv"))
    p_gen.add_argument("--out", default=str(DUMP_PATH))
    p_chk = sub.add_parser("check", help="verify this environment against the dump (no PHP needed)")
    p_chk.add_argument("--dump", default=str(DUMP_PATH))
    p_chk.add_argument(
        "--stamp",
        action="store_true",
        help="on PASS, write the validation marker literary_clock requires "
        "before honoring LITCLOCK_RUNTIME_RENDER (on FAIL, remove a stale one)",
    )
    p_chk.add_argument("--marker", default=str(_REPO / ".runtime-render-validated"))
    args = ap.parse_args()
    return cmd_generate(args) if args.cmd == "generate" else cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
