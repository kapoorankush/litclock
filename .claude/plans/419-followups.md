# Plan: Issue #419 — #416 PR2 /review follow-ups

## Status

| # | Decision | Choice |
|---|----------|--------|
| D1 | Drop obsolete items M6 (placeholder bleed-through guard) + P5 (cache inline Jinja template) | **Dropped** — PR3 already replaced the placeholder; route now uses `render_template("diagnostics.html.j2", ...)` which is cached by Flask's named-template bytecode cache |
| D2 | PR strategy | **3 PRs**: PR1 (tests + refactor bundled) → PR2 (perf, slimmed) → PR3 (docs/API contract) |
| D3 | Re-export contract for `routes/diagnostics/__init__.py` | **Enumerate explicitly** in plan with `__all__` |
| D4 | PR2 cache shape | **SUPERSEDED by D9** — original answer was inadequate; codex caught deeper issues |
| D5 | Shared `_network.read_ssid` return type | **`str \| None`** — adapt status.py callers to `ssid or "—"` at render boundary; keep `_wifi_ssid` as a thin alias per F5 |
| D6 | Time-pin in anomaly tests | **stdlib monkeypatch.setattr** — no new freezegun dep |
| D7 | Outside voice | **Ran codex** — caught 12 issues, all verified |
| D8 | Monkeypatch + re-export trap (codex F1+F2+F4) | **Update tests to patch actual binding sites** — drop the "zero modifications" promise |
| D9 | PR2 cache fate (codex F7+F8+F9, supersedes D4) | **Drop PR2 P2 entirely** — subprocess cache already does the heavy lifting; tuple cache had 3 correctness traps for unmeasured perf win |
| D10 | get_logs asc+limit semantics (codex F11) | **`order` is post-filter on newest-N** — `get_logs(limit=N, order='asc')` = take N newest, reorder ascending |

**Plus mechanical fixes from codex absorbed directly (no fork):**

- F3: Files moving into `routes/diagnostics/` need import depth +1 (`from ..log_buffer` → `from ...log_buffer`)
- F5: Retain `_wifi_ssid` as a thin alias in `routes/status.py` for `tests/test_control_server.py:2525` compat
- F6: `_network.read_ssid(ttl: float = STATUS_SUBPROC_TTL_S)` — status passes default, diagnostics passes `DIAG_SUBPROC_TTL_S`
- F10: `atexit.register(_shutdown_pool)` where `_shutdown_pool` dereferences the current `_JOURNAL_POOL` global (not bound-method capture)

21 items, 3 PRs.

## Scope

