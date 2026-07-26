"""In-process tests for src/literary_clock.py M2 additions (PR #245).

Complements tests/test_literary_clock_dry_run.py (subprocess smoke tests
for the --dry-run contract). These exercise the pure helpers directly:

- get_current_quote() pure function (locked decision A7).
- _write_status_file() atomic write contract (OV3).
- _composite_settings_qr() geometry (A6, paste at x=713, y=0 — nudged from
  (716, 2) for the 4-module quiet zone) + divider-notch quiet zone.
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
        """75×75 QR at x=713, y=0 (A6, nudged from (716, 2) for the quiet
        zone). Sample interior pixels to confirm the QR was actually drawn
        (not the white background)."""
        from PIL import Image

        image = Image.new(mode="1", size=(800, 480), color=255)
        literary_clock._composite_settings_qr(image)

        # Top-left finder pattern of the QR sits at the paste origin.
        # In QR codes, the finder pattern is a 7×7 dark square — sampling
        # the (0, 0) pixel of the pattern (image coords 713, 0) catches
        # an actual dark module if compositing worked.
        assert image.getpixel((713, 0)) == 0, "QR top-left finder pattern missing"
        # And the bottom-right corner of the QR (image coords 713+74, 0+74)
        # is part of the bottom-left finder pattern → also dark.
        assert image.getpixel((713, 74)) == 0, "QR bottom-left finder missing"

    def test_qr_quiet_zone_notches_divider(self) -> None:
        """ISO 18004 quiet zone (Reddit report, 2026-07): the composite must
        white-out the strip's top-right corner so the divider no longer runs
        flush against the QR's bottom modules, and the 4-module white border
        survives on left/right/bottom. Pre-draw the divider from the SAME
        constants compose() uses, so divider geometry and notch can't drift
        apart without this test noticing."""
        from PIL import Image, ImageDraw

        image = Image.new(mode="1", size=(800, 480), color=255)
        draw = ImageDraw.Draw(image)
        draw.line(
            [(0, literary_clock.DIVIDER_Y), (800, literary_clock.DIVIDER_Y)],
            fill=0,
            width=literary_clock.DIVIDER_WIDTH,
        )
        literary_clock._composite_settings_qr(image)

        qx, qy = literary_clock.QR_POSITION
        quiet = literary_clock.QR_QUIET_ZONE
        qr_bottom = qy + literary_clock.QR_SIZE - 1  # 74
        notch_bottom = literary_clock.QR_NOTCH_BOTTOM
        assert quiet >= 4 * literary_clock.QR_BOX_SIZE
        # The notch must reach both past the divider's painted rows and the
        # full 4-module quiet zone below the QR (structural guarantee).
        assert notch_bottom >= literary_clock.DIVIDER_Y + literary_clock.DIVIDER_WIDTH // 2
        assert notch_bottom >= qr_bottom + quiet

        # Everything between the QR's last module row and the notch bottom
        # must be white — divider erased, quiet zone clear...
        for y in range(qr_bottom + 1, notch_bottom + 1):
            assert image.getpixel((740, y)) == 255, f"quiet zone dirty at y={y}"
        # ...but the divider stays intact left of the notch.
        assert image.getpixel((qx - quiet - 2, literary_clock.DIVIDER_Y)) == 0, "divider missing outside the notch"

        # 4-module quiet zone: left column and right column fully white.
        for y in range(0, notch_bottom + 1):
            assert image.getpixel((qx - quiet, y)) == 255, f"left quiet zone dirty at y={y}"
            assert image.getpixel((qx + literary_clock.QR_SIZE + quiet - 1, y)) == 255, (
                f"right quiet zone dirty at y={y}"
            )

    @pytest.mark.skipif(
        not (Path(__file__).resolve().parents[1] / "images").is_dir(),
        reason="corpus images not downloaded on this machine",
    )
    def test_corpus_clear_of_qr_quiet_zone(self) -> None:
        """The notch whites display rows 80..QR_NOTCH_BOTTOM in the quote
        images' top-right corner. Today no corpus image inks that region
        (worst glyph starts at display y=87, one row below the notch) — this
        scan fails loudly if a future regen puts glyphs where the notch
        would clip them, so the clip is a decision, not an accident."""
        from PIL import Image

        images_dir = Path(__file__).resolve().parents[1] / "images"
        x0 = literary_clock.QR_POSITION[0] - literary_clock.QR_QUIET_ZONE
        # Quote images paste at display y=80 → quote-image rows 0..N.
        clip_rows = literary_clock.QR_NOTCH_BOTTOM - 80 + 1
        offenders = []
        for png in sorted(images_dir.glob("quote_*.png")):
            with Image.open(png) as im:
                corner = im.crop((x0, 0, im.width, clip_rows)).convert("L")
            # Ink = dark pixels. point() maps ink→255, paper→0 so getbbox()
            # returns None iff the region is clean.
            if corner.point(lambda v: 255 if v < 128 else 0).getbbox() is not None:
                offenders.append(png.name)
        assert not offenders, (
            f"{len(offenders)} corpus images ink the QR quiet-zone notch region "
            f"(display rows 80..{literary_clock.QR_NOTCH_BOTTOM}, x>={x0}) and would be "
            f"clipped on the e-ink: {offenders[:10]}"
        )

    @pytest.mark.skipif(
        not (Path(__file__).resolve().parents[1] / "images").is_dir(),
        reason="corpus images not downloaded on this machine",
    )
    def test_full_compose_preserves_qr_quiet_zone(self) -> None:
        """End-to-end through main(): the notch happens inside
        _composite_settings_qr, so compose()'s draw-divider-BEFORE-composite
        ordering is load-bearing. The unit tests pre-draw their own divider
        and can't catch a reorder — this renders the real frame and asserts
        the quiet zone survived the full pipeline."""
        image, _meta, _now = literary_clock.main()

        qx, qy = literary_clock.QR_POSITION
        quiet = literary_clock.QR_QUIET_ZONE
        qr_bottom = qy + literary_clock.QR_SIZE - 1
        assert image.getpixel((qx, qy)) == 0, "QR finder pattern missing from composed frame"
        for y in range(qr_bottom + 1, literary_clock.QR_NOTCH_BOTTOM + 1):
            for x in (qx - quiet, 740, 799):
                assert image.getpixel((x, y)) == 255, f"quiet zone dirty at ({x}, {y}) in composed frame"

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
        assert literary_clock.QR_POSITION == (713, 0)
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
        the QR still paints at (713, 0) and stays above the y=78 divider.
        Catches a regression that bumps to fit=True or grows to V3."""
        from PIL import Image

        monkeypatch.setattr(literary_clock, "_resolve_lan_ip", lambda: "192.168.2.132")
        image = Image.new(mode="1", size=(800, 480), color=255)
        literary_clock._composite_settings_qr(image)

        # Same finder-pattern checks as TestQrComposite — geometry must
        # not have shifted.
        assert image.getpixel((713, 0)) == 0, "QR top-left finder pattern missing"
        assert image.getpixel((713, 74)) == 0, "QR bottom-left finder missing"
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


