"""In-process tests for src/literary_clock.py M2 additions (PR #245).

Complements tests/test_literary_clock_dry_run.py (subprocess smoke tests
for the --dry-run contract). These exercise the pure helpers directly:

- get_current_quote() pure function (locked decision A7).
- _write_status_file() atomic write contract (OV3).
- _composite_settings_qr() geometry (A6, paste at x=716, y=2).
- _stamp_update_failed_glyph() relocation (A6, x=4, y=4).

Tests skip on interpreters without PIL/qrcode (dev box without venv).
"""

from __future__ import annotations

import json
import os
import sys
import time as _time
from datetime import datetime
from pathlib import Path

import pytest

# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# PIL + qrcode are project deps; the dry-run test file already gates on
# them. Mirror that gate so this file works under bare system python too.
_HAS_DEPS = True
try:
    import PIL  # noqa: F401
    import qrcode  # noqa: F401
except ImportError:
    _HAS_DEPS = False

pytestmark = pytest.mark.skipif(not _HAS_DEPS, reason="literary_clock deps (PIL / qrcode) not in this interpreter")

if _HAS_DEPS:
    import literary_clock  # noqa: E402


# ---------- get_current_quote() — pure function (A7) ----------


class TestGetCurrentQuoteShape:
    def test_returns_none_when_no_image_for_minute(self, monkeypatch, tmp_path) -> None:
        """No PNG matches the current minute → caller falls back to
        time-as-text. Must NOT raise."""
        # Point PROJECT_ROOT at an empty tmp dir so the glob misses.
        monkeypatch.setattr(literary_clock, "PROJECT_ROOT", str(tmp_path))
        result = literary_clock.get_current_quote(now=datetime(2026, 4, 28, 8, 42))
        assert result is None

    def test_returns_metadata_dict_when_image_present(self, monkeypatch, tmp_path) -> None:
        """When a quote_HHMM_*_credits.png exists, the metadata dict is
        populated with the corpus-derived author/title/quote text plus
        the image_path the e-ink will paste."""
        # Build the expected glob layout: {project_root}/images/metadata/
        meta_dir = tmp_path / "images" / "metadata"
        meta_dir.mkdir(parents=True)
        png = meta_dir / "quote_0842_0_credits.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header — never opened here

        monkeypatch.setattr(literary_clock, "PROJECT_ROOT", str(tmp_path))

        # Stub the corpus lookup so this test doesn't depend on the bundled CSV.
        monkeypatch.setattr(
            literary_clock.quote_corpus,
            "lookup_by_filename",
            lambda fn: {
                "time": "08:42",
                "timestring": "twenty-three minutes to nine",
                "quote": "test quote",
                "title": "Test Title",
                "author": "Test Author",
            },
        )

        result = literary_clock.get_current_quote(now=datetime(2026, 4, 28, 8, 42))
        assert result is not None
        assert result["quote"] == "test quote"
        assert result["author"] == "Test Author"
        assert result["title"] == "Test Title"
        assert result["time"] == "08:42"
        assert result["image_path"].endswith("quote_0842_0_credits.png")
        assert isinstance(result["picked_at"], float)
        # picked_at should be very close to now() — within a few seconds.
        assert abs(result["picked_at"] - _time.time()) < 5

    def test_filters_nsfw_when_disallowed(self, monkeypatch, tmp_path) -> None:
        meta_dir = tmp_path / "images" / "metadata"
        meta_dir.mkdir(parents=True)
        # Only NSFW image present — disallowed → None.
        (meta_dir / "quote_0842_0_nsfw_credits.png").write_bytes(b"\x89PNG")
        monkeypatch.setattr(literary_clock, "PROJECT_ROOT", str(tmp_path))
        result = literary_clock.get_current_quote(now=datetime(2026, 4, 28, 8, 42), allow_nsfw=False)
        assert result is None

    def test_includes_nsfw_when_allowed(self, monkeypatch, tmp_path) -> None:
        meta_dir = tmp_path / "images" / "metadata"
        meta_dir.mkdir(parents=True)
        (meta_dir / "quote_0842_0_nsfw_credits.png").write_bytes(b"\x89PNG")
        monkeypatch.setattr(literary_clock, "PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(
            literary_clock.quote_corpus,
            "lookup_by_filename",
            lambda fn: {"time": "08:42", "quote": "x", "title": "y", "author": "z"},
        )
        result = literary_clock.get_current_quote(now=datetime(2026, 4, 28, 8, 42), allow_nsfw=True)
        assert result is not None
        assert "_nsfw_" in result["image_path"]


