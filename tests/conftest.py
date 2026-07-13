"""Shared fixtures for LitClock tests."""

import json
import os
import shlex
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_env_file(tmp_path):
    """Create a temporary env.sh file with typical content."""
    env_file = tmp_path / "env.sh"
    env_file.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            WEATHER_LATITUDE=0
            WEATHER_LONGITUDE=0
            WEATHER_UNITS=imperial
            OPENWEATHERMAP_APIKEY=
            ALLOW_NSFW_QUOTES=false
        """)
    )
    return str(env_file)


@dataclass
class SandboxResult:
    """Result of running a shell script in the sandbox."""

    completed: subprocess.CompletedProcess
    calls: list  # list of {"cmd": str, "args": [str, ...]} entries, in call order

    @property
    def stdout(self) -> str:
        return self.completed.stdout

    @property
    def stderr(self) -> str:
        return self.completed.stderr

    @property
    def returncode(self) -> int:
        return self.completed.returncode

    def calls_for(self, cmd: str) -> list:
        """All recorded invocations of a given stubbed command."""
        return [c for c in self.calls if c["cmd"] == cmd]


class ScriptSandbox:
    """A sandbox for running shell scripts with stubbed external commands.

    Each stub logs its invocation (command name + args) to a JSONL file so
    tests can assert on call order and arguments without having to capture
    stdout. Stubs default to exit 0 with empty stdout but can be customized.
    """

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "install"
        self.root.mkdir(parents=True, exist_ok=True)
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir(parents=True, exist_ok=True)
        self.call_log = tmp_path / "calls.jsonl"
        self.call_log.touch()

    def stub(self, name: str, exit_code: int = 0, stdout: str = "", stderr: str = "") -> Path:
        """Install a PATH shim for `name` that logs its args and exits."""
        script = self.bindir / name
        # Shell-escape the strings for safe embedding.
        esc_stdout = shlex.quote(stdout)
        esc_stderr = shlex.quote(stderr)
        esc_log = shlex.quote(str(self.call_log))
        esc_name = shlex.quote(name)
        script.write_text(
            textwrap.dedent(f"""\
                #!/bin/bash
                # Build JSON array of args
                args_json="["
                first=1
                for a in "$@"; do
                    esc=$(printf '%s' "$a" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
                    if [[ $first -eq 1 ]]; then
                        args_json="${{args_json}}${{esc}}"
                        first=0
                    else
                        args_json="${{args_json}},${{esc}}"
                    fi
                done
                args_json="${{args_json}}]"
                printf '{{"cmd": %s, "args": %s}}\\n' '"'{esc_name}'"' "$args_json" >> {esc_log}
                if [[ -n {esc_stdout} ]]; then printf '%s' {esc_stdout}; fi
                if [[ -n {esc_stderr} ]]; then printf '%s' {esc_stderr} >&2; fi
                exit {exit_code}
                """)
        )
        script.chmod(0o755)
        return script

    def write_file(self, relpath: str, content: str, mode: int = 0o644) -> Path:
        """Write a file under the sandbox root and return its path."""
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return path

    def run(self, script_path: Path, args=None, env=None, cwd=None) -> SandboxResult:
        """Run a shell script with sandbox PATH and return the result + call log."""
        args = args or []
        run_env = {
            "PATH": f"{self.bindir}:/usr/bin:/bin",
            "HOME": str(self.root),
            "LITCLOCK_DIR": str(self.root),
        }
        if env:
            run_env.update(env)
        completed = subprocess.run(
            ["bash", str(script_path), *args],
            env=run_env,
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
        )
        calls = []
        for line in self.call_log.read_text().splitlines():
            line = line.strip()
            if line:
                calls.append(json.loads(line))
        return SandboxResult(completed=completed, calls=calls)


@pytest.fixture
def script_sandbox(tmp_path):
    """Yields a ScriptSandbox for shell-script execution tests."""
    return ScriptSandbox(tmp_path)


@pytest.fixture
def repo_root():
    """Absolute Path to the repository root."""
    return REPO_ROOT


@pytest.fixture
def clean_env():
    """Remove LOG_LEVEL from environment, restore after test."""
    old = os.environ.pop("LOG_LEVEL", None)
    yield
    if old is not None:
        os.environ["LOG_LEVEL"] = old
    else:
        os.environ.pop("LOG_LEVEL", None)


@pytest.fixture(autouse=True)
def _reset_setup_server_state():
    """Clear setup_server's module-level connect-flow state between tests (#355).

    The WiFi connect handler spawns a daemon thread that writes
    ``WIFI_CONNECT_ERROR`` / ``WIFI_CONNECT_IN_FLIGHT`` asynchronously, so
    a late write from a prior test can leak into the next test's assertions
    under specific pytest orderings (caught on PR #353 CI run 25968703096).

    We import setup_server lazily and skip cleanly on ModuleNotFoundError
    so this fixture is a no-op when ``src`` is off ``sys.path`` (e.g. running
    a test file directly outside the pytest harness). We deliberately do NOT
    catch the broader ``ImportError`` — a syntax error or broken transitive
    import in ``setup_server.py`` should surface loudly, not silently disable
    test isolation. Reset runs both before and after the test for
    belt-and-suspenders isolation.
    """
    try:
        import setup_server
    except ModuleNotFoundError:
        yield
        return

    setup_server.reset_state()
    try:
        yield
    finally:
        setup_server.reset_state()