class TestRuntimeRender:
    """dev#531 Stage 2 — runtime quote rendering behind LITCLOCK_RUNTIME_RENDER."""

    def test_flag_off_values(self, monkeypatch, tmp_path) -> None:
        marker = tmp_path / ".runtime-render-validated"
        marker.write_text("freetype=0.0.0\n")
        monkeypatch.setattr(literary_clock, "RUNTIME_VALIDATED_MARKER", str(marker))
        for raw in ("false", "0", "no", ""):
            monkeypatch.setenv("LITCLOCK_RUNTIME_RENDER", raw)
            assert literary_clock._runtime_render_enabled() is False
        monkeypatch.delenv("LITCLOCK_RUNTIME_RENDER", raising=False)
        assert literary_clock._runtime_render_enabled() is False

    def test_flag_requires_validation_marker(self, monkeypatch, tmp_path) -> None:
        """dev#537 review: the flag alone must not enable runtime rendering —
        the device's freetype environment has to be stamped as validated."""
        monkeypatch.setenv("LITCLOCK_RUNTIME_RENDER", "true")
        monkeypatch.setattr(literary_clock, "RUNTIME_VALIDATED_MARKER", str(tmp_path / "missing-marker"))
        assert literary_clock._runtime_render_enabled() is False

    def _blank_frame(self):
        from PIL import Image

        return Image.new("L", (literary_clock.DISPLAY_SIZE[0], literary_clock.QUOTE_AREA_H), 255)

    def test_frame_ok_accepts_normal_ink(self) -> None:
        from PIL import ImageDraw

        frame = self._blank_frame()
        ImageDraw.Draw(frame).rectangle([(10, 100), (400, 150)], fill=0)
        assert literary_clock._runtime_frame_ok(frame) is None

    def test_frame_ok_rejects_wrong_size(self) -> None:
        from PIL import Image

        assert "size" in literary_clock._runtime_frame_ok(Image.new("L", (800, 480), 255))

    def test_frame_ok_rejects_blank(self) -> None:
        assert "no ink" in literary_clock._runtime_frame_ok(self._blank_frame())

    def test_frame_ok_rejects_notch_ink(self) -> None:
        frame = self._blank_frame()
        # one black pixel inside the QR quiet-zone notch region
        frame.putpixel((literary_clock.QR_POSITION[0], 2), 0)
        # plus normal body ink so the blank check doesn't trip first
        from PIL import ImageDraw

        ImageDraw.Draw(frame).rectangle([(10, 100), (400, 150)], fill=0)
        assert "notch" in literary_clock._runtime_frame_ok(frame)

    def test_frame_ok_ignores_light_grey_in_notch(self) -> None:
        """>=128 grey is not ink: dither=NONE thresholds it to white."""
        from PIL import ImageDraw

        frame = self._blank_frame()
        frame.putpixel((literary_clock.QR_POSITION[0], 2), 200)
        ImageDraw.Draw(frame).rectangle([(10, 100), (400, 150)], fill=0)
        assert literary_clock._runtime_frame_ok(frame) is None

    def test_persist_runtime_frame_atomic(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(literary_clock, "RUNTIME_RENDER_DIR", str(tmp_path))
        replaced = []
        real_replace = os.replace

        def tracking_replace(src, dst):
            replaced.append(dst)
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", tracking_replace)
        path = literary_clock._persist_runtime_frame(self._blank_frame().convert("1"))
        assert path == str(tmp_path / "current-quote.png")
        assert replaced == [path]
        assert (tmp_path / "current-quote.png").exists()
        assert list(tmp_path.iterdir()) == [tmp_path / "current-quote.png"]  # no temp litter

    def test_persist_runtime_frame_swallows_failure(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(literary_clock, "RUNTIME_RENDER_DIR", str(tmp_path / "missing"))
        assert literary_clock._persist_runtime_frame(self._blank_frame().convert("1")) is None

    def _write_runtime_corpus(self, tmp_path, monkeypatch) -> None:
        csv_path = tmp_path / "corpus.csv"
        csv_path.write_text(
            "00:00|midnight|It was midnight already in the quiet house.|T1|A1\n"
            "00:00|midnight|Still midnight there, the clocks all agreed.|T2|A2|yes\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(literary_clock, "CORPUS_CSV", str(csv_path))
        monkeypatch.setattr(literary_clock, "RUNTIME_RENDER_DIR", str(tmp_path))

    def test_runtime_quote_none_on_missing_renderer(self, monkeypatch, tmp_path) -> None:
        """Any import failure degrades to the PNG path, never raises."""
        import builtins

        real_import = builtins.__import__

        def failing_import(name, *a, **kw):
            if name == "quote_renderer":
                raise ImportError("no freetype")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", failing_import)
        assert literary_clock.get_current_quote_runtime(now=datetime(2026, 1, 1, 0, 0)) is None


try:
    import freetype  # noqa: F401

    _HAVE_FREETYPE = True
except ImportError:
    _HAVE_FREETYPE = False


@pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required for runtime render tests")
class TestRuntimeRenderWithFreetype:
    """End-to-end runtime render tests (need freetype-py + repo fonts)."""

    _write_runtime_corpus = TestRuntimeRender._write_runtime_corpus

    def test_runtime_quote_renders_and_persists(self, monkeypatch, tmp_path) -> None:
        self._write_runtime_corpus(tmp_path, monkeypatch)
        meta = literary_clock.get_current_quote_runtime(now=datetime(2026, 1, 1, 0, 0))
        assert meta is not None
        assert meta["image"].mode == "1"
        assert meta["image"].size == (800, literary_clock.QUOTE_AREA_H)
        assert meta["quote"].startswith("It was midnight")  # nsfw row filtered
        assert meta["title"] == "T1" and meta["author"] == "A1"
        assert meta["image_path"] == str(tmp_path / "current-quote.png")
        assert (tmp_path / "current-quote.png").exists()

    def test_runtime_quote_includes_nsfw_when_allowed(self, monkeypatch, tmp_path) -> None:
        self._write_runtime_corpus(tmp_path, monkeypatch)
        seen = set()
        for _ in range(20):
            meta = literary_clock.get_current_quote_runtime(now=datetime(2026, 1, 1, 0, 0), allow_nsfw=True)
            seen.add(meta["title"])
        assert seen == {"T1", "T2"}

    def test_runtime_quote_none_for_empty_bucket(self, monkeypatch, tmp_path) -> None:
        self._write_runtime_corpus(tmp_path, monkeypatch)
        assert literary_clock.get_current_quote_runtime(now=datetime(2026, 1, 1, 3, 7)) is None

    def test_runtime_quote_none_on_render_failure(self, monkeypatch, tmp_path) -> None:
        self._write_runtime_corpus(tmp_path, monkeypatch)
        import quote_renderer

        def boom(row):
            raise quote_renderer.RenderError("nofit", "test")

        monkeypatch.setattr(quote_renderer, "render_row", boom)
        assert literary_clock.get_current_quote_runtime(now=datetime(2026, 1, 1, 0, 0)) is None

    def test_runtime_quote_none_on_sanity_rejection(self, monkeypatch, tmp_path) -> None:
        self._write_runtime_corpus(tmp_path, monkeypatch)
        from PIL import Image

        import quote_renderer

        blank = Image.new("L", (800, literary_clock.QUOTE_AREA_H), 255)

        def blank_render(row):
            return blank, blank, 20, None

        monkeypatch.setattr(quote_renderer, "render_row", blank_render)
        assert literary_clock.get_current_quote_runtime(now=datetime(2026, 1, 1, 0, 0)) is None


class TestQuotePngFallback:
    """dev#537 review: a corrupt/unreadable quote PNG must degrade to the
    plain time draw, never kill the whole per-minute render."""

    def test_corrupt_png_falls_back_to_time_draw(self, monkeypatch, tmp_path) -> None:
        bad = tmp_path / "quote_0000_0_credits.png"
        bad.write_bytes(b"not a png at all")
        monkeypatch.setenv("WEATHER_ENABLED", "false")
        monkeypatch.delenv("LITCLOCK_RUNTIME_RENDER", raising=False)
        monkeypatch.setattr(
            literary_clock,
            "get_current_quote",
            lambda now=None, allow_nsfw=False: {
                "quote": "q",
                "author": "a",
                "title": "t",
                "time": "00:00",
                "image_path": str(bad),
                "picked_at": 0.0,
            },
        )
        image, quote_meta, _now = literary_clock.main()
        assert image is not None and image.size == literary_clock.DISPLAY_SIZE
        # meta cleared so the status file reports the honest time-only frame
        assert quote_meta is None


@pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required")
class TestMainRuntimeWiring:
    """dev#537 review finding 5: pin main()'s flag→runtime→paste and
    flag→runtime-None→PNG-glob wiring, not just the helpers."""

    def _arm(self, monkeypatch, tmp_path) -> None:
        csv_path = tmp_path / "corpus.csv"
        csv_path.write_text(
            "00:00|midnight|It was midnight already in the quiet house.|T1|A1\n",
            encoding="utf-8",
        )
        marker = tmp_path / ".runtime-render-validated"
        import freetype

        marker.write_text(f"freetype={'.'.join(map(str, freetype.version()))}\n")
        monkeypatch.setattr(literary_clock, "CORPUS_CSV", str(csv_path))
        monkeypatch.setattr(literary_clock, "RUNTIME_RENDER_DIR", str(tmp_path))
        monkeypatch.setattr(literary_clock, "RUNTIME_VALIDATED_MARKER", str(marker))
        monkeypatch.setenv("LITCLOCK_RUNTIME_RENDER", "true")
        monkeypatch.setenv("WEATHER_ENABLED", "false")

        class MidnightDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 1, 0, 0)

        monkeypatch.setattr(literary_clock, "datetime", MidnightDT)

    def _real_png(self, tmp_path) -> str:
        """A genuine 800x400 PNG — main() opens image_path, and a missing
        file trips the corrupt-PNG guard (which nulls the meta)."""
        from PIL import Image

        p = tmp_path / "fallback.png"
        Image.new("1", (800, 400), 1).save(p)
        return str(p)

    def test_main_uses_runtime_frame(self, monkeypatch, tmp_path) -> None:
        self._arm(monkeypatch, tmp_path)
        image, quote_meta, _ = literary_clock.main()
        assert quote_meta is not None and "image" in quote_meta
        # the producer must TAG it — deleting that line must fail a test
        assert quote_meta["render_mode"] == "runtime"
        # the runtime frame's ink actually landed in the quote area
        area = image.crop((0, literary_clock.QUOTE_AREA_Y, 800, 480))
        assert area.getbbox() is not None

    def test_main_falls_back_to_png_glob_when_runtime_none(self, monkeypatch, tmp_path) -> None:
        self._arm(monkeypatch, tmp_path)
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: None)
        consulted = []

        def recording_glob(now=None, allow_nsfw=False):
            consulted.append(True)
            return None

        monkeypatch.setattr(literary_clock, "get_current_quote", recording_glob)
        image, quote_meta, _ = literary_clock.main()
        assert consulted == [True]
        assert quote_meta is None  # PNG glob empty -> time draw
        assert image.size == literary_clock.DISPLAY_SIZE

    def test_flag_on_with_matching_marker(self, monkeypatch, tmp_path) -> None:
        self._arm(monkeypatch, tmp_path)
        for raw in ("true", "1", "TRUE"):
            monkeypatch.setenv("LITCLOCK_RUNTIME_RENDER", raw)
            assert literary_clock._runtime_render_enabled() is True

    def test_runtime_attempted_but_lost_is_image_fallback(self, monkeypatch, tmp_path) -> None:
        """dev#543 F3: the alarm condition must be distinguishable from
        'runtime rendering is simply off on this device'."""
        self._arm(monkeypatch, tmp_path)
        png = self._real_png(tmp_path)
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: None)
        monkeypatch.setattr(
            literary_clock,
            "get_current_quote",
            lambda now=None, allow_nsfw=False: {
                "quote": "q", "author": "a", "title": "t", "time": "00:00",
                "image_path": png, "picked_at": 0.0, "render_mode": "image",
            },
        )
        _image, quote_meta, _now = literary_clock.main()
        assert quote_meta["render_mode"] == "image-fallback"

    def test_runtime_disabled_stays_plain_image(self, monkeypatch, tmp_path) -> None:
        self._arm(monkeypatch, tmp_path)
        png = self._real_png(tmp_path)
        monkeypatch.setenv("LITCLOCK_RUNTIME_RENDER", "false")
        monkeypatch.setattr(
            literary_clock,
            "get_current_quote",
            lambda now=None, allow_nsfw=False: {
                "quote": "q", "author": "a", "title": "t", "time": "00:00",
                "image_path": png, "picked_at": 0.0, "render_mode": "image",
            },
        )
        _image, quote_meta, _now = literary_clock.main()
        assert quote_meta["render_mode"] == "image"

    def test_marker_freetype_mismatch_disables_runtime(self, monkeypatch, tmp_path) -> None:
        self._arm(monkeypatch, tmp_path)
        (tmp_path / ".runtime-render-validated").write_text("freetype=9.9.9\n")
        assert literary_clock._runtime_render_enabled() is False


