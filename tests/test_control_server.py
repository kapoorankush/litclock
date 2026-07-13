"""Tests for src/control_server — Flask app factory + M1 routes.

Covers:
- create_app() builds a working app with overridable test_config.
- /api/health returns the documented shape ({ok, version, uptime_s}).
- / renders the PWA shell base.html.j2 with the tab bar wired up.
- M0 static assets are reachable through the Flask static endpoint.
- Type-scale tokens.css ships clamp() values per DESIGN.md D4.
- Jinja2 autoescape is on (XSS protection for M3's gift-mode message route).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from control_server import create_app  # noqa: E402


@pytest.fixture
def app():
    return create_app({"VERSION_OVERRIDE": "v0.test"})


@pytest.fixture
def client(app):
    return app.test_client()


# ---------- /api/health ----------


def test_health_returns_documented_shape(client) -> None:
    """A8 contract: PWA's reconnect probe reads `version` and `uptime_s`. If
    this shape changes, the M4 reconnect detection logic breaks."""
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json
    assert body is not None
    assert body["ok"] is True
    assert body["version"] == "v0.test"
    assert isinstance(body["uptime_s"], int)
    assert body["uptime_s"] >= 0


def test_health_uses_version_override(client) -> None:
    response = client.get("/api/health")
    assert response.json["version"] == "v0.test"


def test_health_works_without_version_override() -> None:
    """No override = falls back to git describe / .images-version / 'unknown'.
    The fallback must be a non-empty string — never crash."""
    from control_server.version import reset_cache

    reset_cache()
    app = create_app({"VERSION_OVERRIDE": None})
    response = app.test_client().get("/api/health")
    assert response.status_code == 200
    version = response.json["version"]
    assert isinstance(version, str)
    assert len(version) > 0


# ---------- / (PWA shell) ----------


def test_index_renders_pwa_shell(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    body = response.data
    assert b"<!DOCTYPE html>" in body
    assert b'<nav class="tabbar"' in body
    assert b"tokens.css" in body
    assert b"icon-192.png" in body


def test_index_renders_diagnostics_drawer_shell(client) -> None:
    """#416 PR3b — the diagnostics shortcut ribbon + drawer are cross-cutting:
    they render in base.html.j2 so every tab can open the drawer. Verifies
    the markup hooks land + the drawer ships closed/inert/aria-hidden by
    default (JS removes those on open).

    The .diag-lantern decoration was removed in v0.214.4 (#431b) — see
    DESIGN.md §"Live-logs drawer" for rationale. The negative assertion
    below catches a regression that would reintroduce the markup."""
    body = client.get("/").data
    # Ribbon button + drawer + page-dim all present.
    assert b"data-diag-ribbon-button" in body
    assert b"data-diag-page-dim" in body
    assert b"data-diag-drawer" in body
    # #431b — lantern markup must NOT be present.
    assert b"data-diag-lantern" not in body
    assert b"diag-lantern" not in body
    # #431a — drawer-header link drops the trailing arrow and carries an
    # aria-label so SR users get the destination. The aria-label is now
    # load-bearing (replaces the visual "→" affordance).
    assert b'aria-label="Open full diagnostics page"' in body
    # Drawer starts hidden + inert + aria-hidden=true (JS lifts on open).
    drawer_pos = body.find(b"data-diag-drawer\n")
    assert drawer_pos > 0
    # The opening <section> tag spans a few lines; check for the start-state
    # attributes within ~600 bytes of the data attribute.
    drawer_chunk = body[max(0, drawer_pos - 200) : drawer_pos + 600]
    assert b'aria-modal="false"' in drawer_chunk  # D6 + D32 non-modal contract
    assert b'role="dialog"' in drawer_chunk
    assert b'aria-hidden="true"' in drawer_chunk
    assert b" inert" in drawer_chunk or b" inert>" in drawer_chunk
    assert b" hidden" in drawer_chunk or b" hidden>" in drawer_chunk
    # D8 level filter radiogroup is in the drawer body.
    assert b'role="radiogroup"' in body
    assert b'data-diag-level="ERROR"' in body
    # D2 four empty states all server-rendered.
    for state in (b"no-entries", b"no-matches", b"journal-denied", b"disconnected"):
        assert b'data-diag-drawer-empty="' + state + b'"' in body
    # D25 welcome card present.
    assert b"data-diag-drawer-welcome" in body
    # drawer.js + drawer.css ship cross-cuttingly.
    assert b"js/drawer.js" in body
    assert b"css/drawer.css" in body


def test_index_marks_status_tab_active(client) -> None:
    response = client.get("/")
    body = response.data.decode()
    # Status tab should carry aria-current="page" by default in M1.
    status_pos = body.find(">Status<")
    settings_pos = body.find(">Settings<")
    assert status_pos > 0
    assert settings_pos > 0
    # The status anchor must have aria-current; settings must not.
    status_anchor_start = body.rfind("<a", 0, status_pos)
    settings_anchor_start = body.rfind("<a", 0, settings_pos)
    assert "aria-current" in body[status_anchor_start:status_pos]
    assert "aria-current" not in body[settings_anchor_start:settings_pos]


def test_index_sets_theme_color_meta(client) -> None:
    """DESIGN.md "PWA Shell Requirements": theme-color tinted to --bg in both
    light and dark modes."""
    body = client.get("/").data.decode()
    assert 'theme-color" content="#FBF6EC"' in body
    assert 'theme-color" content="#14110D"' in body


# ---------- Static assets ----------


def test_m0_paper_grain_served(client) -> None:
    response = client.get("/static/paper-grain.svg")
    assert response.status_code == 200
    assert b"feTurbulence" in response.data
    assert b"fractalNoise" in response.data


@pytest.mark.parametrize(
    "path",
    [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-maskable-512.png",
        "/static/icons/icon-bg-baked-512.png",
    ],
)
def test_m0_icons_served(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.data.startswith(b"\x89PNG")


def test_tokens_css_uses_clamp_for_every_role(client) -> None:
    """DESIGN.md D4: every type role must use clamp() so iOS Larger Text and
    Android system-font scaling propagate. Pin the role list so future edits
    don't silently revert any to a fixed px size."""
    response = client.get("/static/css/tokens.css")
    assert response.status_code == 200
    body = response.data.decode()
    for role in (
        "--fs-caption",
        "--fs-small",
        "--fs-body",
        "--fs-lede",
        "--fs-h3",
        "--fs-h2",
        "--fs-h1",
        "--fs-display",
    ):
        line = next((line for line in body.splitlines() if line.lstrip().startswith(role)), None)
        assert line is not None, f"missing token: {role}"
        assert "clamp(" in line, f"{role} must use clamp() — got: {line.strip()}"


def test_tokens_css_carries_design_color_palette(client) -> None:
    body = client.get("/static/css/tokens.css").data.decode()
    # Sentinel light-mode tokens.
    assert "--bg: #FBF6EC" in body
    assert "--ink: #1A1410" in body
    assert "--accent: #B85C20" in body
    # Sentinel dark-mode tokens.
    assert "prefers-color-scheme: dark" in body
    assert "--bg: #14110D" in body


def test_tokens_css_includes_reduced_motion_block(client) -> None:
    """DESIGN.md a11y: reduced-motion preference must replace transitions
    with snap-cuts, not just shorten them."""
    body = client.get("/static/css/tokens.css").data.decode()
    assert "prefers-reduced-motion: reduce" in body


# ---------- Security ----------


def test_jinja_autoescape_enabled(app) -> None:
    """C3 (locked): Jinja2 autoescape must be on so M3's gift-mode message
    render path gets free XSS protection. Loss of autoescape = silent XSS hole."""
    assert app.jinja_env.autoescape is True


def test_unknown_route_returns_404(client) -> None:
    response = client.get("/this/does/not/exist")
    assert response.status_code == 404


# ---------- Tab routes (no stubs left as of M5) ----------
# All four tabs now have their own templates + dedicated tests:
#   /          — tests/test_control_server.py (Status tab in this file)
#   /settings  — tests/test_control_server_settings.py
#   /system    — tests/test_control_server_system_routes.py
#   /updates   — tests/test_control_server_updates.py


@pytest.mark.parametrize(
    "path,active_tab",
    [
        ("/updates", "updates"),
    ],
)
def test_tab_routes_active_marker(client, path: str, active_tab: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    decoded = response.data.decode()
    label = active_tab.capitalize()
    label_pos = decoded.find(f">{label}<")
    assert label_pos > 0
    anchor_start = decoded.rfind("<a", 0, label_pos)
    assert "aria-current" in decoded[anchor_start:label_pos]


# ---------- Plain-HTTP shape (per locked decision in #257) ----------


class TestPlainHttpShape:
    """control_server drops self-signed TLS in favor of plain HTTP — the
    locked LAN-trust threat model (PLAN A4) accepts cleartext on LAN, and
    self-signed TLS was creating UX deal-breakers on iOS PWAs (cert
    warning every launch, AtHS icon fallback to letter-initial, iOS
    Larger Text suppressed). Pin the shape so a future edit can't
    accidentally re-introduce TLS-only code paths."""

    def test_app_module_imports_without_ssl(self) -> None:
        """The hand-rolled TLS terminator + helpers are gone. If a future
        edit re-adds them, this test forces an explicit decision rather
        than a silent regression."""
        from control_server import app as ctl_app

        assert not hasattr(ctl_app, "_serve_tls"), (
            "TLS terminator removed per #257 — re-introducing requires re-decision"
        )
        assert not hasattr(ctl_app, "_build_tls_context")
        assert not hasattr(ctl_app, "_handle_tls_conn")

    def test_port_constant_present(self) -> None:
        from control_server import app as ctl_app

        assert isinstance(ctl_app.PORT, int)
        # #343: plain HTTP on port 80 so the URL a user scans/types carries no
        # port. PORT is sourced from the shared control_url.CONTROL_PORT (default
        # 80) — the same value the clock's QR encodes, so they can't drift.
        assert ctl_app.PORT == 80
        assert 1 <= ctl_app.PORT <= 65535

    def test_threads_constant_matches_pi_cores(self) -> None:
        """waitress threads=4 maps to the Pi Zero 2W's 4 ARM cores."""
        from control_server import app as ctl_app

        assert isinstance(ctl_app.THREADS, int)
        assert 1 <= ctl_app.THREADS <= 16


class TestVersionCachePrimed:
    """create_app should resolve get_version once at factory time so the
    first /api/health post-restart doesn't pay the 50-200ms `git describe`
    fork on a Pi Zero 2W. PWA reconnect probe uses 1s timeout (PLAN A8) —
    a cold first call could exceed it."""

    def test_factory_primes_version_cache(self, monkeypatch) -> None:
        from control_server import create_app
        from control_server import version as ver_mod

        ver_mod.reset_cache()
        calls = {"n": 0}

        real_run = ver_mod.subprocess.run

        def counting_run(*args, **kwargs):
            calls["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(ver_mod.subprocess, "run", counting_run)

        # Build the app — should call get_version once internally.
        create_app({"VERSION_OVERRIDE": None})
        n_after_factory = calls["n"]

        # A subsequent call should hit the lru_cache, not subprocess.
        ver_mod.get_version(None)
        assert calls["n"] == n_after_factory, "create_app must prime the version cache so /api/health is fast"


# ---------- /review fixups: design CRITICAL ----------


class TestDesignTokensAccentScope:
    """Pin the accent-link scope fix. DESIGN.md line 315 names the trap:
    `--accent` (3.2:1 on `--bg`) is restricted to ≥18pt Fraunces or
    ≥14pt bold sans, never inline-link body text at 14-16px. The global
    rule `a { color: var(--accent) }` violated this."""

    def _tokens_css(self) -> str:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        return (repo_root / "src/control_server/static/css/tokens.css").read_text()

    def test_default_link_color_is_ink_not_accent(self) -> None:
        css = self._tokens_css()
        # The trap rule must be gone.
        assert "a { color: var(--accent)" not in css, "global accent-on-link violates DESIGN.md line 315"
        # Default `a` rule sets --ink. Match either single- or multi-line form.
        assert "a {" in css
        # Find the `a {` block and confirm --ink is set within it.
        a_block_start = css.find("a {")
        a_block_end = css.find("}", a_block_start)
        assert "var(--ink)" in css[a_block_start:a_block_end]

    def test_accent_link_class_is_explicit_opt_in(self) -> None:
        css = self._tokens_css()
        # Explicit class for places that meet the contrast guardrail.
        assert "a.accent-link" in css

    def test_tabbar_transition_uses_micro_not_short(self) -> None:
        """Tap-state color change → --t-micro per DESIGN.md motion table."""
        css = self._tokens_css()
        tabbar_a_start = css.find(".tabbar a {")
        tabbar_a_end = css.find("}", tabbar_a_start)
        block = css[tabbar_a_start:tabbar_a_end]
        assert "var(--t-micro)" in block, "tab-color transition should use --t-micro (100ms)"
        assert "var(--t-short)" not in block, "--t-short is for toasts, not tap-state"


class TestIosLargerTextScaling:
    """Hardware-found regression on PR #252: iOS Larger Text scaled the
    Safari address bar but not the PWA body. Root cause: setting
    `font-size: clamp(15px, 1rem, 20px)` on `html` is circular — `1rem`
    resolves against the value being computed, falls back to 16px, and
    iOS Larger Text never propagates."""

    def _tokens_css(self) -> str:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        return (repo_root / "src/control_server/static/css/tokens.css").read_text()

    def test_html_does_not_set_font_size(self) -> None:
        """The bug. Pinning the absence of a circular font-size on html so a
        future refactor doesn't reintroduce it."""
        css = self._tokens_css()
        # Find the html block (could be `html {` or `html, body {` — match
        # whichever standalone selector appears).
        html_start = css.find("html {")
        if html_start < 0:
            # Fallback for combined selectors.
            html_start = css.find("html, body {")
        assert html_start >= 0, "html selector must exist"
        html_end = css.find("}", html_start)
        block = css[html_start:html_end]
        assert "font-size:" not in block, (
            "html must not set font-size — circular reference defeats iOS Larger Text. Apply font-size to body instead."
        )

    def test_body_sets_fluid_body_font_size(self) -> None:
        """Body picks up the fluid scale; clamp() on body resolves against
        html's user-preference root size, which propagates iOS Larger Text."""
        css = self._tokens_css()
        body_start = css.find("body {")
        # Skip the combined `html, body {` selector — find the body-only one.
        while body_start >= 0 and "html" in css[max(body_start - 12, 0) : body_start]:
            body_start = css.find("body {", body_start + 1)
        assert body_start >= 0, "standalone body selector must exist"
        body_end = css.find("}", body_start)
        block = css[body_start:body_end]
        assert "font-size: var(--fs-body)" in block

    def test_text_size_adjust_present(self) -> None:
        """`text-size-adjust: 100%` opts iOS standalone PWAs into the user's
        font-size preference (some iOS versions ignore Dynamic Type without
        this)."""
        css = self._tokens_css()
        assert "text-size-adjust: 100%" in css
        assert "-webkit-text-size-adjust: 100%" in css

    def test_apple_system_body_root_opt_in(self) -> None:
        """Hardware-found 2026-04-27: text-size-adjust alone is not enough.
        iOS Safari in standalone PWA mode hardcodes root font-size to 16px
        regardless of the user's Larger Text preference. The only way to
        propagate Dynamic Type into a custom-font PWA is `font: -apple-
        system-body` on :root — iOS scales html to the user-preference
        size, body's clamp() on 1rem then scales correctly."""
        css = self._tokens_css()
        # Find the html block (could be `html {` standalone — match the
        # selector that opens with html alone).
        html_start = css.find("html {")
        assert html_start >= 0
        html_end = css.find("}", html_start)
        block = css[html_start:html_end]
        assert "font: -apple-system-body" in block, "iOS PWA Dynamic Type requires `font: -apple-system-body` on root"

    def test_body_keeps_project_font_family(self) -> None:
        """`font: -apple-system-body` shorthand on html sets font-family to
        the iOS system font. Body MUST override back to the project stack
        so the literary serif/sans split is preserved."""
        css = self._tokens_css()
        # Find the combined html, body block where font-family is set.
        combined = css.find("html, body {")
        assert combined >= 0
        end = css.find("}", combined)
        block = css[combined:end]
        assert "font-family: var(--font-sans)" in block

    def test_tabbar_caption_ceiling_raised_above_global(self) -> None:
        """Hardware-found 2026-04-27 (PR #252): tab labels weren't scaling
        under iOS Dynamic Type because --fs-caption ceilings at 14px and
        0.75rem at iOS Larger 3 root (23px) computes to 17.25px — clipped
        instantly. M5 fixed via tactical local override on .tabbar a; M6
        D6 (#258) raised the global ceiling 14 → 18px so the local
        override is no longer needed AND tabs still scale. The fix now
        lives on the global token; pin both halves here so a future edit
        can't silently revert either side."""
        css = self._tokens_css()
        # Global ceiling lifted to 18px.
        assert "--fs-caption: clamp(11px, 0.75rem, 18px)" in css, (
            "M6 D6 / #258: --fs-caption ceiling must be 18px so iOS Dynamic Type can grow tab labels past iOS Larger 1."
        )
        # Tabbar uses the global token now (M5 tactical override removed).
        tabbar_a_start = css.find(".tabbar a {")
        tabbar_a_end = css.find("}", tabbar_a_start)
        block = css[tabbar_a_start:tabbar_a_end]
        assert "font-size: var(--fs-caption)" in block, (
            ".tabbar a should use the global --fs-caption now that the "
            "ceiling is 18px (M6 D6); the M5 tactical local override is gone."
        )


class TestColdLaunchPaint:
    """Hardware-found 2026-04-27: iOS PWA cold launch shows ~2s of black
    screen before content paints. The external tokens.css link blocks
    first paint. Inline critical CSS in <head> paints the bg as soon as
    the HTML parser reaches it, eliminating the black flash."""

    def test_inline_critical_css_paints_bg(self, client) -> None:
        body = client.get("/").data.decode()
        # The inline <style> block must set the antique-paper background
        # so the browser doesn't show the OS-default canvas (black on iOS
        # standalone) while waiting for tokens.css to fetch.
        head_end = body.find("</head>")
        head = body[:head_end]
        assert "<style>" in head
        # Specific value match — drift from --bg in tokens.css is what
        # this test catches.
        assert "background: #FBF6EC" in head
        assert "background: #14110D" in head  # dark mode counterpart

    def test_color_scheme_meta_present(self, client) -> None:
        """`color-scheme: light dark` lets the browser pick the right
        canvas color before user CSS loads — without it, iOS may use a
        white default in dark mode."""
        body = client.get("/").data.decode()
        assert 'color-scheme" content="light dark"' in body

    def test_ios_pwa_meta_tags_present(self, client) -> None:
        """Without these meta tags, iOS treats the page as a regular
        browser tab and AtHS launches into Safari chrome instead of
        standalone mode. Pinning so a future template edit doesn't drop
        them."""
        body = client.get("/").data.decode()
        assert 'name="apple-mobile-web-app-capable" content="yes"' in body
        assert 'name="mobile-web-app-capable" content="yes"' in body  # Android
        assert 'name="apple-mobile-web-app-status-bar-style"' in body
        assert 'name="apple-mobile-web-app-title" content="LitClock"' in body


class TestAppleTouchIconMatrix:
    """Hardware-found regression on PR #252: iOS Add-to-Home-Screen showed a
    capital "L" placeholder instead of the logo. iOS 17 generates the
    initial-letter placeholder when no apple-touch-icon matches the
    device's pixel density. Pinning the size matrix so each iPhone/iPad
    pixel-density tier has a matching icon."""

    @pytest.mark.parametrize(
        "size,filename",
        [
            (180, "icon-bg-baked-180.png"),
            (167, "icon-bg-baked-167.png"),
            (152, "icon-bg-baked-152.png"),
        ],
    )
    def test_apple_touch_icon_size_present_on_disk(self, size: int, filename: str) -> None:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        path = repo_root / "src/control_server/static/icons" / filename
        assert path.exists(), f"missing apple-touch-icon variant: {filename}"
        # Confirm dimensions match the iOS-required size.
        from PIL import Image

        with Image.open(path) as img:
            assert img.size == (size, size)
            # bg-baked corner pixel = #FBF6EC, never transparent — iOS's auto
            # mask shouldn't see through the icon.
            corner = img.convert("RGBA").getpixel((0, 0))
            assert corner == (251, 246, 236, 255)

    def test_template_links_each_apple_touch_icon_size(self, client) -> None:
        """The fix isn't just shipping the files — the template must
        reference each size with an explicit `sizes=` attribute or iOS won't
        find them."""
        body = client.get("/").data.decode()
        for size in ("180x180", "167x167", "152x152"):
            assert f'sizes="{size}"' in body, f"apple-touch-icon size={size} link missing"
        # Universal fallback (no sizes attr) must point at a real bg-baked
        # variant so iOS versions that ignore the size matrix still get an icon.
        assert 'rel="apple-touch-icon" href=' in body or 'apple-touch-icon" href=' in body


# ---------- M2: /api/status + Status hero render (PR #245) ----------


def _write_status_payload(path: Path, **overrides) -> None:
    """Write a synthetic status JSON like the one literary_clock.py writes."""
    import json
    import time as _time

    payload = {
        "time": "08:42",
        "picked_at": _time.time(),
        "quote": "It was the best of times, it was the worst of times.",
        "author": "Charles Dickens",
        "title": "A Tale of Two Cities",
        "image_path": "/var/lib/litclock/images/metadata/quote_0842_0_credits.png",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def status_file(tmp_path):
    """Tmp path to a synthetic status file. Each test writes the payload
    they want before fetching /api/status or /."""
    return tmp_path / "litclock-current-quote.json"


@pytest.fixture
def status_app(status_file):
    """App with STATUS_FILE pointed at the tmp path."""
    return create_app({"VERSION_OVERRIDE": "v0.test", "STATUS_FILE": str(status_file)})


@pytest.fixture
def status_client(status_app):
    return status_app.test_client()


class TestApiStatus:
    """/api/status response shape contract — must stay stable across M3-M5."""

    def test_returns_documented_shape_with_fresh_quote(self, status_client, status_file) -> None:
        _write_status_payload(status_file)
        r = status_client.get("/api/status")
        assert r.status_code == 200
        body = r.json
        assert body["ok"] is True
        assert body["stale"] is False
        assert body["quote"] == "It was the best of times, it was the worst of times."
        assert body["author"] == "Charles Dickens"
        assert body["title"] == "A Tale of Two Cities"
        assert body["time"] == "08:42"
        assert isinstance(body["picked_at"], (int, float))
        assert body["picked_at_age_s"] is not None
        assert body["picked_at_age_s"] < 90  # just-written file is fresh
        assert body["version"] == "v0.test"
        assert isinstance(body["uptime_s"], int)
        assert "uptime_human" in body
        # Subprocess-derived fields are present even when subprocess fails;
        # the contract is "string, possibly empty", not "missing key".
        assert "wifi_ssid" in body
        assert "weather_city" in body
        # last_update_at can be null when neither update.status nor lkg-sha
        # exists in the test sandbox; the keys must always be present (#330).
        assert "last_update_at" in body
        assert "last_update_at_relative" in body
        assert "last_update_version" in body

    def test_marks_stale_when_picked_at_is_old(self, status_client, status_file) -> None:
        """D2 stale threshold: picked_at_age_s ≥ 90s flips `stale: true`.
        Without the flag the PWA hero card would silently lie about what's
        on the e-ink."""
        import time as _time

        _write_status_payload(status_file, picked_at=_time.time() - 120)
        r = status_client.get("/api/status")
        assert r.status_code == 200
        assert r.json["stale"] is True
        assert r.json["picked_at_age_s"] >= 90

    def test_marks_stale_when_status_file_missing(self, status_client, status_file) -> None:
        """File never existed (clock never rendered) → stale + empty quote
        fields. PWA renders the "Starting up…" empty-state hero copy."""
        assert not status_file.exists()
        r = status_client.get("/api/status")
        assert r.status_code == 200
        body = r.json
        assert body["stale"] is True
        assert body["quote"] == ""
        assert body["author"] == ""
        assert body["picked_at"] is None
        assert body["picked_at_age_s"] is None

    def test_uptime_human_format(self, status_client, status_file) -> None:
        """Uptime appears in the documented "Nd Nh Nm" form so the PWA
        doesn't have to client-format it."""
        _write_status_payload(status_file)
        r = status_client.get("/api/status")
        human = r.json["uptime_human"]
        # Always ends with minutes; bigger units precede when present.
        assert human.endswith("m"), f"uptime_human must end with 'm': {human!r}"

    def test_weather_city_from_env_file_location_name(self, status_file, tmp_path) -> None:
        """Codex /review on M2 caught: weather settings live in env.sh and
        are sourced by runtheclock.sh for litclock.service, NOT inherited
        by litclock-control.service. /api/status must read env.sh directly.
        Adversarial /review on M2 caught the follow-up: env_file MUST be
        plumbed via app.config so test overrides win — reading os.environ
        directly silently bypasses the test fixture."""
        _write_status_payload(status_file)
        env_sh = tmp_path / "env.sh"
        env_sh.write_text("WEATHER_LOCATION_NAME=Austin, TX\nWEATHER_LATITUDE=30.27\nWEATHER_LONGITUDE=-97.74\n")
        app = create_app(
            {
                "VERSION_OVERRIDE": "v0.test",
                "STATUS_FILE": str(status_file),
                "ENV_FILE": str(env_sh),
            }
        )
        r = app.test_client().get("/api/status")
        assert r.json["weather_city"] == "Austin, TX"

    def test_weather_city_falls_back_to_env_file_coords(self, status_file, tmp_path) -> None:
        _write_status_payload(status_file)
        env_sh = tmp_path / "env.sh"
        env_sh.write_text("WEATHER_LATITUDE=30.27\nWEATHER_LONGITUDE=-97.74\n")
        app = create_app(
            {
                "VERSION_OVERRIDE": "v0.test",
                "STATUS_FILE": str(status_file),
                "ENV_FILE": str(env_sh),
            }
        )
        r = app.test_client().get("/api/status")
        assert r.json["weather_city"] == "30.27, -97.74"

    def test_app_config_env_file_wins_over_process_env(self, status_file, tmp_path, monkeypatch) -> None:
        """Adversarial /review on M2: app.config['ENV_FILE'] must beat
        os.environ['LITCLOCK_ENV_FILE'] so test overrides aren't bypassed.
        Without this fix, the test fixture's env_sh is silently ignored
        in favor of whatever LITCLOCK_ENV_FILE the parent process exports."""
        _write_status_payload(status_file)
        process_env_sh = tmp_path / "process-env.sh"
        process_env_sh.write_text("WEATHER_LOCATION_NAME=Process Env\n")
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(process_env_sh))

        config_env_sh = tmp_path / "config-env.sh"
        config_env_sh.write_text("WEATHER_LOCATION_NAME=Via App Config\n")
        app = create_app(
            {
                "VERSION_OVERRIDE": "v0.test",
                "STATUS_FILE": str(status_file),
                "ENV_FILE": str(config_env_sh),
            }
        )
        r = app.test_client().get("/api/status")
        assert r.json["weather_city"] == "Via App Config"

    def test_weather_city_empty_when_env_file_missing(self, status_client, status_file, tmp_path, monkeypatch) -> None:
        _write_status_payload(status_file)
        # config.load_config returns {} for missing files; weather row
        # should show "—" gracefully.
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-such-env.sh"))
        r = status_client.get("/api/status")
        assert r.json["weather_city"] == ""

    def test_weather_does_not_use_process_env(self, status_client, status_file, monkeypatch) -> None:
        """Anti-regression: process-env WEATHER_* must NOT leak into the
        Status response. The control_server process inherits only
        LITCLOCK_* from systemd; reading process-env for weather would
        silently reintroduce the bug codex caught."""
        _write_status_payload(status_file)
        monkeypatch.delenv("LITCLOCK_ENV_FILE", raising=False)
        monkeypatch.setenv("WEATHER_LOCATION_NAME", "should-not-appear")
        monkeypatch.setenv("WEATHER_LATITUDE", "1")
        monkeypatch.setenv("WEATHER_LONGITUDE", "2")
        r = status_client.get("/api/status")
        assert r.json["weather_city"] == ""


class TestLastUpdateRowResolution:
    """#330: Status hero 'Last update' row was rendering em-dash right after
    a successful update because the route queried systemctl
    ActiveEnterTimestamp on litclock-update.service, which doesn't reliably
    populate for the PWA-triggered apply path. The fix reads the on-disk
    files instead — preferring /run/litclock/update.status (state=complete)
    and falling back to /var/lib/litclock/lkg-sha mtime + sha7.

    These tests pin the three file-presence permutations so a future refactor
    can't silently regress the em-dash bug."""

    def _make_app(self, *, status_file, update_status_path=None, last_update_path=None, lkg_sha_path=None):
        config = {"VERSION_OVERRIDE": "v0.test", "STATUS_FILE": str(status_file)}
        # All three paths default to nonexistent tmp targets so a test that wants
        # "neither file present" doesn't accidentally pick up the real Pi paths
        # under /run or /var/lib if the dev box has them (it won't, but pin it).
        config["UPDATE_STATUS_FILE"] = str(update_status_path) if update_status_path else "/nonexistent/update.status"
        config["LAST_UPDATE_FILE"] = str(last_update_path) if last_update_path else "/nonexistent/last-update.json"
        config["LKG_SHA_FILE"] = str(lkg_sha_path) if lkg_sha_path else "/nonexistent/lkg-sha"
        return create_app(config).test_client()

    def test_update_status_complete_surfaces_to_version_and_relative_time(self, status_file, tmp_path) -> None:
        """Source 1: /run/litclock/update.status with state=complete must
        surface `to_version` + a relative-time string built from
        `finished_at_unix`. The bug from the issue (em-dash) lives here —
        if this assertion fails, the regression is back."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        finished = _time.time() - 180  # 3 minutes ago
        update_status.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "phase_index": 7,
                    "phase_name": "Restarting",
                    "started_at_unix": int(finished - 30),
                    "finished_at_unix": int(finished),
                    "from_version": "116db7d",
                    "to_version": "5f12b8b",
                    "error": None,
                }
            )
        )
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json
        assert body["last_update_version"] == "5f12b8b"
        # 180s = 3 minutes — _format_relative renders "3 minutes ago".
        assert body["last_update_at_relative"] == "3 minutes ago"
        assert body["last_update_at"] is not None
        assert "T" in body["last_update_at"], "last_update_at must be ISO-8601"

    def test_update_status_non_complete_falls_through_to_lkg(self, status_file, tmp_path) -> None:
        """If update.status exists but state is `running` / `failed_*`, that's
        not a 'last update' signal — fall through to the LKG mtime so the
        row reflects the most recent SHIPPED version, not the in-flight one."""
        import json

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(json.dumps({"state": "running", "phase_index": 3}))
        lkg = tmp_path / "lkg-sha"
        lkg.write_text("a5c0b35538cf9bd1234abcdef0987654321deadb\n")
        # Backdate to a known mtime.
        import os as _os
        import time as _time

        ts = _time.time() - 7200  # 2 hours ago
        _os.utime(lkg, (ts, ts))
        client = self._make_app(status_file=status_file, update_status_path=update_status, lkg_sha_path=lkg)
        body = client.get("/api/status").json
        assert body["last_update_version"] == "a5c0b35"  # 7-char prefix
        assert body["last_update_at_relative"] == "2 hours ago"

    def test_falls_back_to_lkg_when_update_status_absent(self, status_file, tmp_path) -> None:
        """Source 2: /run is tmpfs — after a reboot, update.status is gone
        even though the Pi just shipped a new version. lkg-sha persists in
        /var/lib so the row keeps reporting the most recent shipped SHA."""
        import os as _os
        import time as _time

        _write_status_payload(status_file)
        lkg = tmp_path / "lkg-sha"
        lkg.write_text("1063fa33aabbccddee0011223344556677889900\n")
        ts = _time.time() - 90000  # ~1 day ago
        _os.utime(lkg, (ts, ts))
        client = self._make_app(status_file=status_file, lkg_sha_path=lkg)
        body = client.get("/api/status").json
        assert body["last_update_version"] == "1063fa3"
        assert body["last_update_at_relative"] == "1 day ago"

    def test_em_dash_when_neither_source_exists(self, status_file) -> None:
        """Fresh install with no updates yet: both files absent → em-dash
        and last_update_version=None. The original 'no signal' state is
        preserved — we only kill the em-dash when there's a real signal to
        replace it with."""
        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file)
        body = client.get("/api/status").json
        assert body["last_update_version"] is None
        assert body["last_update_at"] is None
        assert body["last_update_at_relative"] == "—"

    def test_template_renders_version_and_relative_when_present(self, status_file, tmp_path) -> None:
        """SSR side of the fix: status.html.j2 must render the mono SHA span,
        the comma separator, and the relative-time span (all unhidden) when
        last_update_version is non-null. Pins the DOM hooks status.js
        depends on for in-place patching."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "finished_at_unix": int(_time.time() - 60),
                    "to_version": "5f12b8b",
                }
            )
        )
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/").data.decode()
        # Version span renders with the mono class + sha; separator + relative
        # span are present and unhidden.
        assert "data-status-last-update-version" in body
        assert "5f12b8b" in body
        assert "data-status-last-update-sep" in body
        assert "data-status-last-update-relative" in body
        # When version is present, neither version nor separator carries [hidden].
        version_pos = body.find("data-status-last-update-version")
        version_tag_end = body.find(">", version_pos)
        assert " hidden" not in body[version_pos:version_tag_end]
        sep_pos = body.find("data-status-last-update-sep")
        sep_tag_end = body.find(">", sep_pos)
        assert " hidden" not in body[sep_pos:sep_tag_end]

    def test_template_hides_version_span_when_no_signal(self, status_file) -> None:
        """SSR side of the em-dash branch: when last_update_version is None,
        the version + separator spans must carry [hidden] so they don't
        leak whitespace/punctuation. status.js relies on these toggles."""
        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file)
        body = client.get("/").data.decode()
        version_pos = body.find("data-status-last-update-version")
        version_tag_end = body.find(">", version_pos)
        assert "hidden" in body[version_pos:version_tag_end]
        sep_pos = body.find("data-status-last-update-sep")
        sep_tag_end = body.find(">", sep_pos)
        assert "hidden" in body[sep_pos:sep_tag_end]

    def test_template_pins_parent_and_child_hooks_for_status_js(self, status_file) -> None:
        """#335 + post-#333 contract: the SSR'd `/` must carry BOTH

        - the parent `[data-status-last-update]` element (the fallback target
          status.js writes a combined string to when child hooks are absent),
        - all three child hooks (`-version`, `-sep`, `-relative`) that the
          post-#333 patch path queries.

        If a future template change drops the parent, the #335 service-worker-
        backward-compat shim quietly stops working. If it drops the children,
        post-#333 in-place patching stops working AND the shim takes over
        unconditionally — both regressions silent in the browser. This test
        fires loudly instead.
        """
        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file)
        body = client.get("/").data.decode()
        # Parent: shim fallback target. Negative-lookahead excludes child-hook
        # matches (`data-status-last-update-version`, `-sep`, `-relative`) so
        # dropping ONLY the parent attribute still fails this assertion. The
        # plain `"data-status-last-update" in body` form was satisfied by any
        # child hook and would have silently passed a parent removal — exactly
        # the regression #335 added this test to prevent (review C4).
        assert re.search(r"data-status-last-update(?!-)", body), (
            "parent [data-status-last-update] hook missing — #335 shim has nothing to fall back to"
        )
        # Children: post-#333 in-place patch hooks.
        for hook in (
            "data-status-last-update-version",
            "data-status-last-update-sep",
            "data-status-last-update-relative",
        ):
            assert hook in body, f"child hook missing — post-#333 patch path broken: {hook}"