# ---------- _write_status_file — atomic publish (OV3) ----------


class TestWriteStatusFile:
    def test_writes_payload_with_quote_meta(self, monkeypatch, tmp_path) -> None:
        target = tmp_path / "status.json"
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(target))

        meta = {
            "quote": "It was the best of times.",
            "author": "Charles Dickens",
            "title": "A Tale of Two Cities",
            "image_path": "/dummy/path.png",
            "time": "08:42",
            "picked_at": 1234567890.0,
        }
        literary_clock._write_status_file(meta, datetime(2026, 4, 28, 8, 42))

        assert target.exists()
        payload = json.loads(target.read_text())
        assert payload["time"] == "08:42"
        assert payload["quote"] == "It was the best of times."
        assert payload["author"] == "Charles Dickens"
        assert payload["title"] == "A Tale of Two Cities"
        assert isinstance(payload["picked_at"], float)

    def test_writes_minimal_payload_when_quote_missing(self, monkeypatch, tmp_path) -> None:
        """Empty-bucket fallback path: clock drew the time-as-text but
        the status file should still publish picked_at + time so /api/status
        knows the clock is alive without a quote."""
        target = tmp_path / "status.json"
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(target))

        literary_clock._write_status_file(None, datetime(2026, 4, 28, 8, 42))
        payload = json.loads(target.read_text())
        assert payload["time"] == "08:42"
        assert "picked_at" in payload
        # Quote fields are absent (or empty) — PWA hero shows the
        # "no quote available" empty state rather than rendering blanks.
        assert payload.get("quote", "") == ""

    def test_uses_atomic_replace(self, monkeypatch, tmp_path) -> None:
        """Pin the tempfile + os.replace pattern. Without it, a power loss
        mid-write could leave a torn JSON that /api/status crashes on."""
        target = tmp_path / "status.json"
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(target))

        # Pre-existing content: must remain readable through a write attempt
        # that we'll track. Real os.replace IS atomic; this assertion just
        # pins that we use it.
        target.write_text('{"existing": true}')
        original_replace = os.replace
        replace_calls = []

        def tracking_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        monkeypatch.setattr(os, "replace", tracking_replace)
        literary_clock._write_status_file(
            {"quote": "x", "author": "a", "title": "t", "image_path": "/p", "time": "08:42"},
            datetime(2026, 4, 28, 8, 42),
        )
        assert len(replace_calls) == 1
        # The temp file's name must live in target.parent (so the rename
        # is on the same filesystem and therefore atomic).
        tmp_src, dst = replace_calls[0]
        assert Path(tmp_src).parent == target.parent
        assert Path(dst) == target

    def test_failure_is_swallowed(self, monkeypatch, tmp_path) -> None:
        """A missing /var/run mustn't fail the render. Logs at WARN and
        moves on — the e-ink frame is more important than the status
        file."""
        # Point at a directory that can't be created (a path under a
        # regular file).
        not_a_dir = tmp_path / "not_a_dir"
        not_a_dir.write_text("file, not a dir")
        target = not_a_dir / "status.json"
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(target))

        # Must not raise.
        literary_clock._write_status_file(None, datetime(2026, 4, 28, 8, 42))


# ---------- QR composite + glyph relocation (A6) ----------


