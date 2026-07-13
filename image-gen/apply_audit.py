"""Apply the reviewed #192 audit to produce litclock_annotated_audited.csv.

Reads a reviewed CSV (audit_fails.csv with a `decision` column filled in by
hand) and writes a new corpus with DROP rows removed. Decisions:

    DROP  - remove this row from the corpus
    KEEP  - the judge was wrong; leave the row in place
    FLAG  - ambiguous, come back to it later. Stays in the corpus for now; a
            separate flagged.csv is written so you can see the magnitude.

Verifies each reviewed idx still matches the current corpus at that idx
(aborts on mismatch). Computes per-HH:MM coverage before/after; aborts if
any slot would drop to zero unless --allow-empty-slots is set.

Usage:
    python3 apply_audit.py --reviewed audit_fails_reviewed.csv
    python3 apply_audit.py --reviewed audit_fails_reviewed.csv --allow-empty-slots

Exit codes:
    0  Applied successfully.
    2  Fail-fast: idx mismatch, invalid decision, missing corpus column, or --out == --corpus.
    3  Empty-slot gate would drop HH:MM slots to zero quotes (override: --allow-empty-slots).
"""

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path

CORPUS = Path(__file__).parent / "litclock_annotated.csv"
OUT = Path(__file__).parent / "litclock_annotated_audited.csv"

VALID_DECISIONS = {"DROP", "KEEP", "FLAG"}
MIN_CORPUS_COLS = 5


def load_corpus(path: Path) -> list[list[str]]:
    """Return corpus as a list of raw row lists (preserves original order + all 6 cols)."""
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        for r in reader:
            rows.append(r)
    return rows


def load_reviewed(path: Path) -> list[dict]:
    """Load a reviewed audit CSV (comma-delimited, has `decision` column).

    Uses utf-8-sig to transparently strip the BOM that Excel/Numbers prepend when
    saving CSVs — otherwise the first column header reads as '\\ufeffidx' and the
    required-column check fails with a misleading error.
    """
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "decision" not in reader.fieldnames:
            print(f"ERROR: {path} missing required `decision` column", file=sys.stderr)
            raise SystemExit(2)
        for required in ("idx", "time", "match", "quote"):
            if required not in reader.fieldnames:
                print(f"ERROR: {path} missing required `{required}` column", file=sys.stderr)
                raise SystemExit(2)
        return [dict(r) for r in reader]


def validate(reviewed: list[dict], corpus: list[list[str]]) -> tuple[set[int], set[int]]:
    """Validate reviewed rows against corpus; return (to_drop, to_flag) idx sets.

    Checks:
      - idx is an integer and in range
      - corpus row at idx is well-formed (>=MIN_CORPUS_COLS)
      - (time, match, quote) at idx hasn't drifted since the audit
      - title/author also match when the reviewed CSV carries them (optional)
      - decision is DROP, KEEP, or FLAG
      - duplicate idx rows are allowed only if their decisions agree (a later
        row silently overriding an earlier one would be too easy to misread)
    """
    to_drop: set[int] = set()
    to_flag: set[int] = set()
    seen_decisions: dict[int, str] = {}
    errors: list[str] = []
    for r in reviewed:
        try:
            idx = int(r["idx"])
        except (ValueError, TypeError):
            errors.append(f"row idx={r.get('idx')!r} is not an integer")
            continue
        decision = (r.get("decision") or "").strip().upper()
        if decision not in VALID_DECISIONS:
            errors.append(f"idx={idx} has invalid decision {r.get('decision')!r} (expected DROP, KEEP, or FLAG)")
            continue
        if idx in seen_decisions:
            if seen_decisions[idx] != decision:
                errors.append(
                    f"idx={idx} has conflicting decisions ({seen_decisions[idx]} and {decision}); "
                    "duplicate idx rows must agree"
                )
                continue
            # Same decision repeated — idempotent, no-op.
            continue
        if idx < 0 or idx >= len(corpus):
            errors.append(f"idx={idx} out of range (corpus has {len(corpus)} rows)")
            continue
        row = corpus[idx]
        if len(row) < MIN_CORPUS_COLS:
            errors.append(f"idx={idx}: corpus row is malformed (<{MIN_CORPUS_COLS} cols)")
            continue
        c_time, c_match, c_quote = row[0].strip(), row[1].strip(), row[2].strip()
        if c_time != r["time"].strip() or c_match != r["match"].strip() or c_quote != r["quote"].strip():
            errors.append(
                f"idx={idx}: (time,match,quote) drifted since audit; "
                f"corpus has ({c_time!r},{c_match!r}), reviewed has ({r['time']!r},{r['match']!r})"
            )
            continue
        # title/author drift — only checked when the reviewed CSV carries them.
        r_title, r_author = r.get("title"), r.get("author")
        if r_title is not None and row[3].strip() != r_title.strip():
            errors.append(f"idx={idx}: title drifted; corpus has {row[3]!r}, reviewed has {r_title!r}")
            continue
        if r_author is not None and row[4].strip() != r_author.strip():
            errors.append(f"idx={idx}: author drifted; corpus has {row[4]!r}, reviewed has {r_author!r}")
            continue
        seen_decisions[idx] = decision
        if decision == "DROP":
            to_drop.add(idx)
        elif decision == "FLAG":
            to_flag.add(idx)
    if errors:
        print("ERROR: validation failed:", file=sys.stderr)
        for e in errors[:20]:
            print(f"  {e}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more", file=sys.stderr)
        raise SystemExit(2)
    return to_drop, to_flag


