# Control PWA — M0 Validation Checklist

This file collects the hardware/visual evidence that gates the M0 PR for the
LitClock Control PWA epic (issue #245). Each row has a status box; record
evidence (photo path, eyedropper value, terminal output, observation notes)
beside it.

**Source artifacts:**
- `PRD-LitClock-Control-PWA.md`
- `PLAN-LitClock-Control-PWA.md` (locked decisions A6 + D5 + D6)
- `DESIGN.md` (locked v1 design system)

**Scope reminder:** M0 is assets-only. No runtime code changes; nothing in
`src/literary_clock.py` or `src/eink_display.py` is modified. The QR composite
and the relocated update-failed glyph are wired into runtime in M2.

---

## 1. Paper-grain SVG (D6)

- File: `src/control_server/static/paper-grain.svg`
- Spec: `<feTurbulence type="fractalNoise" baseFrequency~0.9 numOctaves=2 stitchTiles="stitch">` over solid `<rect>`, alpha squeezed to 0..0.04 via `feColorMatrix`. Target file size <2 KB.

| Check | Status | Evidence |
|---|---|---|
| File exists at expected path | [x] | `ls -la src/control_server/static/paper-grain.svg` → … |
| File size <2 KB | [x] | `wc -c src/control_server/static/paper-grain.svg` → … bytes |
| Reads as paper texture, not visible noise, in a browser preview at ~480 px tile | [x] | (eyeball, light-mode preview) |
| Tiles seamlessly when used as `background-image: url(...); background-repeat: repeat` (no visible seams at tile boundaries) | [x] | (browser DevTools test) |
| RGB locked to warm dark ink (no blue/green tint visible) | [x] | (eyeball + DevTools eyedropper a high-contrast pixel) |

---

## 2. PWA icon set (D5)

- Generator: `tools/control-pwa/generate_pwa_icons.py`
- Output dir: `src/control_server/static/icons/`
- Source: `logo.png` (512×512 RGBA)

| Variant | Spec | Status | Corner pixel | Evidence |
|---|---|---|---|---|
| `icon-192.png` | 192×192 RGBA, transparent bg | [x] | (0, 0, 0, 0) | … |
| `icon-512.png` | 512×512 RGBA, transparent bg | [x] | (0, 0, 0, 0) | … |
| `icon-maskable-512.png` | 512×512, logo at ~80% scale (~410 px) centered, solid `--bg` (`#FBF6EC`) full-canvas fill, 10% safe-zone padding around logo | [x] | (251, 246, 236, 255) | … |
| `icon-bg-baked-512.png` | 512×512, logo at 100% scale on solid `--bg` (`#FBF6EC`) | [x] | (251, 246, 236, 255) | … |

| Check | Status | Evidence |
|---|---|---|
| Generator is idempotent: `python3 tools/control-pwa/generate_pwa_icons.py --check` exits 0 on a fresh checkout | [x] | (terminal output) |
| Maskable safe zone: paste `icon-maskable-512.png` into a 512×512 frame with a centered 410-diameter circle overlay; logo content fits inside the circle | [x] | (screenshot or [maskable.app](https://maskable.app) result) |
| Bg-baked corner pixel is exactly `#FBF6EC` (251, 246, 236) | [x] | DevTools eyedropper or `python3 -c "from PIL import Image; print(Image.open('…').getpixel((0,0)))"` |
| All four files commit byte-identical when generator is re-run | [x] | `git diff --stat src/control_server/static/icons/` after re-run |

---

## 3. E-ink top-strip QR layout (A6) — gates the merge

- Generator: `tools/control-pwa/validate_qr_layout.py`
- Output: `/tmp/litclock-qr-layout-preview.png` (800×480, mode "1")
- QR spec: version 2 (25 modules), error-correction M, 3 px/module, 0 border → 75×75 px output, encodes `https://litclock.local`
- QR position: `x=716, y=2`
- Relocated update-failed glyph: `x=4, y=4` (was `x=784, y=4` in `src/literary_clock.py:166-170`)

| Check | Status | Evidence |
|---|---|---|
| Layout collision check (script) reports no overlaps | [x] | (terminal output of `python3 tools/control-pwa/validate_qr_layout.py`) |
| QR output is exactly 75×75 px | [x] | (script logs `size: (75, 75)`) |
| QR bottom edge (y=77) sits inside the top strip (divider at y=78) | [x] | (visible in preview PNG) |
| **iPhone Camera scan** of preview at ~30 cm decodes to `https://litclock.local` | [x] | (photo path) |
| **Android Camera scan** of preview at ~30 cm decodes to `https://litclock.local` | [x] | (photo path) |
| Update-failed glyph visible at top-left of weather area (x=4, y=4), does not touch the weather icon (x=20, 64×64) | [x] | (visible in preview PNG) |
| Date text "Mon, April, 27" at (250, 10) does not touch QR left edge (x=716) | [x] | (visible in preview PNG; 460-px conservative bbox + 716 = comfortable gap) |
| Horizontal divider at y=78 intact (existing line) | [x] | (visible in preview PNG) |
| Quote area below y=80 untouched (no encroachment) | [x] | (visible in preview PNG) |

---

## 4. Lint + tests (CLAUDE.md pre-commit checklist)

| Check | Status | Evidence |
|---|---|---|
| `ruff check src/ image-gen/ tests/` clean | [x] | (terminal output) |
| `python3 -m pytest tests/ --ignore=tests/test_eink_display.py -q` green | [x] | (terminal output, test count) |

---

## Sign-off

**All gates passed 2026-04-27.**

- Lint + tests: `ruff` clean across `src/ image-gen/ tests/ tools/control-pwa/`; pytest 725 passed / 11 skipped (CLAUDE.md scope).
- Generator idempotency: `python3 tools/control-pwa/generate_pwa_icons.py --check` exits 0 (no drift on rebuild).
- Layout collision check: `python3 tools/control-pwa/validate_qr_layout.py` reports no overlaps among QR / glyph / weather / date.
- QR generates at exactly 75×75 px; preview written to `/tmp/litclock-qr-layout-preview.png`.
- Bg-baked + maskable corner pixels = `(251, 246, 236, 255)` = `#FBF6EC`.
- Standard 192/512 corner pixels = `(0, 0, 0, 0)` (transparent).
- `paper-grain.svg` parses as well-formed XML (codex caught and flagged unescaped markup in `<desc>` in the first draft; fixed in `07e46b46`).
- **QR scan: passed** from real phone camera at ~30 cm — decodes `https://litclock.local`.
- **Maskable safe zone: passed** under all standard adaptive-icon shapes (square / circle / squircle / teardrop). Logo content fits inside the 80% safe-zone circle on a solid `--bg` canvas.

Ready to land. The next PR (M1) creates the Flask control server skeleton, fluid type tokens via `clamp()`, and `src/config.py`. M2 is when the assets validated here actually wire into runtime.
