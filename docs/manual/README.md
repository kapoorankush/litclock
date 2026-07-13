# LitClock printable manual (#404)

A 4-page booklet to enclose when LitClock is given as a gift. Designed for a
non-technical recipient: read in five minutes, no jargon. Visual register
matches the Control PWA / `DESIGN.md` (Fraunces + Instrument Sans, warm paper),
and reads cleanly in black-and-white on a home printer (no colour or bleed
required).

## Files

- `litclock-manual.html` — the source. Edit this.
- `build.sh` — renders the two booklet PDFs below (needs Chromium/Chrome).
- `litclock-manual-A4-booklet.pdf` — pages are A5. Print **Booklet** on **A4**.
- `litclock-manual-Letter-booklet.pdf` — pages are half-Letter. Print **Booklet** on **US Letter**.

Each PDF is 4 pages = one folded sheet.

## How to print (one sheet, folded)

1. Open the PDF that matches your paper (A4 or Letter).
2. Print double-sided (**flip on short edge**), and choose the **Booklet** /
   2-up layout in the print dialog.
3. Fold the sheet in half. Cover is page 1.

Black-and-white is fine. If you print in colour you get the warm paper tones;
nothing depends on it.

## Regenerate after editing

```sh
docs/manual/build.sh
```

**The committed PDFs are generated artifacts — always rerun `build.sh` after
editing the HTML and commit the refreshed PDFs**, or the booklets in the repo
go stale. `build.sh` needs a real Chromium/Chrome on `PATH` (the Playwright
cache is only a dev-machine shortcut); it fails loudly rather than committing a
wrong-sized or partial PDF.

## Notes

- The fonts come from `src/control_server/static/fonts/` (same as the PWA), so
  edits stay visually in sync with the app. They're referenced by a relative
  path, so the HTML must stay at `docs/manual/` (rendered in place by
  `build.sh`); if `src/` is ever reorganized, update the `@font-face` URLs.
- Copy deliberately avoids "IP / network / address" etc. per the `DESIGN.md`
  persona rule, and contains no personal details (it ships with every gift).
- The line-art is intentionally minimal; swap in real device photos later if
  wanted (the `<svg>` blocks in the HTML are the placeholders).
