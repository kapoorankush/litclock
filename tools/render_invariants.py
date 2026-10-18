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

    ``--language CODE`` (litclock-dev#532 T4) checks a language's corpus
    instead of ``--csv``. A registered code resolves through the device's own
    containment-checked registry path (a path the clock would refuse cannot
    pass here); a code the registry does not list yet is read from
    ``languages/<code>/quotes.csv`` (T8), so a volunteer can check a corpus
    before anyone registers it. Every language gets these, English included
    (L7 and L8 only once registered: they read the strings bundle the
    maintainer writes at registration):

      L1  the sixth column is exactly ``YES`` (mature), ``NO`` or empty
          (safe). A localized flag (``SÍ``, ``oui``) reads as SAFE on the
          clock, so mature quotes would show on a filtered device; case and
          space variants (``yes``, ``YES ``) happen to read as mature and are
          rejected for one unambiguous spelling (eng review R6);
      L2  the time field is a real ``HH:MM`` minute in ASCII digits —
          anything else keys a bucket no minute ever asks for;
      L3  every character is in the face that draws it: the quote in the
          regular face, the bold run in the bold face, the composed credits
          line in the credits face (R11) — whitespace included, since only
          ASCII whitespace is collapsed: a missing glyph is an empty box;
      L4  the time phrase is not empty — the quote would render with
          nothing bold;
      L5  every non-blank line has 5 or 6 fields: fewer is a row the clock
          silently skips, more ignores a shifted mature flag;
      L6  every line parses as strict CSV on its own: a field opening with
          an unescaped quotation mark loses its marks on the clock, and an
          unterminated one swallows the lines after it;
      L8  every character of the language's PANEL strings (the e-ink
          namespaces) is in the panel's Literata faces — symbols and spaces
          included, since the panel has no fallback (D16/R11);
      L7  every LETTER (and combining mark) in the language's strings
          bundle is in each of the control app's web fonts — they are
          Latin-1 subsets, so a Polish, Greek or Cyrillic UI would be drawn
          in a fallback font mid-word (design D16, DT8). Symbols and
          punctuation are not checked: a browser falls back per glyph, and
          English itself relies on that for "→".

    The corpus gets the same letter check as a REPORTED count
    (``pwa_fallback_rows``), not a gate: the app shows the current quote, and
    an occasional proper name (Čapek) falling back for one letter is the
    graceful degradation D16 accepts — English has three such rows.

    and reports coverage for the owner's floor decision (D19): SFW coverage
    of the day, of daytime 07:00-23:00, and the longest run of consecutive
    uncovered minutes (wrapping past midnight). Coverage is reported, never
    gated here — the registry's floor is its own test.

``probe``
    Emit the Stage-0 parity fingerprint (``row|fs|breaks`` lines + #inputs
    header) from the PRODUCTION module, byte-compatible with
    spikes/531-runtime-render/gd_layout_probe.php output and
    compare_layout.py --diff. This is how the port is proven to make the
    same layout decisions as GD: run the PHP probe, run this, diff.

Ink threshold: any pixel < 255 counts as ink (anti-aliased fringes
included) — I3/I4 are strict by design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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


# L1: the only flag values the clock reads correctly (R6, corrected for
# English's NO — see the module docstring).
NSFW_FLAGS = frozenset({"YES", "NO", ""})
_CLOCK_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")  # ASCII digits: \d matches ٦ and ６
DAYTIME = (7 * 60, 23 * 60)  # [07:00, 23:00)


def _language_corpus(code: str) -> Path | None:
    """The registry corpus for ``code`` exactly as the device resolves it."""
    import quote_corpus  # noqa: PLC0415

    # None for an unlisted code too: _registry_corpus resolves through
    # strings_catalog.corpus_relpath, which knows only registered codes.
    return quote_corpus._registry_corpus(code)


