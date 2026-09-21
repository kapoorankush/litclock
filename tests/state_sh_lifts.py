"""Lifts of scripts/lib/state.sh functions shared by the shell-script test
files (litclock-dev#868). One required-parts table, so a mutation of the
helper is caught by every file that executes it rather than by whichever
copy happened to list that part (litclock-dev#873 review)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_SH = REPO_ROOT / "scripts" / "lib" / "state.sh"


def extract_history_helper() -> str:
    """lib/state.sh's clear_and_lock_bash_history, verbatim, with its
    load-bearing parts asserted (the litclock-dev#662 rule: a lifted span that
    asserts nothing about itself can lose the lock and stay extractable)."""
    body = STATE_SH.read_text()
    assert body.count("clear_and_lock_bash_history() {") == 1, "helper anchor is not unique"
    start = body.index("clear_and_lock_bash_history() {")
    end = body.index("\n}\n", start) + len("\n}\n")
    fn = body[start:end]
    for required, why in (
        ("history -c", "the clear of the script's own shell"),
        (
            'if [[ -L "$_h" && ! -c "$_h" ]]; then',
            "the refuse-to-follow symlink check BEFORE removal (litclock-dev#873 review)",
        ),
        ("stat -c %h", "the hard-link count check (litclock-dev#873 review)"),
        ('chattr -ia "$_h"', "clearing append-only/immutable before removal (litclock-dev#873 review)"),
        ("rmdir", "taking a previous run's lock (and refusing a non-empty one)"),
        ("rm -f", "the removal of a real history file"),
        ('if mkdir "$_h" 2>/dev/null && [[ -d "$_h" && ! -L "$_h" ]]; then', "mkdir's status as the lock verdict"),
        ("_HIST_DIRTY+=", "the survived-contents verdict"),
        ("_HIST_UNLOCKED+=", "the could-not-lock verdict"),
        ("_HIST_LOCKED+=", "the placed-locks report"),
        ('for _h in "$@"; do', "the variadic path loop"),
    ):
        assert required in fn, f"clear_and_lock_bash_history lost {why} ({required!r})"
    return fn
