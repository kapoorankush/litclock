"""Build the gold-set scaffold for the #192 audit calibration.

Emits 50 unlabeled rows to `gold_set_192.csv`:
  - First 16 rows: regex-duration candidates (matched substring starts with
    in/after/for/within/another) — deterministic order.
  - Next 34 rows: seeded random sample from the rest of the corpus.

After running, hand-label the `label` column as PASS or FAIL, then:
  python3 audit_quotes.py --gold gold_set_192.csv --out gold_results.csv

Usage:
    python3 build_gold_set.py
    python3 build_gold_set.py --force   # overwrite existing gold_set_192.csv
"""

import argparse
import csv
import random
import re
import sys
from pathlib import Path

CORPUS = Path(__file__).parent / "litclock_annotated.csv"
OUT = Path(__file__).parent / "gold_set_192.csv"

DURATION_PREFIX = re.compile(r"^(in|after|for|within|another)\b", re.IGNORECASE)
TOTAL = 50
RANDOM_SEED = 42


def load_corpus(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        for i, r in enumerate(reader):
            if len(r) < 5:
                continue
            rows.append(
                {
                    "idx": i,
                    "time": r[0].strip(),
                    "match": r[1].strip(),
                    "quote": r[2].strip(),
                    "title": r[3].strip(),
                    "author": r[4].strip(),
                }
            )
    return rows


def pick(rows: list[dict]) -> list[dict]:
    suspects = [r for r in rows if DURATION_PREFIX.match(r["match"])]
    suspects.sort(key=lambda r: r["idx"])
    remaining = [r for r in rows if r["idx"] not in {s["idx"] for s in suspects}]
    rnd = random.Random(RANDOM_SEED)
    random_slice = rnd.sample(remaining, TOTAL - len(suspects))
    return suspects + random_slice


def write_gold(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="|")
        w.writerow(["idx", "time", "match", "quote", "title", "author", "label"])
        for r in rows:
            w.writerow([r["idx"], r["time"], r["match"], r["quote"], r["title"], r["author"], ""])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--force", action="store_true", help="overwrite existing gold_set_192.csv")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"ERROR: {out} exists. Use --force to overwrite.", file=sys.stderr)
        return 2

    rows = load_corpus(Path(args.corpus))
    picked = pick(rows)
    write_gold(out, picked)

    suspects = sum(1 for r in picked if DURATION_PREFIX.match(r["match"]))
    print(
        f"Wrote {len(picked)} rows ({suspects} duration-regex suspects + {len(picked) - suspects} random) -> {out}",
        file=sys.stderr,
    )
    print("Next: hand-label the `label` column (PASS or FAIL) for every row.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
