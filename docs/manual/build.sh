#!/usr/bin/env bash
# Render the LitClock manual (#404) to print-ready booklet PDFs.
#
# Source of truth: litclock-manual.html. We emit TWO PDFs that differ only in
# folded-page size, so a gift-giver can print on whichever paper they have:
#   - litclock-manual-A4-booklet.pdf      pages are A5  -> print "Booklet" on A4
#   - litclock-manual-Letter-booklet.pdf  pages are 1/2 Letter -> "Booklet" on US Letter
# Both are 4 pages = one folded sheet. Print double-sided (flip on short edge),
# choose the Booklet/2-up layout, fold in half.
#
# Needs a Chromium/Chrome for --print-to-pdf (modern @page + woff2 support).
set -euo pipefail
cd "$(dirname "$0")"
trap 'rm -f ._manual-*.html ._manual-*.pdf ._manual-*.log' EXIT

find_chrome() {
  for c in chromium chromium-browser google-chrome google-chrome-stable chrome; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return; }
  done
  # Playwright's bundled chromium (dev machines).
  local pw
  pw=$(find "$HOME/.cache/ms-playwright" -maxdepth 3 -name chrome -type f 2>/dev/null | head -1 || true)
  [ -n "$pw" ] && { echo "$pw"; return; }
  return 1
}

CHROME=$(find_chrome) || { echo "No Chromium/Chrome found for PDF rendering." >&2; exit 1; }
echo "Rendering with: $CHROME"

PAGE_LINE='@page { size: A5; margin: 0; }'

render() {
  local label="$1" size="$2" out="$3"
  local tmp="._manual-${label}.html" tmppdf="._manual-${label}.pdf" log="._manual-${label}.log"

  # The source must contain exactly one swappable page-size line. Fail LOUD if
  # it was reformatted — otherwise sed would silently no-op and the Letter
  # build would render A5 pages while reporting success (cross-model /review).
  local n
  n=$(grep -cF "$PAGE_LINE" litclock-manual.html || true)
  [ "$n" = "1" ] || { echo "ERROR: expected exactly one '$PAGE_LINE' in litclock-manual.html, found $n. Did the CSS get reformatted?" >&2; exit 1; }

  sed "s|${PAGE_LINE}|@page { size: ${size}; margin: 0; }|" litclock-manual.html > "$tmp"
  grep -qF "@page { size: ${size}; margin: 0; }" "$tmp" \
    || { echo "ERROR: page-size swap to '${size}' did not apply." >&2; exit 1; }

  # Render to a TEMP pdf and validate before touching the committed output, so
  # a failed/partial render can't leave a stale or truncated PDF in the repo.
  if ! "$CHROME" --headless=new --no-sandbox --disable-gpu --no-pdf-header-footer \
      --print-to-pdf="$tmppdf" "file://$(pwd)/$tmp" >"$log" 2>&1; then
    echo "ERROR: Chrome failed to render ${out}:" >&2; cat "$log" >&2; exit 1
  fi
  { [ -s "$tmppdf" ] && [ "$(wc -c < "$tmppdf")" -gt 10000 ]; } \
    || { echo "ERROR: ${out} render is empty or too small — not committing it." >&2; exit 1; }

  # Page-count guard (#404 /review): the booklet is exactly 4 pages = one
  # folded sheet. A content edit that overflows would add a 5th page and
  # silently break the fold/imposition. Assert 4 pages when pdfinfo is
  # available; skip gracefully if it isn't (pdfinfo is not a build dependency).
  if command -v pdfinfo >/dev/null 2>&1; then
    pages=$(pdfinfo "$tmppdf" 2>/dev/null | awk '/^Pages:/{print $2}')
    [ "$pages" = "4" ] \
      || { echo "ERROR: ${out} has ${pages:-?} pages, expected 4 (content overflow?) — not committing it." >&2; exit 1; }
  fi

  mv "$tmppdf" "$out"
  echo "  -> $out"
}

render "a4"     "A5"          "litclock-manual-A4-booklet.pdf"
render "letter" "5.5in 8.5in" "litclock-manual-Letter-booklet.pdf"
echo "Done."