def coverage(corpus: list[list[str]], dropped: set[int]) -> Counter:
    """Per-HH:MM quote count. Matches audit_quotes.load_rows by skipping rows <MIN_CORPUS_COLS cols
    so coverage numbers agree with what the auditor actually evaluated.
    """
    c: Counter = Counter()
    for i, row in enumerate(corpus):
        if i in dropped or len(row) < MIN_CORPUS_COLS:
            continue
        c[row[0].strip()] += 1
    return c


def empty_slot_regressions(before: Counter, after: Counter) -> list[str]:
    """Return sorted list of HH:MM slots that go from >0 to 0."""
    return sorted(t for t, n in before.items() if n > 0 and after.get(t, 0) == 0)


def write_audited(path: Path, corpus: list[list[str]], dropped: set[int]) -> int:
    """Atomic write: tmp+rename so a crash mid-write can't leave a truncated corpus on disk."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    kept = 0
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="|")
        for i, row in enumerate(corpus):
            if i in dropped:
                continue
            w.writerow(row)
            kept += 1
    os.replace(tmp, path)
    return kept


def write_flagged(path: Path, reviewed: list[dict], flagged_idx: set[int]) -> int:
    """Write the subset of reviewed rows marked FLAG to a sidecar CSV.

    Preserves whatever columns the reviewed CSV carried so the reviewer has
    the full context (rationale, quote, title, author) when they come back
    to these rows for a re-tag or edit pass.
    """
    flagged_rows = [r for r in reviewed if int(r["idx"]) in flagged_idx]
    if not flagged_rows:
        return 0
    # Preserve original column order from the reviewed CSV.
    cols = list(flagged_rows[0].keys())
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in flagged_rows:
            w.writerow(r)
    os.replace(tmp, path)
    return len(flagged_rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--reviewed", required=True, help="reviewed audit CSV with DROP/KEEP decisions")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument(
        "--allow-empty-slots",
        action="store_true",
        help="proceed even if DROPs would leave HH:MM slots with zero quotes",
    )
    args = ap.parse_args(argv)

    # Prevent writing over the source-of-truth corpus. Resolve both paths so
    # `image-gen/x.csv` and `./image-gen/x.csv` are treated as the same file.
    corpus_path = Path(args.corpus).resolve()
    out_path = Path(args.out).resolve()
    if corpus_path == out_path:
        print(
            f"ERROR: --out ({args.out}) resolves to the same file as --corpus ({args.corpus}). "
            "Write to a different path to avoid overwriting the source-of-truth corpus.",
            file=sys.stderr,
        )
        return 2

    corpus = load_corpus(Path(args.corpus))
    reviewed = load_reviewed(Path(args.reviewed))
    print(f"Loaded {len(corpus)} corpus rows, {len(reviewed)} reviewed rows", file=sys.stderr)

    to_drop, to_flag = validate(reviewed, corpus)
    # Count unique reviewed idx rather than list length so duplicate-but-agreeing
    # rows don't inflate the KEEP denominator.
    unique_reviewed = len({int(r["idx"]) for r in reviewed if str(r.get("idx", "")).strip().lstrip("-").isdigit()})
    kept_count = unique_reviewed - len(to_drop) - len(to_flag)
    print(
        f"DROP: {len(to_drop)}  KEEP: {kept_count}  FLAG: {len(to_flag)}",
        file=sys.stderr,
    )

    # FLAG rows stay in the corpus (they need follow-up, not deletion), so they
    # don't affect coverage math. They're logged separately below for review.
    before = coverage(corpus, set())
    after = coverage(corpus, to_drop)
    regressions = empty_slot_regressions(before, after)
    if regressions:
        print(
            f"\nWARNING: {len(regressions)} HH:MM slot(s) would drop to zero quotes:",
            file=sys.stderr,
        )
        for t in regressions:
            print(f"  {t}  (was {before[t]})", file=sys.stderr)
        if not args.allow_empty_slots:
            print(
                "\nAborting. Rerun with --allow-empty-slots to accept these regressions "
                "(and source replacement quotes for each slot).",
                file=sys.stderr,
            )
            return 3
        print("\n--allow-empty-slots set; proceeding anyway.", file=sys.stderr)

    kept = write_audited(Path(args.out), corpus, to_drop)
    print(
        f"\nWrote {kept} rows (dropped {len(to_drop)}) -> {args.out}",
        file=sys.stderr,
    )

    # Write the FLAG sidecar next to --out so reviewers have a concrete
    # magnitude + list to follow up on (re-tag, edit, or drop in a later pass).
    if to_flag:
        flagged_path = out_path.with_name(out_path.stem + ".flagged.csv")
        write_flagged(flagged_path, reviewed, to_flag)
        print(f"Wrote {len(to_flag)} flagged rows -> {flagged_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