# A volunteer's corpus before the maintainer registers the language (T8):
# one conventional place, so `--language CODE` works on the day the CSV
# lands, with no registry entry, strings bundle or fonts decision needed yet.
# The convention and the code shape live with the CI discovery that uses them.
from language_corpora import CODE as _CODE  # noqa: E402
from language_corpora import UNREGISTERED_CORPUS  # noqa: E402


def _unregistered_corpus(code: str) -> tuple[Path | None, str]:
    """``(languages/<code>/quotes.csv, "")`` for a code the registry does NOT
    list, else ``(None, why)``. A registered code never falls through to
    here: its corpus is whatever the registry names, exactly as the clock
    resolves it. Contained, non-empty, and not a symlink — a link to the
    English corpus would pass on English rows (litclock-dev#926 review)."""
    import quote_corpus  # noqa: PLC0415
    import strings_catalog  # noqa: PLC0415

    rel = f"languages/{code}/{UNREGISTERED_CORPUS}"
    if code in strings_catalog._registry().get("languages", {}):
        return None, "registered, and its registry corpus is missing, empty or outside the checkout"
    if not _CODE.fullmatch(code):
        return None, f"{code!r} is not a language code (lower-case, like es or pt-BR)"
    try:
        # One guard, as in quote_corpus._registry_corpus: resolve() and the
        # stat calls raise on EACCES, a symlink loop or a NUL byte.
        root = quote_corpus._PROJECT_ROOT.resolve()
        candidate = root / "languages" / code / UNREGISTERED_CORPUS
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            return None, f"{rel} must be a file in the checkout, not a link"
        if not candidate.is_file():
            return None, f"no {rel}"
        if candidate.stat().st_size == 0:
            return None, f"{rel} is empty"
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"{rel} is unreadable ({exc})"
    return candidate, ""


def _faces() -> dict[str, object]:
    import freetype  # noqa: PLC0415

    return {name: freetype.Face(str(qr.FONTS_DIR / name)) for name in {qr.FONT_REGULAR, qr.FONT_BOLD, qr.FONT_CREDITS}}


# The PWA font helpers live in a module of their own so the translator
# validator can use them without this file's rendering stack (litclock-dev#925 review).
from pwa_glyphs import (  # noqa: E402,F401
    EINK_NAMESPACES,
    PWA_FONTS_DIR,
    eink_cmaps,
    eink_missing_chars,
    pwa_cmaps,
    pwa_missing_letters,
)


def _pwa_failure(prefix: str, gaps: dict[str, list[str]]) -> str:
    chars = sorted({ch for chars in gaps.values() for ch in chars})
    return (
        f"{prefix}: the control app's fonts ({', '.join(sorted(gaps))}) have no "
        f"{''.join(chars)!r} ({', '.join(f'U+{ord(c):04X}' for c in chars)}) — see translations/README.md"
    )


def catalog_checks(code: str) -> list[str]:
    """L7: the language's strings bundle against the web fonts; L8: its
    panel strings against the panel's faces, every character."""
    import strings_catalog  # noqa: PLC0415

    cmaps, faces = pwa_cmaps(), eink_cmaps()
    failures = []
    for key, value in sorted(strings_catalog._catalog(code).items()):
        if key.startswith("_") or not isinstance(value, str):
            continue
        gaps = pwa_missing_letters(value, cmaps)
        if gaps:
            failures.append(_pwa_failure(f"L7 {key}", gaps))
        if key.split(".", 1)[0] in EINK_NAMESPACES:
            missing = eink_missing_chars(value, faces)
            if missing:
                chars = sorted({ch for found in missing.values() for ch in found})
                failures.append(
                    f"L8 {key}: the panel's fonts ({', '.join(sorted(missing))}) have no {''.join(chars)!r} "
                    f"({', '.join(f'U+{ord(c):04X}' for c in chars)}) — it would draw as an empty box"
                )
    return failures