class TestQrComposite:
    def test_qr_pasted_at_locked_position(self) -> None:
        """75×75 QR at x=716, y=2 per locked decision A6. Sample interior
        pixels to confirm the QR was actually drawn (not the white background)."""
        from PIL import Image

        image = Image.new(mode="1", size=(800, 480), color=255)
        literary_clock._composite_settings_qr(image)

        # Top-left finder pattern of the QR sits at the paste origin.
        # In QR codes, the finder pattern is a 7×7 dark square — sampling
        # the (0, 0) pixel of the pattern (image coords 716, 2) catches
        # an actual dark module if compositing worked.
        assert image.getpixel((716, 2)) == 0, "QR top-left finder pattern missing"
        # And the bottom-right corner of the QR (image coords 716+74, 2+74)
        # is part of the bottom-left finder pattern → also dark.
        assert image.getpixel((716, 2 + 74)) == 0, "QR bottom-left finder missing"

    def test_qr_does_not_overlap_top_strip_divider(self) -> None:
        """QR ends at y=77 (2+75=77); the top-strip divider lives at y=78.
        Sampling y=78 must be still 255 (no QR pixel) and the runtime then
        draws the divider over the QR-free zone."""
        from PIL import Image

        image = Image.new(mode="1", size=(800, 480), color=255)
        literary_clock._composite_settings_qr(image)
        # The QR stops above y=78. Image was initialized to white (255), and
        # the QR composite shouldn't have written below row 76.
        assert image.getpixel((740, 78)) == 255

    def test_qr_url_fallback_locked_to_plain_http(self) -> None:
        """Pin the QR_URL fallback. #257 dropped TLS (plain HTTP only); #343
        moved control_server to port 80 so the URL carries NO port — a
        recipient scans/types bare `http://litclock.local`. The port is built
        by control_url.control_base_url, which omits `:80`.

        Issue #306: this is the FALLBACK now — the runtime path prefers
        the IP-encoded URL via _resolve_lan_ip() because mDNS is
        unreliable on Android Chrome and many home networks. The
        hostname stays here for the no-network kiosk case."""
        assert literary_clock.QR_URL == "http://litclock.local"
        assert "https://" not in literary_clock.QR_URL, (
            "QR URL must be plain HTTP per #257; control_server has no TLS listener"
        )
        # #343: the port must be OMITTED at 80 — a visible port defeats the change.
        assert "litclock.local:" not in literary_clock.QR_URL, "QR URL must not carry a port at 80"
        assert literary_clock.QR_POSITION == (716, 2)
        assert literary_clock.QR_VERSION == 2
        assert literary_clock.QR_BOX_SIZE == 3


# ---------- LAN IP resolution + IP-encoded QR (#306) ----------


class _StubSocket:
    """Minimal socket stub used to force a deterministic getsockname() result
    without touching the real network. Mirrors only the methods _resolve_lan_ip
    actually calls."""

    def __init__(self, ip: str = "192.168.2.132", *, raise_on: str | None = None) -> None:
        self._ip = ip
        self._raise_on = raise_on
        self.closed = False

    def settimeout(self, _seconds: float) -> None:
        if self._raise_on == "settimeout":
            raise OSError("forced settimeout failure")

    def connect(self, _addr: tuple[str, int]) -> None:
        if self._raise_on == "connect":
            raise OSError("Network is unreachable")

    def getsockname(self) -> tuple[str, int]:
        if self._raise_on == "getsockname":
            raise OSError("forced getsockname failure")
        return (self._ip, 0)

    def close(self) -> None:
        self.closed = True


