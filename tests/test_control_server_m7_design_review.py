"""M7 — design-review fix pins.

Each test guards a fix that landed during M7's `/design-review` cycle.
Pinning so a future refactor can't silently revert the design fix.

Findings covered:
- F-001: `<button>` user-agent default Arial leaks into `.aths-hint__dismiss`
- F-002 / F-003: AtHS hint card overlaid Status WiFi/Weather + Settings
  Gift Mode rows on first-run mobile viewport (highest-impact M7 finding)
- F-004: Updates `Last checked` row showed raw Unix epoch instead of
  human-readable relative time
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from control_server import create_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    return create_app({"VERSION_OVERRIDE": "v0.test-m7"}).test_client()


# ============================================================
# F-001: Arial leak in dismiss button user-agent default
# ============================================================


class TestAthsDismissButtonFontFamily:
    def test_dismiss_button_inherits_project_font(self, client) -> None:
        """`<button>` defaults to Arial in Chromium. The dismiss button
        ships no visible text today — but a future label addition should
        inherit the project sans family. M7 design-review caught Arial
        leaking via the rendered-fonts extractor on test Pi."""
        css = client.get("/static/css/aths-hint.css").data.decode()
        idx = css.find(".aths-hint__dismiss {")
        assert idx > 0
        block = css[idx : css.find("}", idx)]
        assert "font-family: inherit" in block, (
            "M7 F-001: .aths-hint__dismiss must declare font-family so Chrome's <button> Arial default doesn't leak"
        )


# ============================================================
# F-002 / F-003: AtHS hint overlays critical content on mobile
# ============================================================


class TestAthsHintOverlayFix:
    """Hardware-found 2026-05-01: the AtHS hint card was `position: fixed`
    at `bottom: ~88px`. On mobile portrait viewports the document flow put
    Status WiFi/Weather rows + Settings Gift Mode toggle at exactly that
    band, so first-run users couldn't see them until they dismissed the
    hint. Fix (after a body-padding hack didn't work — `padding-bottom`
    extends scroll area but doesn't push content up on short pages):
    move the hint OUT of fixed positioning into the document flow inside
    `<main>` so it renders below content, never overlaying."""

    def _css(self, client) -> str:
        return client.get("/static/css/aths-hint.css").data.decode()

    def test_hint_is_no_longer_position_fixed(self, client) -> None:
        """The original M6 fixed-position bottom-sheet was the source of
        the overlay bug. Pin that the .aths-hint base rule does NOT use
        position: fixed so a future refactor can't silently reintroduce
        it and re-cause the overlay."""
        import re as _re

        css = self._css(client)
        # Strip /* ... */ comments so explanatory prose mentioning the
        # phrase "position: fixed" doesn't trip the substring match.
        css_no_comments = _re.sub(r"/\*.*?\*/", "", css, flags=_re.DOTALL)
        idx = css_no_comments.find(".aths-hint {")
        assert idx > 0
        block = css_no_comments[idx : css_no_comments.find("}", idx)]
        assert "position: fixed" not in block, (
            "M7 F-002/F-003: the AtHS hint must NOT be position: fixed — "
            "fixed-positioning overlaid Status/Settings content rows on mobile."
        )
        # And no `bottom:` declaration either, which was the symptom signature.
        # Match the property declaration (skip text in comments / animation
        # keyframes elsewhere in the file via the comment-stripped block).
        assert not _re.search(r"\bbottom:\s*calc", block)

    def test_hint_renders_inside_main_in_document_flow(self, client) -> None:
        """The hint <aside> markup must live INSIDE <main> so it flows
        below page content. Outside of main (the original M6 placement
        after the tab bar) means fixed-positioning was the only way to
        keep it visible — which caused the overlay."""
        body = client.get("/").data.decode()
        main_start = body.find("<main")
        main_end = body.find("</main>", main_start)
        assert main_start > 0 and main_end > main_start
        main_block = body[main_start:main_end]
        assert 'class="aths-hint' in main_block, (
            "AtHS hint <aside> must render inside <main> so it flows below "
            "content instead of overlaying via position: fixed"
        )

    def test_no_body_padding_hack(self, client) -> None:
        """The earlier-attempted body:has() padding-bottom hack didn't
        work (padding extends scroll area, doesn't push content up on
        short pages) and was replaced by inline-flow positioning. Pin
        that the hack is gone — the M2 body padding-bottom (tab bar
        clearance) is the only padding rule that should remain."""
        css = self._css(client)
        assert "body:has(.aths-hint" not in css, (
            "the body:has() padding-bottom hack was the failed first attempt; "
            "inline-flow positioning is the working fix and the hack must not "
            "linger in the stylesheet"
        )


# ============================================================
# F-004: Updates `Last checked` raw Unix epoch fix
# ============================================================


class TestUpdatesLastCheckedRelative:
    """Hardware-found 2026-05-01: the Updates tab's "Last checked" row
    showed `1777673436` (raw Unix epoch) because the M5 template comment
    referenced a JS replacer that was never wired up. Fix: server-render
    the relative time via `_format_relative` so the no-JS path is
    correct on first paint."""

    def test_renders_human_relative_when_cache_present(self, tmp_path, monkeypatch) -> None:
        cache_file = tmp_path / "update-check.json"
        # 5 minutes ago — should render "5 minutes ago".
        import time as _time

        five_min_ago = int(_time.time()) - 5 * 60
        cache_file.write_text(
            json.dumps(
                {
                    "fetched_at_unix": five_min_ago,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.210.0",
                    "available": False,
                }
            )
        )
        monkeypatch.setenv("LITCLOCK_UPDATE_CHECK_CACHE", str(cache_file))
        app = create_app({"VERSION_OVERRIDE": "v0.test-m7"})
        body = app.test_client().get("/updates").data.decode()
        # Must NOT show the raw unix epoch in the visible text content.
        assert f">{five_min_ago}<" not in body, (
            "M7 F-004: raw unix epoch must not appear as the visible "
            "'Last checked' text — rendered as relative time instead"
        )
        # Must contain something that reads like relative time.
        assert "minute" in body or "just now" in body or "hour" in body, (
            "expected human-readable relative time in 'Last checked' row"
        )

    def test_data_attribute_preserved_for_future_ticker(self, tmp_path, monkeypatch) -> None:
        """The `data-fetched-at` attribute stays so a future client-side
        ticker can keep "minutes ago" fresh without a page reload. Only
        the visible text changed in the M7 fix."""
        cache_file = tmp_path / "update-check.json"
        import time as _time

        ts = int(_time.time()) - 60
        cache_file.write_text(
            json.dumps(
                {
                    "fetched_at_unix": ts,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.210.0",
                    "available": False,
                }
            )
        )
        monkeypatch.setenv("LITCLOCK_UPDATE_CHECK_CACHE", str(cache_file))
        app = create_app({"VERSION_OVERRIDE": "v0.test-m7"})
        body = app.test_client().get("/updates").data.decode()
        assert f'data-fetched-at="{ts}"' in body


# ============================================================
# OV-4: Restart shouldn't render destructive red
# ============================================================


class TestSystemActionDefaultColor:
    """Codex /design-review (M7 OV-4): `.action-card__submit` made all 3
    system actions destructive --error red. Restart is recoverable (30s
    blank then resume); only Power off + Reset WiFi are truly destructive."""

    def test_action_card_submit_default_is_accent_not_error(self, client) -> None:
        css = client.get("/static/css/system.css").data.decode()
        # Find the .action-card__submit base rule and verify color is --accent.
        idx = css.find(".action-card__submit {")
        assert idx > 0
        block = css[idx : css.find("}", idx)]
        assert "color: var(--accent)" in block, (
            "M7 OV-4: default `.action-card__submit` must be --accent (tertiary "
            "text-link), not --error. Restart is recoverable; only Power off / "
            "Reset WiFi should render destructive red."
        )

    def test_destructive_actions_keep_error_color(self, client) -> None:
        """Power off + Reset WiFi cards should still get --error color via
        attribute selectors. Pin so a future cleanup can't drop them."""
        css = client.get("/static/css/system.css").data.decode()
        assert '[data-action="poweroff"] .action-card__submit' in css
        assert '[data-action="wifi_reset"] .action-card__submit' in css


# ============================================================
# OV-5: Tab bar Phosphor icons
# ============================================================


class TestTabBarIcons:
    """Codex /design-review (M7 OV-5): DESIGN.md "Icon mapping" locks
    Phosphor glyphs per tab (book-open / gear / power / download-simple).
    M1 shipped text-only tab bar."""

    def test_each_tab_has_inline_svg_icon(self, client) -> None:
        body = client.get("/").data.decode()
        # Find the tab bar and count <svg class="tabbar__icon"> within.
        nav_start = body.find('class="tabbar"')
        nav_end = body.find("</nav>", nav_start)
        nav_block = body[nav_start:nav_end]
        assert nav_block.count('class="tabbar__icon"') == 4, (
            "M7 OV-5: each tab in the bottom tab bar must include an inline "
            "Phosphor icon (book-open / gear / power / download-simple)"
        )

    def test_icons_use_currentColor_for_active_tab_tinting(self, client) -> None:
        """Icons should inherit color via stroke=currentColor so the active
        tab's --accent flows through to the icon automatically."""
        body = client.get("/").data.decode()
        nav_start = body.find('class="tabbar"')
        nav_end = body.find("</nav>", nav_start)
        nav_block = body[nav_start:nav_end]
        assert nav_block.count('stroke="currentColor"') == 4

    def test_icons_have_aria_hidden(self, client) -> None:
        """Screen readers should announce only the visible text label, not
        the decorative icon."""
        body = client.get("/").data.decode()
        nav_start = body.find('class="tabbar"')
        nav_end = body.find("</nav>", nav_start)
        nav_block = body[nav_start:nav_end]
        # 4 SVG icons each with aria-hidden.
        assert nav_block.count('aria-hidden="true"') >= 4


# ============================================================
# OV-6: Settings WiFi-reset accent contrast
# ============================================================


class TestSettingsFooterContrast:
    """Codex /design-review (M7 OV-6): the Settings footer "Need to change
    WiFi? System → Reset WiFi" used .accent-link on inline body text, which
    DESIGN.md "Color contrast" explicitly forbids (3.2:1 fails AA Normal at
    14-16px)."""

    def test_settings_footer_drops_accent_link_class(self, client) -> None:
        body = client.get("/settings").data.decode()
        # Find the settings-footer paragraph and confirm no accent-link.
        footer_start = body.find('class="settings-footer"')
        assert footer_start > 0
        footer_end = body.find("</p>", footer_start)
        footer = body[footer_start:footer_end]
        assert "accent-link" not in footer, (
            "M7 OV-6: settings footer link must NOT use .accent-link (DESIGN.md "
            "line 315 forbids accent on body text — fails WCAG AA at 14-16px)"
        )


# ============================================================
# OV-1 / OV-2 / OV-3: updates.css design-token migration + shared confirm-sheet
# ============================================================


class TestUpdatesCssTokenMigration:
    """Codex + Claude /design-review (M7 OV-1/OV-2/OV-3): updates.css was
    a parallel design system — hardcoded spacing/radius/font-size,
    `system-ui` fallback in `font:` shorthand (DESIGN.md font blacklist),
    wrong fallback hex values, divergent confirm-sheet reimplementation."""

    def _css(self, client) -> str:
        return client.get("/static/css/updates.css").data.decode()

    def test_no_font_shorthand_with_system_ui(self, client) -> None:
        """`font:` shorthand pulled `system-ui` into the rendered stack —
        DESIGN.md line 72 forbids system-ui as a primary face. Pin that
        the rewrite uses `font-family` + `font-size` + `font-weight`
        properties separately, with --font-* tokens."""
        import re as _re

        css = self._css(client)
        # Strip /* ... */ comments — the explanatory header mentions the
        # original problem ("system-ui in font: shorthand") and would
        # trip a naive substring match.
        css_no_comments = _re.sub(r"/\*.*?\*/", "", css, flags=_re.DOTALL)
        assert "system-ui" not in css_no_comments, (
            "M7 OV-3: updates.css CSS rules must not use system-ui (DESIGN.md font blacklist)"
        )

    def test_uses_design_tokens_for_typography(self, client) -> None:
        """Type sizes must come from --fs-* tokens so iOS Dynamic Type
        propagates to the Updates surface (the persona DESIGN.md was
        written for)."""
        css = self._css(client)
        # Sample a few known roles.
        assert "var(--fs-small)" in css
        assert "var(--fs-body)" in css
        assert "var(--font-sans)" in css
        assert "var(--font-serif)" in css
        assert "var(--font-mono)" in css

    def test_no_hardcoded_hex_fallbacks(self, client) -> None:
        """Codex caught: every `var(--token, #fallback)` in updates.css
        had a wrong fallback (e.g., `#FFFAF0` for --surface vs locked
        `#F4ECDC`). When the fallback path fires the user sees a different
        brand. Tokens.css ships before this file via base.html.j2; no
        fallback path is ever needed."""
        import re as _re

        css = self._css(client)
        # Strip /* ... */ comments so explanatory hex codes inside data: URLs
        # don't trip this assertion. The locked SVG icon hexes inside
        # `url("data:image/svg+xml;...stroke='%23XXXXXX'...")` are intentional
        # because data: URLs can't reference CSS custom properties.
        # Match `var(--something, #XXXXXX)` patterns specifically.
        matches = _re.findall(r"var\(--[\w-]+,\s*#[0-9A-Fa-f]{3,8}\)", css)
        assert not matches, f"M7 OV-1: updates.css must not have hex fallbacks in var(): {matches}"

    def test_uses_spacing_tokens(self, client) -> None:
        """Hardcoded `gap: 16px`, `margin: 24px auto`, `padding: 14px 20px`
        all replaced with --sp-* tokens."""
        css = self._css(client)
        assert "var(--sp-md)" in css or "var(--sp-lg)" in css
        # And --pad-card / --gap-list shadow tokens are gone.
        assert "--pad-card" not in css
        assert "--gap-list" not in css

    def test_confirm_sheet_styles_extracted_to_shared_partial(self, client) -> None:
        """OV-2: confirm-sheet duplicated between system.css and updates.css.
        Now lives in confirm-sheet.css; both system.html.j2 and updates.html.j2
        link it."""
        css = self._css(client)
        # The duplicated dialog.confirm-sheet rules should be GONE from
        # updates.css now. They live in confirm-sheet.css.
        assert "dialog.confirm-sheet" not in css
        assert ".confirm-sheet__inner" not in css

    def test_confirm_sheet_partial_exists_and_is_canonical(self, client) -> None:
        r = client.get("/static/css/confirm-sheet.css")
        assert r.status_code == 200
        css = r.data.decode()
        # Carries the canonical rules.
        assert "dialog.confirm-sheet" in css
        assert ".confirm-sheet__inner" in css
        assert "@keyframes sheet-slide-up" in css
        assert "@media (prefers-reduced-motion: reduce)" in css
        # Uses tokens (not hardcoded values).
        assert "var(--surface-raised)" in css
        assert "var(--t-medium)" in css

    def test_both_tabs_link_confirm_sheet_partial(self, client) -> None:
        """Pin that both /system and /updates link confirm-sheet.css so a
        future refactor can't silently drop one of them and reintroduce
        the divergence."""
        for path in ("/system", "/updates"):
            body = client.get(path).data.decode()
            assert "css/confirm-sheet.css" in body, f"{path} must link the shared confirm-sheet.css partial"

    def test_system_css_no_longer_carries_confirm_sheet(self, client) -> None:
        """Confirmation that the duplicate is gone from system.css too."""
        css = client.get("/static/css/system.css").data.decode()
        # The dialog.confirm-sheet rules should have moved out.
        assert "dialog.confirm-sheet" not in css


# ============================================================
# #305 — confirm-sheet slide-up didn't render on iOS Safari
# ============================================================


class TestConfirmSheetSlideAnimation:
    """#305: iOS Safari pre-17.5 doesn't fire CSS keyframes gated on the
    native `<dialog>` `[open]` attribute because top-layer promotion +
    display flip happen in the same paint. Fix: animate `.is-opening`
    instead, added by JS via double-rAF after `showModal()`."""

    @staticmethod
    def _confirm_css(client) -> str:
        return client.get("/static/css/confirm-sheet.css").data.decode()

    def test_animations_target_is_opening_class_not_open_attribute(self, client) -> None:
        css = self._confirm_css(client)
        # The slide-up + fade-in must trigger on the class, not [open].
        assert "dialog.confirm-sheet.is-opening" in css, (
            "#305: slide-up must animate `.is-opening` so iOS Safari's "
            "top-layer promotion doesn't eat the keyframe `from` state"
        )
        # The legacy `[open]` selector for the keyframes must be gone.
        # (It's fine elsewhere — e.g., positioning rules — but the
        # animation rules need the class.)
        assert "dialog.confirm-sheet[open] {\n    animation: sheet-slide-up" not in css
        assert "dialog.confirm-sheet[open] {\n    animation: sheet-fade-in" not in css

    def test_reduced_motion_still_snaps(self, client) -> None:
        """The reduced-motion override must still hit the same selector
        the animation uses, otherwise the snap-cut breaks."""
        css = self._confirm_css(client)
        # Find the reduced-motion media query and verify it scopes
        # animation: none to the new selector.
        idx = css.find("@media (prefers-reduced-motion: reduce)")
        assert idx > 0
        block = css[idx : css.find("}\n}", idx) + 3]
        assert "is-opening" in block, (
            "reduced-motion override must target `.is-opening` — same selector "
            "the animation uses; otherwise the snap-cut for users opting out breaks"
        )

    def test_system_js_uses_double_raf_helper(self, client) -> None:
        js = client.get("/static/js/system.js").data.decode()
        # The helper must exist (function definition) and the open-on-submit
        # path must call it instead of `dialog.showModal()` directly.
        assert "function openConfirmSheet" in js
        # Two nested requestAnimationFrames separate top-layer promotion
        # from the class toggle. Pin the structural pattern.
        assert js.count("requestAnimationFrame") >= 2, (
            "#305: openConfirmSheet must use a double-rAF chain to defer "
            "the `.is-opening` toggle past iOS Safari's top-layer promotion"
        )
        assert "classList.add('is-opening')" in js
        # Close handler strips the class so re-opens re-trigger the keyframe.
        assert "classList.remove('is-opening')" in js
        # No more bare showModal() in the submit path — must go through helper.
        assert "openConfirmSheet(dialog)" in js

    def test_updates_js_uses_double_raf_helper(self, client) -> None:
        js = client.get("/static/js/updates.js").data.decode()
        assert "function openConfirmSheet" in js
        assert js.count("requestAnimationFrame") >= 2
        assert "classList.add('is-opening')" in js
        assert "classList.remove('is-opening')" in js
        assert "openConfirmSheet(dialog)" in js

    def test_open_helpers_guard_against_stale_is_opening_class(self, client) -> None:
        """Codex /review on #305 PR: if the user cancels within the ~33ms
        double-rAF window, the pending rAF still adds `.is-opening` to a
        closed dialog. The next open inherits the stale class and the
        animation is suppressed (defeats the fix). Two defensive guards:
        strip the class before showModal, and check `dialog.open` inside
        the inner rAF before adding it."""
        for path in ("/static/js/system.js", "/static/js/updates.js"):
            js = client.get(path).data.decode()
            # Find the openConfirmSheet body and look for the guards.
            idx = js.find("function openConfirmSheet")
            assert idx > 0, f"{path}: openConfirmSheet not defined"
            # Take a generous slice — function bodies in these files are short.
            body = js[idx : idx + 800]
            # Guard 1: strip stale class BEFORE showModal.
            remove_idx = body.find("classList.remove('is-opening')")
            show_idx = body.find(".showModal(")
            assert remove_idx > 0 and show_idx > 0
            assert remove_idx < show_idx, (
                f"{path}: stale `.is-opening` must be cleared BEFORE showModal "
                "so an interrupted previous open doesn't suppress the next animation"
            )
            # Guard 2: check `.open` inside the rAF callback before adding.
            assert ".open" in body, (
                f"{path}: rAF callback must guard `classList.add('is-opening')` "
                "behind a `dialog.open` check — codex /review #305"
            )

    def test_pre_opening_state_pre_positions_dialog_at_keyframe_from(self, client) -> None:
        """Codex /review on #305 PR: showModal() paints the dialog at its
        final resting position for ≥1 frame before `.is-opening` triggers
        the keyframe from translateY(100%). Without a pre-opening CSS
        rule, users see the dialog flash at its final position, then snap
        off-screen, then slide back. Pre-positioning at the keyframe
        `from` state during the `[open]:not(.is-opening)` window keeps
        the slide reading as one motion."""
        css = self._confirm_css(client)
        # Phone variant: pre-opening transform must be translateY(100%).
        # Find the rule for `[open]:not(.is-opening)` inside the ≤640px MQ.
        assert "dialog.confirm-sheet[open]:not(.is-opening) {\n    transform: translateY(100%)" in css, (
            "#305 codex: phone-variant pre-opening rule missing — dialog will flash at final position before sliding"
        )
        # Tablet variant: pre-opening opacity must be 0.
        assert "dialog.confirm-sheet[open]:not(.is-opening) {\n    opacity: 0" in css, (
            "#305 codex: tablet-variant pre-opening rule missing — dialog will flash before fading in"
        )

    def test_backdrop_fade_synced_with_sheet_animation(self, client) -> None:
        """Codex /review on #305 PR: backdrop animation was unconditional
        on `::backdrop`, so it fired on showModal() while the sheet
        animation was still in the pre-opening hold. Sheet + backdrop
        de-synced. Fix: scope backdrop fade to `.is-opening` and hide it
        during the pre-opening window."""
        css = self._confirm_css(client)
        # Backdrop animation must be scoped to `.is-opening`.
        assert "dialog.confirm-sheet.is-opening::backdrop" in css
        # Pre-opening backdrop must be transparent.
        assert "dialog.confirm-sheet[open]:not(.is-opening)::backdrop" in css
        # The unconditional `::backdrop { animation: ... }` rule must be GONE.
        # (The base ::backdrop rule still sets the final background; only the
        # animation moves to the scoped selector.)
        idx = css.find("dialog.confirm-sheet::backdrop {")
        assert idx > 0
        block = css[idx : css.find("}", idx)]
        assert "animation" not in block, (
            "#305 codex: backdrop animation must be scoped to `.is-opening` "
            "so it stays in lockstep with the sheet's slide-up"
        )

    def test_reduced_motion_overrides_pre_opening_state(self, client) -> None:
        """Without explicit overrides for the new pre-opening rules,
        reduced-motion users would see a 1-2 frame `transform/opacity`
        change before snap — defeats the opt-out. Pin that the reduced-
        motion block resets transform + opacity for both
        `[open]:not(.is-opening)` AND `.is-opening` selectors."""
        css = self._confirm_css(client)
        idx = css.find("@media (prefers-reduced-motion: reduce)")
        assert idx > 0
        # Take everything up to the closing `}` of the media query (the
        # block contains nested rules, so look for `}\n}` which ends the
        # outer wrapper).
        block = css[idx : css.find("}\n}", idx) + 3]
        # Both selectors must be inside the override.
        assert "[open]:not(.is-opening)" in block, (
            "#305 codex: reduced-motion must override the pre-opening rule so users opting out get a true snap-cut"
        )
        assert "transform: none" in block
        assert "opacity: 1" in block


# ============================================================
# #300 — confirm button always rendered destructive red
# ============================================================


class TestConfirmSheetDestructiveAttribute:
    """#300: `.confirm-sheet__confirm` hardcoded `--error` red regardless
    of action. Restart (recoverable) and Apply Update (non-destructive
    upgrade) opened sheets with red confirm buttons. Fix: default to
    `--accent`; opt into `--error` via `data-destructive="true"` on the
    dialog, scoped via attribute selector."""

    @staticmethod
    def _confirm_css(client) -> str:
        return client.get("/static/css/confirm-sheet.css").data.decode()

    def test_default_confirm_color_is_accent_not_error(self, client) -> None:
        css = self._confirm_css(client)
        # Find the `.confirm-sheet__confirm {` base rule and verify color.
        idx = css.find(".confirm-sheet__confirm {")
        assert idx > 0
        block = css[idx : css.find("}", idx)]
        assert "color: var(--accent)" in block, (
            '#300: default confirm must be --accent; only `data-destructive="true"` '
            "dialogs should render destructive red"
        )
        assert "color: var(--error)" not in block, (
            "#300: hard-coded --error in the base rule defeats the data-destructive scope"
        )

    def test_destructive_variant_scopes_error_color(self, client) -> None:
        css = self._confirm_css(client)
        assert 'dialog.confirm-sheet[data-destructive="true"] .confirm-sheet__confirm' in css
        # And that scoped rule still uses --error (not some other token).
        idx = css.find('dialog.confirm-sheet[data-destructive="true"] .confirm-sheet__confirm')
        block = css[idx : css.find("}", idx)]
        assert "color: var(--error)" in block

    @staticmethod
    def _dialog_block(body: str, action: str) -> str:
        """Slice out the <dialog>...</dialog> for the given action.

        `data-action="<x>"` appears on both the action-card AND the dialog;
        anchor on `<dialog ` to land on the dialog hit, not the card."""
        # Find each `<dialog ` and pick the one whose attributes include
        # the matching data-action.
        cursor = 0
        while True:
            start = body.find("<dialog ", cursor)
            if start < 0:
                break
            end = body.find("</dialog>", start)
            block = body[start : end + len("</dialog>")]
            if f'data-action="{action}"' in block:
                return block
            cursor = end + 1
        raise AssertionError(f"no <dialog> with data-action={action!r} found")

    def test_reboot_dialog_marked_non_destructive(self, client) -> None:
        body = client.get("/system").data.decode()
        block = self._dialog_block(body, "reboot")
        assert 'data-destructive="false"' in block, (
            "#300: Restart is recoverable — confirm button should NOT be destructive red"
        )

    def test_poweroff_dialog_marked_destructive(self, client) -> None:
        body = client.get("/system").data.decode()
        block = self._dialog_block(body, "poweroff")
        assert 'data-destructive="true"' in block, (
            "#300: Power off is unrecoverable from the PWA — confirm should be red"
        )

    def test_wifi_reset_dialog_marked_destructive(self, client) -> None:
        body = client.get("/system").data.decode()
        block = self._dialog_block(body, "wifi_reset")
        assert 'data-destructive="true"' in block, (
            "#300: Reset WiFi wipes saved profiles + drops the LAN — confirm should be red"
        )

    def test_update_apply_dialog_marked_non_destructive(self, client) -> None:
        body = client.get("/updates").data.decode()
        block = self._dialog_block(body, "update_apply")
        assert 'data-destructive="false"' in block, (
            "#300: Apply Update is an upgrade, not destruction — confirm should be accent"
        )
