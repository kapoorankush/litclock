"""Audit litclock_annotated.csv for time-of-day vs duration/coincidence mismatches.

Uses the Anthropic API (claude-sonnet-4-6) as an LLM judge. Each quote is classified
PASS (scene happens at the label time) or FAIL (substring is a duration, coincidental
number, or the scene is set at a different time). See issue #192.

Usage:
    # Full audit (~5-10 min, ~$4-5 on ~4800 rows):
    python3 audit_quotes.py --out audit_fails.csv --all-out audit_all.csv

    # Pilot / sample run (deterministic):
    python3 audit_quotes.py --sample 50 --seed 42 --out pilot.csv

    # Gold-set calibration (gates the full run):
    python3 audit_quotes.py --gold gold_set_192.csv

Crash / Ctrl-C recovery: each verdict is streamed to {out}.progress.jsonl as it
completes. A re-run with the same --out resumes by skipping already-judged idx,
so a mid-run SIGINT doesn't cost the full $4-5 over again. Delete the
.progress.jsonl file to force a clean run.

Requires: ANTHROPIC_API_KEY env var; `pip install -r requirements-dev.txt`.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 200
CONCURRENCY = 10
# Let the Anthropic SDK handle exponential backoff on rate limits and connection
# errors — 5 retries is generous enough to ride out a typical rate-limit burst
# on a 4800-row run without silently dropping rows as ERROR.
API_MAX_RETRIES = 5
CSV_PATH = Path(__file__).parent / "litclock_annotated.csv"

# Programming errors — never mask these as ERROR rows, let them propagate so
# real bugs surface instead of silently polluting the audit output.
_PROGRAMMING_ERRORS = (TypeError, NameError, AttributeError, KeyError, IndexError)

# Rationale is persisted to CSV for reviewer inspection — cap at a bounded length
# so a runaway model response can't bloat the output file.
RATIONALE_MAX_LEN = 300

# Gold-set calibration thresholds (see issue #192).
GOLD_TUNE_SIZE = 20
GOLD_PRECISION_GATE = 0.80
GOLD_RECALL_GATE = 0.90

# OWASP CSV injection: leading =, +, -, @ can be interpreted as formulas when
# audit_fails.csv is opened in Excel/Sheets/Numbers. Prefix these with a single
# quote so the cell renders as literal text.
_FORMULA_PREFIX = ("=", "+", "-", "@")

SYSTEM_PROMPT = """You audit quotes for a literary clock. The clock displays a quote at \
minute HH:MM with the matched substring rendered in BOLD. A reader glances at the bold to \
read the time.

Apply a STRICT bold-first rule: if the bold substring, read IN ISOLATION, reads as the label \
time, PASS. The surrounding quote's scene, tense, or narrative function is SECONDARY. \
Context only overrides the bold when the quote EXPLICITLY names a different specific clock \
time (e.g., the quote says "session began at 12:25 PM earlier").

When in doubt, PASS. The corpus may intentionally reuse the same quote at multiple label \
times for completeness — we prefer coverage over perfection. Do NOT FAIL just because the \
narrative scene isn't set at the label time.

PASS when the bold reads as the label time:
  - Direct clock: "1.42pm" at 13:42; "8:37 P.M." at 20:37
  - Spelled-out: "a quarter past seven" at 19:15; "five in the afternoon" at 17:00
  - "After/past X" within a small margin (0-6 min): "after nine" at 21:01/21:05/21:10; \
"after eight o'clock" at 20:06
  - "Before/to X" similarly: "just before ten" at 21:55
  - On-theme vagueness: "midnight" at 00:00; "in the heart of the night" at 00:00
  - "12:XX" at "00:XX" is DEFAULT PASS. Readers can parse "12:15" as either noon or \
midnight; absent explicit midday context in the quote, either is acceptable.
  - Counting / incidental words whose bold reads as a time: "two" from "One, two" at 02:00 \