class TestResolveLanIp:
    def test_returns_ip_on_success(self, monkeypatch) -> None:
        """Connect-trick happy path: socket binds to the egress interface,
        getsockname() returns its IP. No packet actually sent."""

        def fake_socket(family, type_):  # noqa: A002 — mirrors stdlib param name
            assert family == literary_clock.socket.AF_INET
            assert type_ == literary_clock.socket.SOCK_DGRAM
            return _StubSocket(ip="192.168.2.132")

        monkeypatch.setattr(literary_clock.socket, "socket", fake_socket)
        assert literary_clock._resolve_lan_ip() == "192.168.2.132"

    def test_returns_none_on_oserror(self, monkeypatch) -> None:
        """No network / no default route: OSError → None. Caller falls
        back to the mDNS hostname URL so a kiosk Pi without network still
        renders a QR with consistent geometry."""

        def fake_socket(_family, _type):
            return _StubSocket(raise_on="connect")

        monkeypatch.setattr(literary_clock.socket, "socket", fake_socket)
        assert literary_clock._resolve_lan_ip() is None

    def test_returns_none_on_socket_constructor_failure(self, monkeypatch) -> None:
        """A socket() that itself raises (e.g., resource exhaustion) must
        not propagate — render path is best-effort."""

        def fake_socket(_family, _type):
            raise OSError("Too many open files")

        monkeypatch.setattr(literary_clock.socket, "socket", fake_socket)
        assert literary_clock._resolve_lan_ip() is None

    def test_returns_none_for_loopback(self, monkeypatch) -> None:
        """Pi with only `lo` interface up returns 127.x — useless for a
        phone scan, so treat as None and fall back to the hostname URL."""

        def fake_socket(_family, _type):
            return _StubSocket(ip="127.0.1.1")

        monkeypatch.setattr(literary_clock.socket, "socket", fake_socket)
        assert literary_clock._resolve_lan_ip() is None

    def test_returns_none_for_link_local_apipa(self, monkeypatch) -> None:
        """Pi with DHCP-failed self-assigned 169.254.x (APIPA) is on a
        link-local segment phones rarely share. Encoding it would put a
        broken URL on the e-ink. Caller falls back to the mDNS hostname
        which is no worse — flagged by codex /review."""

        def fake_socket(_family, _type):
            return _StubSocket(ip="169.254.42.7")

        monkeypatch.setattr(literary_clock.socket, "socket", fake_socket)
        assert literary_clock._resolve_lan_ip() is None

    def test_returns_none_for_empty_ip(self, monkeypatch) -> None:
        """Defensive: getsockname() returning an empty string (shouldn't
        happen in practice) maps to None instead of producing a
        host-less `http://` which would be a broken QR."""

        def fake_socket(_family, _type):
            return _StubSocket(ip="")

        monkeypatch.setattr(literary_clock.socket, "socket", fake_socket)
        assert literary_clock._resolve_lan_ip() is None

    def test_socket_is_closed_on_success(self, monkeypatch) -> None:
        """Don't leak FDs across per-minute renders."""
        stub = _StubSocket(ip="10.0.0.5")
        monkeypatch.setattr(literary_clock.socket, "socket", lambda *_a, **_kw: stub)
        literary_clock._resolve_lan_ip()
        assert stub.closed is True

    def test_socket_is_closed_after_oserror(self, monkeypatch) -> None:
        """Even when connect() raises, the FD must close."""
        stub = _StubSocket(raise_on="connect")
        monkeypatch.setattr(literary_clock.socket, "socket", lambda *_a, **_kw: stub)
        literary_clock._resolve_lan_ip()
        assert stub.closed is True