class TestPhase3SkipMarkerSurface:
    """#274 follow-up #5 — /api/status surfaces `phase3_skipped_at_unix`
    when the Phase 3 skip marker is fresh, and the Status hero template
    renders a hidden banner whose state status.js toggles.

    The reader-side staleness clamp is enforced here: stale markers
    (mtime older than PHASE3_SKIP_FRESH_WINDOW_S) return null even
    when present, so a Pi whose cron stopped firing months ago doesn't
    leave a permanent banner.
    """

    def _make_app(self, *, status_file, phase3_path=None):
        config = {
            "VERSION_OVERRIDE": "v0.test",
            "STATUS_FILE": str(status_file),
            # Default everything else to nonexistent so unrelated readers
            # don't accidentally pick up real Pi paths under /run /var/lib.
            "UPDATE_STATUS_FILE": "/nonexistent/update.status",
            "LAST_UPDATE_FILE": "/nonexistent/last-update.json",
            "LKG_SHA_FILE": "/nonexistent/lkg-sha",
        }
        config["PHASE3_SKIPPED_FILE"] = str(phase3_path) if phase3_path else "/nonexistent/phase3-skipped"
        return create_app(config).test_client()

    def test_marker_absent_returns_null(self, status_file):
        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file)
        body = client.get("/api/status").json
        assert "phase3_skipped_at_unix" in body, "field must always be present in the API contract"
        assert body["phase3_skipped_at_unix"] is None

    def test_fresh_marker_surfaces_mtime(self, status_file, tmp_path):
        """Marker file mtime within the last day → field carries the
        epoch seconds. PWA renders the banner."""
        import os as _os
        import time as _time

        marker = tmp_path / "phase3-skipped"
        marker.write_text("")  # mtime-only marker; zero-byte body
        # Backdate to a known-fresh mtime: 10 minutes ago.
        ts = _time.time() - 600
        _os.utime(marker, (ts, ts))
        client = self._make_app(status_file=status_file, phase3_path=marker)
        _write_status_payload(status_file)
        body = client.get("/api/status").json
        assert body["phase3_skipped_at_unix"] is not None
        # Floating-point round-trip ≤ 1s.
        assert abs(body["phase3_skipped_at_unix"] - ts) < 1.0

    def test_stale_marker_returns_null(self, status_file, tmp_path):
        """Marker older than PHASE3_SKIP_FRESH_WINDOW_S (1 day) → null.
        Banner self-clears even if cron stopped firing."""
        import os as _os
        import time as _time

        marker = tmp_path / "phase3-skipped"
        marker.write_text("")
        # 2 days ago — well past the 1-day window.
        ts = _time.time() - (2 * 86400)
        _os.utime(marker, (ts, ts))
        client = self._make_app(status_file=status_file, phase3_path=marker)
        _write_status_payload(status_file)
        body = client.get("/api/status").json
        assert body["phase3_skipped_at_unix"] is None

    def test_future_mtime_returns_null(self, status_file, tmp_path):
        """mtime in the future (clock drift pre-NTP on Pi Zero 2W with no
        RTC) should clamp to null rather than report a negative age."""
        import os as _os
        import time as _time

        marker = tmp_path / "phase3-skipped"
        marker.write_text("")
        ts = _time.time() + 3600  # 1 hour in the future
        _os.utime(marker, (ts, ts))
        client = self._make_app(status_file=status_file, phase3_path=marker)
        _write_status_payload(status_file)
        body = client.get("/api/status").json
        assert body["phase3_skipped_at_unix"] is None

    def test_symlink_marker_returns_null(self, status_file, tmp_path):
        """Defense-in-depth — a planted symlink at the marker path should
        not follow. Same guard the Last-Update bounded readers use."""
        target = tmp_path / "elsewhere"
        target.write_text("")
        marker = tmp_path / "phase3-skipped"
        marker.symlink_to(target)
        client = self._make_app(status_file=status_file, phase3_path=marker)
        _write_status_payload(status_file)
        body = client.get("/api/status").json
        assert body["phase3_skipped_at_unix"] is None

    def test_status_template_renders_hidden_banner_when_marker_absent(self, status_file):
        """Server-side first-paint correctness: with no fresh marker, the
        banner element is in the DOM but `[hidden]`. status.js toggles
        the attribute on subsequent polls — needs the element present.

        Anchor-match the `hidden` HTML attribute precisely (the SVG inside
        the banner has `aria-hidden="true"`, which is a different DOM
        property and not what we're gating)."""
        import re

        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file)
        r = client.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "data-status-phase3-skip-banner" in body, "banner element must be rendered (hidden) on SSR"
        # Look for the banner opening tag and check it has the `hidden` attribute.
        # The Jinja template prints `data-status-phase3-skip-banner hidden>` when
        # marker is absent, `data-status-phase3-skip-banner >` when fresh.
        match = re.search(r"data-status-phase3-skip-banner\s*([^>]*)>", body)
        assert match is not None, "banner opening tag not found"
        # The attribute list between the data-* hook and the closing `>`.
        attrs = match.group(1)
        assert re.search(r"\bhidden\b", attrs), "banner must have the `hidden` HTML attribute when marker is absent"

    def test_status_template_renders_visible_banner_when_marker_fresh(self, status_file, tmp_path):
        """SSR correctness with a fresh marker — the element renders
        without the `hidden` HTML attribute so even the no-JS path sees
        the banner. (The SVG inside the banner has `aria-hidden`, which
        is a different DOM property — we anchor the regex on the opening
        tag attribute list to avoid matching the SVG's aria-hidden.)"""
        import os as _os
        import re
        import time as _time

        marker = tmp_path / "phase3-skipped"
        marker.write_text("")
        ts = _time.time() - 600
        _os.utime(marker, (ts, ts))
        client = self._make_app(status_file=status_file, phase3_path=marker)
        _write_status_payload(status_file)
        r = client.get("/")
        body = r.get_data(as_text=True)
        match = re.search(r"data-status-phase3-skip-banner\s*([^>]*)>", body)
        assert match is not None
        attrs = match.group(1)
        assert not re.search(r"\bhidden\b", attrs), (
            "banner must NOT have the `hidden` HTML attribute when marker is fresh — "
            "SSR no-JS path should display the banner immediately"
        )


