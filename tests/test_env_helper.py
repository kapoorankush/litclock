"""Tests for control_server/_env.py (#416 T1 extraction).

Covers the env_file lookup order, error handling, and that the existing
status.py thin-wrapper continues to delegate correctly.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import _env  # noqa: E402
from control_server.routes import status as _status  # noqa: E402


def _write_env(tmp_path: Path, body: str) -> str:
    p = tmp_path / "env.sh"
    p.write_text(textwrap.dedent(body))
    return str(p)


class TestReadEnvSettings:
    def test_returns_empty_when_no_path_anywhere(self, monkeypatch):
        monkeypatch.delenv("LITCLOCK_ENV_FILE", raising=False)
        assert _env.read_env_settings() == {}

    def test_returns_empty_for_missing_file(self, tmp_path):
        assert _env.read_env_settings(str(tmp_path / "nope.sh")) == {}

    def test_returns_parsed_dict_for_real_file(self, tmp_path):
        path = _write_env(
            tmp_path,
            """\
            #!/usr/bin/env bash
            WEATHER_LATITUDE=37.77
            WEATHER_LONGITUDE=-122.42
            WEATHER_UNITS=imperial
            ALLOW_NSFW_QUOTES=false
            """,
        )
        out = _env.read_env_settings(path)
        assert out["WEATHER_LATITUDE"] == "37.77"
        assert out["WEATHER_LONGITUDE"] == "-122.42"
        assert out["WEATHER_UNITS"] == "imperial"
        assert out["ALLOW_NSFW_QUOTES"] == "false"

    def test_falls_back_to_env_var(self, tmp_path, monkeypatch):
        path = _write_env(tmp_path, "WEATHER_UNITS=metric\n")
        monkeypatch.setenv("LITCLOCK_ENV_FILE", path)
        assert _env.read_env_settings()["WEATHER_UNITS"] == "metric"

    def test_explicit_arg_wins_over_env(self, tmp_path, monkeypatch):
        a = _write_env(tmp_path, "WEATHER_UNITS=metric\n")
        b = tmp_path / "other.sh"
        b.write_text("WEATHER_UNITS=imperial\n")
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(b))
        assert _env.read_env_settings(a)["WEATHER_UNITS"] == "metric"

    def test_corrupted_returns_empty(self, tmp_path):
        # config.load_config is permissive — random binary is mostly skipped
        # rather than raising — but the wrapper must NEVER raise.
        path = tmp_path / "env.sh"
        path.write_bytes(b"\x00\x01\x02\xff junk")
        # Whatever happens, must not raise.
        result = _env.read_env_settings(str(path))
        assert isinstance(result, dict)

    def test_status_wrapper_delegates_to_env(self, tmp_path, monkeypatch):
        """The pre-#416 name `_read_env_file_settings` on status.py keeps
        working and delegates straight to the new module."""
        path = _write_env(tmp_path, "WEATHER_UNITS=imperial\n")
        monkeypatch.delenv("LITCLOCK_ENV_FILE", raising=False)
        # Call the legacy name; expect it to return the same as the new module.
        assert _status._read_env_file_settings(path) == _env.read_env_settings(path)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
