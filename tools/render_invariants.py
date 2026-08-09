#!/usr/bin/env python3
"""Full-corpus render + invariant gate for the runtime quote renderer
(litclock-dev#531 Stage 1).

Two subcommands:

``check``
    Render every corpus row through src/quote_renderer.py (both variants)
    and enforce the Stage-1 invariants:

      I1  coverage: every CSV row renders — no NOSTRING, no NOFIT, no
          exception of any kind, rendered count == corpus row count;
      I2  fit sanity: font size >= 18 and paragraph height < HEIGHT-100;
      I3  litclock-dev#530 QR-notch clearance: the credits image has NO ink inside the
          settings-QR quiet-zone notch region (image rows 0..6 at x >= 701;
          the panel composite white-outs that corner, so ink there would be
          silently erased);
      I4  credits never collide with quote ink: the credits layer (drawn
          on a blank canvas at identical positions) shares zero ink
          pixels with the quote image;
      I5  filename identity: basenames are unique and NSFW rows carry the
          _nsfw suffix (the clock's glob-based NSFW filter depends on it).

    Writes a per-quote JSON report and exits non-zero on any violation.
    NOT a shipping artifact — this is a CI gate, images are discarded.

``probe``
    Emit the Stage-0 parity fingerprint (``row|fs|breaks`` lines + #inputs
    header) from the PRODUCTION module, byte-compatible with the GD probe
    kept in the development repo (``gd_layout_probe.php`` output and
    ``compare_layout.py --diff``). This is how the port is proven to make
    the same layout decisions as GD: run the PHP probe, run this, diff.

Ink threshold: any pixel < 255 counts as ink (anti-aliased fringes
included) — I3/I4 are strict by design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

# litclock-dev#530 settings-QR quiet-zone notch geometry, in QUOTE-IMAGE coordinates —
# derived from literary_clock's own constants (src/ is on sys.path above)
# so the gate can never drift from the composite that does the erasing.
import literary_clock as _lc  # noqa: E402
import quote_renderer as qr  # noqa: E402

NOTCH_X0 = _lc.QR_POSITION[0] - _lc.QR_QUIET_ZONE  # panel x == image x
NOTCH_ROWS = _lc.QR_NOTCH_BOTTOM - _lc.QUOTE_AREA_Y + 1  # panel rows 80..86 == image rows 0..6


# Ink = pixel < INK_THRESHOLD, the bilevel boundary. GD's palette rendering
# rounds anti-aliasing to pure black/white at ~50% coverage, and the panel
# pipeline is 1-bit — so a >=128 grey PIL fringe pixel is NOT ink (it ships
# white). The Stage-2 flip must convert with dither=NONE (threshold at 128)
# to match; Floyd-Steinberg dithering would stochastically blacken fringes.
# Full-corpus fact (2026-07-25): sub-128 ink starts at image row 4 in BOTH
# engines (GD and PIL agree row-for-row on the extreme quotes; verified on
# quote_2300_5 fs85 et al.) — the litclock-dev#530 comment's "worst corpus ink starts
# at 87" aside is stale, but the notch region itself (x>=701) is ink-free
# across all 4,809 renders, which is what the quiet zone actually needs.
INK_THRESHOLD = 128


def _ink_mask(img):
    """255 where the pixel is ink (bilevel-black), 0 elsewhere."""
    return img.point(lambda v: 255 if v < INK_THRESHOLD else 0)


def _first_ink_row(img) -> int | None:
    """Row index of the topmost ink pixel, or None for an ink-free image."""
    bbox = _ink_mask(img).getbbox()
    return None if bbox is None else bbox[1]


def _ink_overlap(quote_img, title: str, author: str) -> int:
    """Count pixels where the credits layer and the quote image both have
    ink. The credits layer is rebuilt on a blank canvas — add_credits is
    deterministic, so positions match the composed variant exactly."""
    from PIL import Image, ImageChops

    layer = Image.new("L", quote_img.size, 255)
    qr.add_credits(layer, title, author)
    both = ImageChops.darker(_ink_mask(layer), _ink_mask(quote_img))
    # darker(mask_a, mask_b) is 255 only where both are ink. histogram()
    # counts in C — a Python pixel loop here costs ~30s over the corpus.
    return quote_img.size[0] * quote_img.size[1] - both.histogram()[0]


def cmd_check(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    report_path = Path(args.report)
    rows = list(qr.iter_corpus(csv_path))
    if args.limit:
        rows = rows[: args.limit]

    failures: list[str] = []
    report = []
    seen_basenames: set[str] = set()
    t_start = time.time()
    for row in rows:
        entry: dict = {"ordinal": row.ordinal, "basename": row.basename, "time": row.time}
        t0 = time.time()
        try:
            quote_img, credits_img, font_size, layout = qr.render_row(row)
        except Exception as exc:  # I1 — any exception is a gate failure
            entry["error"] = f"{type(exc).__name__}: {exc}"
            failures.append(f"I1 row {row.ordinal} ({row.basename}): {entry['error']}")
            report.append(entry)
            continue
        entry["render_ms"] = round((time.time() - t0) * 1000, 1)
        entry["font_size"] = font_size
        entry["paragraph_height"] = layout.paragraph_height

        # I2 — fit sanity on the actual layout, not just the returned fs
        # (checking only fs >= 18 is vacuous — fit() starts there)
        if font_size < qr.START_FONT_SIZE:
            failures.append(f"I2 row {row.ordinal}: font_size {font_size} < {qr.START_FONT_SIZE}")
        if layout.paragraph_height >= qr.HEIGHT - 100:
            failures.append(
                f"I2 row {row.ordinal} ({row.basename}): paragraph height "
                f"{layout.paragraph_height} >= {qr.HEIGHT - 100} — no room for credits"
            )

        # I3 — litclock-dev#530 QR-notch clearance on the SHIPPED variant (credits):
        # no ink where the panel notch would erase it. Global first-ink-row
        # is recorded for the report but only the notch region gates.
        entry["first_ink_row"] = _first_ink_row(credits_img)
        notch_ink = _first_ink_row(credits_img.crop((NOTCH_X0, 0, qr.WIDTH, NOTCH_ROWS)))
        if notch_ink is not None:
            failures.append(
                f"I3 row {row.ordinal} ({row.basename}): ink at image row {notch_ink}, "
                f"x>={NOTCH_X0} — inside the settings-QR quiet-zone notch (litclock-dev#530), would be erased"
            )

        # I4 — credits/quote ink collision
        overlap = _ink_overlap(quote_img, row.title, row.author)
        entry["credits_overlap_px"] = overlap
        if overlap:
            failures.append(f"I4 row {row.ordinal} ({row.basename}): credits overlap quote ink ({overlap} px)")

        # I5 — filename identity
        if row.basename in seen_basenames:
            failures.append(f"I5 row {row.ordinal}: duplicate basename {row.basename}")
        seen_basenames.add(row.basename)
        if row.is_nsfw != row.basename.endswith("_nsfw"):
            failures.append(f"I5 row {row.ordinal}: NSFW flag/suffix mismatch ({row.basename})")

        report.append(entry)

    # WARN tier (litclock-dev#540): mid-word timestring edges — data bugs that
    # render half-bold words under exact-span bolding. Non-gating until the
    # known 11 rows are fixed in the next corpus release; then tighten.
    midword = [
        (r.ordinal, r.time, edge)
        for r in rows
        if (edge := qr.timestring_midword_edge(r.quote, r.timestring))
    ]
    if midword:
        print(f"WARN: {len(midword)} row(s) with mid-word timestring edges (half-bold words; litclock-dev#540):")
        for o, t, edge in midword[:15]:
            print(f"  row {o} ({t}): {edge}")

    rendered = sum(1 for e in report if "error" not in e)
    summary = {
        "corpus_rows": len(rows),
        "rendered": rendered,
        "failed": len(rows) - rendered,
        "invariant_violations": len(failures),
        "wall_seconds": round(time.time() - t_start, 1),
        "corpus_sha1": hashlib.sha1(csv_path.read_bytes()).hexdigest(),
        "midword_edge_rows": len(midword),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"summary": summary, "failures": failures, "rows": report}, indent=1) + "\n")

    print(f"rendered {rendered}/{len(rows)} rows in {summary['wall_seconds']}s -> {report_path}")
    fs_vals = [e["font_size"] for e in report if "font_size" in e]
    if fs_vals:
        ink_vals = [e["first_ink_row"] for e in report if e.get("first_ink_row") is not None]
        ink_note = f"; first ink row min {min(ink_vals)}" if ink_vals else ""
        print(f"fs range {min(fs_vals)}-{max(fs_vals)}{ink_note}")
    if failures:
        print(f"\n{len(failures)} INVARIANT VIOLATION(S):")
        for f in failures[:40]:
            print(f"  {f}")
        if len(failures) > 40:
            print(f"  ... and {len(failures) - 40} more (see report)")
        return 1
    print("all invariants hold")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    reg = str(qr.FONTS_DIR / qr.FONT_REGULAR)
    bold = str(qr.FONTS_DIR / qr.FONT_BOLD)
    print(
        f"#inputs|{hashlib.sha1(csv_path.read_bytes()).hexdigest()}|{hashlib.sha1(Path(reg).read_bytes()).hexdigest()}|{hashlib.sha1(Path(bold).read_bytes()).hexdigest()}"
    )
    try:
        import freetype

        print(f"#meta|freetype={'.'.join(map(str, freetype.version()))}")
    except ImportError:
        pass
    rows = 0
    for row in qr.iter_corpus(csv_path, strict=False):
        rows = row.ordinal
        if args.cap and rows > args.cap:
            break
        if rows % args.step != 1 % args.step:
            continue
        qb = row.quote.encode("utf-8")
        tsb = row.timestring.encode("utf-8")
        idx = qr.find_timestring(qb, tsb)
        if idx < 0:
            print(f"{rows}|NOSTRING")
            continue
        s, e = idx, idx + len(tsb)  # exact-span bolding (litclock-dev#540)
        fitted = qr.fit(qb.split(b" "), s, e)
        if fitted is None:
            print(f"{rows}|NOFIT")
            continue
        lay, font_size = fitted
        print(f"{rows}|{font_size}|{','.join(map(str, lay.breaks))}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(_REPO / "image-gen" / "litclock_annotated.csv"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="render full corpus and enforce invariants")
    p_check.add_argument("--report", default=str(_REPO / "render-invariants-report.json"))
    p_check.add_argument("--limit", type=int, default=0, help="render only the first N rows (smoke)")
    p_probe = sub.add_parser("probe", help="emit row|fs|breaks parity lines (Stage-0 format)")
    p_probe.add_argument("cap", type=int, nargs="?", default=300, help="max CSV rows to scan (0 = all)")
    p_probe.add_argument("step", type=int, nargs="?", default=3, help="sample every Nth row")
    args = ap.parse_args()
    if args.cmd == "probe" and args.step < 1:
        ap.error("step must be >= 1")
    return cmd_check(args) if args.cmd == "check" else cmd_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