class TestUpdateProgressSurface:
    """#274 follow-up #2 — /api/status surfaces `update_state` and
    `update_phase_index` for the Settings tab's "Update in progress"
    banner. Both null when no update.sh is in flight (the common
    steady-state). When update.status has `state=running`, the fields
    carry the current phase so the PWA can show the banner only when
    Save is actually at risk of blocking on the env.sh flock (Phase 3/4).
    """

    def _make_app(self, *, status_file, update_status_path=None):
        config = {
            "VERSION_OVERRIDE": "v0.test",
            "STATUS_FILE": str(status_file),
            "LAST_UPDATE_FILE": "/nonexistent/last-update.json",
            "LKG_SHA_FILE": "/nonexistent/lkg-sha",
        }
        config["UPDATE_STATUS_FILE"] = str(update_status_path) if update_status_path else "/nonexistent/update.status"
        return create_app(config).test_client()

    def test_no_update_in_flight_returns_nulls(self, status_file):
        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file)
        body = client.get("/api/status").json
        assert "update_state" in body
        assert "update_phase_index" in body
        assert body["update_state"] is None
        assert body["update_phase_index"] is None

    def test_running_state_phase4_surfaced(self, status_file, tmp_path):
        """Phase 4 (pip install) — the long-running phase where users
        are most likely to see the banner. State should round-trip from
        update.status."""
        import json

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(
            json.dumps({"state": "running", "phase_index": 4, "phase_name": "Updating Python packages"})
        )
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_state"] == "running"
        assert body["update_phase_index"] == 4

    def test_running_state_phase3_surfaced(self, status_file, tmp_path):
        """Phase 3 (env.sh merge) — the only phase that actually holds
        the shared sidecar flock. Most consequential for the banner."""
        import json

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(json.dumps({"state": "running", "phase_index": 3}))
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_state"] == "running"
        assert body["update_phase_index"] == 3

    def test_complete_state_surfaces_too(self, status_file, tmp_path):
        """state=complete should also round-trip; the PWA banner gate
        ignores anything that isn't `running` so the surface is honest
        but the banner stays hidden post-completion."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "phase_index": 7,
                    "phase_name": "Restarting",
                    "started_at_unix": int(_time.time() - 60),
                    "finished_at_unix": int(_time.time()),
                    "to_version": "5507be5",
                }
            )
        )
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_state"] == "complete"
        assert body["update_phase_index"] == 7

    def test_malformed_json_returns_nulls(self, status_file, tmp_path):
        """Garbage in update.status (truncated write, SD-card flip) must
        not crash the route — fall through to null and let the banner
        stay hidden."""
        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text("not valid json {{{")
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_state"] is None
        assert body["update_phase_index"] is None

    def test_stale_running_state_returns_nulls(self, status_file, tmp_path):
        """Adversarial-review P1 — `state=running` with started_at_unix
        older than `UPDATE_RUNNING_TIMEOUT_S` (30min) must be treated as
        stale and return null so the Settings banner self-clears.

        Covers the SIGKILL/OOM/power-loss case where update.sh dies
        during Phase 4 without writing a terminal state=complete or
        state=failed_*. /run is tmpfs and clears at reboot, but a user
        who interacts only via the web (no reboot) would otherwise see
        the banner stuck on every 15s, every visit to /settings.
        """
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        # 1 hour ago — well past the 30-min budget.
        stale_start = _time.time() - 3600
        update_status.write_text(
            json.dumps({"state": "running", "phase_index": 4, "started_at_unix": int(stale_start)})
        )
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_state"] is None, (
            "stale state=running (started > UPDATE_RUNNING_TIMEOUT_S ago) "
            "must clamp to null so the Settings banner self-clears"
        )
        assert body["update_phase_index"] is None

    def test_fresh_running_state_within_budget_surfaces(self, status_file, tmp_path):
        """Mirror: a fresh state=running (started a few seconds ago) must
        STILL surface. Otherwise the clamp would defeat the banner's
        whole purpose."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(
            json.dumps({"state": "running", "phase_index": 4, "started_at_unix": int(_time.time() - 30)})
        )
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_state"] == "running"
        assert body["update_phase_index"] == 4

    def test_running_state_without_started_at_surfaces(self, status_file, tmp_path):
        """If `started_at_unix` is absent (old update.sh schema), don't
        apply the clamp — surface the state. Backward-compat with any
        pre-#274 writer that omitted the field."""
        import json

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(json.dumps({"state": "running", "phase_index": 4}))
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_state"] == "running"
        assert body["update_phase_index"] == 4

    def test_phase_index_boolean_rejected(self, status_file, tmp_path):
        """Adversarial-review P2 — `isinstance(True, int) is True` in
        Python. A hand-edited or buggy writer emitting
        `"phase_index": true` must NOT round-trip as the integer 1
        (which would match the banner's `{3, 4}` lookup if the bool
        somehow resolved to 1)."""
        import json

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(json.dumps({"state": "running", "phase_index": True}))
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        body = client.get("/api/status").json
        assert body["update_phase_index"] is None, (
            "boolean phase_index must not round-trip as int — exclude bool explicitly in the isinstance check"
        )

    def test_settings_template_renders_hidden_banner(self, status_file):
        """SSR correctness — the Settings banner element renders hidden
        by default. settings.js toggles based on /api/status poll.

        The SSR template doesn't have live update state available (it
        renders before the JS polls), so the banner is always hidden at
        first paint. JS shows it within ~15s if an update.sh is in flight."""
        import re

        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file)
        r = client.get("/settings")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "data-settings-update-banner" in body, "Settings update banner element must be present"
        match = re.search(r"data-settings-update-banner\s*([^>]*)>", body)
        assert match is not None
        attrs = match.group(1)
        assert re.search(r"\bhidden\b", attrs), "Settings update banner must have the `hidden` HTML attribute on SSR"