def _missing_glyphs(face, text: str) -> list[str]:
    """Every character the face cannot draw — whitespace included: the
    renderer only collapses ASCII whitespace, so a U+202F (the French space
    before ; : ! ?), U+2028 or U+3000 is DRAWN, as an empty box or a wrong
    gap (litclock-dev#914 review, P1)."""
    return sorted({ch for ch in text if face.get_char_index(ord(ch)) == 0})


def language_checks(csv_path: Path) -> tuple[list[str], dict]:
    """L1-L6 over the RAW lines, plus the D19 coverage numbers from the
    clock's own walk (``iter_corpus``), so "covered" means what the clock
    would actually show."""
    import csv as _csv  # noqa: PLC0415

    failures: list[str] = []
    faces = _faces()
    cmaps = pwa_cmaps()
    fallback_rows = 0
    ordinal = 0
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for line_no, raw in enumerate(fh, start=1):
            # L6: the clock's csv.reader is lenient, so a field OPENING with a
            # quotation mark is read as CSV quoting and the marks vanish
            # (`"Come at seven," she said.` paints as `Come at seven, she
            # said.`), and an unterminated one swallows the lines after it.
            # Strict parsing of each line on its own refuses both; a correctly
            # escaped field, or a mark mid-field, parses as written (litclock-dev#926
            # review). One record per line is what the corpus format is.
            try:
                next(_csv.reader([raw], delimiter="|", strict=True), None)
            except _csv.Error as exc:
                failures.append(
                    f"L6 line {line_no}: quotation marks the clock would misread ({exc}) — wrap a field that "
                    'contains " in double quotes and double each one inside it (see translations/README.md)'
                )
        fh.seek(0)
        if any(f.startswith("L6 ") for f in failures):
            # An unterminated quote makes the lenient pass below swallow the
            # rest of the file (past 128 KB it raises csv.Error): the L6
            # line numbers above are the actionable report (litclock-dev#926 re-review).
            return failures, {}
        for line_no, row in enumerate(_csv.reader(fh, delimiter="|"), start=1):
            if not row:
                continue  # an empty line yields no fields and is no row, on the clock either
            # (A line of empty fields — `||||` — IS a row to the clock's walk,
            # so it falls through to L2/L5 rather than being skipped here.)
            if len(row) not in (5, 6):
                # Fewer than five fields is a row the clock silently skips;
                # more than six ignores what a shifted mature flag says.
                failures.append(
                    f"L5 line {line_no}: {len(row)} fields, want 5 or 6 (time|phrase|quote|title|author[|flag])"
                )
                if len(row) < 5:
                    continue
            ordinal += 1
            flag = row[5] if len(row) > 5 else ""
            if flag not in NSFW_FLAGS:
                failures.append(f"L1 row {ordinal}: mature flag {flag!r} is not YES, NO or empty")
            if not _CLOCK_TIME.fullmatch(row[0]):
                hint = " (a byte-order mark: save the file as UTF-8 without one)" if row[0].startswith("\ufeff") else ""
                failures.append(f"L2 row {ordinal}: time {row[0]!r} is not an HH:MM minute{hint}")
            quote = qr.preprocess_quote(row[2])
            timestring = row[1].strip(qr.PHP_TRIM_CHARS)
            if not timestring:
                failures.append(f"L4 row {ordinal}: empty time phrase — the quote would render with nothing bold")
            # What the renderer actually DRAWS in each face: the quote, the
            # bold run as it sits in the quote, and the composed credits line
            # (the renderer adds the dash and the comma itself).
            qb, tsb = quote.encode("utf-8"), timestring.encode("utf-8")
            span = qr.find_timestring_span(qb, tsb)
            bold = qb[span[0] : span[1]].decode("utf-8", "replace") if span else timestring
            credits = qr.DASH + f"{row[3].strip(qr.PHP_TRIM_CHARS)}, {row[4].strip(qr.PHP_TRIM_CHARS)}"
            for face_name, text in ((qr.FONT_REGULAR, quote), (qr.FONT_BOLD, bold), (qr.FONT_CREDITS, credits)):
                missing = _missing_glyphs(faces[face_name], text)
                if missing:
                    failures.append(
                        f"L3 row {ordinal}: {face_name} has no glyph for {''.join(missing)!r} "
                        f"({', '.join(f'U+{ord(c):04X}' for c in missing)})"
                    )
            if pwa_missing_letters("".join(row[1:5]), cmaps):
                fallback_rows += 1
    if ordinal == 0:
        # Only blank lines: nothing to render, so I1's "every row renders"
        # holds vacuously — and the clock would raise CorpusUnavailable
        # (Codex, litclock-dev#914 round 2).
        failures.append("L5 the corpus has no rows")
    covered = {
        int(r.hhmm[:2]) * 60 + int(r.hhmm[2:])
        for r in qr.iter_corpus(csv_path, strict=False)
        if not r.is_nsfw and _CLOCK_TIME.fullmatch(r.time)
    }
    day = range(24 * 60)
    longest = run = 0
    for minute in list(day) + list(day):  # twice, so a run across midnight counts whole
        run = 0 if minute in covered else run + 1
        longest = max(longest, run)
    coverage = {
        "sfw_minutes": len(covered),
        "sfw_coverage_pct": round(100 * len(covered) / len(day), 1),
        "daytime_sfw_coverage_pct": round(
            100 * sum(1 for m in range(*DAYTIME) if m in covered) / (DAYTIME[1] - DAYTIME[0]), 1
        ),
        "longest_uncovered_run_minutes": min(longest, len(day)),
        # Rows with a letter the control app's fonts lack (reported, not gated).
        "pwa_fallback_rows": fallback_rows,
    }
    return failures, coverage