PASS — the bold alone reads "2".
  - Past-tense / schedule / habitual / narrative references whose bold names the label \
time: "twelve o'clock at night" at 00:00 even inside "last September twelvemonth..."; \
"I lie at twelve" at 00:00 when "morning" appears later (implying night).
  - Fictional / stopped clocks whose bold reads as label: "4:30" at 04:30 even when the \
quote describes a stopped pocket watch.
  - Ambiguous am/pm when the quote doesn't strongly disambiguate: "3:42" at 03:42; \
"Ten eighteen" at 22:18; "12:55" at 00:55 WITHOUT explicit midday context.
  - "Almost X:YY" within ~2-3 min: "almost 2:04" at 02:02.
  - Informal spellings: "ten-thirteen" at 22:13.

FAIL when the bold does NOT read as the label time:
  - Duration / elapsed time: "In twenty minutes" at 22:20 (bold reads as "20 min from now"); \
"two past hours of the night" at 02:00 (reads as "two hours past nightfall").
  - Bold spans clock + duration requiring arithmetic: "Clock time is 0 Hours, 12 Minutes. \
Twenty three minutes later" at 00:35 — reader can't pluck the label time off the bold.
  - "After/past/soon after X" stretched 7+ min past X: "after eight o'clock" at 20:09 FAIL; \
"Soon after 5 a.m." at 05:00 FAIL ("soon after" stretches arbitrarily).
  - Bold clearly names a different clock time, AND the quote explicitly anchors to that \
other time: "12:55" at 00:55 FAIL ONLY when quote explicitly anchors midday (e.g., "session \
began at 12:25 PM").
  - Explicit am/pm mismatch: "four thirty" at 04:30 when quote says "in the afternoon".

Key nuances:
  - Same quote text at different label times: judge each independently on bold-fit (e.g., \
"after eight o'clock" at 20:06 PASS, at 20:09 FAIL — margin matters).
  - Do NOT FAIL for AM/PM ambiguity UNLESS the quote explicitly anchors to the wrong half \
of the day.
  - Default toward PASS — empty HH:MM slots are worse than imperfect matches.