class TestUpdatesJsSeenRunningArmed:
    """#329 (review C2): the optimistic-tick branch in updates.js depends on a
    module-local `seenRunning` flag that MUST be armed at user-action time
    (inside `fireApply`) AND from poll observations (inside
    `handleStatusPayload`). If a refactor drops the fireApply assignment, the
    very first-poll-timeout after Apply skips the tick — the user stares at a
    Phase 1 spinner during Phase 7 reconnect, exactly the regression the fix
    exists to prevent.

    Primary behavior coverage now lives in
    tests/js/updates.optimistic-tick.test.js (vitest + jsdom, #338). These
    structural greps stay as belt-and-suspenders — they fail fast on a
    literal-token drop without spinning up the JS runner, complementing the
    behavior tests rather than replacing them.
    """

    UPDATES_JS = Path(__file__).resolve().parents[1] / "src" / "control_server" / "static" / "js" / "updates.js"

    def _extract_function_body(self, src: str, name: str) -> str:
        """Return the brace-balanced body of `function name(...) { ... }`.
        Brittle on nested-string `}` but updates.js doesn't have any."""
        match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", src)
        assert match, f"function {name}(...) not found in updates.js"
        depth = 1
        i = match.end()
        while i < len(src) and depth > 0:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, f"unbalanced braces while scanning {name}"
        return src[match.end() : i - 1]

    def test_seen_running_armed_in_fire_apply(self) -> None:
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "fireApply")
        assert "seenRunning = true" in body, (
            "fireApply() must arm seenRunning at user-action time so the optimistic "
            "Phase-7 tick still fires when the first scheduled poll times out (#329 review C2)"
        )

    def test_seen_running_armed_in_handle_status_payload(self) -> None:
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "handleStatusPayload")
        assert "seenRunning = true" in body, (
            "handleStatusPayload() must arm seenRunning when a poll observes state=running "
            "so a tab navigated back mid-update also gets the optimistic-tick safety net"
        )


class TestUpdatesJsSchedulePollGuard:
    """#348 + codex adversarial findings 1+2: the polling state machine has
    TWO scheduled units (pending setTimeout AND in-flight pollStatusOnce fetch)
    that can each independently re-arm the cycle. The shallow #348 fix only
    covered the pending-timer case and left a 1-2s window where a delayed
    `fireApply.catch()` could slip past the `if (pollTimer)` guard (timer is
    null mid-fetch) and arm a NEW timer in parallel with an in-flight fetch
    that's about to transition to terminal/reconnect — fork point for
    competing pollHealth loops.

    Deeper fix introduces THREE invariants this test class pins:

    1. A `pollGeneration` counter (or equivalent) is captured at arm time
       and checked when callbacks fire — so any callback whose generation
       was bumped fails closed instead of re-arming.
    2. A `cancelPolling()` helper bumps the generation AND clears the
       pending timer. Every terminal branch of `handleStatusPayload`
       (idle, complete, failed_reverted, failed_unrecovered, null/network-
       failure) and `enterReconnectMode` must invoke it.
    3. `enterReconnectMode` has an idempotence guard so that even if a
       stale callback somehow slips through, a second call can't fork
       a parallel `pollHealth` loop.

    Vitest infra landed in #338 (tests/js/updates.optimistic-tick.test.js
    covers the #329 optimistic-tick branch with real DOM behavior tests).
    The #348/#352/#354 race-logic pinned by this class does NOT yet have a
    behavior counterpart in tests/js/ — porting these invariants to vitest
    is the natural next follow-up. Keep these structural greps live until
    that port lands.
    """

    UPDATES_JS = Path(__file__).resolve().parents[1] / "src" / "control_server" / "static" / "js" / "updates.js"

    # All terminal/reconnect-entering branches of handleStatusPayload. Each
    # must invalidate the polling generation before transitioning away
    # from the polling loop, or a late callback can re-enter.
    TERMINAL_STATES = ("complete", "failed_reverted", "failed_unrecovered", "idle")

    def _extract_function_body(self, src: str, name: str) -> str:
        """Brace-balanced body extract, mirrors TestUpdatesJsSeenRunningArmed."""
        match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", src)
        assert match, f"function {name}(...) not found in updates.js"
        depth = 1
        i = match.end()
        while i < len(src) and depth > 0:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, f"unbalanced braces while scanning {name}"
        return src[match.end() : i - 1]

    def _extract_state_branch(self, body: str, state: str) -> str:
        """Return the body of `if (payload.state === '<state>') { ... }`."""
        match = re.search(
            rf"if\s*\(\s*payload\.state\s*===\s*['\"]{re.escape(state)}['\"]\s*\)\s*\{{",
            body,
        )
        assert match, f"state branch for '{state}' not found in handleStatusPayload"
        depth = 1
        i = match.end()
        while i < len(body) and depth > 0:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, f"unbalanced braces while scanning state branch '{state}'"
        return body[match.end() : i - 1]

    def test_schedule_poll_has_sentinel_guard(self) -> None:
        """schedulePoll must early-return when pollTimer is already armed.
        Without this, two callers in quick succession arm parallel polling
        cycles (#348 base scenario)."""
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "schedulePoll")
        assert re.search(r"if\s*\(\s*pollTimer\s*\)\s*return\s*;", body), (
            "schedulePoll() must early-return when pollTimer is already set so "
            "two callers in quick succession can't double-arm parallel polling cycles (#348)"
        )

    def test_schedule_poll_clears_timer_at_callback_start(self) -> None:
        """The setTimeout callback must clear `pollTimer = null` BEFORE
        invoking pollStatusOnce, so handleStatusPayload's recursive
        schedulePoll() can arm the next tick (#348)."""
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "schedulePoll")
        inner = re.search(
            r"setTimeout\s*\(\s*function\s*\([^)]*\)\s*\{(?P<inner>.*?)\}\s*,",
            body,
            re.DOTALL,
        )
        assert inner, "setTimeout(function () { ... }, ...) shape changed — review #348 guard"
        inner_body = inner.group("inner")
        null_pos = inner_body.find("pollTimer = null")
        poll_pos = inner_body.find("pollStatusOnce(")
        assert null_pos != -1, "setTimeout callback missing `pollTimer = null` (#348)"
        assert poll_pos != -1, "pollStatusOnce(...) call missing from schedulePoll callback"
        assert null_pos < poll_pos, (
            "`pollTimer = null` must appear BEFORE pollStatusOnce(...) — clearing "
            "after would block handleStatusPayload's recursive schedulePoll() (#348)"
        )

    def test_cancel_polling_helper_exists(self) -> None:
        """A dedicated cancelPolling() (or equivalent) function must exist
        as the single source of truth for invalidating pending+in-flight
        work. Without it, terminal branches each have to know to clear the
        timer AND bump generation, and refactor drift will desync them
        (codex finding 1)."""
        src = self.UPDATES_JS.read_text()
        # Function declaration must exist somewhere top-level
        assert re.search(r"function\s+cancelPolling\s*\(", src), (
            "cancelPolling() helper missing — terminal/reconnect branches need a "
            "single function that invalidates BOTH pending timer AND in-flight "
            "fetch callbacks (codex finding 1)"
        )

    def test_cancel_polling_bumps_generation(self) -> None:
        """cancelPolling must bump a generation counter (or equivalent
        invalidation token) so any in-flight pollStatusOnce callback that
        captured the prior generation short-circuits when it lands. Just
        clearing pollTimer is not sufficient — the fetch is already in
        flight (codex finding 1)."""
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "cancelPolling")
        # Accept either `pollGeneration++` or `pollGeneration += 1` style
        assert re.search(r"pollGeneration\s*(\+\+|\+=\s*1)", body), (
            "cancelPolling() must bump pollGeneration so any in-flight fetch "
            "callback that captured the prior generation fails the gen check "
            "and short-circuits (codex finding 1)"
        )

    def test_schedule_poll_captures_generation_at_arm_time(self) -> None:
        """schedulePoll's setTimeout callback must compare the captured
        generation against the current pollGeneration before re-entering
        handleStatusPayload. Without this, a stale callback whose timer
        cancelPolling missed (e.g. between arm and clearTimeout) still
        re-arms the cycle (codex finding 1)."""
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "schedulePoll")
        # `var gen = pollGeneration` (capture) and `gen !== pollGeneration`
        # (check) must both appear inside schedulePoll.
        assert re.search(r"var\s+gen\s*=\s*pollGeneration", body), (
            "schedulePoll must capture `var gen = pollGeneration` at arm time so "
            "the callback can detect cancellation (codex finding 1)"
        )
        assert re.search(r"gen\s*!==\s*pollGeneration", body), (
            "schedulePoll's callback must check `gen !== pollGeneration` to "
            "short-circuit when the generation has been bumped (codex finding 1)"
        )

    def test_terminal_branches_cancel_polling(self) -> None:
        """Every terminal branch of handleStatusPayload (and the network-
        failure branch) must call cancelPolling() before transitioning out
        of the polling loop. Otherwise an in-flight fetch lands after the
        terminal action and re-enters handleStatusPayload (codex finding 1)."""
        src = self.UPDATES_JS.read_text()
        handler_body = self._extract_function_body(src, "handleStatusPayload")
        for state in self.TERMINAL_STATES:
            branch = self._extract_state_branch(handler_body, state)
            assert "cancelPolling()" in branch, (
                f"terminal branch for state='{state}' must call cancelPolling() "
                "before transitioning out of the polling loop, or an in-flight "
                "fetch can re-enter handleStatusPayload after terminal action "
                "(codex finding 1)"
            )

    def test_network_failure_branch_cancels_polling(self) -> None:
        """The `if (!payload)` (network failure) branch enters reconnect
        mode. It must cancel polling first so a delayed in-flight fetch
        can't slip through and re-enter (codex finding 1)."""
        src = self.UPDATES_JS.read_text()
        handler_body = self._extract_function_body(src, "handleStatusPayload")
        # Grab the body of `if (!payload) { ... }` — first if-block inside.
        match = re.search(r"if\s*\(\s*!\s*payload\s*\)\s*\{", handler_body)
        assert match, "`if (!payload)` branch missing from handleStatusPayload"
        depth = 1
        i = match.end()
        while i < len(handler_body) and depth > 0:
            if handler_body[i] == "{":
                depth += 1
            elif handler_body[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, "unbalanced braces in `if (!payload)` branch"
        branch_body = handler_body[match.end() : i - 1]
        assert "cancelPolling()" in branch_body, (
            "`if (!payload)` branch must call cancelPolling() before entering "
            "reconnect mode — otherwise an in-flight fetch can re-enter "
            "handleStatusPayload after reconnect arms (codex finding 1)"
        )

    def test_enter_reconnect_mode_has_idempotence_guard(self) -> None:
        """enterReconnectMode must early-return if already armed.
        Defense-in-depth: even though cancelPolling() should prevent any
        stale callback from reaching here, a missed guard somewhere would
        let two pollHealth loops race against /api/health (codex finding 2)."""
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "enterReconnectMode")
        # Match `if (reconnectArmed) return` allowing whitespace variations.
        assert re.search(r"if\s*\(\s*reconnectArmed\s*\)\s*return\s*;", body), (
            "enterReconnectMode() must early-return when reconnectArmed is true, "
            "so a stale callback that slips past cancelPolling can't fork a "
            "parallel pollHealth loop (codex finding 2)"
        )
        # And it must set the flag for next time.
        assert re.search(r"reconnectArmed\s*=\s*true", body), (
            "enterReconnectMode() must set reconnectArmed = true so the next call early-returns (codex finding 2)"
        )