class TestQrUsesResolvedIp:
    """End-to-end: _composite_settings_qr's QR scan output (when decoded)
    matches the URL produced by _resolve_lan_ip — IP path on success,
    hostname fallback on None."""

    def test_qr_encodes_ip_url_when_ip_resolves(self, monkeypatch) -> None:
        from PIL import Image

        monkeypatch.setattr(literary_clock, "_resolve_lan_ip", lambda: "192.168.2.132")

        captured: dict[str, str] = {}
        # Capture the data passed to qr.add_data — saves us from needing a
        # full QR decoder in the test.
        import qrcode  # noqa: PLC0415

        original_add_data = qrcode.QRCode.add_data

        def spy_add_data(self, data, *args, **kwargs):
            captured["data"] = data
            return original_add_data(self, data, *args, **kwargs)

        monkeypatch.setattr(qrcode.QRCode, "add_data", spy_add_data)

        image = Image.new(mode="1", size=(800, 480), color=255)
        literary_clock._composite_settings_qr(image)

        assert captured["data"] == "http://192.168.2.132"

    def test_qr_falls_back_to_hostname_when_no_ip(self, monkeypatch) -> None:
        from PIL import Image

        monkeypatch.setattr(literary_clock, "_resolve_lan_ip", lambda: None)

        captured: dict[str, str] = {}
        import qrcode  # noqa: PLC0415

        original_add_data = qrcode.QRCode.add_data

        def spy_add_data(self, data, *args, **kwargs):
            captured["data"] = data
            return original_add_data(self, data, *args, **kwargs)

        monkeypatch.setattr(qrcode.QRCode, "add_data", spy_add_data)

        image = Image.new(mode="1", size=(800, 480), color=255)
        literary_clock._composite_settings_qr(image)

        assert captured["data"] == literary_clock.QR_URL
        assert captured["data"] == "http://litclock.local"

    def test_qr_geometry_unchanged_with_ip_url(self, monkeypatch) -> None:
        """A6 geometry pin: even with the longer/shorter IP-encoded URL,
        the QR still paints at (716, 2) and stays above the y=78 divider.
        Catches a regression that bumps to fit=True or grows to V3."""
        from PIL import Image

        monkeypatch.setattr(literary_clock, "_resolve_lan_ip", lambda: "192.168.2.132")
        image = Image.new(mode="1", size=(800, 480), color=255)
        literary_clock._composite_settings_qr(image)

        # Same finder-pattern checks as TestQrComposite — geometry must
        # not have shifted.
        assert image.getpixel((716, 2)) == 0, "QR top-left finder pattern missing"
        assert image.getpixel((716, 2 + 74)) == 0, "QR bottom-left finder missing"
        assert image.getpixel((740, 78)) == 255, "QR overflowed past divider"


class TestGlyphRelocation:
    def test_glyph_at_top_left_when_marker_present(self, monkeypatch, tmp_path) -> None:
        """A6 relocates the glyph from x=784 (legacy top-right) to x=4
        (top-left) so the QR can sit top-right unobstructed. Sample the
        glyph's known pixels at the new origin."""
        from PIL import Image, ImageDraw

        # Marker file must exist for the glyph to render.
        marker = tmp_path / "update-failed"
        marker.write_text("")
        monkeypatch.setattr(literary_clock, "UPDATE_FAILED_MARKER", str(marker))

        image = Image.new(mode="1", size=(800, 480), color=255)
        draw = ImageDraw.Draw(image)
        literary_clock._stamp_update_failed_glyph(image, draw)

        # Glyph "!" at x0=4, y0=4: vertical bar at (4+5..6, 4+1..7), dot at
        # (4+5..6, 4+9..10). Sample the bar mid-pixel — should be 0 (dark).
        assert image.getpixel((9, 5)) == 0, "vertical bar missing at top-left"
        # Sample the dot pixel.
        assert image.getpixel((9, 13)) == 0, "dot missing at top-left"
        # And the OLD position must NOT have a glyph anymore.
        assert image.getpixel((789, 5)) == 255, "glyph still rendering at legacy x=784"

    def test_glyph_skipped_when_marker_absent(self, monkeypatch) -> None:
        from PIL import Image, ImageDraw

        monkeypatch.setattr(literary_clock, "UPDATE_FAILED_MARKER", "/tmp/__definitely_does_not_exist__")
        image = Image.new(mode="1", size=(800, 480), color=255)
        draw = ImageDraw.Draw(image)
        literary_clock._stamp_update_failed_glyph(image, draw)
        # No pixel should have been written. Sample the glyph location.
        assert image.getpixel((9, 5)) == 255
        assert image.getpixel((9, 13)) == 255