Call the record_verdict tool once with your verdict and a brief (~1 sentence) rationale \
focused on HOW THE BOLD READS."""

VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record the audit verdict for a single quote.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["PASS", "FAIL"],
                "description": "PASS if the bold reads as the label time; FAIL otherwise.",
            },
            "rationale": {
                "type": "string",
                "description": "Short reason (~1 sentence) for the verdict.",
            },
        },
        "required": ["verdict", "rationale"],
    },
}


def load_rows(path=CSV_PATH):
    """Yield corpus rows as dicts. Matches the 6-column pipe CSV format."""
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        for i, r in enumerate(reader):
            if len(r) < 5:
                continue
            yield {
                "idx": i,
                "time": r[0].strip(),
                "match": r[1].strip(),
                "quote": r[2].strip(),
                "title": r[3].strip(),
                "author": r[4].strip(),
            }


def load_gold_set(path):
    """Load hand-labeled gold set. Columns: idx|time|match|quote|title|author|label."""
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        header = next(reader, None)
        if header is None or len(header) < 7:
            raise ValueError(f"Gold set {path} missing header or columns")
        for r in reader:
            if len(r) < 7:
                continue
            label = r[6].strip().upper()
            if label not in ("PASS", "FAIL"):
                raise ValueError(f"Gold row idx={r[0]} has unlabeled/invalid label: {r[6]!r}")
            rows.append(
                {
                    "idx": int(r[0]),
                    "time": r[1].strip(),
                    "match": r[2].strip(),
                    "quote": r[3].strip(),
                    "title": r[4].strip(),
                    "author": r[5].strip(),
                    "label": label,
                }
            )
    return rows


def parse_tool_response(message) -> tuple[str, str]:
    """Extract (verdict, rationale) from an Anthropic Message. Raise on missing tool use."""
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "record_verdict":
            data = block.input or {}
            verdict = str(data.get("verdict", "")).upper()
            rationale = str(data.get("rationale", ""))[:RATIONALE_MAX_LEN]
            if verdict not in ("PASS", "FAIL"):
                raise ValueError(f"Invalid verdict in tool response: {verdict!r}")
            return verdict, rationale
    raise ValueError("Response missing record_verdict tool_use block")


def _build_user_message(row: dict) -> str:
    """Build the judge's user-message payload.

    Uses an f-string (not .format()) because quotes frequently contain literal
    '{' or '}' characters — .format() would raise KeyError on those.
    """
    return f"Label time (24h): {row['time']}\nMatched substring: {row['match']}\nQuote: {row['quote']}"


async def judge_one(client, sem, row: dict) -> dict:
    """Judge a single row. Returns row + verdict/rationale/elapsed.

    Network/API errors and tool-parse failures become ERROR rows so one bad row
    doesn't abort the whole audit. Programming errors (TypeError, KeyError from
    our own code, etc.) are re-raised so real bugs don't get buried in the
    ERROR column.
    """
    t0 = time.time()
    user = _build_user_message(row)
    try:
        async with sem:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
                tools=[VERDICT_TOOL],
                tool_choice={"type": "tool", "name": "record_verdict"},
            )
        verdict, rationale = parse_tool_response(resp)
    except _PROGRAMMING_ERRORS:
        raise
    except Exception as e:
        return {
            **row,
            "verdict": "ERROR",
            "rationale": f"{type(e).__name__}: {e}"[:RATIONALE_MAX_LEN],
            "elapsed": time.time() - t0,
        }
    return {**row, "verdict": verdict, "rationale": rationale, "elapsed": time.time() - t0}


def _progress_path_for(out_path) -> Path:
    """Sidecar JSONL path for incremental checkpointing."""
    p = Path(out_path)
    return p.with_name(p.stem + ".progress.jsonl")


def _load_progress(path: Path) -> dict[int, dict]:
    """Load already-judged verdicts from a progress JSONL. Returns {idx: result}."""
    if not path.exists():
        return {}
    done: dict[int, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                idx = int(rec["idx"])
                done[idx] = rec
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # Tolerate a partial final line from a prior crash — skip it.
                continue
    return done


async def judge_all(rows: list, workers: int, progress_path: Path | None = None) -> list:
    """Judge all rows concurrently, capped by a semaphore.

    If progress_path is set, each completed verdict is appended to it as JSONL
    so a re-run can skip already-judged idx. Cancels in-flight tasks on exit.
    """
    from anthropic import AsyncAnthropic

    already: dict[int, dict] = _load_progress(progress_path) if progress_path else {}
    if already:
        print(
            f"Resuming: {len(already)} verdicts loaded from {progress_path} (delete that file for a fresh run)",
            file=sys.stderr,
        )

    rows_to_do = [r for r in rows if r["idx"] not in already]
    pf = open(progress_path, "a", encoding="utf-8") if progress_path else None

    client = AsyncAnthropic(max_retries=API_MAX_RETRIES)
    sem = asyncio.Semaphore(workers)
    tasks = [asyncio.create_task(judge_one(client, sem, r)) for r in rows_to_do]

    results: list = list(already.values())
    t0 = time.time()
    try:
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            res = await coro
            results.append(res)
            if pf is not None:
                pf.write(json.dumps(res, default=str) + "\n")
                pf.flush()
            if i % 25 == 0 or i == len(tasks):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                eta = (len(tasks) - i) / rate if rate else 0
                print(f"  {i}/{len(tasks)}  {elapsed:.0f}s elapsed  {eta:.0f}s ETA", file=sys.stderr)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if pf is not None:
            pf.close()
    return results


def score_gold(results: list, gold_rows: list) -> dict:
    """Compute precision/recall treating FAIL as the positive class."""
    labels_by_idx = {g["idx"]: g["label"] for g in gold_rows}
    tp = fp = fn = tn = errors = 0
    for r in results:
        gold = labels_by_idx.get(r["idx"])
        if gold is None:
            continue
        pred = r["verdict"]
        if pred == "ERROR":
            errors += 1
            continue
        if gold == "FAIL" and pred == "FAIL":
            tp += 1
        elif gold == "PASS" and pred == "FAIL":
            fp += 1
        elif gold == "FAIL" and pred == "PASS":
            fn += 1
        elif gold == "PASS" and pred == "PASS":
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "errors": errors,
        "precision": precision,
        "recall": recall,
    }


def print_gold_report(split_name: str, scores: dict) -> None:
    p, r = scores["precision"], scores["recall"]
    print(
        f"[{split_name}] precision={p:.3f} recall={r:.3f}  "
        f"TP={scores['tp']} FP={scores['fp']} FN={scores['fn']} TN={scores['tn']} ERR={scores['errors']}",
        file=sys.stderr,
    )


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception as e:
        print(f"WARNING: git_sha() unavailable ({type(e).__name__}); meta will record 'unknown'", file=sys.stderr)
        return "unknown"


def prompt_hash() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]


def _sanitize_csv_cell(value):
    """Prefix a leading =, +, -, or @ with a single quote to defeat CSV formula injection.

    Called before writing reviewer-facing text fields (rationale, quote, match, title,
    author) to audit_fails.csv / audit_all.csv since reviewers will open those in Excel
    or Google Sheets.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIX):
        return "'" + value
    return value