def cmd_check(args: argparse.Namespace) -> int:
    language_failures: list[str] = []
    coverage: dict = {}
    if args.language:
        resolved = _language_corpus(args.language)
        registered = resolved is not None
        if resolved is None:
            resolved, why = _unregistered_corpus(args.language)
            if resolved is None:
                print(f"--language {args.language}: no corpus to check — {why}")
                return 2
        if not registered:
            print(
                f"NOTE: {args.language} is not in languages.json yet — checking "
                f"languages/{args.language}/{UNREGISTERED_CORPUS}; the maintainer registers it"
            )
        args.csv = str(resolved)
        try:
            language_failures, coverage = language_checks(resolved)
            if registered:
                # An unregistered language has no strings bundle to check yet
                # — the maintainer writes it (design D12); CI's validator
                # gate checks any bundle that is present, registered or not.
                language_failures += catalog_checks(args.language)
        except UnicodeDecodeError as exc:
            # A spreadsheet export in Latin-1 is the likely case: a rejection
            # the contributor can act on, not a traceback (litclock-dev#914 review).
            print(f"--language {args.language}: the corpus is not UTF-8 ({exc}); save it as UTF-8 and re-run")
            return 1
        l6 = [f for f in language_failures if f.startswith("L6 ")]
        if l6:
            # Rendering walks the corpus with the clock's lenient reader, which
            # an unterminated quote sends past csv's field limit: report the
            # lines that need fixing instead of a traceback (litclock-dev#926 re-review).
            print(f"{len(l6)} INVARIANT VIOLATION(S) — fix these before the corpus can be rendered:")
            for f in l6:
                print(f"  {f}")
            return 1
    csv_path = Path(args.csv)
    report_path = Path(args.report)
    try:
        rows = list(qr.iter_corpus(csv_path))
    except qr.CorpusGuardError as exc:
        print(f"{csv_path}: {exc}")
        if language_failures:
            print(f"(and {len(language_failures)} corpus-check failure(s), first: {language_failures[0]})")
        return 1
    if args.limit:
        rows = rows[: args.limit]

    failures: list[str] = list(language_failures)
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
    midword = [(r.ordinal, r.time, edge) for r in rows if (edge := qr.timestring_midword_edge(r.quote, r.timestring))]
    if midword:
        print(f"WARN: {len(midword)} row(s) with mid-word timestring edges (half-bold words; litclock-dev#540):")
        for o, t, edge in midword[:15]:
            print(f"  row {o} ({t}): {edge}")

    # WARN tier (litclock-dev#539): fitted-fs floors (canonical values in
    # quote_renderer). Below the hard floor the panel
    # strains legibility at shelf distance. Non-gating for the existing
    # corpus (the 126-row backlog is a review queue, not an auto-fail);
    # per-EDIT enforcement lives in corpus_edit.py. Tighten to a gate once
    # the backlog is worked off.
    subfloor_hard = [e for e in report if "font_size" in e and e["font_size"] < qr.FS_HARD_FLOOR]
    subfloor_soft = [e for e in report if "font_size" in e and qr.FS_HARD_FLOOR <= e["font_size"] < qr.FS_SOFT_FLOOR]
    if subfloor_hard:
        print(f"WARN: {len(subfloor_hard)} row(s) fit below the fs hard floor {qr.FS_HARD_FLOOR} (litclock-dev#539):")
        for e in subfloor_hard[:15]:
            print(f"  row {e['ordinal']} ({e['time']}): fs {e['font_size']}")

    rendered = sum(1 for e in report if "error" not in e)
    summary = {
        "corpus_rows": len(rows),
        "rendered": rendered,
        "failed": len(rows) - rendered,
        "invariant_violations": len(failures),
        "wall_seconds": round(time.time() - t_start, 1),
        "corpus_sha1": hashlib.sha1(csv_path.read_bytes()).hexdigest(),
        "midword_edge_rows": len(midword),
        "fs_below_hard_floor": len(subfloor_hard),
        "fs_soft_band_rows": len(subfloor_soft),
        "language": args.language or None,
        **coverage,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"summary": summary, "failures": failures, "rows": report}, indent=1) + "\n")

    print(f"rendered {rendered}/{len(rows)} rows in {summary['wall_seconds']}s -> {report_path}")
    if coverage:
        print(
            f"{args.language}: SFW coverage {coverage['sfw_coverage_pct']}% of the day, "
            f"{coverage['daytime_sfw_coverage_pct']}% of 07:00-23:00; longest uncovered run "
            f"{coverage['longest_uncovered_run_minutes']} min; {coverage['pwa_fallback_rows']} row(s) with a "
            "letter the app's fonts lack"
        )
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
        span = qr.find_timestring_span(qb, tsb)
        if span is None:
            print(f"{rows}|NOSTRING")
            continue
        s, e = span  # exact-span bolding (litclock-dev#540), Unicode-aware (litclock-dev#532 T5)
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
    ap.add_argument(
        "--language",
        help=argparse.SUPPRESS,  # accepted here for compatibility; documented on `check`
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="render full corpus and enforce invariants")
    p_check.add_argument("--report", default=str(_REPO / "render-invariants-report.json"))
    p_check.add_argument("--limit", type=int, default=0, help="render only the first N rows (smoke)")
    p_check.add_argument(
        "--language",
        dest="check_language",
        help="check this language's corpus (overrides --csv) and add the L1-L8 checks + coverage; "
        "an unregistered code reads languages/<code>/quotes.csv",
    )
    p_probe = sub.add_parser("probe", help="emit row|fs|breaks parity lines (Stage-0 format)")
    p_probe.add_argument("cap", type=int, nargs="?", default=300, help="max CSV rows to scan (0 = all)")
    p_probe.add_argument("step", type=int, nargs="?", default=3, help="sample every Nth row")
    args = ap.parse_args()
    if args.cmd == "check":
        args.language = args.check_language or args.language
    elif args.language:
        ap.error("--language applies to `check` only")
    if args.cmd == "probe" and args.step < 1:
        ap.error("step must be >= 1")
    return cmd_check(args) if args.cmd == "check" else cmd_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