class TestUpdatesJsProbeRendersTerminalCopy:
    """#352: the page-load probe (the top-level `pollStatusOnce(function (payload) { ... })`
    immediately under `refreshCheck()`) was a cold-load gap. When a user navigated
    to /updates AFTER a failed update — rather than being on the page when it failed —
    the probe called `enterReadingList(payload)` only. So the phase reading-list
    rendered with a frozen "active" row and NO terminal copy. Looks like a stuck
    in-flight update instead of a finished failure.

    The in-flight polling path is fine: `handleStatusPayload`'s failed_* branches
    call `showTerminal` directly. The gap was the cold-load / refresh path only.

    Fix shape pinned by these tests:
        - The probe's failed branch (or its dispatch) must call `showTerminal`
          for BOTH `failed_reverted` and `failed_unrecovered` states.
        - The default messages must match the in-flight handler's defaults
          (consistency — otherwise the user sees different copy depending on
          whether they were on the page during the failure).

    Vitest infra is deferred to #338, so this is a structural grep: it isolates
    the probe block (top-level pollStatusOnce ... pre-`fireApply`) and asserts
    the failed_* state names + `showTerminal(` symbol both appear inside it.
    Cheap and durable until #338 lands real JS unit tests.
    """

    UPDATES_JS = Path(__file__).resolve().parents[1] / "src" / "control_server" / "static" / "js" / "updates.js"

    # Mirror the in-flight handler's defaults — these strings are user-facing
    # copy that must stay consistent across cold-load and in-flight terminal
    # renders. If either string changes, update BOTH locations or the user
    # sees different copy depending on whether they were on the page during
    # the failure (UX inconsistency #352 fix exists to prevent).
    REVERTED_DEFAULT = "Update failed verification — rolled back. Your clock is running normally."
    UNRECOVERED_DEFAULT = (
        "Update did not finish. Try again in a few minutes; if it still fails, restart from the System tab."
    )

    def _extract_probe_block(self, src: str) -> str:
        """Return the body of the cold-load probe handler. Was an anonymous
        `pollStatusOnce(function (payload) {...})` callback; #354 codex P2
        follow-up extracted it to a named `handleProbePayload(payload)` so
        the dialog 'close' listener can replay deferred payloads through the
        same logic. Accept either shape so a future intentional re-inline
        doesn't break this helper."""
        match = re.search(
            r"function\s+handleProbePayload\s*\(\s*payload\s*\)\s*\{",
            src,
        )
        if match is None:
            match = re.search(
                r"pollStatusOnce\s*\(\s*function\s*\(\s*payload\s*\)\s*\{",
                src,
            )
        assert match, "page-load probe handler not found (looked for handleProbePayload and the legacy anonymous form)"
        depth = 1
        i = match.end()
        while i < len(src) and depth > 0:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, "unbalanced braces while scanning probe block"
        return src[match.end() : i - 1]

    def test_probe_block_references_both_failed_states(self) -> None:
        """Sanity: the probe block must explicitly mention BOTH failed state
        names. Without this, the fix could quietly drop one branch and only
        half the failure surface gets terminal copy on cold load (#352)."""
        src = self.UPDATES_JS.read_text()
        probe = self._extract_probe_block(src)
        assert "failed_reverted" in probe, (
            "probe block must reference 'failed_reverted' so cold-load renders "
            "rolled-back terminal copy after a failed update (#352)"
        )
        assert "failed_unrecovered" in probe, (
            "probe block must reference 'failed_unrecovered' so cold-load renders "
            "manual-recovery terminal copy after a failed update (#352)"
        )

    def test_probe_block_calls_show_terminal(self) -> None:
        """The probe block must call showTerminal so the user sees recovery
        copy on cold load — not just a frozen phase reading-list (#352).

        Pre-fix the probe called enterReadingList only; the failure state was
        rendered as a static row with no banner, indistinguishable from a
        stuck in-flight update to a user navigating in fresh."""
        src = self.UPDATES_JS.read_text()
        probe = self._extract_probe_block(src)
        assert "showTerminal(" in probe, (
            "probe block must call showTerminal(...) on failed_* states so a "
            "cold-load navigation to /updates after a failed update surfaces "
            "recovery copy, not just a frozen phase reading-list (#352)"
        )

    def test_probe_block_uses_canonical_default_messages(self) -> None:
        """The cold-load defaults must match the in-flight handler's defaults
        verbatim — otherwise the user sees different copy depending on whether
        they were on the page during the failure (UX inconsistency #352 was
        filed to prevent)."""
        src = self.UPDATES_JS.read_text()
        probe = self._extract_probe_block(src)
        assert self.REVERTED_DEFAULT in probe, (
            "probe block's failed_reverted default must match handleStatusPayload's "
            f"verbatim: {self.REVERTED_DEFAULT!r} (#352)"
        )
        assert self.UNRECOVERED_DEFAULT in probe, (
            "probe block's failed_unrecovered default must match handleStatusPayload's "
            f"verbatim: {self.UNRECOVERED_DEFAULT!r} (#352)"
        )


class TestUpdatesJsProbeRaceGuards:
    """#354 (codex adversarial on PR #353) + claude adversarial on PR #359:
    three narrow second-order races where the page-load probe interacts
    with concurrent user action. All are pre-existing edge windows that
    #353's cold-load showTerminal call exposed (Races 1+2) or that the
    Race 2 guard's branch-local placement left half-covered (Race 3).

    Race 1 (MEDIUM) — stale terminal banner inside fresh running list:
        A prior failed_* run leaves its banner copy in terminalMsg (the
        DOM node persists, hidden under the hidden reading list — only
        the card surface is restored on idle). User taps Apply →
        fireApply.then POSTs → enterReadingList({state:'running', ...})
        un-hides the reading list. Without a Race 1 clear, the OLD
        failure banner is now visible inside the NEW reading list.
        #345's `readingList.hidden` probe guard blocks the cold-load
        probe from re-entering on top of fireApply, so the banner can
        only come from in-DOM residue, not a racing probe.
        Fix: enterReadingList clears terminalMsg when payload.state === 'running'.

    Race 2 (LOW) — probe's failed_* yanks UI from under open modal:
        Probe lands while the confirm modal is open with stale failed_*
        snapshot. Without a guard, the failed_* branch flips card →
        reading-list under the user mid-confirm and paints terminal
        copy for a PRIOR run on top.

    Race 3 (LOW) — probe's running branch yanks UI from under open modal:
        SAME UX defect as Race 2 on the running branch. While the user
        has the confirm modal open, an auto-update weekly timer fire
        or a sibling-tab apply produces a `running` snapshot. Without
        an above-switch guard, enterReadingList yanks the card surface
        out from under the modal; the user confirms against a UI for
        a DIFFERENT in-flight update, and their POST returns 409.
        Fix: hoist `if (dialog && dialog.open) return;` ABOVE the state
        switch so it covers both failed_* AND running probe transitions.

    Vitest infra is deferred to #338, so these are structural greps. Pair
    with TestUpdatesJsProbeRendersTerminalCopy (#352) which pins the probe
    block shape.
    """

    UPDATES_JS = Path(__file__).resolve().parents[1] / "src" / "control_server" / "static" / "js" / "updates.js"

    def _extract_function_body(self, src: str, name: str) -> str:
        """Brace-balanced body extract, mirrors siblings above."""
        match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", src)
        assert match, f"function {name}(...) not found in updates.js"
        depth = 1
        i = match.end()
        while i < len(src) and depth > 0:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, f"unbalanced braces while scanning {name}"
        return src[match.end() : i - 1]

    def _extract_probe_block(self, src: str) -> str:
        """Body of `handleProbePayload(payload)` — the cold-load probe's
        handler. Was an anonymous `pollStatusOnce(function (payload) {...})`
        callback until the #354 codex P2 follow-up extracted it to a named
        function so the dialog 'close' listener can replay deferred payloads
        through the same logic path. Both shapes are accepted so an
        intentional refactor either direction doesn't break this helper."""
        match = re.search(
            r"function\s+handleProbePayload\s*\(\s*payload\s*\)\s*\{",
            src,
        )
        if match is None:
            # Legacy anonymous shape — kept as a fallback so the helper
            # doesn't go stale if someone reverts the extraction.
            match = re.search(
                r"pollStatusOnce\s*\(\s*function\s*\(\s*payload\s*\)\s*\{",
                src,
            )
        assert match, "page-load probe handler not found (looked for handleProbePayload and the legacy anonymous form)"
        depth = 1
        i = match.end()
        while i < len(src) and depth > 0:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, "unbalanced braces while scanning probe block"
        return src[match.end() : i - 1]

    def test_enter_reading_list_clears_terminal_on_running(self) -> None:
        """#354 Race 1: enterReadingList must clear terminalMsg when entering
        for a fresh `running` payload. Actual sequence: a prior failed_*
        run left its banner copy in terminalMsg (the node persists in
        the DOM, hidden underneath the hidden reading list — only the
        card surface is restored on idle). User taps Apply → fireApply.then
        POSTs and calls enterReadingList({state:'running', phase_index:1})
        which un-hides the reading list. Without this clear, the OLD
        failure banner is now visible INSIDE the NEW reading list —
        the user sees `Update failed verification…` while a fresh update
        is actually advancing. #345's `readingList.hidden` probe guard
        blocks a racing cold-load probe from re-entering on top of
        fireApply, so the stale banner can only come from in-DOM
        residue. The clear has to live inside enterReadingList — not
        just exitReadingList, which is only called on idle."""
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "enterReadingList")
        # The running-state clear must reference terminalMsg + payload.state === 'running'.
        assert re.search(r"payload\.state\s*===\s*['\"]running['\"]", body), (
            "enterReadingList must gate the terminalMsg clear on payload.state === 'running' "
            "so it only fires for fresh running updates, not on every entry (#354 Race 1)"
        )
        assert "terminalMsg.hidden = true" in body, (
            "enterReadingList must set `terminalMsg.hidden = true` on running entry so a "
            "stale failure banner from a prior probe doesn't sit above the new reading "
            "list (#354 Race 1)"
        )
        assert "terminalMsg.textContent = ''" in body, (
            "enterReadingList must clear terminalMsg.textContent on running entry — "
            "leaving the text in DOM lets a CSS-only override re-expose it (#354 Race 1)"
        )
        assert "delete terminalMsg.dataset.tone" in body, (
            "enterReadingList must delete terminalMsg.dataset.tone on running entry so "
            "the stale tone class doesn't bleed into the next render (#354 Race 1)"
        )

    def test_probe_failed_branch_bails_when_modal_open(self) -> None:
        """#354 Race 2 + claude adversarial Race 3: the probe must early-return
        when the confirm modal is open BEFORE dispatching to either the
        running or failed_* branches. Otherwise the probe flips the card
        surface to the reading list mid-confirm and the user confirms
        against a UI that no longer matches their staged action — for
        failed_* (Race 2) the user sees stale terminal copy; for running
        (Race 3) the user's POST returns 409 against a different
        in-flight update."""
        src = self.UPDATES_JS.read_text()
        probe = self._extract_probe_block(src)
        # The bail-out must exist. Match either the bare single-line form
        # `if (dialog && dialog.open) return;` (Race 2/3 original) OR the
        # multi-line form that also stashes a deferred payload before
        # returning (#354 codex P2 follow-up). Both short-circuit the same
        # way for the modal-guard contract; only the no-state-mutation
        # invariant matters here.
        bare = re.search(r"if\s*\(\s*dialog\s*&&\s*dialog\.open\s*\)\s*return\s*;", probe)
        with_stash = re.search(
            r"if\s*\(\s*dialog\s*&&\s*dialog\.open\s*\)\s*\{[^{}]*?return\s*;[^{}]*?\}",
            probe,
            flags=re.DOTALL,
        )
        assert bare or with_stash, (
            "probe must short-circuit when `dialog && dialog.open` is true — either bare "
            "`return;` or a block that stashes and returns. Without it the probe yanks the "
            "card surface out from under an open confirm modal (#354 Race 2 + Race 3)."
        )

    def test_probe_modal_guard_lives_above_state_switch(self) -> None:
        """Claude adversarial Race 3: the `dialog && dialog.open` early-return
        must live ABOVE the `payload.state === 'running' / failed_*` switch,
        not inside any one branch. Race 2's branch-local placement covered
        failed_* but left running uncovered — an auto-update weekly timer
        fire or sibling-tab apply lands a `running` snapshot mid-confirm,
        enterReadingList yanks the card surface to the reading list, and
        the user confirms against a UI for a DIFFERENT in-flight update
        (their POST returns 409). Pin the guard's position relative to
        the first state-switch reference so a future refactor can't
        regress it back into the failed_* branch."""
        src = self.UPDATES_JS.read_text()
        probe = self._extract_probe_block(src)
        # Accept both the bare-return form and the stash-then-return form
        # (#354 codex P2 follow-up). The position-pinning invariant is the
        # same either way.
        guard_match = re.search(
            r"if\s*\(\s*dialog\s*&&\s*dialog\.open\s*\)\s*(?:return\s*;|\{[^{}]*?return\s*;[^{}]*?\})",
            probe,
            flags=re.DOTALL,
        )
        assert guard_match, "probe must contain the `if (dialog && dialog.open) ...return;` guard"
        first_state_match = re.search(r"payload\.state\s*===", probe)
        assert first_state_match, "probe must reference `payload.state ===` for the state switch"
        assert guard_match.start() < first_state_match.start(), (
            "the `dialog && dialog.open` early-return must appear ABOVE the first "
            "`payload.state ===` switch arm so it covers BOTH the running branch "
            "(claude adversarial Race 3: auto-update / sibling-tab POST lands mid-confirm) "
            "AND the failed_* branch (#354 Race 2). A branch-local placement inside "
            "failed_* leaves the running path uncovered."
        )

    def test_probe_modal_guard_defers_instead_of_dropping(self) -> None:
        """#354 codex P2 follow-up: the modal-open bail must NOT permanently
        discard the only cold-load probe sample. The probe is one-shot — if
        the user cancels the modal after the guard fired, there's no other
        path to transition the page out of the stale card. Stash the payload
        on bail (`deferredProbePayload = payload`), and on dialog `close`,
        replay through `handleProbePayload` UNLESS the dialog closed with
        returnValue 'confirm' (in which case fireApply takes over and arms
        its own enterReadingList). Clear the stash either way so a re-open
        doesn't double-fire."""
        src = self.UPDATES_JS.read_text()
        probe = self._extract_probe_block(src)
        # 1. The bail-out branch stashes the payload before returning.
        assert "deferredProbePayload = payload" in probe, (
            "modal-open bail must stash the payload in deferredProbePayload so the dialog "
            "close listener can replay it; otherwise the only cold-load sample is dropped "
            "and a modal cancel leaves the page on the stale card until manual reload."
        )
        # 2. The dialog 'close' listener references deferredProbePayload AND
        #    discriminates on returnValue so the confirm path doesn't
        #    double-fire (fireApply will arm enterReadingList itself).
        close_match = re.search(
            r"dialog\.addEventListener\s*\(\s*['\"]close['\"]\s*,\s*function\s*\(\)\s*\{",
            src,
        )
        assert close_match, "dialog 'close' listener not found"
        depth = 1
        i = close_match.end()
        while i < len(src) and depth > 0:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        assert depth == 0, "unbalanced braces while scanning dialog 'close' listener"
        close_body = src[close_match.end() : i - 1]
        assert "deferredProbePayload" in close_body, (
            "dialog 'close' listener must reference deferredProbePayload to replay the "
            "deferred probe sample after the modal closes"
        )
        assert "handleProbePayload" in close_body, (
            "dialog 'close' listener must call handleProbePayload(stashed) so the replay "
            "goes through the same state-switch logic as a fresh probe"
        )
        assert re.search(
            r"dialog\.returnValue\s*!==?\s*['\"]confirm['\"]",
            close_body,
        ), (
            "dialog 'close' listener must gate the replay on dialog.returnValue !== 'confirm' "
            "so the confirm path doesn't double-fire (fireApply arms enterReadingList itself)"
        )

    def test_fireApply_replays_deferred_probe_on_error(self) -> None:
        """#354 codex P2 round 5: if the deferred probe captured a `running`
        state (auto-update or sibling-tab apply already in flight) and the
        user confirms the modal, fireApply POSTs and gets 409
        update_in_progress. The original close-handler-replays-on-cancel
        path doesn't fire because returnValue === 'confirm'. Without a
        fireApply-error replay path, the page sits on the stale card after
        an unexplained alert.

        fireApply's non-OK branch must replay deferredProbePayload (if
        any) through handleProbePayload before the alert, so the user gets
        the in-flight reading-list. On 2xx success, fireApply owns the
        new running state and clears the stash to prevent the close
        listener's cancel-path replay from later double-firing."""
        src = self.UPDATES_JS.read_text()
        body = self._extract_function_body(src, "fireApply")
        # The 2xx success branch must clear the stash so the close
        # listener's cancel-path replay can't fire on a later cancel of
        # an unrelated modal re-open with the same stash.
        success_pattern = re.search(
            r"if\s*\(\s*response\.ok\s*\)\s*\{",
            body,
        )
        assert success_pattern, "fireApply must check response.ok"
        # Search the whole body for the success-branch clear and the
        # error-branch replay. Both must reference deferredProbePayload.
        assert "deferredProbePayload = null" in body, (
            "fireApply must clear deferredProbePayload on success — otherwise the close "
            "listener's cancel-path replay could double-fire on a later modal cycle "
            "(#354 codex round 5)"
        )
        assert "handleProbePayload(stashed)" in body or re.search(
            r"handleProbePayload\s*\(\s*\w+\s*\)",
            body,
        ), (
            "fireApply must replay the deferred probe payload (handleProbePayload(stashed)) on "
            "non-OK response so the page enters the in-flight reading-list instead of sitting "
            "on a stale card after an alert (#354 codex round 5)"
        )