_SANITIZE_FIELDS = ("rationale", "quote", "match", "title", "author")


def write_results(path: Path, rows: list, cols: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            safe = {k: (_sanitize_csv_cell(v) if k in _SANITIZE_FIELDS else v) for k, v in r.items()}
            w.writerow(safe)


def write_meta(path: Path, results: list, gold_scores: dict | None, args_dict: dict) -> None:
    fails = sum(1 for r in results if r["verdict"] == "FAIL")
    passes = sum(1 for r in results if r["verdict"] == "PASS")
    errors = sum(1 for r in results if r["verdict"] == "ERROR")
    meta = {
        "model": MODEL,
        "prompt_hash": prompt_hash(),
        "git_sha": git_sha(),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "rows": len(results),
        "pass": passes,
        "fail": fails,
        "error": errors,
        "args": args_dict,
    }
    if gold_scores is not None:
        meta["gold"] = gold_scores
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


async def run_gold_mode(args) -> int:
    gold = load_gold_set(args.gold)
    if len(gold) < GOLD_TUNE_SIZE + 1:
        print(f"Gold set has only {len(gold)} rows; need > {GOLD_TUNE_SIZE}", file=sys.stderr)
        return 1
    tune = gold[:GOLD_TUNE_SIZE]
    holdout = gold[GOLD_TUNE_SIZE:]
    print(f"Judging gold set: {len(tune)} tune + {len(holdout)} holdout", file=sys.stderr)
    progress_path = _progress_path_for(args.out) if args.out else None
    results = await judge_all(gold, args.workers, progress_path=progress_path)
    by_idx = {r["idx"]: r for r in results}

    tune_scores = score_gold([by_idx[t["idx"]] for t in tune if t["idx"] in by_idx], tune)
    holdout_scores = score_gold([by_idx[h["idx"]] for h in holdout if h["idx"] in by_idx], holdout)
    print_gold_report("tune", tune_scores)
    print_gold_report("holdout", holdout_scores)

    gate_pass = holdout_scores["precision"] >= GOLD_PRECISION_GATE and holdout_scores["recall"] >= GOLD_RECALL_GATE
    print(
        f"\nGATE {'PASSED' if gate_pass else 'FAILED'} — "
        f"need precision >= {GOLD_PRECISION_GATE}, recall >= {GOLD_RECALL_GATE} on holdout",
        file=sys.stderr,
    )

    if args.out:
        cols = ["idx", "time", "match", "verdict", "rationale", "quote", "title", "author"]
        write_results(Path(args.out), sorted(results, key=lambda x: x["idx"]), cols)
        print(f"Wrote gold results -> {args.out}", file=sys.stderr)

    meta_path = Path(args.out).with_suffix(".meta.json") if args.out else Path("gold.meta.json")
    write_meta(
        meta_path,
        results,
        {"tune": tune_scores, "holdout": holdout_scores, "gate_passed": gate_pass},
        vars(args),
    )
    print(f"Wrote meta -> {meta_path}", file=sys.stderr)
    return 0 if gate_pass else 4


def _batch_id_path(out_path) -> Path:
    """Sidecar file that remembers an in-flight batch id so re-runs can resume polling."""
    p = Path(out_path)
    return p.with_name(p.stem + ".batch_id")


def _build_batch_request(row: dict) -> dict:
    """Build a single Message Batch request entry for a corpus row."""
    return {
        "custom_id": f"idx-{row['idx']}",
        "params": {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": _build_user_message(row)}],
            "tools": [VERDICT_TOOL],
            "tool_choice": {"type": "tool", "name": "record_verdict"},
        },
    }


async def run_batch_mode(args) -> int:
    """Submit the audit as a Message Batch (50% cheaper, no rate-limit anxiety).

    Resumable: stores the batch id next to --out; a re-run picks up polling where
    it left off. Batch results are retained by Anthropic for 29 days.
    """
    from anthropic import AsyncAnthropic

    rows = list(load_rows())
    if args.sample:
        random.seed(args.seed)
        rows = random.sample(rows, min(args.sample, len(rows)))

    bid_path = _batch_id_path(args.out)
    client = AsyncAnthropic()

    # Submit or resume
    if bid_path.exists():
        batch_id = bid_path.read_text().strip()
        print(f"Resuming existing batch {batch_id} from {bid_path}", file=sys.stderr)
    else:
        print(f"Submitting Message Batch with {len(rows)} rows ({MODEL})", file=sys.stderr)
        requests = [_build_batch_request(r) for r in rows]
        batch = await client.messages.batches.create(requests=requests)
        batch_id = batch.id
        bid_path.write_text(batch_id)
        print(f"Submitted batch {batch_id} -> saved id to {bid_path}", file=sys.stderr)

    # Poll until ended
    t0 = time.time()
    while True:
        batch = await client.messages.batches.retrieve(batch_id)
        c = batch.request_counts
        total_done = c.succeeded + c.errored + c.canceled + c.expired
        total = total_done + c.processing
        elapsed = time.time() - t0
        print(
            f"  status={batch.processing_status}  "
            f"succeeded={c.succeeded}  errored={c.errored}  processing={c.processing}  "
            f"({total_done}/{total} @ {elapsed:.0f}s)",
            file=sys.stderr,
        )
        if batch.processing_status == "ended":
            break
        await asyncio.sleep(30)

    # Stream results. Keyed by idx for join-back against the input rows.
    rows_by_idx = {r["idx"]: r for r in rows}
    results: list = []
    async for entry in await client.messages.batches.results(batch_id):
        try:
            idx = int(entry.custom_id.removeprefix("idx-"))
        except ValueError:
            continue
        row = rows_by_idx.get(idx)
        if row is None:
            continue
        rtype = getattr(entry.result, "type", "unknown")
        if rtype == "succeeded":
            try:
                verdict, rationale = parse_tool_response(entry.result.message)
                results.append({**row, "verdict": verdict, "rationale": rationale, "elapsed": 0.0})
            except ValueError as e:
                results.append(
                    {
                        **row,
                        "verdict": "ERROR",
                        "rationale": f"{type(e).__name__}: {e}"[:RATIONALE_MAX_LEN],
                        "elapsed": 0.0,
                    }
                )
        else:
            err_info = getattr(entry.result, "error", None) or rtype
            results.append(
                {
                    **row,
                    "verdict": "ERROR",
                    "rationale": f"batch_{rtype}: {err_info}"[:RATIONALE_MAX_LEN],
                    "elapsed": 0.0,
                }
            )

    fails = [r for r in results if r["verdict"] == "FAIL"]
    errors = [r for r in results if r["verdict"] == "ERROR"]
    passes = len(results) - len(fails) - len(errors)
    print(
        f"\nBatch done in {time.time() - t0:.0f}s. PASS={passes}  FAIL={len(fails)}  ERROR={len(errors)}",
        file=sys.stderr,
    )

    cols = ["idx", "time", "match", "verdict", "rationale", "quote", "title", "author"]
    write_results(Path(args.out), sorted(fails + errors, key=lambda x: x["time"]), cols)
    print(f"Wrote {len(fails) + len(errors)} flagged rows -> {args.out}", file=sys.stderr)

    if args.all_out:
        write_results(Path(args.all_out), sorted(results, key=lambda x: x["idx"]), cols)
        print(f"Wrote all {len(results)} rows -> {args.all_out}", file=sys.stderr)

    meta_path = Path(args.out).with_suffix(".meta.json")
    write_meta(meta_path, results, None, vars(args))
    print(f"Wrote meta -> {meta_path}", file=sys.stderr)

    # Leave the batch_id file on disk as a receipt; user can `rm` it for a fresh submission.
    return 0


async def run_audit_mode(args) -> int:
    rows = list(load_rows())
    if args.sample:
        random.seed(args.seed)
        rows = random.sample(rows, min(args.sample, len(rows)))
    print(f"Judging {len(rows)} rows with concurrency={args.workers}", file=sys.stderr)

    t0 = time.time()
    progress_path = _progress_path_for(args.out)
    results = await judge_all(rows, args.workers, progress_path=progress_path)
    fails = [r for r in results if r["verdict"] == "FAIL"]
    errors = [r for r in results if r["verdict"] == "ERROR"]
    passes = len(results) - len(fails) - len(errors)
    print(
        f"\nDone in {time.time() - t0:.0f}s. PASS={passes}  FAIL={len(fails)}  ERROR={len(errors)}",
        file=sys.stderr,
    )

    cols = ["idx", "time", "match", "verdict", "rationale", "quote", "title", "author"]
    write_results(
        Path(args.out),
        sorted(fails + errors, key=lambda x: x["time"]),
        cols,
    )
    print(f"Wrote {len(fails) + len(errors)} flagged rows -> {args.out}", file=sys.stderr)

    if args.all_out:
        write_results(Path(args.all_out), sorted(results, key=lambda x: x["idx"]), cols)
        print(f"Wrote all {len(results)} rows -> {args.all_out}", file=sys.stderr)

    meta_path = Path(args.out).with_suffix(".meta.json")
    write_meta(meta_path, results, None, vars(args))
    print(f"Wrote meta -> {meta_path}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--sample", type=int, default=0, help="N random rows (0 = full corpus)")
    ap.add_argument("--seed", type=int, default=42, help="random seed for --sample")
    ap.add_argument(
        "--workers",
        type=int,
        default=CONCURRENCY,
        help=f"concurrent API calls (default {CONCURRENCY})",
    )
    ap.add_argument("--out", default="audit_fails.csv", help="CSV path for FAIL/ERROR rows")
    ap.add_argument("--all-out", default=None, help="also write every verdict here")
    ap.add_argument(
        "--gold",
        default=None,
        help="run gold-set calibration against this hand-labeled CSV (mutually exclusive with full audit)",
    )
    ap.add_argument(
        "--batch",
        action="store_true",
        help="submit via the Anthropic Message Batch API (50%% cheaper, async). Only for full audit.",
    )
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    if args.gold:
        return asyncio.run(run_gold_mode(args))
    if args.batch:
        return asyncio.run(run_batch_mode(args))
    return asyncio.run(run_audit_mode(args))


if __name__ == "__main__":
    sys.exit(main())