class TestMastheadGeometry:
    """dev#538 V8-G2: shared-baseline masthead with centered weather lockup.
    Ink = pixel 0 on the mode-'1' strip. All bounds from the review:
    horizontal rule ink rows 77..80, vertical rule ink cols 224..227,
    QR notch erases x>=701, update-failed glyph zone x<=16."""

    RULE_TOP = 77
    VRULE_LEFT = 224

    def _strip(self, date_text="Sat, July 25", hi="100°F", lo="82°F", icon="sun"):
        from PIL import Image, ImageDraw

        img = Image.new("1", literary_clock.DISPLAY_SIZE, 1)
        draw = ImageDraw.Draw(img)
        icon_path = os.path.join(literary_clock.PROJECT_ROOT, "icons", f"{icon}.xbm") if icon else None
        literary_clock._compose_masthead(img, draw, date_text, hi, lo, icon_path)
        return img

    def _ink_bbox(self, img, box):
        region = img.crop(box)
        inv = region.point(lambda v: 255 if v == 0 else 0)
        return inv.getbbox()

    def test_date_clearance_and_bound_full_sweep(self):
        """Worst-case dates found by width/descender sweep (review C1: the
        widest is 'Mon, September 04', NOT September 30), then rendered and
        checked: descenders >= RULE_CLEARANCE above the rule, right edge
        clear of the QR notch with real margin."""
        from PIL import Image, ImageDraw

        probe = ImageDraw.Draw(Image.new("1", (4, 4)))
        f = literary_clock._masthead_metrics()["date_font"]
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        months = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]
        candidates = [f"{d}, {m} {n:02d}" for d in days for m in months for n in (4, 9, 30, 28)]
        widest = sorted(candidates, key=lambda s: probe.textbbox((0, 0), s, font=f)[2])[-6:]
        deepest = [f"{d}, {m} 30" for d in days[:1] for m in months]  # descender set
        for date_text in widest + deepest:
            img = self._strip(date_text=date_text)
            bb = self._ink_bbox(img, (literary_clock.DATE_X, 0, 800, self.RULE_TOP))
            assert bb is not None
            bottom = bb[3]  # exclusive bottom
            assert bottom - 1 <= self.RULE_TOP - 1 - literary_clock.RULE_CLEARANCE, (date_text, bb)
            right = literary_clock.DATE_X + bb[2]
            assert right < 695, f"{date_text} right edge {right} too close to QR notch (701)"

    def test_temp_matrix_clearances_and_line_separation(self):
        temps = [("9°F", "5°F"), ("82°F", "64°F"), ("100°F", "82°F"), ("110°F", "99°F"),
                 ("-8°F", "-22°F"), ("-40°F", "-44°F"), ("38°C", "27°C"), ("-12°C", "-22°C"),
                 ("-40°C", "-44°C")]
        for hi, lo in temps:
            img = self._strip(hi=hi, lo=lo)
            # nothing in the clearance band left of the vertical rule
            gx = self.VRULE_LEFT - literary_clock.RULE_CLEARANCE
            gap = self._ink_bbox(img, (gx, 0, self.VRULE_LEFT, self.RULE_TOP))
            assert gap is None, (hi, lo, "ink in vertical-rule clearance band")
            # nothing in the clearance band above the horizontal rule (whole cell)
            clr = self.RULE_TOP - literary_clock.RULE_CLEARANCE
            band = self._ink_bbox(img, (0, clr, self.VRULE_LEFT, self.RULE_TOP))
            assert band is None, (hi, lo, "ink in horizontal-rule clearance band")
            # the two temp lines never touch (review I3: >= 2px separation)
            mh = literary_clock._masthead_metrics()
            x0 = mh["icon_x"] + literary_clock.ICON_SIZE + 1
            hi_bb = self._ink_bbox(img, (x0, 0, self.VRULE_LEFT, mh["baseline"] - literary_clock.TEMP_LINE_PITCH + 2))
            split = mh["baseline"] - literary_clock.TEMP_LINE_PITCH + 2
            lo_bb = self._ink_bbox(img, (x0, split, self.VRULE_LEFT, self.RULE_TOP))
            assert hi_bb is not None and lo_bb is not None
            # REAL separation: bottom of the high line to top of the low line
            hi_bottom_abs = hi_bb[3]  # exclusive, in absolute rows
            lo_top_abs = split + lo_bb[1]
            assert lo_top_abs - hi_bottom_abs >= 2, (hi, lo, hi_bottom_abs, lo_top_abs)

    def test_all_22_icons_stay_inside_lockup(self):
        import glob as _glob

        mh = literary_clock._masthead_metrics()
        for p in sorted(_glob.glob(os.path.join(literary_clock.PROJECT_ROOT, "icons", "*.xbm"))):
            name = os.path.basename(p)[:-4]
            img = self._strip(icon=name)
            icon_bb = self._ink_bbox(img, (0, 0, mh["icon_x"] + literary_clock.ICON_SIZE + 1, self.RULE_TOP))
            assert icon_bb is not None, name
            # icon ink never below the clearance band nor left of the glyph zone
            assert mh["icon_x"] >= literary_clock.LOCKUP_MIN_X
            assert icon_bb[3] - 1 <= self.RULE_TOP - 1 - literary_clock.RULE_CLEARANCE, name
            assert icon_bb[0] >= literary_clock.LOCKUP_MIN_X, name

    def test_icon_position_static_across_temps(self):
        a = self._strip(hi="9°F", lo="5°F")
        b = self._strip(hi="-40°C", lo="-44°C")
        mh = literary_clock._masthead_metrics()
        x, y, px = mh["icon_x"], mh["icon_y"], literary_clock.ICON_SIZE
        box = (x, y, x + px, y + px)
        assert list(a.crop(box).getdata()) == list(b.crop(box).getdata())

    def test_missing_icon_keeps_slot_and_survives(self, tmp_path):
        img = self._strip(icon=None)
        ok = self._strip()
        mh = literary_clock._masthead_metrics()
        # temps identical with and without icon (fixed slot geometry)
        tbox = (mh["icon_x"] + literary_clock.ICON_SIZE, 0, self.VRULE_LEFT, self.RULE_TOP)
        assert list(img.crop(tbox).getdata()) == list(ok.crop(tbox).getdata())

    def test_corrupt_icon_survives(self, tmp_path):
        bad = tmp_path / "junk.xbm"
        bad.write_bytes(b"this is not an xbm")
        from PIL import Image, ImageDraw

        img = Image.new("1", literary_clock.DISPLAY_SIZE, 1)
        literary_clock._compose_masthead(img, ImageDraw.Draw(img), "Sat, July 25", "100°F", "82°F", str(bad))
        # temps still drawn
        mh = literary_clock._masthead_metrics()
        assert self._ink_bbox(img, (mh["icon_x"] + literary_clock.ICON_SIZE, 0, self.VRULE_LEFT, self.RULE_TOP))

    def test_no_weather_state(self):
        img = self._strip(hi=None, lo=None, icon=None)
        # no vertical rule, no lockup ink in the weather cell
        assert self._ink_bbox(img, (0, 0, self.VRULE_LEFT + 4, self.RULE_TOP)) is None
        # date still present
        assert self._ink_bbox(img, (literary_clock.DATE_X, 0, 800, self.RULE_TOP)) is not None

    def test_lockup_clear_of_update_glyph_zone(self):
        img = self._strip()
        assert self._ink_bbox(img, (0, 0, 17, self.RULE_TOP)) is None