21 informational items deferred from PR2 (#418) /review. All hygiene/perf/test/docs — no correctness bugs (yet — the plan itself had to avoid creating some). Categories:

- Maintainability: 5 items (was 6 — M6 dropped per D1)
- Performance: 3 items (was 5 — P5 dropped per D1, P2 dropped per D9)
- Testing: 7 items
- API contract: 4 items
- Plus 1 "envelope flattening" decision item

## PR strategy (locked)

```
PR1: tests + refactor (bundled)
├── New tests against new package layout
├── Test files updated to patch actual binding sites (D8)
├── routes/diagnostics.py → routes/diagnostics/ package
├── Relative-import depth +1 in all moved files (F3)
├── New helpers: control_server/_format.py, control_server/_network.py
├── _wifi_ssid alias retained in status.py (F5)
└── Docstring refresh (M5)

PR2: performance (slimmer post-D9)
├── Module-level ThreadPoolExecutor + reset_for_tests (F10-aware atexit)
├── handler.snapshot() single-lock method
└── get_logs(order='asc') as post-filter on limit (D10)

PR3: docs + API contract
├── SSE error-frame shape docstring
├── section_order ↔ anomalies invariant docstring
├── capacity-exceeded SSE wire event docstring
└── Envelope shape decision (document, do not flatten)
```

---

## PR1: tests + refactor

**Branch:** `refactor/419-pr1-tests-and-package`

### Re-export contract (per D3, expanded for codex F1)

`routes/diagnostics/__init__.py` MUST re-export everything existing tests reach into. Verified via grep across all `tests/test_*.py`. Note: re-export alone does NOT make `monkeypatch.setattr(diagnostics, "X", ...)` redirect bindings inside `_sse.py` (D8) — that requires patching the actual binding sites. Re-export is for plain imports like `from control_server.routes.diagnostics import collect_diagnostics`.

```python
# routes/diagnostics/__init__.py
from ._collectors import (
    # subprocess + cache
    cached_subprocess,
    _lazy_cache, _lazy_cache_lock,
    # public API
    collect_diagnostics, schema_keys,
    # per-row readers
    _read_iface, _read_ssid, _read_lan_ip, _read_gateway, _read_signal_dbm,
    _read_timezone, _read_kernel_release, _read_cpu_temp_c,
    _read_recent_log_entries,
    _batched_is_active, _batched_journal_tails,
    # constants
    DIAG_UNITS, DIAG_JOURNAL_LINES_PER_UNIT, DIAG_SUBPROC_TTL_S,
    SECTION_IDS, PRIVACY_POLICY,
)
from ._anomalies import _compute_anomalies, _recent_logs_contain_error
from ._copy_payload import build_copy_payload
from ._sse import (
    bp,
    _sse_registry, _register_sse, _unregister_sse,
    _sse_format, _generate_sse,
)

__all__ = [
    # Public route surface
    "bp",
    "collect_diagnostics", "build_copy_payload", "schema_keys",
    # Subprocess + cache (tests reach in)
    "cached_subprocess", "_lazy_cache", "_lazy_cache_lock",
    # Anomaly engine
    "_compute_anomalies", "_recent_logs_contain_error",
    # Per-row readers
    "_read_iface", "_read_ssid", "_read_lan_ip", "_read_gateway",
    "_read_signal_dbm", "_read_timezone", "_read_kernel_release",
    "_read_cpu_temp_c", "_read_recent_log_entries",
    "_batched_is_active", "_batched_journal_tails",
    # SSE machinery (tests reach in)
    "_sse_registry", "_register_sse", "_unregister_sse",
    "_sse_format", "_generate_sse",
    # Constants
    "DIAG_UNITS", "DIAG_JOURNAL_LINES_PER_UNIT", "DIAG_SUBPROC_TTL_S",
    "SECTION_IDS", "PRIVACY_POLICY",
]
```

PR1 adds `tests/test_control_server_diagnostics_reexport.py` — single test that does every import and asserts `__all__` matches.

### Test-modification policy (per D8, supersedes earlier acceptance)

PR1 EXPLICITLY updates 5-10 existing test files to patch the new binding sites. The Python rule: "patch where the name is LOOKED UP, not where it is DEFINED."

Affected files + patches:

| File | Today | Update to |
|------|-------|-----------|
| `tests/test_control_server_diagnostics.py:438` | `setattr(diag_mod, "collect_diagnostics", ...)` | `setattr("control_server.routes.diagnostics._sse.collect_diagnostics", ...)` (route's binding) |
| `tests/test_control_server_diagnostics.py:468` | same pattern | same fix |
| `tests/test_control_server_diagnostics.py:551` | `setattr(diagnostics, "_batched_journal_tails", ...)` | `setattr("control_server.routes.diagnostics._collectors._batched_journal_tails", ...)` |
| `tests/test_control_server_logs_routes.py:*` | `diagnostics._sse_registry` direct access | unchanged — direct attribute READS work fine on the re-exported name |

Audit pattern: any `monkeypatch.setattr(diag_mod_or_diagnostics, "<name>", ...)` where `<name>` is called from inside a submodule needs the actual-binding-site path.

### Relative-import depth (per F3)

Every file moved from `routes/` to `routes/diagnostics/` needs `..` → `...` for ancestor-package imports:

| Old | New |
|-----|-----|
| `from ..log_buffer import ...` | `from ...log_buffer import ...` |
| `from .._env import ...` | `from ..._env import ...` |
| `from .._subprocess import ...` | `from ..._subprocess import ...` |
| `from .._diagnostics_privacy import ...` | `from ..._diagnostics_privacy import ...` |
| `from .._redaction import ...` | `from ..._redaction import ...` |
| `from .status import _resolve_last_update` | `from ..status import _resolve_last_update` |

PR1 acceptance includes `python3 -c "from control_server.routes.diagnostics import *"` succeeds without ImportError.

### Refactor tasks

1. **Split `routes/diagnostics.py` → `routes/diagnostics/` package** (M1)
   - `routes/diagnostics/__init__.py` — re-exports per contract above + bp registration
   - `routes/diagnostics/_collectors.py` — per-row readers + `collect_diagnostics` + subprocess cache + schema
   - `routes/diagnostics/_anomalies.py` — anomaly thresholds + `_compute_anomalies` + `_recent_logs_contain_error`
   - `routes/diagnostics/_copy_payload.py` — `build_copy_payload`
   - `routes/diagnostics/_sse.py` — bp + SSE registry + generator + ALL routes (`/api/diagnostics`, `/diagnostics`, `/api/logs`, `/api/logs/stream`)
   - DIAG_UNITS / DIAG_JOURNAL_LINES_PER_UNIT / DIAG_SUBPROC_TTL_S live in `_collectors.py`
   - Imports updated per F3 depth fix

2. **Extract `control_server/_format.py`** (M2)
   - Move `_format_uptime` (byte-identical between status + diagnostics)
   - Both call sites import from `_format`

3. **Extract `control_server/_network.py`** (M3, per D5+F6)
   ```python
   # control_server/_network.py
   STATUS_SUBPROC_TTL_S = 5.0  # status default

   def read_ssid(ttl: float = STATUS_SUBPROC_TTL_S) -> str | None: ...
   def read_lan_ip(ttl: float = STATUS_SUBPROC_TTL_S) -> str | None: ...
   def read_gateway(ttl: float = STATUS_SUBPROC_TTL_S) -> str | None: ...
   def read_signal_dbm(ttl: float = STATUS_SUBPROC_TTL_S) -> int | None: ...
   ```
   - status.py: keeps `_wifi_ssid` as a thin alias (`def _wifi_ssid() -> str: return read_ssid() or ""`) — preserves test_control_server.py:2525 monkeypatch surface (F5)
   - `_collectors.py`: calls `read_ssid(ttl=DIAG_SUBPROC_TTL_S)` etc.

4. **Document `os.environ` ↔ `current_app.config` precedence** (M4)
   - Module docstring in `routes/diagnostics/__init__.py`
   - Inline comment in `_collectors.py` at the precedence site

5. **Refresh stale docstring** (M5)
   - Kill "minimal HTML placeholder" line
   - Kill "PR3 lands /api/logs*" references
   - Full module docstring audit

### Test tasks

1. **Per-row reader tests with monkeypatched `cached_subprocess`** (T1)
   - New file `tests/test_control_server_diagnostics_readers.py`
   - Patches `control_server.routes.diagnostics._collectors.cached_subprocess` (actual binding) — and a parallel test imports via the re-exported path to confirm both work
   - Cover: `_read_iface`, `_read_ssid`, `_read_signal_dbm`, `_read_timezone`, `_read_kernel_release`, `_batched_is_active` — 3 cases each (18 total)

2. **Pin clock for time-based anomaly tests via monkeypatch** (T2, per D6)
   - `monkeypatch.setattr("control_server.routes.diagnostics._anomalies.datetime", FakeDatetime(...))`
   - `monkeypatch.setattr("control_server.routes.diagnostics._collectors.time", FakeTimeModule(...))`
   - Threshold-boundary tests (at, +1ms, −1ms)

3. **Per-row failure-path tests for malformed sources** (T3) — same as before

4. **`build_copy_payload` edge-case tests** (T4) — same as before

5. **Sid-shape parametrize extension** (T5) — same as before

6. **Replace `cutoff = 0` hardcode in `test_backfill_then_hello`** (T6) — same as before

7. **`/api/diagnostics` 500-envelope test** (T7) — same as before

8. **Tighten deny-list positive control via env.sh** (T8) — same as before

**Acceptance for PR1:**
- Lint clean
- All NEW tests pass
- All EXISTING tests pass (some test files have been updated per D8 to patch the new binding sites)
- `from control_server.routes.diagnostics import *` succeeds
- Re-export test asserts `__all__` matches

---

## PR2: performance (slimmer post-D9)

**Branch:** `perf/419-pr2-diagnostics`

1. **Module-level `ThreadPoolExecutor`** (P1, F10-aware)
   ```python
   # routes/diagnostics/_collectors.py
   _JOURNAL_POOL: ThreadPoolExecutor | None = None

   def _get_journal_pool() -> ThreadPoolExecutor:
       global _JOURNAL_POOL
       if _JOURNAL_POOL is None:
           _JOURNAL_POOL = ThreadPoolExecutor(
               max_workers=4, thread_name_prefix="diag-journal"
           )
       return _JOURNAL_POOL

   def _shutdown_pool() -> None:
       global _JOURNAL_POOL
       if _JOURNAL_POOL is not None:
           _JOURNAL_POOL.shutdown()
           _JOURNAL_POOL = None

   atexit.register(_shutdown_pool)

   def reset_for_tests() -> None:
       _shutdown_pool()  # mirrors log_buffer.reset_for_tests
   ```
   - F10 fix: `atexit` registers a function that dereferences the current global, NOT a bound method that captured the original executor

2. **`handler.snapshot()` single-lock method** (P3)
   ```python
   def snapshot(self, level: str | None = None) -> tuple[list[LogEntry], int, int]:
       """Atomic snapshot under one lock acquire."""
       with self._lock:
           return (
               self._collect_entries_unlocked(level),
               self._total_count_unlocked(level),
               self._latest_seq_unlocked(),
           )
   ```
   - `GET /api/logs` switches to `snapshot()`
   - Tests: empty buffer, populated buffer, concurrent emit (thread test)

3. **`order='asc'` param on `get_logs()`** (P4, per D10)
   - **Semantics:** `get_logs(limit=N, order='asc')` returns the **N newest** entries, sorted oldest-first (in chronological order). Limit ALWAYS means "newest N"; order is a post-filter on that selection.
   - Default stays `'desc'` (newest-first) for back-compat.
   - `_generate_sse` backfill: `entries = handler.get_logs(limit=BACKFILL_N, order='asc')` — no `reversed()` needed.
   - Tests:
     - `get_logs(limit=4, order='asc')` on 10-entry buffer → entries[6..9] in chrono order
     - default desc unchanged
     - asc + level filter combo
     - explicit docstring contract test

**Acceptance:** all PR1 tests pass + new perf tests. ~5 new test cases.

---

## PR3: docs + API contract

**Branch:** `docs/419-pr3-api-contract`

1. **Document SSE error-frame shape** (A1)
2. **Document `section_order` ↔ `anomalies` invariant** (A2)
3. **Document `capacity-exceeded` SSE wire event** (A3) — verify PR3 client backoff
4. **Keep `/api/diagnostics` envelope wrapped** (A4) — document why in `errors.py`

**Acceptance:** docstring-only. No behavior change.

---

## Cross-cutting risks (post-decisions)

- **Monkeypatch binding-site updates (D8):** every test that monkeypatched `diag_mod.X` must now patch `diag_mod._collectors.X` or `diag_mod._sse.X`. PR1 explicitly audits + updates.
- **Relative-import depth (F3):** every moved file needs `..` → `...`. PR1 acceptance includes `import *` smoke test.
- **`_wifi_ssid` alias (F5):** preserved as a 1-line shim so existing test stays valid.
- **Cache TTL parameterized (F6):** `read_ssid(ttl=...)` so status + diagnostics keep their independent cache freshness.
- **PR2 perf is now small (D9):** the heavy cache work is dropped; PR2 has only the safe wins (pool hoist, snapshot, asc-order).

## NOT in scope

- Pre-#337 settings IA work (already shipped)
- Any change to `_diagnostics_privacy.py` redaction chain (locked in PR1 of EPIC #416)
- Diagnostics drawer client JS (PR3 of #416 shipped; #419 is server-side cleanup only)
- Flattening the `/api/diagnostics` envelope (would break the just-shipped PR3 client)
- PR2 P2 (the tuple cache for `(values, anomalies, copy_payload)`) — dropped per D9. If a real CPU bottleneck is later measured on a Pi Zero 2W, a targeted cache can be added with measurement.
- Distribution: no new artifact; refactoring only

## What already exists

- `routes/status.py:360` — `_format_uptime` (extracted to `_format.py`)
- `routes/status.py:125` — `_wifi_ssid` (kept as alias for `read_ssid()`)
- `src/control_server/log_buffer.py:390` — `reset_for_tests()` pattern (mirrored in `_collectors.py`)
- `src/control_server/_subprocess.py` — `cached_subprocess` (still the single subprocess-dedup point)
- `src/control_server/_diagnostics_privacy.py` — PRIVACY_POLICY (untouched)
- All existing tests under `tests/test_*.py` — some get monkeypatch-path updates per D8; most untouched

## Validation per PR

- Lint: `ruff check src/ image-gen/ tests/`
- Python tests: `python3 -m pytest tests/ --ignore=tests/test_eink_display.py -q`
- JS tests: `npm run test:js` (PR2 may touch log_buffer.py; verify SSE client JS still works)
- `/review` before each merge

## Failure modes

| Path | Failure mode | Test? | Error handling? | User-visible? |
|------|--------------|-------|-----------------|---------------|
| `read_ssid()` returns None (nmcli missing) | None propagates | ✅ PR1 T1 | status alias maps to ""; diagnostics keeps None | "—" via render | 
| `_JOURNAL_POOL` shutdown after atexit fired | RuntimeError on submit | ⚠ Add catch+empty-list fallback in `_batched_journal_tails` | yes | Silent empty section (acceptable) |
| Monkeypatched test miss after D8 audit | False-positive test pass | ✅ PR1 re-export test + audit | n/a | Test infra only |
| `get_logs(asc, limit=N)` on empty buffer | Returns [] | ✅ explicit test | n/a | n/a |
| `handler.snapshot()` while emit holds lock | Brief contention (microseconds) | ✅ thread test | n/a | None |
| Relative-import depth wrong | ImportError at startup | ✅ `import *` smoke test in PR1 acceptance | yes — service won't start | Catastrophic (caught at deploy) |

**No critical gaps.** (Restated honestly now — the codex pass caught the gaps the inside review missed; this revised plan addresses them.)

## Worktree parallelization strategy

Sequential implementation, no parallelization opportunity. PR2 depends on PR1's package layout; PR3 docs touch PR1+PR2 surfaces.

## Implementation Tasks

```
PR1 — tests + refactor (largest):
- [ ] T1 (P1, human: ~3.5h / CC: ~30min) — routes/diagnostics/ package — Split + relative-import depth fix per F3 + re-export contract per D3
- [ ] T2 (P1, human: ~30min / CC: ~5min) — control_server/_format.py — Extract _format_uptime
- [ ] T3 (P1, human: ~45min / CC: ~10min) — control_server/_network.py — Extract read_* with TTL param per F6; _wifi_ssid alias per F5
- [ ] T4 (P1, human: ~1h / CC: ~10min) — tests/test_control_server_*.py — Audit + update monkeypatch sites per D8
- [ ] T5 (P2, human: ~20min / CC: ~5min) — tests/test_control_server_diagnostics_reexport.py — Re-export contract test
- [ ] T6 (P1, human: ~2h / CC: ~15min) — tests/test_control_server_diagnostics_readers.py — 18 reader tests
- [ ] T7 (P1, human: ~1h / CC: ~10min) — tests/test_control_server_diagnostics_anomalies.py — Threshold tests with monkeypatched time
- [ ] T8 (P2, human: ~1h / CC: ~10min) — tests/test_control_server_diagnostics_failures.py — Malformed-source paths (5)
- [ ] T9 (P2, human: ~45min / CC: ~10min) — tests/test_control_server_diagnostics.py — Extend build_copy_payload + sid-shape
- [ ] T10 (P2, human: ~15min / CC: ~3min) — tests/test_control_server_logs_routes.py — Replace cutoff=0 hardcode
- [ ] T11 (P2, human: ~15min / CC: ~3min) — tests/test_control_server_diagnostics.py — /api/diagnostics 500-envelope test
- [ ] T12 (P2, human: ~30min / CC: ~5min) — tests/test_diagnostics_no_secrets.py — Tighten deny-list positive control
- [ ] T13 (P3, human: ~20min / CC: ~5min) — All touched modules — Docstring refresh (M5, M4)

PR2 — perf (slimmer):
- [ ] T14 (P1, human: ~45min / CC: ~10min) — routes/diagnostics/_collectors.py — Module-level _JOURNAL_POOL + F10-aware atexit + reset_for_tests
- [ ] T15 (P1, human: ~1h / CC: ~15min) — log_buffer.py — handler.snapshot() + get_logs(order='asc') as post-filter on newest-N (D10)
- [ ] T16 (P1, human: ~1h / CC: ~15min) — tests/test_control_server_perf.py — ~5 new tests (pool, snapshot, asc-order semantics)

PR3 — docs + API:
- [ ] T17 (P3, human: ~1h / CC: ~10min) — routes/diagnostics/_sse.py + __init__.py + errors.py — A1-A4 docstrings
```

Total: 17 build tasks. Down from 21 items (some merged, some absorbed mechanically).

## TODOS.md updates

None — this issue IS the TODO bucket. Two new follow-ups created by D9:
- **Future: measure `/api/diagnostics` CPU on Pi Zero 2W; if >50ms, add a targeted cache with real numbers.** (Filed as a comment on #419 itself, not a new issue.)

## Completion summary

- Step 0: Scope reduced per D1+D2 (24 items → 21 items, 4 PRs → 3 PRs)
- Architecture Review: 2 issues found, both resolved (D3, D4→superseded by D9)
- Code Quality Review: 1 issue found, resolved (D5)
- Test Review: coverage diagram produced, ~25 PLANNED branches + 0 GAP
- Performance Review: 0 new issues (P1+P3+P4 in plan; P2 dropped per D9)
- Outside voice (codex): 12 findings, all verified; 3 forks resolved (D8, D9, D10), 4 mechanical fixes absorbed (F3, F5, F6, F10), 2 already covered (F4, F1 via D8/D3 expanded)
- NOT in scope: written
- What already exists: written
- Failure modes: 0 critical gaps (the codex pass de-risked the plan)
- Parallelization: sequential, no opportunity
- Lake Score: 10/10 recommendations chose complete option (D1+D2+D3+D4+D5+D6+D7+D8+D9+D10)

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found→resolved | 12 findings, all verified, 3 absorbed as decisions (D8/D9/D10) + 4 mechanical fixes + 2 already covered |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 15 issues found (3 inside + 12 codex), 0 critical gaps remaining, 10 decisions locked |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a — no UI scope |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** caught 5+ critical issues the inside review missed (monkeypatch-trap, anomaly-cache-staleness, asc+limit semantic trap, atexit-bound-method bug, incomplete `__all__`). All resolved or absorbed mechanically.
- **CROSS-MODEL:** strong agreement on the 3 absorbed decisions (D8/D9/D10). No unresolved tensions.
- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement. Start with PR1 on branch `refactor/419-pr1-tests-and-package`. No design review needed (server-side cleanup only).

