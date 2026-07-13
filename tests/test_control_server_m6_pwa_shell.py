"""M6 — PWA shell + manifest + service worker + AtHS hint + splash matrix.

Tests the locked decisions D1–D6, D8–D9 + F1–F5 + DD1–DD6 from
PLAN-LitClock-Control-PWA.md (M6 row + design-review section). Includes the
14 mandatory regression invariants from the test plan
(`~/.gstack/projects/kapoorankush-litclock/ankush-master-m6-eng-review-test-plan-20260430-183708.md`):

1.  /api/* always passes through the SW unchanged (M5 #254 contract preserved).
2.  Non-GET methods pass through the SW (codex F-NEW finding).
3.  --fs-caption ceiling is now 18px.
4.  M5 tabbar local override removed.
5.  All 17 splash sizes × 2 modes = 34 PNGs exist + match expected dimensions.
6.  All 34 splash link tags rendered in base.html.j2.
7.  Manifest theme_color matches tokens.css --bg light.
8.  DESIGN.md type-scale section reflects 18px caption ceiling (covered by
    test_tokens_css_caption_ceiling_18 — DESIGN.md is informational, the CSS
    is the contract).
9.  DESIGN.md PWA Shell Requirements notes the platform-asymmetric SW reality
    (covered by test_design_md_documents_platform_asymmetric_sw).
10. NOTICE.md includes SIL OFL entries for Fraunces, Instrument Sans, Geist Mono.
11. AtHS hint checks both navigator.standalone AND (display-mode: standalone).
12. localStorage access guarded with try/catch (F5 — Safari Private mode safety).
13. A8 reload-on-version-mismatch still works (covered indirectly: SW activate
    skipWaiting + clients.claim path is asserted in test_sw_lifecycle_directives).
14. sw-register.js short-circuits on iOS — observable as console.info, no
    register() call when isSecureContext is false.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from control_server import create_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    return create_app({"VERSION_OVERRIDE": "v0.test-m6"})


@pytest.fixture
def client(app):
    return app.test_client()


# ============================================================
# /sw.js — service worker route (D1)
# ============================================================


class TestServiceWorkerRoute:
    """D1: templated /sw.js with CACHE_NAME = 'litclock-{version}'."""

    def test_sw_js_served_with_javascript_content_type(self, client) -> None:
        r = client.get("/sw.js")
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith("text/javascript"), (
            "browsers refuse to register a SW served with the wrong MIME"
        )

    def test_sw_js_no_cache_headers(self, client) -> None:
        """SW spec already short-circuits cache for /sw.js, but pinning
        the headers keeps proxies + future Flask defaults honest."""
        r = client.get("/sw.js")
        cache_control = r.headers.get("Cache-Control", "")
        assert "max-age=0" in cache_control
        assert "must-revalidate" in cache_control

    def test_sw_js_stamps_cache_name_with_version(self, client) -> None:
        """Cache name must include the resolved version so update.sh-driven
        deploys land a fresh cache without manual bumping."""
        body = client.get("/sw.js").data.decode()
        assert "CACHE_NAME = 'litclock-v0.test-m6'" in body, "D1 contract: CACHE_NAME bumps via {{ version }} stamp"

    def test_sw_lifecycle_directives(self, client) -> None:
        """D3: skipWaiting() in install + clients.claim() in activate so
        M2's A8 reload-on-version-mismatch can deliver fresh code without
        a manual force-quit."""
        body = client.get("/sw.js").data.decode()
        assert "self.skipWaiting()" in body
        assert "self.clients.claim()" in body


# ============================================================
# /sw.js — IRON RULE invariants (D2 + codex F-NEW)
# ============================================================


class TestServiceWorkerInvariants:
    """The two hardest contracts to defend: /api/* always pass-through and
    non-GET requests never cached. Either invariant breaking is a P0 user-
    facing regression — destructive POST replays, stale envelope from a
    mid-update reconnect probe."""

    def test_sw_api_passthrough_invariant(self, client) -> None:
        """Regression #1 from M6 test plan: /api/* must skip SW entirely."""
        body = client.get("/sw.js").data.decode()
        # The fetch handler must early-return on /api/* — assert the
        # textual guard is present. Brittle vs. an unrolled regex but
        # cheap, and the structure is locked in D2.
        assert "url.pathname.startsWith('/api/')" in body
        # Same body must NOT have a cache-mutation right next to that test —
        # the early-return precedes any caching path.
        api_pos = body.index("url.pathname.startsWith('/api/')")
        # 200 chars after the guard, we should see `return` (early exit).
        guard_window = body[api_pos : api_pos + 200]
        assert "return" in guard_window, "/api/* guard must early-return without caching"

    def test_sw_non_get_passthrough_invariant(self, client) -> None:
        """Regression #2 from M6 test plan: POST/PUT/DELETE/PATCH never cached.
        Caching a destructive request and replaying it from cache is the
        worst-possible-class bug."""
        body = client.get("/sw.js").data.decode()
        assert "req.method !== 'GET'" in body
        non_get_pos = body.index("req.method !== 'GET'")
        guard_window = body[non_get_pos : non_get_pos + 200]
        assert "return" in guard_window

    def test_sw_navigate_strategy_is_network_first(self, client) -> None:
        """D2: navigate → network-first w/ 1.5s timeout. Pin the timeout
        + the strategy name so a future refactor can't silently invert
        the strategy (cache-first navigate would freeze users on stale
        shells across deploys)."""
        body = client.get("/sw.js").data.decode()
        assert "NAV_TIMEOUT_MS = 1500" in body
        assert "navigateNetworkFirst" in body

    def test_sw_static_strategy_is_cache_first(self, client) -> None:
        """Static asset → cache-first; cache miss → fetch + store."""
        body = client.get("/sw.js").data.decode()
        assert "staticCacheFirst" in body

    def test_sw_install_uses_cache_reload_to_bypass_http_cache(self, client) -> None:
        """codex /review (M6): without `cache: 'reload'` the SW's install fetches
        respect the browser HTTP cache, so an in-place upgrade can populate the
        new cache name with pre-update bytes from HTTP cache. Pin `cache: 'reload'`
        so install always hits the network."""
        body = client.get("/sw.js").data.decode()
        assert "cache: 'reload'" in body, "SW install must bypass HTTP cache via Request init `cache: 'reload'`"

    def test_sw_install_resilient_via_allSettled(self, client) -> None:
        """codex /review + adversarial /review (M6): cache.addAll fails atomically
        on any single 5xx during a Pi mid-restart, leaving the SW in `redundant`.
        Pin `Promise.allSettled` so survivors get cached and missing URLs hit the
        runtime cache-first path on first use."""
        body = client.get("/sw.js").data.decode()
        assert "Promise.allSettled" in body
        # And NOT cache.addAll which is the atomic-or-rollback footgun.
        assert "cache.addAll(" not in body, (
            "cache.addAll is atomic — a single 5xx on install poisons the whole "
            "SW. Use per-URL cache.add with allSettled instead."
        )

    def test_sw_cache_put_swallows_redirect_throws(self, client) -> None:
        """adversarial /review (M6): cache.put rejects on a redirected response
        (Cache spec). Without a .catch the rejection propagates as an unhandled
        promise rejection, which on Chromium can push the SW into `redundant`.
        Pin defensive .catch on both nav + static cache.put paths."""
        body = client.get("/sw.js").data.decode()
        # Both cache.put callsites must guard with .catch(() => {}).
        assert body.count("cache.put(req, fresh.clone()).catch(() => {})") >= 2, (
            "both navigateNetworkFirst and staticCacheFirst must catch cache.put "
            "rejection (redirect responses, quota errors, etc.)"
        )


# ============================================================
# Manifest (F3)
# ============================================================


class TestManifestRoute:
    """F3: explicit Content-Type + 1d Cache-Control."""

    def test_manifest_served_with_correct_mime(self, client) -> None:
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "application/manifest+json"

    def test_manifest_cache_control_is_one_day(self, client) -> None:
        r = client.get("/manifest.webmanifest")
        assert "max-age=86400" in r.headers.get("Cache-Control", "")

    def test_manifest_required_fields(self, client) -> None:
        body = json.loads(client.get("/manifest.webmanifest").data)
        assert body["name"]
        assert body["short_name"]
        assert body["display"] == "standalone"
        assert body["start_url"] == "/"
        assert body["scope"] == "/"
        assert isinstance(body["icons"], list)
        assert len(body["icons"]) >= 3, "manifest needs ≥3 icons: 192, 512, maskable-512"

    def test_manifest_theme_color_matches_tokens_css_bg_light(self, client) -> None:
        """Regression #7: manifest theme_color must equal tokens.css --bg
        light, otherwise iOS shows one tint at AtHS preview and another
        when launched."""
        body = json.loads(client.get("/manifest.webmanifest").data)
        css = client.get("/static/css/tokens.css").data.decode()
        assert body["theme_color"] == "#FBF6EC"
        assert body["background_color"] == "#FBF6EC"
        # Cross-check against tokens.css.
        assert "--bg: #FBF6EC" in css

    def test_manifest_icons_have_maskable_variant(self, client) -> None:
        """Android adaptive-icon shapes need a `purpose: maskable` icon."""
        body = json.loads(client.get("/manifest.webmanifest").data)
        purposes = [icon.get("purpose", "") for icon in body["icons"]]
        assert any("maskable" in p for p in purposes)

    def test_template_links_to_manifest(self, client) -> None:
        body = client.get("/").data.decode()
        assert 'rel="manifest" href="/manifest.webmanifest"' in body


# ============================================================
# Splash matrix (D4 + D8 + DD3 + DD6)
# ============================================================


SPLASH_SIZES = (
    (640, 1136),
    (750, 1334),
    (828, 1792),
    (1080, 2340),
    (1125, 2436),
    (1170, 2532),
    (1179, 2556),
    (1206, 2622),
    (1242, 2208),
    (1242, 2688),
    (1260, 2736),
    (1284, 2778),
    (1290, 2796),
    (1320, 2868),
    (1536, 2048),
    (1620, 2160),
    (2048, 2732),
)


class TestStaticAssetCaching:
    """PWA load perf (#436). Flask's static handler ships no ``max-age`` by
    default, forcing a conditional revalidation round-trip per asset on every
    PWA navigation — a burst that reads as "slow / feels broken" on a Pi Zero
    2W, and one iOS can't escape because the service worker never registers at
    our plain-HTTP private-IP origin. ``SEND_FILE_MAX_AGE_DEFAULT`` adds a
    15-minute cache so repeat navigations within a session serve from the
    browser cache, bounded short so a non-fingerprinted asset can't stay stale
    long after an in-place ``update.sh``."""

    @pytest.mark.parametrize(
        "url",
        [
            "/static/css/tokens.css",
            "/static/js/diagnostics.js",
            "/static/fonts/fraunces-wght-normal.woff2",
            "/static/icons/icon-192.png",
        ],
    )
    def test_static_asset_has_max_age(self, client, url: str) -> None:
        r = client.get(url)
        assert r.status_code == 200
        cache_control = r.headers.get("Cache-Control", "")
        # 15 min — the shortest TTL still longer than a browsing session, so it
        # kills the per-navigation revalidation storm (#436) while bounding
        # post-update.sh staleness on iOS to <=15 min (static URLs are not
        # fingerprinted).
        assert "max-age=900" in cache_control, f"{url} missing 15m max-age: {cache_control!r}"

    def test_templated_sw_keeps_no_cache(self, client) -> None:
        """The static cache default must NOT leak onto /sw.js — the service
        worker spec needs a fresh fetch so a new release installs promptly."""
        r = client.get("/sw.js")
        assert "max-age=0" in r.headers.get("Cache-Control", "")

    def test_api_still_no_store(self, client) -> None:
        """errors.py forces no-store on /api/* — the static default must not
        override it (a cached /api/health 500 mid-update is the bug #254 fixed)."""
        r = client.get("/api/health")
        assert r.headers.get("Cache-Control") == "no-store"

    def test_manifest_keeps_own_cache_not_static_default(self, client) -> None:
        """/manifest.webmanifest uses send_from_directory, so the global static
        default WOULD apply unless its route overrides Cache-Control (sw.py
        sets max-age=86400). Pin that override: if it's ever dropped, the
        manifest must not silently inherit the global 15-min static default."""
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert "max-age=86400" in r.headers.get("Cache-Control", "")
        assert "max-age=900" not in r.headers.get("Cache-Control", "")


class TestSplashMatrix:
    """D4 (17 sizes) + D8 (16/17 Pro Max + iPhone Air added) + DD6 (light +
    dark = 34 PNGs total)."""

    def test_seventeen_locked_sizes(self) -> None:
        assert len(SPLASH_SIZES) == 17

    @pytest.mark.parametrize("w,h", SPLASH_SIZES)
    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_splash_png_exists_at_correct_dimensions(self, w: int, h: int, mode: str) -> None:
        """Regression #5: 34 PNGs exist + dimensions match. Pillow.Image.open
        size assertion per file (per test plan)."""
        from PIL import Image

        path = REPO_ROOT / "src/control_server/static/splash" / f"splash-{w}x{h}-{mode}.png"
        assert path.exists(), f"missing splash: {path.name}"
        with Image.open(path) as img:
            assert img.size == (w, h), f"{path.name} expected {(w, h)}, got {img.size}"

    @pytest.mark.parametrize("w,h", SPLASH_SIZES)
    def test_splash_light_canvas_is_bg_color(self, w: int, h: int) -> None:
        """Light splash canvas must be #FBF6EC corner-pixel — DD6 says light
        canvas is --bg light. Inversion would flash dark before the shell."""
        from PIL import Image

        path = REPO_ROOT / "src/control_server/static/splash" / f"splash-{w}x{h}-light.png"
        with Image.open(path) as img:
            corner = img.convert("RGBA").getpixel((0, 0))
            assert corner == (251, 246, 236, 255), f"{path.name} corner must be --bg light #FBF6EC"

    @pytest.mark.parametrize("w,h", SPLASH_SIZES)
    def test_splash_dark_canvas_is_bg_dark(self, w: int, h: int) -> None:
        """Dark splash canvas must be #14110D — DD6's whole point is the
        dark-mode user not seeing a cream flash."""
        from PIL import Image

        path = REPO_ROOT / "src/control_server/static/splash" / f"splash-{w}x{h}-dark.png"
        with Image.open(path) as img:
            corner = img.convert("RGBA").getpixel((0, 0))
            assert corner == (20, 17, 13, 255), f"{path.name} corner must be --bg dark #14110D"

    def test_template_renders_all_34_splash_link_tags(self, client) -> None:
        """Regression #6: 34 link tags in base.html.j2 with portrait-orientation
        + prefers-color-scheme media queries."""
        body = client.get("/").data.decode()
        light_links = body.count("apple-touch-startup-image")
        # Each splash size emits 1 light + 1 dark = 2 link tags.
        assert light_links == 34, f"expected 34 apple-touch-startup-image links (17 sizes × 2 modes); got {light_links}"
        # Every link must carry an orientation: portrait media query.
        portrait_count = body.count("(orientation: portrait)")
        assert portrait_count >= 34
        # And both color-scheme branches must appear.
        assert "(prefers-color-scheme: light)" in body
        assert "(prefers-color-scheme: dark)" in body

    def test_template_links_specific_splash_files(self, client) -> None:
        """Smoke: at least the iPhone 16 Pro Max + iPad Pro entries are
        wired up. Catches whole-rows-missing in the for-loop in base.html.j2."""
        body = client.get("/").data.decode()
        for w, h in [(1320, 2868), (2048, 2732), (640, 1136)]:
            for mode in ("light", "dark"):
                assert f"splash-{w}x{h}-{mode}.png" in body


# ============================================================
# Fonts (D5, F1, F4)
# ============================================================


class TestFonts:
    """4 self-hosted variable woff2 files (Fraunces normal + italic per F4,
    Instrument Sans, Geist Mono) + @font-face declarations + 2 preloads."""

    @pytest.mark.parametrize(
        "filename",
        [
            "fraunces-wght-normal.woff2",
            "fraunces-wght-italic.woff2",
            "instrument-sans-wght-normal.woff2",
            "geist-mono-wght-normal.woff2",
        ],
    )
    def test_font_file_served(self, client, filename: str) -> None:
        r = client.get(f"/static/fonts/{filename}")
        assert r.status_code == 200
        # woff2 magic header: "wOF2"
        assert r.data[:4] == b"wOF2", f"{filename} is not a woff2 file"

    def test_tokens_css_has_font_face_declarations(self, client) -> None:
        css = client.get("/static/css/tokens.css").data.decode()
        # All four declarations.
        assert css.count("@font-face") >= 4
        for fam in ("Fraunces", "Instrument Sans", "Geist Mono"):
            assert f'font-family: "{fam}"' in css

    def test_tokens_css_uses_font_display_swap(self, client) -> None:
        """font-display: swap means system fallback paints first → ≤100ms
        FOUT instead of FOIT. Pinned per D5."""
        css = client.get("/static/css/tokens.css").data.decode()
        # Every @font-face block must carry font-display: swap.
        for block in re.findall(r"@font-face\s*\{[^}]*\}", css):
            assert "font-display: swap" in block, f"missing swap: {block[:80]}"

    def test_template_preloads_two_primary_faces(self, client) -> None:
        """D5: Fraunces + Instrument Sans preloaded; Geist Mono lazy on
        Updates tab via that tab's own asset block."""
        body = client.get("/").data.decode()
        assert 'rel="preload"' in body
        assert "fraunces-wght-normal.woff2" in body
        assert "instrument-sans-wght-normal.woff2" in body
        # Geist Mono must NOT be in the preload set on /.
        preload_block = "\n".join(line for line in body.splitlines() if 'rel="preload"' in line)
        assert "geist-mono" not in preload_block

    def test_fraunces_italic_axis_strategy(self) -> None:
        """F4: variable Fraunces wght-normal woff2 lacks the italic axis;
        Fontsource splits italic into a separate file. Pin so a future
        upgrade that drops the italic file is caught."""
        path = REPO_ROOT / "src/control_server/static/fonts/fraunces-wght-italic.woff2"
        assert path.exists(), "F4: italic Fraunces shipped as separate woff2 (variable wght-italic file)"
        # Sanity: it's a real woff2.
        assert path.read_bytes()[:4] == b"wOF2"


# ============================================================
# AtHS first-run hint (DD1 / DD2 / DD4 / DD5 / F2 / F5)
# ============================================================


class TestAthsHint:
    """Bottom-sheet card per DESIGN.md "First-run hint" + variant B layout
    locked 2026-04-30. Phone-only viewport gate at <600px (DD4)."""

    def test_aths_hint_partial_in_base_template(self, client) -> None:
        body = client.get("/").data.decode()
        # The card now ships with platform-prefixed default class
        # (aths-hint aths-hint--ios) per the codex /review fix that branches
        # icon + copy. Either form is acceptable for "partial is rendered."
        assert 'class="aths-hint aths-hint--ios"' in body or 'class="aths-hint"' in body

    def test_aths_hint_role_region_with_label(self, client) -> None:
        """DD5: <aside role="region" aria-label="Add to Home Screen hint">."""
        body = client.get("/").data.decode()
        assert 'role="region" aria-label="Add to Home Screen hint"' in body, (
            "DD5: AtHS card must be role=region with aria-label"
        )

    def test_aths_hint_dismiss_has_aria_label(self, client) -> None:
        """DD5: dismiss button gets aria-label='Dismiss hint'."""
        body = client.get("/").data.decode()
        assert 'aria-label="Dismiss hint"' in body

    def test_aths_hint_copy_locked_strings(self, client) -> None:
        """DD1 variant B copy: title + body + italic micro-instruction."""
        body = client.get("/").data.decode()
        assert "Add to Home Screen" in body
        assert "for one-tap access to your clock" in body
        assert "Tap the share icon below" in body

    def test_aths_hint_uses_ios_native_share_glyph(self, client) -> None:
        """DD2: iOS-native UIActivityViewController glyph (square + up-arrow
        exiting top), not Phosphor `share`. Inline SVG; no font icon."""
        body = client.get("/").data.decode()
        # Locate the AtHS partial — match either the bare class or the
        # platform-prefixed default `aths-hint--ios` from the M6 codex
        # /review fix that branches the icon by platform.
        partial_start = body.index('class="aths-hint')
        partial_end = body.index("</aside>", partial_start)
        partial = body[partial_start:partial_end]
        # An <svg> with up-arrow + square base path data.
        assert "<svg" in partial
        # Must reference both platform variant icon classes per codex /review.
        assert "aths-hint__icon--ios" in partial
        assert "aths-hint__icon--android" in partial

    def test_aths_hint_branches_icon_and_copy_for_android(self, client) -> None:
        """codex /review M6 P3 + DD2 plan deviation: Android Chrome installs
        via the menu, not the share toolbar. The hint must show a download-
        simple icon + Android-appropriate copy on Android. iOS keeps the
        UIActivityViewController glyph + 'Tap the share icon below' copy."""
        body = client.get("/").data.decode()
        # Two icon variants present.
        assert "aths-hint__icon--ios" in body
        assert "aths-hint__icon--android" in body
        # Two instruction variants present with platform-correct copy.
        assert "Tap the share icon below" in body  # iOS
        # Android — covers both newer Chrome ("Install app") + older Chrome /
        # non-PWA-eligible variants ("Add to Home screen"). Plain-HTTP private-
        # IP origin can fall back to the legacy menu word, hardware-found 2026-04-30.
        assert "Install app or Add to Home screen" in body
        # Default class on the <aside> is iOS so no-JS / unknown platform
        # users see iOS copy (the dominant install target).
        assert "aths-hint aths-hint--ios" in body

    def test_aths_hint_js_detects_android_via_useragent(self, client) -> None:
        """codex /review M6 — branch via navigator.userAgent test. UA-CH
        is the modern API but is gated behind permissions on iOS/older
        Chrome; UA test is the lowest-friction approach for this LAN-only
        appliance."""
        js = client.get("/static/js/aths-hint.js").data.decode()
        assert "navigator.userAgent" in js
        assert "Android" in js
        # Must add the .aths-hint--android class when matched.
        assert "aths-hint--android" in js

    def test_settings_js_refreshes_csrf_at_submit(self, client) -> None:
        """adversarial /review M6 — when the SW caches /settings HTML, the
        embedded csrf_token goes stale on Pi restart. settings.js intercepts
        form submit, fetches /api/csrf for a fresh token, writes it into
        the hidden input, then submits. No-JS path keeps the render-time
        token (acceptable trade-off for the no-JS edge case)."""
        js = client.get("/static/js/settings.js").data.decode()
        assert "/api/csrf" in js
        assert 'name="csrf_token"' in js
        # Must hook on the .settings-form selector that wraps each form.
        assert "settings-form" in js

    def test_api_csrf_endpoint_returns_fresh_token(self, client) -> None:
        """adversarial /review M6 + D2 architecture choice — /api/csrf returns
        a fresh CSRF token usable on POST /settings or /api/settings. Shape:
        {ok: true, csrf_token: <urlsafe>, expires_at: <unix-seconds>}."""
        r = client.get("/api/csrf")
        assert r.status_code == 200
        body = r.json
        assert body["ok"] is True
        assert isinstance(body["csrf_token"], str)
        assert len(body["csrf_token"]) >= 32  # secrets.token_urlsafe(32)
        assert isinstance(body["expires_at"], int)

    def test_api_csrf_token_is_valid_for_settings_post(self, client) -> None:
        """The token /api/csrf hands out must validate against the same
        store the POST /settings + POST /api/settings paths use. Otherwise
        the JS-refresh path would bake in a 403 every time."""
        token = client.get("/api/csrf").json["csrf_token"]
        # Use the token in a POST /api/settings (advanced section, NSFW
        # toggle is the smallest mutation we can verify the token-flow on).
        r = client.post(
            "/api/settings",
            json={"section": "advanced", "ALLOW_NSFW_QUOTES": "false", "csrf_token": token},
            headers={"Origin": "http://localhost", "Host": "localhost"},
        )
        # Either accepted (200) or rejected for non-token reasons (env file
        # missing in test env → 422). Critical: NOT 403 from CSRF mismatch.
        assert r.status_code != 403, f"CSRF token from /api/csrf rejected by POST: {r.json}"

    def test_aths_hint_css_default_hides_android_variants(self, client) -> None:
        """No-JS users land on iOS copy (default class); Android variants
        are display:none until JS adds .aths-hint--android."""
        css = client.get("/static/css/aths-hint.css").data.decode()
        # Android icon + instruction selectors must be in a display:none rule.
        assert ".aths-hint__icon--android" in css
        assert ".aths-hint__instruction--android" in css
        assert ".aths-hint--android .aths-hint__icon--android" in css

    def test_aths_hint_css_viewport_gate(self, client) -> None:
        """DD4: hidden by default; @media (max-width: 599px) opens it."""
        css = client.get("/static/css/aths-hint.css").data.decode()
        assert "display: none" in css
        assert "@media (max-width: 599px)" in css

    def test_aths_hint_css_44px_tap_area(self, client) -> None:
        """DD5: 24×24 visible glyph + 10px transparent padding = 44×44."""
        css = client.get("/static/css/aths-hint.css").data.decode()
        # The dismiss button block must declare 44px on each side.
        dismiss_idx = css.find(".aths-hint__dismiss {")
        assert dismiss_idx > 0
        block = css[dismiss_idx : css.find("}", dismiss_idx)]
        assert "width: 44px" in block
        assert "height: 44px" in block
        assert "padding: 10px" in block

    def test_aths_hint_js_checks_both_standalone_signals(self, client) -> None:
        """Regression #11 / F2: both `(display-mode: standalone)` AND
        navigator.standalone."""
        js = client.get("/static/js/aths-hint.js").data.decode()
        assert "(display-mode: standalone)" in js
        assert "navigator.standalone" in js

    def test_aths_hint_js_localstorage_try_catch(self, client) -> None:
        """Regression #12 / F5: localStorage access wrapped in try/catch
        (Safari Private mode setItem throws)."""
        js = client.get("/static/js/aths-hint.js").data.decode()
        # Find getItem + setItem; each must be inside a try block.
        # Simpler check: every localStorage reference is followed within
        # 200 chars by a `catch (`. Robust against minor refactoring.
        for match in re.finditer(r"localStorage\.(get|set)Item", js):
            window = js[max(0, match.start() - 200) : match.end() + 200]
            assert "catch (" in window, f"localStorage call at index {match.start()} not in try/catch"

    def test_aths_hint_600ms_delay(self, client) -> None:
        """DESIGN.md spec: card slides up 600ms after page load."""
        js = client.get("/static/js/aths-hint.js").data.decode()
        assert "600" in js


# ============================================================
# Service worker registration (D9)
# ============================================================


class TestServiceWorkerRegistration:
    """D9: feature-detect + isSecureContext gate so iOS short-circuits
    cleanly without registering a SW that can't run."""

    def test_sw_register_js_served(self, client) -> None:
        r = client.get("/static/js/sw-register.js")
        assert r.status_code == 200

    def test_sw_register_checks_isSecureContext(self, client) -> None:
        """Regression #14: iOS Safari at our plain-HTTP origin reports
        isSecureContext=false. The register path MUST short-circuit
        (console.info, no register call)."""
        js = client.get("/static/js/sw-register.js").data.decode()
        assert "isSecureContext" in js
        # Short-circuit must precede any register() call textually.
        ctx_idx = js.index("isSecureContext")
        register_idx = js.find(".register(")
        assert register_idx > ctx_idx, "isSecureContext guard must come before .register() call"

    def test_sw_register_checks_serviceWorker_in_navigator(self, client) -> None:
        """Belt-and-suspenders: guard against very old browsers."""
        js = client.get("/static/js/sw-register.js").data.decode()
        assert "'serviceWorker' in navigator" in js

    def test_sw_register_loaded_in_template(self, client) -> None:
        body = client.get("/").data.decode()
        assert "sw-register.js" in body
        assert "aths-hint.js" in body


# ============================================================
# #258 caption ceiling (D6)
# ============================================================


class TestCaptionCeilingFix:
    """D6: --fs-caption ceiling raised 14 → 18 so iOS Dynamic Type can grow
    tab labels visibly past iOS Larger 1. M5 tactical local override on
    .tabbar a removed."""

    def test_caption_ceiling_is_18px(self, client) -> None:
        """Regression #3."""
        css = client.get("/static/css/tokens.css").data.decode()
        assert "--fs-caption: clamp(11px, 0.75rem, 18px)" in css

    def test_tabbar_a_uses_global_caption_token(self, client) -> None:
        """Regression #4: M5 tactical override gone; tabbar uses global token."""
        css = client.get("/static/css/tokens.css").data.decode()
        idx = css.find(".tabbar a {")
        block = css[idx : css.find("}", idx)]
        assert "font-size: var(--fs-caption)" in block


# ============================================================
# Documentation invariants (regression #9, #10)
# ============================================================


class TestDocumentationPins:
    """DESIGN.md + NOTICE.md must reflect M6's locked decisions."""

    def test_design_md_documents_platform_asymmetric_sw(self) -> None:
        """Regression #9: DESIGN.md PWA Shell Requirements notes the SW
        secure-context limitation — hardware-found post-merge on Android
        that BOTH iOS AND Android short-circuit at our private-IP plain-
        HTTP origin (not just iOS as the original D9 lock assumed)."""
        design_md = (REPO_ROOT / "DESIGN.md").read_text()
        # PWA shell section must mention isSecureContext / inert / both-platforms.
        assert "isSecureContext" in design_md, (
            "DESIGN.md PWA Shell Requirements must reference isSecureContext as the gating condition for the SW path"
        )

    def test_platform_limits_doc_exists(self) -> None:
        """Hardware-found 2026-04-30 — the v1 SW + Android PWA install
        limits MUST be documented somewhere durable so the next maintainer
        doesn't re-discover them. `docs/control-pwa-platform-limits.md` is
        the single source of truth referenced from DESIGN.md, the plan,
        and CHANGELOG."""
        path = REPO_ROOT / "docs" / "control-pwa-platform-limits.md"
        assert path.exists(), (
            "docs/control-pwa-platform-limits.md must exist + document the v1 plain-HTTP SW limitations"
        )
        body = path.read_text()
        # Mention all three things the doc has to nail down.
        assert "isSecureContext" in body
        assert "192.168" in body  # private IP discussion
        assert "v2" in body.lower()  # forward-compat path

    def test_design_md_caption_ceiling_18(self) -> None:
        """Type scale section must reflect the 18px ceiling from D6."""
        design_md = (REPO_ROOT / "DESIGN.md").read_text()
        assert "clamp(11px, 0.75rem, 18px)" in design_md, (
            "DESIGN.md Type scale row for `caption` must show the 18px ceiling"
        )

    def test_design_md_documents_aths_share_icon_exception(self) -> None:
        """DD2: single-component DESIGN.md exception for the iOS-native
        share glyph in the AtHS hint. Pin so a future cleanup doesn't
        silently revert the spec."""
        design_md = (REPO_ROOT / "DESIGN.md").read_text()
        # First-run hint section must contain the exception note.
        hint_idx = design_md.find('First-run "Add to Home Screen" hint')
        assert hint_idx > 0
        # 2KB window after the heading (short section).
        section = design_md[hint_idx : hint_idx + 4000]
        assert "exception" in section.lower(), (
            "DESIGN.md §First-run hint must document the iOS share-icon exception per DD2"
        )

    def test_notice_md_carries_sil_ofl_entries(self) -> None:
        """Regression #10 / F1: SIL OFL entries for Fraunces, Instrument
        Sans, Geist Mono."""
        notice = (REPO_ROOT / "NOTICE.md").read_text()
        for fam in ("Fraunces", "Instrument Sans", "Geist Mono"):
            assert fam in notice, f"NOTICE.md missing {fam} entry per F1"
        # SIL OFL reference must be in the same document.
        assert "SIL Open Font License" in notice or "OFL" in notice
