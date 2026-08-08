"""Dump-coverage rejection tests for tools/validate_measurement.py (litclock-dev#537).

A truncated or weakened dump must be refused BEFORE scoring — otherwise the
check iterates whatever the dump contains and prints a meaningless 100%.
These run the tool as a subprocess (tools/ is not on pythonpath) and need
neither PHP nor freetype: coverage validation fails before any measurement.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "validate_measurement.py"

# import the tool for its constants (stdlib-only at module level)
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("validate_measurement", TOOL)
_vm = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_vm)


def _run_check(dump_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "check", "--dump", str(dump_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write_dump(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "dump.json.gz"
    with gzip.open(p, "wt", encoding="utf-8") as gz:
        json.dump(payload, gz)
    return p


def _minimal_payload(n_strings: int, faces: dict, sizes: range) -> dict:
    # edge strings included so the content checks pass and the test hits
    # the SPECIFIC coverage check it targets
    strings = (list(_vm.EDGE_STRINGS) + [f"w{i}" for i in range(n_strings)])[:n_strings]
    return {
        "meta": {"gd": "2.3.3", "freetype_py_binds": "2.13.2", "fonts": faces, "measurements": 0},
        "strings": strings,
        "boxes": {tag: {str(fs): [[0, 1, 0, 1]] * n_strings for fs in sizes} for tag in faces},
    }


FULL_FACES = {
    "REG": "Literata72pt-ExtraLight.ttf",
    "BOLD": "Literata72pt-Black.ttf",
    "CRED": "Literata72pt-SemiBoldItalic.ttf",
}


def test_truncated_string_sample_rejected(tmp_path) -> None:
    dump = _write_dump(tmp_path, _minimal_payload(10, FULL_FACES, range(18, 111)))
    proc = _run_check(dump)
    assert proc.returncode != 0
    assert "expected the full" in proc.stderr + proc.stdout


def test_missing_face_rejected(tmp_path) -> None:
    faces = {k: v for k, v in FULL_FACES.items() if k != "CRED"}
    dump = _write_dump(tmp_path, _minimal_payload(822, faces, range(18, 111)))
    proc = _run_check(dump)
    assert proc.returncode != 0
    assert "faces" in proc.stderr + proc.stdout


def test_missing_sizes_rejected(tmp_path) -> None:
    dump = _write_dump(tmp_path, _minimal_payload(822, FULL_FACES, range(18, 45)))
    proc = _run_check(dump)
    assert proc.returncode != 0
    assert "range" in proc.stderr + proc.stdout


def test_short_row_rejected(tmp_path) -> None:
    payload = _minimal_payload(822, FULL_FACES, range(18, 111))
    payload["boxes"]["REG"]["44"] = payload["boxes"]["REG"]["44"][:5]
    dump = _write_dump(tmp_path, payload)
    proc = _run_check(dump)
    assert proc.returncode != 0
    assert "rows" in proc.stderr + proc.stdout