class TestLastUpdatePersistentMirror:
    """#334: persistent /var/lib/litclock/last-update.json mirror of the
    tmpfs /run/litclock/update.status. update.sh writes both on terminal
    state=complete; the persistent file lets the Status hero "Last update"
    row survive the tmpfs clear at reboot during the 15-min LKG soak window
    (Codex Window 1) AND the offline-graceful-exit window where Phase 1
    already cleared lkg-sha but no new LKG was recorded yet (Codex Window 2).

    Resolver order pinned by these tests:
        1. update.status (state=complete) — freshest signal
        2. last-update.json — persistent mirror (NEW)
        3. lkg-sha mtime — pre-#334 fallback
    """

    def _make_app(
        self,
        *,
        status_file,
        update_status_path=None,
        last_update_path=None,
        lkg_sha_path=None,
    ):
        # Mirror the helper from TestLastUpdateRowResolution so each test stays
        # readable. We re-declare here rather than subclassing to keep the test
        # class an independent collection in pytest output.
        config = {"VERSION_OVERRIDE": "v0.test", "STATUS_FILE": str(status_file)}
        config["UPDATE_STATUS_FILE"] = str(update_status_path) if update_status_path else "/nonexistent/update.status"
        config["LAST_UPDATE_FILE"] = str(last_update_path) if last_update_path else "/nonexistent/last-update.json"
        config["LKG_SHA_FILE"] = str(lkg_sha_path) if lkg_sha_path else "/nonexistent/lkg-sha"
        return create_app(config).test_client()

    def test_only_last_update_json_present_surfaces_to_version(self, status_file, tmp_path) -> None:
        """Codex Window 1 — reboot during LKG soak. /run is tmpfs so
        update.status is gone; lkg-sha was cleared by Phase 1 and the new
        LKG hasn't been recorded yet. last-update.json (written by update.sh
        AFTER update_status_complete validates) is the only signal — must
        surface to_version + relative-time."""
        import json
        import time as _time

        _write_status_payload(status_file)
        last_update = tmp_path / "last-update.json"
        finished = _time.time() - 240  # 4 minutes ago
        last_update.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "phase_index": 7,
                    "phase_name": "Restarting",
                    "started_at_unix": int(finished - 30),
                    "finished_at_unix": int(finished),
                    "from_version": "116db7d",
                    "to_version": "5f12b8b",
                    "error": None,
                }
            )
        )
        client = self._make_app(status_file=status_file, last_update_path=last_update)
        body = client.get("/api/status").json
        assert body["last_update_version"] == "5f12b8b"
        assert body["last_update_at_relative"] == "4 minutes ago"

    def test_source1_missing_source2_present_uses_source2(self, status_file, tmp_path) -> None:
        """Codex Window 1 alternate path — same as above but with the
        update.status path explicitly pointed at a missing file (rather
        than the default /nonexistent path). Pins the source-2 fallback
        when source 1 is the explicit "tmpfs cleared at boot" case."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"  # path exists in tmp_path but file does not
        last_update = tmp_path / "last-update.json"
        finished = _time.time() - 600  # 10 minutes ago
        last_update.write_text(
            json.dumps({"state": "complete", "finished_at_unix": int(finished), "to_version": "abc1234"})
        )
        client = self._make_app(
            status_file=status_file,
            update_status_path=update_status,
            last_update_path=last_update,
        )
        body = client.get("/api/status").json
        assert body["last_update_version"] == "abc1234"
        assert body["last_update_at_relative"] == "10 minutes ago"

    def test_source1_running_source2_present_uses_source2(self, status_file, tmp_path) -> None:
        """update.status reports state=running (in-flight update) — that's
        not a "last update" signal so resolver must fall through. Source 2
        carries the LAST successful update's data — surface it instead of
        falling all the way to lkg-sha."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(json.dumps({"state": "running", "phase_index": 4}))
        last_update = tmp_path / "last-update.json"
        finished = _time.time() - 1800  # 30 minutes ago
        last_update.write_text(
            json.dumps({"state": "complete", "finished_at_unix": int(finished), "to_version": "deadbee"})
        )
        client = self._make_app(
            status_file=status_file,
            update_status_path=update_status,
            last_update_path=last_update,
        )
        body = client.get("/api/status").json
        assert body["last_update_version"] == "deadbee"
        assert body["last_update_at_relative"] == "30 minutes ago"

    def test_source1_failed_unrecovered_source2_present_uses_source2(self, status_file, tmp_path) -> None:
        """Codex Window 2 — offline-graceful-exit + manual recovery state.
        update.status was overwritten by the trap with state=failed_unrecovered;
        source 2 carries the LAST successful update's data. Resolver must
        fall through past failed_unrecovered to last-update.json (NOT to
        em-dash, NOT to lkg-sha for the freshness)."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        update_status.write_text(json.dumps({"state": "failed_unrecovered", "error": "killed mid-run"}))
        last_update = tmp_path / "last-update.json"
        finished = _time.time() - 3600  # 1 hour ago
        last_update.write_text(
            json.dumps({"state": "complete", "finished_at_unix": int(finished), "to_version": "1063fa3"})
        )
        client = self._make_app(
            status_file=status_file,
            update_status_path=update_status,
            last_update_path=last_update,
        )
        body = client.get("/api/status").json
        assert body["last_update_version"] == "1063fa3"
        assert body["last_update_at_relative"] == "1 hour ago"

    def test_source1_complete_wins_over_source2_freshness_regression(self, status_file, tmp_path) -> None:
        """When source 1 (update.status) is also state=complete, it MUST
        win over source 2 — the tmpfs file is the freshest signal (written
        first by update_status_complete; the persistent mirror is written
        AFTER, so it can never be newer). This is the freshness invariant
        the eng-review plan locked in: source 1 always wins when it's
        valid."""
        import json
        import time as _time

        _write_status_payload(status_file)
        update_status = tmp_path / "update.status"
        finished_fresh = _time.time() - 60  # 1 minute ago
        update_status.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "finished_at_unix": int(finished_fresh),
                    "to_version": "freshhh",
                }
            )
        )
        last_update = tmp_path / "last-update.json"
        finished_stale = _time.time() - 86400  # 1 day ago
        last_update.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "finished_at_unix": int(finished_stale),
                    "to_version": "stale01",
                }
            )
        )
        client = self._make_app(
            status_file=status_file,
            update_status_path=update_status,
            last_update_path=last_update,
        )
        body = client.get("/api/status").json
        # Source 1 wins — fresh data, not the stale persistent mirror.
        assert body["last_update_version"] == "freshhh"
        assert body["last_update_at_relative"] == "1 minute ago"


class TestLastUpdateBoundedReads:
    """#336 — DoS / hardening guards on the three "Last update" sources.
    A 1MB junk file, FIFO, symlink, or directory at any of the three paths
    must NOT crash /api/status, must NOT pull garbage into memory, and
    must NOT hang the request handler. The fallback chain still produces
    a sane response (em-dash if all sources are bad)."""

    def _make_app(
        self,
        *,
        status_file,
        update_status_path=None,
        last_update_path=None,
        lkg_sha_path=None,
    ):
        config = {"VERSION_OVERRIDE": "v0.test", "STATUS_FILE": str(status_file)}
        config["UPDATE_STATUS_FILE"] = str(update_status_path) if update_status_path else "/nonexistent/update.status"
        config["LAST_UPDATE_FILE"] = str(last_update_path) if last_update_path else "/nonexistent/last-update.json"
        config["LKG_SHA_FILE"] = str(lkg_sha_path) if lkg_sha_path else "/nonexistent/lkg-sha"
        return create_app(config).test_client()

    def test_oversize_update_status_falls_through(self, status_file, tmp_path) -> None:
        """1MB malformed JSON at update.status must be rejected by the
        bounded reader and the resolver must fall through to the next
        source. /api/status returns 200, em-dash if no other source."""
        update_status = tmp_path / "update.status"
        update_status.write_text("x" * (1024 * 1024))  # 1MB
        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file, update_status_path=update_status)
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json
        assert body["last_update_version"] is None
        assert body["last_update_at_relative"] == "—"

    def test_oversize_last_update_json_falls_through(self, status_file, tmp_path) -> None:
        """1MB malformed JSON at last-update.json must be rejected and
        the resolver must fall through to lkg-sha."""
        import os as _os
        import time as _time

        last_update = tmp_path / "last-update.json"
        last_update.write_text("y" * (1024 * 1024))  # 1MB
        lkg = tmp_path / "lkg-sha"
        lkg.write_text("a5c0b35538cf9bd1234abcdef0987654321deadb\n")
        ts = _time.time() - 7200
        _os.utime(lkg, (ts, ts))
        _write_status_payload(status_file)
        client = self._make_app(
            status_file=status_file,
            last_update_path=last_update,
            lkg_sha_path=lkg,
        )
        body = client.get("/api/status").json
        # Fell through past oversize last-update.json to lkg-sha.
        assert body["last_update_version"] == "a5c0b35"
        assert body["last_update_at_relative"] == "2 hours ago"

    def test_oversize_lkg_sha_returns_em_dash(self, status_file, tmp_path) -> None:
        """64-byte cap on lkg-sha — anything larger is rejected (a real
        SHA-1 hex is 40 bytes + newline). 1MB junk → em-dash."""
        lkg = tmp_path / "lkg-sha"
        lkg.write_text("z" * (1024 * 1024))  # 1MB
        _write_status_payload(status_file)
        client = self._make_app(status_file=status_file, lkg_sha_path=lkg)
        body = client.get("/api/status").json
        assert body["last_update_version"] is None
        assert body["last_update_at_relative"] == "—"

    @pytest.mark.parametrize("path_kind", ["update_status", "last_update", "lkg_sha"])
    def test_fifo_at_any_path_does_not_hang(self, status_file, tmp_path, path_kind) -> None:
        """A FIFO (named pipe) at any of the three paths would block
        forever on open() if the bounded reader didn't gate via lstat +
        S_ISREG. With the gate, the reader returns None → resolver falls
        through → /api/status returns 200 quickly."""
        import os as _os

        fifo = tmp_path / f"{path_kind}.fifo"
        _os.mkfifo(fifo)
        kwargs = {"status_file": status_file}
        kwargs[f"{path_kind}_path"] = fifo
        _write_status_payload(status_file)
        client = self._make_app(**kwargs)
        r = client.get("/api/status")
        assert r.status_code == 200
        # Resolver fell through; final value depends on which path was the FIFO.
        # We only care that the request returned at all — no hang.
        assert "last_update_version" in r.json

    @pytest.mark.parametrize("path_kind", ["update_status", "last_update", "lkg_sha"])
    def test_symlink_to_regular_file_is_rejected(self, status_file, tmp_path, path_kind) -> None:
        """The bounded reader uses os.lstat (NOT Path.stat which follows
        symlinks). A symlink to a regular file must be rejected by the
        S_ISREG check — defends against a planted symlink swap.
        Resolver must fall through cleanly to the next source."""
        import json as _json
        import os as _os
        import time as _time

        # Plant a real, valid file off-path that the symlink points to.
        target = tmp_path / "real-payload.json"
        target.write_text(
            _json.dumps({"state": "complete", "finished_at_unix": int(_time.time()), "to_version": "evil123"})
        )
        link = tmp_path / f"{path_kind}.link"
        _os.symlink(target, link)
        kwargs = {"status_file": status_file}
        kwargs[f"{path_kind}_path"] = link
        _write_status_payload(status_file)
        client = self._make_app(**kwargs)
        body = client.get("/api/status").json
        # The symlink was rejected — "evil123" must NOT have been promoted
        # to last_update_version. (Note: lkg-sha symlink would also fall
        # through to em-dash; for update.status / last-update.json the
        # other two sources are also missing so we get em-dash too.)
        assert body["last_update_version"] != "evil123"

    @pytest.mark.parametrize("path_kind", ["update_status", "last_update", "lkg_sha"])
    def test_directory_at_any_path_is_rejected(self, status_file, tmp_path, path_kind) -> None:
        """A directory at any of the three paths must be rejected by the
        S_ISREG gate. Without the gate, open() would raise IsADirectoryError
        which the existing handler also catches — but the lstat gate
        rejects earlier (before open) and is uniform across all guard
        types, so pin it explicitly."""
        directory = tmp_path / f"{path_kind}.d"
        directory.mkdir()
        kwargs = {"status_file": status_file}
        kwargs[f"{path_kind}_path"] = directory
        _write_status_payload(status_file)
        client = self._make_app(**kwargs)
        r = client.get("/api/status")
        assert r.status_code == 200


class TestStatusHeroRendering:
    """Server-side rendering of `/` populates the hero card from the same
    payload /api/status returns (PRD §7.5 progressive enhancement — the
    page must render meaningfully without JS)."""

    def test_hero_renders_quote_and_attribution(self, status_client, status_file) -> None:
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        assert '<section class="hero"' in body
        assert "It was the best of times" in body
        # Attribution format from DESIGN.md "Component composition specs":
        # "— {Author}, *{Title}* · {HH:MM}".
        assert "— Charles Dickens" in body
        assert "<em" in body and "A Tale of Two Cities</em>" in body
        # #290 /review fix: '·&nbsp;' separator lives OUTSIDE the inner
        # [data-status-attr-time] node so JS's setText() can patch the bare
        # time without wiping the locked separator. Verify the separator is
        # present and the bare time is inside the JS-patched node.
        assert "·&nbsp;" in body, "DESIGN.md-locked '·&nbsp;' separator missing from SSR"
        # The bare time must be inside [data-status-attr-time] for JS to patch it.
        time_node_start = body.find("data-status-attr-time>")
        time_node_end = body.find("</span>", time_node_start)
        assert time_node_start > 0 and "08:42" in body[time_node_start:time_node_end], (
            "the bare HH:MM must live inside [data-status-attr-time] so status.js can patch "
            "just the time without overwriting the locked '·&nbsp;' separator"
        )

    def test_hero_uses_blockquote_for_screen_reader_semantics(self, status_client, status_file) -> None:
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        # #290 added a data-status-quote hook for client-side polling — the
        # blockquote element + hero-quote class are what screen readers and
        # CSS depend on, not the verbatim opening-tag string.
        assert '<blockquote class="hero-quote"' in body
        assert "data-status-quote" in body

    def test_hero_region_has_aria_label(self, status_client, status_file) -> None:
        """DESIGN.md a11y: hero is `role=region aria-labelledby=status-heading`
        with a visually-hidden h2 anchoring it. Pin so a11y refactors don't
        accidentally drop the heading."""
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        assert 'aria-labelledby="status-heading"' in body
        assert '<h2 id="status-heading" class="visually-hidden">Now</h2>' in body

    def test_stale_banner_renders_when_picked_at_is_old(self, status_client, status_file) -> None:
        """D2: ochre warning banner appears when picked_at ≥ 90s old."""
        import time as _time

        _write_status_payload(status_file, picked_at=_time.time() - 200)
        body = status_client.get("/").data.decode()
        # #290 keeps the banner element in the DOM always (so status.js can
        # toggle visibility without DOM creation); when stale, the [hidden]
        # attribute must NOT be present on the banner.
        banner_start = body.find("data-status-stale-banner")
        assert banner_start >= 0, "stale-banner element missing from DOM"
        banner_open_tag_end = body.find(">", banner_start)
        banner_open_tag = body[banner_start:banner_open_tag_end]
        assert "hidden" not in banner_open_tag, f"stale banner unexpectedly hidden: {banner_open_tag!r}"
        assert 'class="stale-banner"' in body
        # DESIGN.md line 296 mandates assertive (not polite) for the stale-
        # quote banner first appearance — interrupts mid-utterance to
        # announce that the clock service is paused. Adversarial /review
        # on M2 caught the M2 draft using polite.
        assert 'role="status"' in body
        assert 'aria-live="assertive"' in body
        assert (
            'aria-live="polite"'
            not in body[body.find('class="stale-banner"') : body.find("</div>", body.find('class="stale-banner"'))]
        )
        # Minutes-since-last-quote message — the user can judge severity.
        assert "min ago" in body

    def test_stale_banner_absent_when_quote_is_fresh(self, status_client, status_file) -> None:
        """#290: the banner DOM element is always present (so status.js can
        un-hide it on a poll that turns stale), but when the quote is fresh
        the element must carry the `hidden` attribute so it's invisible."""
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        banner_start = body.find("data-status-stale-banner")
        assert banner_start >= 0, "stale-banner DOM element should always be present for #290 client-side toggle"
        banner_open_tag_end = body.find(">", banner_start)
        banner_open_tag = body[banner_start:banner_open_tag_end]
        assert "hidden" in banner_open_tag, (
            f"stale banner must carry [hidden] when fresh — JS toggles it on/off. Got: {banner_open_tag!r}"
        )

    def test_status_rows_present_in_order(self, status_client, status_file) -> None:
        """5 rows in the locked order: WiFi → Weather → Version → Uptime
        → Last update. Spec line ~362 of DESIGN.md."""
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        positions = [
            body.find(">WiFi<"),
            body.find(">Weather<"),
            body.find(">Version<"),
            body.find(">Uptime<"),
            body.find(">Last update<"),
        ]
        assert all(p > 0 for p in positions), f"missing labels: {positions}"
        assert positions == sorted(positions), f"status rows out of locked order: {positions}"

    def test_mono_class_applied_to_geist_mono_values(self, status_client, status_file) -> None:
        """SSID, Version, Uptime should carry the .mono class so CSS
        applies Geist Mono + tabular-nums per DESIGN.md row spec. Without
        the class the 14px digits don't align column-wise."""
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        # The version row's <dd> must carry .mono.
        version_pos = body.find(">Version<")
        version_dd_start = body.find("<dd", version_pos)
        version_dd_end = body.find("</dd>", version_dd_start)
        assert 'class="mono"' in body[version_dd_start:version_dd_end]