class TestFormatTemp:
    """dev#538 review: bound the weather temp domain."""

    def test_valid_values(self):
        assert literary_clock._format_temp(82.4, "°F") == "82°F"
        assert literary_clock._format_temp(-12.6, "°C") == "-13°C"
        assert literary_clock._format_temp(0, "°F") == "0°F"
        assert literary_clock._format_temp("99", "°F") == "99°F"
        assert literary_clock._format_temp(-0.4, "°F") == "0°F"

    def test_garbage_returns_none(self):
        for bad in (None, float("nan"), float("inf"), -float("inf"), "N/A", "", [], {}, 1000, -300):
            assert literary_clock._format_temp(bad, "°F") is None, bad


class TestMainWeatherHardening:
    """dev#541 review: malformed weather payloads reach main() safely."""

    def _run_main(self, monkeypatch, payload):
        from weather_providers import open_meteo

        class Stub:
            def __init__(self, *a, **kw): ...
            def get_weather(self):
                return payload

        monkeypatch.setenv("WEATHER_ENABLED", "true")
        monkeypatch.setenv("WEATHER_LATITUDE", "30")
        monkeypatch.setenv("WEATHER_LONGITUDE", "-97")
        monkeypatch.delenv("OPENWEATHERMAP_APIKEY", raising=False)
        monkeypatch.delenv("LITCLOCK_RUNTIME_RENDER", raising=False)
        monkeypatch.setattr(open_meteo, "OpenMeteo", Stub)
        monkeypatch.setattr(literary_clock, "open_meteo", open_meteo)
        image, _meta, _now = literary_clock.main()
        assert image is not None and image.size == literary_clock.DISPLAY_SIZE
        return image

    def test_missing_icon_key_survives(self, monkeypatch):
        img = self._run_main(monkeypatch, {"temperatureMax": 100, "temperatureMin": 82})
        # temps drawn even with no icon key
        mh = literary_clock._masthead_metrics()
        region = img.crop((mh["icon_x"] + literary_clock.ICON_SIZE, 0, 224, 77))
        assert region.point(lambda v: 255 if v == 0 else 0).getbbox() is not None

    def test_non_mapping_payload_survives(self, monkeypatch):
        self._run_main(monkeypatch, ["not", "a", "dict"])

    def test_garbage_temps_suppress_weather(self, monkeypatch):
        img = self._run_main(monkeypatch, {"temperatureMax": "N/A", "temperatureMin": None, "icon": "sun"})
        region = img.crop((0, 0, 228, 77))
        assert region.point(lambda v: 255 if v == 0 else 0).getbbox() is None