# ---------- Structural anti-regression ----------


class TestStructural:
    """Source-shape pins so a future refactor can't silently undo M2."""

    def _src(self) -> str:
        repo_root = Path(__file__).resolve().parents[1]
        return (repo_root / "src" / "literary_clock.py").read_text()

    def test_get_current_quote_is_module_level(self) -> None:
        """A7: pure function at module scope so /api/status path can
        import it without re-running main()."""
        src = self._src()
        assert "def get_current_quote(" in src

    def test_weather_gated_on_coordinates(self) -> None:
        """EPIC #383 PR2 / design-review A2 (T27): weather must stay gated on
        BOTH coordinates being set, and empty coords must take the explicit
        "no location → skip weather" path. If a refactor dropped this gate, a
        Pi whose IP-geo failed (empty WEATHER_LATITUDE) would fetch weather
        against bogus/default coords AND — more importantly — the handoff's
        "timezone known ⇔ latitude set" proxy (control_server/handoff.py)
        would no longer line up with what the clock actually does. Pin it."""
        src = self._src()
        # The coords gate guards the weather-provider construction.
        assert "elif location_lat and location_long:" in src
        # Empty coords fall through to the explicit skip branch.
        assert "No location configured, skipping weather" in src

    def test_status_file_path_is_env_overridable(self) -> None:
        """OV3: tests + dev boxes need to point STATUS_FILE elsewhere.
        Hard-coding /var/run would force every test to run as root."""
        src = self._src()
        assert "LITCLOCK_STATUS_FILE" in src

    def test_status_file_default_lives_under_run_litclock(self) -> None:
        """Codex /review on M2 caught: /var/run is root-owned, so the
        per-minute write under User=pi gets Permission-denied. The
        existing #241 tmpfiles.d entry creates /run/litclock (tmpfs,
        pi:pi-owned) — status file lives there too. Pinning to prevent
        a future copy-paste from a `/var/run` doc."""
        src = self._src()
        assert "/run/litclock/current-quote.json" in src
        assert "/var/run/litclock-current-quote.json" not in src

    def test_qr_helper_exists_and_called_from_main(self) -> None:
        src = self._src()
        assert "def _composite_settings_qr" in src
        # Must be invoked from main() (not just defined).
        assert "_composite_settings_qr(image)" in src

    def test_status_file_write_called_from_main(self) -> None:
        src = self._src()
        assert "def _write_status_file" in src
        assert "_write_status_file(" in src

    def test_glyph_x_origin_relocated_to_left(self) -> None:
        """A6 commitment: glyph lives at x=4 now, not x=784. Pin the new
        origin so a regression to the legacy top-right placement (which
        would collide with the QR) fails loudly."""
        src = self._src()
        assert "x0 = 4" in src
        # Belt-and-suspenders: make sure the legacy form isn't lurking.
        assert "x0 = w - 16" not in src

    def test_resolve_lan_ip_helper_exists(self) -> None:
        """#306: IP-encoded QR URL replaces the hardcoded mDNS hostname for
        the scan path. Pin the helper so a refactor can't silently regress
        to QR_URL only."""
        src = self._src()
        assert "def _resolve_lan_ip(" in src
        # Helper must be invoked from the QR composite — not just defined.
        assert "_resolve_lan_ip()" in src

    def test_qr_composite_uses_resolved_ip_format(self) -> None:
        """#306 + #343 contract: when an IP is available, the QR encodes the
        control URL for that IP via the shared control_url helper (which emits
        plain http and omits the port at 80). Pin the call so a refactor can't
        silently drop the scheme (camera apps need it to recognize the URL) or
        re-hardcode a stale port."""
        src = self._src()
        assert "control_base_url(ip)" in src