class TestStatusLivePoll:
    """#290: status.js polls /api/status every 30s + on visibilitychange and
    patches the SSR'd hero/banner/row DOM in place. These tests pin the wiring
    contract — every node the JS patches must carry a data-status-* hook and
    the script must be wired into the Status page (only)."""

    def test_status_js_script_loaded_on_status_page(self, status_client, status_file) -> None:
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        assert "js/status.js" in body, "status.js must be wired into the Status template"

    def test_status_js_not_loaded_on_other_tabs(self, status_client, status_file) -> None:
        """Per-tab JS scoping (mirrors updates.js / settings.js / system.js).
        Loading status.js on /settings would pay the cost on every page."""
        _write_status_payload(status_file)
        for path in ("/settings", "/system", "/updates"):
            body = status_client.get(path).data.decode()
            assert "js/status.js" not in body, f"status.js leaked into {path}"

    def test_all_dom_hooks_present_for_client_side_patch(self, status_client, status_file) -> None:
        """status.js needs every patch target to exist at SSR. Without these
        hooks, a poll silently no-ops and the user keeps seeing stale data."""
        _write_status_payload(status_file)
        body = status_client.get("/").data.decode()
        # Stale banner + its text node.
        assert "data-status-stale-banner" in body
        assert "data-status-stale-text" in body
        # Hero quote + the two hero branches (full + empty) for show/hide.
        assert "data-status-hero-full" in body
        assert "data-status-hero-empty" in body
        assert "data-status-quote" in body
        # Attribution: prefix span (dash + author + comma), title wrap, time wrap.
        assert "data-status-attr-prefix" in body
        assert "data-status-attr-title-wrap" in body
        assert "data-status-attr-title" in body
        assert "data-status-attr-time-wrap" in body
        assert "data-status-attr-time" in body
        # 5 status rows.
        for hook in (
            "data-status-wifi",
            "data-status-weather",
            "data-status-version",
            "data-status-uptime",
            "data-status-last-update",
        ):
            assert hook in body, f"missing hook: {hook}"

    def test_static_status_js_is_served(self, status_client, status_file) -> None:
        """Sanity check that the new static asset is actually reachable.
        Without this, a typo in the filename or block name silently 404s
        and the page stops auto-refreshing."""
        _write_status_payload(status_file)
        r = status_client.get("/static/js/status.js")
        assert r.status_code == 200, f"status.js not served: {r.status_code}"
        text = r.data.decode()
        assert "/api/status" in text, "status.js missing the polled endpoint"
        assert "visibilitychange" in text, "status.js missing focus-refresh trigger"

    def test_status_js_in_service_worker_precache(self, status_client, status_file) -> None:
        """#290 /review fix: every per-tab JS asset must be in the SW precache
        list. The other three (settings.js, system.js, updates.js) all are.
        status.js was missing in the initial #290 patch, so Android offline
        cold-starts served the cached navigation HTML but 404'd on status.js
        — breaking auto-refresh in exactly the scenario the SW was designed
        to defend against."""
        _write_status_payload(status_file)
        sw_body = status_client.get("/sw.js").data.decode()
        for path in (
            "/static/js/status.js",
            "/static/js/settings.js",
            "/static/js/system.js",
            "/static/js/updates.js",
        ):
            assert f"'{path}'" in sw_body, (
                f"{path} must appear in SW PRECACHE_URLS so Android offline cold-start can "
                "serve it from cache. Missing this means the cached Status HTML loads but its "
                "<script> reference 404s on the very network blip the SW exists to handle."
            )

    def test_hidden_attribute_overrides_display_flex(self, status_client, status_file) -> None:
        """#290 /review fix: status.js relies on toggling the [hidden]
        attribute to show/hide the stale banner and the hero branches.
        Without an `!important` author rule (or a higher-specificity
        attribute selector), `.stale-banner { display: flex }` ties with
        UA's `[hidden] { display: none }` on specificity (0,1,0) but wins
        on cascade origin (author beats user-agent). Result: SSR rendering
        with `class=stale-banner hidden` paints visibly anyway. The fix
        is a global `[hidden] { display: none !important }` in tokens.css."""
        _write_status_payload(status_file)
        css = status_client.get("/static/css/tokens.css").data.decode()
        # Look for the actual rule, not the [hidden] reference in comments.
        # Strip /* ... */ blocks before pattern matching.

        css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        assert "[hidden]" in css_no_comments, "tokens.css must carry a global [hidden] rule"
        # The [hidden] rule must use `display: none !important` to defeat any
        # author `.foo { display: flex }` rule on an element that also
        # carries the [hidden] attribute.
        # Match the rule block after the [hidden] selector.
        match = re.search(r"\[hidden\]\s*\{[^}]*\}", css_no_comments)
        assert match is not None, "tokens.css must declare a [hidden] rule block"
        assert "display: none !important" in match.group(0), (
            "[hidden] rule must use `display: none !important` to defeat author class "
            "rules like `.stale-banner { display: flex }`. Without it, the [hidden]-toggle "
            "pattern in #290 silently fails to hide elements that have their own display rule."
        )