class TestTempSlotDomain:
    """dev#541 review: every string _format_temp can emit fits the slot."""

    def test_band_edges_fit_slot(self):
        from PIL import Image, ImageDraw

        mh = literary_clock._masthead_metrics()
        probe = ImageDraw.Draw(Image.new("1", (4, 4)))
        slot = mh["slot_right"] - (mh["icon_x"] + literary_clock.ICON_SIZE + literary_clock.LOCKUP_GAP)
        for n in (-99, -40, 0, 100, 199):
            for deg in ("°F", "°C"):
                s = literary_clock._format_temp(n, deg)
                assert s is not None
                w = probe.textbbox((0, 0), s, font=mh["temp_font"])[2]
                assert w <= slot, (s, w, slot)


class TestRenderModeSignal:
    """dev#531: the status file must say WHICH render tier painted the frame.
    The fallback chain is invisible to the viewer, so this field is the only
    evidence distinguishing a healthy runtime-render fleet from one silently
    falling back — it gates the images-retirement decision."""

    def _write_and_read(self, monkeypatch, tmp_path, quote_meta):
        target = tmp_path / "current-quote.json"
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(target))
        literary_clock._write_status_file(quote_meta, datetime(2026, 1, 1, 12, 0))
        return json.loads(target.read_text())

    def test_runtime_mode_recorded(self, monkeypatch, tmp_path):
        meta = {"quote": "q", "author": "a", "title": "t", "image_path": "/run/litclock/current-quote.png",
                "picked_at": 1.0, "render_mode": "runtime"}
        assert self._write_and_read(monkeypatch, tmp_path, meta)["render_mode"] == "runtime"

    def test_image_fallback_mode_recorded(self, monkeypatch, tmp_path):
        meta = {"quote": "q", "author": "a", "title": "t", "image_path": "/x/quote_1200_0_credits.png",
                "picked_at": 1.0, "render_mode": "image"}
        assert self._write_and_read(monkeypatch, tmp_path, meta)["render_mode"] == "image"

    def test_no_quote_is_time_only(self, monkeypatch, tmp_path):
        assert self._write_and_read(monkeypatch, tmp_path, None)["render_mode"] == "time-only"

    def test_legacy_meta_without_field_is_unknown_not_a_claim(self, monkeypatch, tmp_path):
        """A meta dict lacking the field means UNKNOWN — not runtime (a false
        all-clear) and not time-only (a false claim that no quote painted;
        dev#543 review F4). Only quote_meta=None is genuinely time-only."""
        meta = {"quote": "q", "author": "a", "title": "t", "image_path": "/x.png", "picked_at": 1.0}
        assert self._write_and_read(monkeypatch, tmp_path, meta)["render_mode"] is None

    def test_glob_path_tags_image_mode(self, monkeypatch, tmp_path):
        (tmp_path / "images" / "metadata").mkdir(parents=True)
        png = tmp_path / "images" / "metadata" / "quote_1200_0_credits.png"
        png.write_bytes(b"x")
        monkeypatch.setattr(literary_clock, "PROJECT_ROOT", str(tmp_path))
        meta = literary_clock.get_current_quote(now=datetime(2026, 1, 1, 12, 0))
        assert meta is not None and meta["render_mode"] == "image"