class TestM2DesignTweaks:
    """Hardware-QA 2026-04-28 design decisions (user testing PR #261):

    1. Drop the LitClock h1 from base.html.j2 default block. The literary
       hero on Status IS the brand; a header above competes with the quote
       for attention. Stub tabs ship milestone copy alone. Browser title
       bar + home-screen icon already say LitClock.
    2. Drop the 6-line clamp on .hero-quote. Corpus has 8-12 line quotes
       that were getting clipped mid-sentence on real phones. Layout
       integrity isn't compromised — rows below flow as the card grows.
    """

    def test_no_h1_litclock_on_any_tab(self, status_client, status_file) -> None:
        """No tab renders an h1 with LitClock text (with or without
        attributes/whitespace). Anti-regression on the brand-quiet posture.
        The visually-hidden screen-reader heading on Status uses h2, not h1."""

        _write_status_payload(status_file)
        for path in ("/", "/settings", "/system", "/updates"):
            body = status_client.get(path).data.decode()
            assert not re.search(r"<h1[^>]*>\s*LitClock\s*</h1>", body), f"redundant LitClock h1 found on {path}"

    def test_hero_quote_has_no_line_clamp(self, status_client) -> None:
        """Anti-regression on the 6-line cap removal. -webkit-line-clamp
        truncates mid-sentence; corpus has long-form quotes that the cap
        was eating. Either declaration would silently re-introduce the bug."""

        css = status_client.get("/static/css/tokens.css").data.decode()
        # Strip /* ... */ comments so the explanatory comment that mentions
        # `line-clamp` doesn't trip the substring check.
        css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        hero_quote_start = css_no_comments.find(".hero-quote {")
        hero_quote_end = css_no_comments.find("}", hero_quote_start)
        block = css_no_comments[hero_quote_start:hero_quote_end]
        assert "-webkit-line-clamp" not in block, (
            "line-clamp truncates literary quotes mid-sentence — removed per hardware-QA 2026-04-28"
        )
        assert "line-clamp:" not in block

    def test_hero_quote_still_uses_locked_typography(self, status_client) -> None:
        """The line-clamp was the only thing dropped; the rest of DESIGN.md's
        hero spec (Fraunces italic, clamp font size, line-height, opsz)
        stays locked."""
        css = status_client.get("/static/css/tokens.css").data.decode()
        hero_quote_start = css.find(".hero-quote {")
        hero_quote_end = css.find("}", hero_quote_start)
        block = css[hero_quote_start:hero_quote_end]
        assert "var(--font-serif)" in block
        assert "font-style: italic" in block
        assert "clamp(22px, 1.5625rem, 31px)" in block
        assert "line-height: 1.4" in block
        assert 'font-variation-settings: "opsz" 144' in block


class TestM2ReviewFixups:
    """Pin the fixes from /review on PR #261 so a future refactor can't
    silently undo them."""

    def test_stale_banner_uses_phosphor_svg_not_emoji(self, status_client, status_file) -> None:
        """DESIGN.md anti-pattern (line 472): emoji as UI affordances.
        Locked icon-per-action mapping (line 205): Phosphor `clock-countdown`
        for the stale-quote banner. The M2 draft used ⏱; design /review
        flagged it. Inline SVG is the fix."""
        import time as _time

        _write_status_payload(status_file, picked_at=_time.time() - 200)
        body = status_client.get("/").data.decode()
        # Banner block boundary.
        banner_start = body.find('class="stale-banner"')
        banner_end = body.find("</div>", banner_start)
        block = body[banner_start:banner_end]
        # Must contain inline SVG; must NOT contain the ⏱ placeholder.
        assert "<svg" in block, "stale-banner missing inline Phosphor SVG"
        assert "⏱" not in block, "stale-banner still uses emoji placeholder"
        # SVG must carry the icon CSS class so it scales with --warning + Dynamic Type.
        assert 'class="stale-banner-icon"' in block

    def test_ssid_truncates_at_18_chars(self, status_file, monkeypatch, tmp_path) -> None:
        """DESIGN.md "Status row list" line 379 locks SSID truncation at
        18 chars. Adversarial /review on M2 caught the M2 draft using only
        CSS ellipsis (truncates at flex-allowed pixel width, not 18 chars).
        Jinja `truncate(18, true, '…')` is the fix."""
        from control_server.routes import status as status_route

        _write_status_payload(status_file)
        # Stub _wifi_ssid to return a 30-char SSID; verify Jinja truncates
        # to 17 chars + '…' (truncate(18, killwords=True, end='…') hard-cuts
        # to length-1 chars then appends '…' for total = 18 visible).
        long_ssid = "MyHomeWiFi-2.4GHz-Network"  # 25 chars
        monkeypatch.setattr(status_route, "_wifi_ssid", lambda: long_ssid)
        app = create_app({"VERSION_OVERRIDE": "v0.test", "STATUS_FILE": str(status_file)})
        body = app.test_client().get("/").data.decode()
        # The full SSID must NOT appear in the rendered hero.
        assert long_ssid not in body
        # The truncation marker must be there.
        assert "…" in body

    def test_uptime_is_appliance_not_service(self, status_client, status_file, monkeypatch) -> None:
        """Adversarial /review on M2: M2 draft surfaced control_server's
        own process uptime as `uptime_s`. After update.sh restarts the
        unit, the user-facing 'Uptime' row jumps to 3m while the appliance
        has been ticking for days. Misleading. Fix reads /proc/uptime."""
        from control_server.routes import status as status_route

        _write_status_payload(status_file)
        # Stub /proc/uptime read to return a fake "5 days" value (432000s).
        monkeypatch.setattr(status_route, "_appliance_uptime_s", lambda: 432000)
        # Process uptime stays small.
        monkeypatch.setattr(status_route, "_service_uptime_s", lambda: 60)
        r = status_client.get("/api/status")
        body = r.json
        # Front-line uptime is the appliance number (5 days = 432000s).
        assert body["uptime_s"] == 432000
        assert "5d" in body["uptime_human"]
        # Service-uptime exposed separately for M4 reconnect-probe consumers.
        assert body["service_uptime_s"] == 60
        assert body["service_uptime_s"] != body["uptime_s"]

    def test_main_does_not_write_status_file(self, monkeypatch, tmp_path) -> None:
        """Adversarial + codex /review on M2: status-file publication moved
        out of main() into __main__ AFTER `epd.display()` confirms the new
        frame is on the panel.
        Two bugs prevented:
        (a) update.sh phase 4.5 smoke test calls main() — the M2 draft
            stomped the live file with phantom quotes that were never
            displayed.
        (b) Production hardware failure (epd.display() raises) used to
            publish a fresh quote anyway, defeating the stale signal.
        Both fixed by making main() pure-ish (composition only); main()
        now returns (image, quote_meta, now) and __main__ publishes after
        the hardware update succeeds."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        import literary_clock

        target = tmp_path / "should-not-exist.json"
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(target))
        monkeypatch.setattr(literary_clock, "UPDATE_FAILED_MARKER", "/nonexistent/__litclock_test_marker__")
        try:
            result = literary_clock.main()
        except Exception as e:
            import pytest as _pt

            _pt.skip(f"main() can't run in this test env: {e}")
        assert isinstance(result, tuple) and len(result) == 3, (
            f"main() must return (image, quote_meta, now) tuple; got {type(result).__name__}"
        )
        image, _quote_meta, _now = result
        assert image is not None
        assert not target.exists(), (
            "main() must NOT write the status file — that's __main__'s job after "
            "epd.display() confirms hardware success (OV3 + codex catch)"
        )

    def test_status_file_skip_when_corpus_lookup_empty(self, monkeypatch, tmp_path) -> None:
        """Adversarial /review on M2: when glob finds a PNG but corpus
        lookup returns None, M2 draft wrote a status file with empty
        quote/author/title at fresh `picked_at`. PWA hero rendered the
        'Starting up…' empty-state copy while the e-ink had a real quote
        on screen — gaslighting. Fix: skip the write when quote text is
        empty so the status file goes stale and the PWA shows the
        stale-quote banner instead (honest signal)."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        import literary_clock

        target = tmp_path / "status.json"
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(target))
        # Pre-write a stale payload so we can verify the lookup-fail path
        # leaves it untouched (the right behavior).
        target.write_text('{"time": "01:23", "picked_at": 1.0, "quote": "old"}')
        original = target.read_text()

        # quote_meta with empty text simulates corpus-out-of-sync condition.
        empty_meta = {"quote": "", "author": "", "title": "", "image_path": "/dummy.png"}
        from datetime import datetime

        literary_clock._write_status_file(empty_meta, datetime(2026, 4, 28, 8, 42))
        assert target.read_text() == original, (
            "lookup-fail path must skip the write — leaving the stale file "
            "lets the PWA render the stale banner instead of an empty hero"
        )

    """Pin the CSS additions for hero / status rows / stale banner so a
    future refactor can't silently drop the components."""

    def _css(self, status_client) -> str:
        return status_client.get("/static/css/tokens.css").data.decode()

    def test_hero_paper_grain_light_mode_only(self, status_client) -> None:
        """DESIGN.md "Three deliberate risks #3": grain SVG only in light
        mode. The dark hero is type + tone only — adding the grain there
        would betray the brand."""
        css = self._css(status_client)
        # Find the @media block that enables the hero grain — the
        # background-image rule must live INSIDE a light-mode media query.
        light_block_idx = css.find("prefers-color-scheme: light")
        assert light_block_idx > 0
        # The next `}` closes the rule. Grab a generous slice and check.
        block = css[light_block_idx : light_block_idx + 400]
        assert "paper-grain.svg" in block

    def test_hero_clamp_matches_design_md_spec(self, status_client) -> None:
        """`.hero-quote` must use clamp(22px, 1.5625rem, 31px) per
        DESIGN.md "Component composition specs" line ~343. Drift here
        breaks the hero composition spec contract."""
        css = self._css(status_client)
        hero_quote_idx = css.find(".hero-quote {")
        assert hero_quote_idx > 0
        block = css[hero_quote_idx : hero_quote_idx + 600]
        assert "clamp(22px, 1.5625rem, 31px)" in block
        assert "line-height: 1.4" in block
        assert "font-style: italic" in block

    def test_status_row_44px_min_height(self, status_client) -> None:
        """44px is the iOS HIG touch target floor that DESIGN.md inherits.
        Status rows aren't tappable in M2 but will be in M3 (each row
        becomes a settings link); pinning the height keeps M3 mechanics
        unchanged."""
        css = self._css(status_client)
        row_idx = css.find(".status-row {")
        assert row_idx > 0
        block = css[row_idx : row_idx + 400]
        assert "min-height: 44px" in block

    def test_stale_banner_uses_warning_token(self, status_client) -> None:
        css = self._css(status_client)
        banner_idx = css.find(".stale-banner {")
        assert banner_idx > 0
        block = css[banner_idx : banner_idx + 400]
        assert "var(--warning)" in block
        assert "var(--surface-raised)" in block


class TestCreateAppEnvDefaults:
    """Review I5 — create_app must plumb LITCLOCK_*_FILE env vars into
    app.config defaults so a test author setting LITCLOCK_LAST_UPDATE_FILE
    before import doesn't silently get the production /var/lib path while
    thinking they overrode it. Mirrors the existing ENV_FILE pattern."""

    def test_env_file_default_plumbed_from_environment(self, monkeypatch, tmp_path) -> None:
        """Sanity check the existing ENV_FILE pattern works — anchor for
        the parametrized check below."""
        target = tmp_path / "env.sh"
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(target))
        app = create_app()
        assert app.config["ENV_FILE"] == str(target)

    @pytest.mark.parametrize(
        ("env_var", "config_key"),
        [
            ("LITCLOCK_UPDATE_STATUS_FILE", "UPDATE_STATUS_FILE"),
            ("LITCLOCK_LAST_UPDATE_FILE", "LAST_UPDATE_FILE"),
            ("LITCLOCK_LKG_SHA_FILE", "LKG_SHA_FILE"),
        ],
    )
    def test_last_update_file_paths_plumbed_from_env(self, monkeypatch, tmp_path, env_var, config_key) -> None:
        """Each of the three "Last update" path env vars must surface in
        app.config under the routes/status.py-expected key. Without this
        plumbing, a test that sets the env var pre-import would silently
        get DEFAULT_LAST_UPDATE_FILE etc. (production /var/lib paths) and
        either pass-by-coincidence or hit permission errors."""
        target = tmp_path / "test-file"
        monkeypatch.setenv(env_var, str(target))
        app = create_app()
        assert app.config[config_key] == str(target), (
            f"create_app must plumb {env_var} into app.config['{config_key}'] — "
            "review I5: future test authors using app.config overrides should see consistent behavior"
        )

    @pytest.mark.parametrize(
        "config_key",
        ["UPDATE_STATUS_FILE", "LAST_UPDATE_FILE", "LKG_SHA_FILE"],
    )
    def test_unset_env_var_results_in_none_default(self, monkeypatch, config_key) -> None:
        """When the env var is unset, the config key must default to None
        so routes/status.py falls back to its module-level DEFAULT_*
        constants (the production paths). This pins the "no surprising
        empty-string default" behavior."""
        monkeypatch.delenv(f"LITCLOCK_{config_key}", raising=False)
        app = create_app()
        assert app.config[config_key] is None

    def test_test_config_overrides_env_default(self, monkeypatch, tmp_path) -> None:
        """The standard precedence holds: test_config dict passed to
        create_app wins over the environment-derived default."""
        env_path = tmp_path / "from-env"
        config_path = tmp_path / "from-test-config"
        monkeypatch.setenv("LITCLOCK_LAST_UPDATE_FILE", str(env_path))
        app = create_app({"LAST_UPDATE_FILE": str(config_path)})
        assert app.config["LAST_UPDATE_FILE"] == str(config_path)
