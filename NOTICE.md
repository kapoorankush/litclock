# Third-Party Notices

This project incorporates material from the projects listed below. The original
copyright notices and license terms are included here.

---

## Origin

This project was originally forked from
[jadonn/literary-clock](https://github.com/jadonn/literary-clock) and has since
been extensively rewritten. The original project did not include an explicit
license.

The literary clock concept originates from
[Jaap Meijers's Instructables project](https://www.instructables.com/Literary-Clock-Made-From-E-reader/)
(2018). The PHP image generation script (`image-gen/quote_to_image.php`) is
derived from his work.

---

## Quote Database

The quote database (`image-gen/litclock_annotated.csv`) and pre-generated quote
images (`images/`) incorporate data from the following sources:

| Source | License |
|--------|---------|
| JohannesNE/literature-clock (now [JohsEnevoldsen/literature-clock](https://github.com/JohsEnevoldsen/literature-clock)) | [CC BY-NC-SA 2.5](https://creativecommons.org/licenses/by-nc-sa/2.5/) |
| [cdmoro/literature-clock](https://github.com/cdmoro/literature-clock) | [MIT](https://opensource.org/licenses/MIT) |
| [The Guardian "Books blog" reader thread](https://www.theguardian.com/books/booksblog/2011/apr/21/literary-clock) | No explicit license — community-sourced reader comments, quoted with attribution |

`image-gen/gather_quotes.py` also fetched
[arthurgassner/timeteller](https://github.com/arthurgassner/timeteller), whose
quote file is **byte-identical** to the JohannesNE database above (verified by
checksum). It is a mirror, not an independent source: every row it supplied is
already covered by the first row of this table. That project's own contribution
to LitClock is the case design, credited under "3D-Printed Case" below.

Because the quote database includes material licensed under **Creative Commons
Attribution-NonCommercial-ShareAlike 2.5 Generic (CC BY-NC-SA 2.5)**, the
assembled database and derived images are subject to the following terms:

- **Attribution** — You must give appropriate credit to the original authors.
- **NonCommercial** — You may not use the material for commercial purposes.
- **ShareAlike** — If you remix, transform, or build upon the material, you must
  distribute your contributions under the same license.

### Version: distributed under CC BY-NC-SA 4.0

`image-gen/litclock_annotated.csv` is a **derivative work** of the
JohannesNE/literature-clock database (extended with additional quotes and
re-annotated for mature content). Because the ShareAlike condition on that
CC-licensed portion governs the assembled database as a whole — including
material folded in from the other sources in the table above — this project
distributes the derivative under
**[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)**, as
expressly permitted by section 4(b) of the 2.5 license (emphasis added):

> You may distribute, publicly display, publicly perform, or publicly digitally
> perform a Derivative Work only under the terms of this License, **a later
> version of this License with the same License Elements as this License**, or a
> Creative Commons iCommons license that contains the same License Elements as
> this License […]

The same terms cover every corpus-derived artifact: the tooling sample
`image-gen/gold_set_192.csv`, the generated images (`images/`, shipped in
quote-image releases), and the rendered examples committed to the repository
(`example.png`, `docs/media/litclock-intro.gif`).

CC BY-NC-SA 4.0 carries the same three License Elements (Attribution,
NonCommercial, ShareAlike), so the obligations above are unchanged. Version 4.0
is used because it, unlike 2.5:

- explicitly licenses **sui generis database rights** (2.5 does not mention
  databases at all) — the relevant right for a curated quote compilation in the
  EU and UK;
- reinstates a terminated license automatically if the violation is **cured
  within 30 days** (under 2.5, a breach terminates the license automatically,
  with no automatic reinstatement);
- is a single international license rather than a per-jurisdiction port.

Upstream's original database remains available under CC BY-NC-SA 2.5 from
[JohannesNE/literature-clock](https://github.com/JohannesNE/literature-clock);
this re-versioning applies only to this project's derivative.

Full license text: https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode
Upstream (2.5) license text: https://creativecommons.org/licenses/by-nc-sa/2.5/legalcode

---

## Weather Icons

The weather icons in `icons/` are from
[Dhole/weather-pixel-icons](https://github.com/Dhole/weather-pixel-icons),
licensed under **Creative Commons Attribution-ShareAlike 4.0 International
(CC BY-SA 4.0)**.

- **Attribution** — You must give appropriate credit.
- **ShareAlike** — If you remix, transform, or build upon the material, you must
  distribute your contributions under the same license.

Full license text: https://creativecommons.org/licenses/by-sa/4.0/legalcode

---

## Fonts

### E-ink display (`fonts/`)

The Literata font files in `fonts/` are from
[Google Fonts](https://fonts.google.com/specimen/Literata), licensed under the
**SIL Open Font License 1.1 (OFL-1.1)**.

### Control PWA (`src/control_server/static/fonts/`)

The Control PWA self-hosts variable woff2 fonts fetched from Fontsource via
`tools/control-pwa/fetch_fonts.py` (pinned versions + SHA256 verified). All
three families ship under the **SIL Open Font License 1.1 (OFL-1.1)**.

| Family | Source | Files |
|--------|--------|-------|
| [Fraunces](https://fonts.google.com/specimen/Fraunces) — variable wght axis | [Fontsource](https://fontsource.org/fonts/fraunces) | `fraunces-wght-normal.woff2`, `fraunces-wght-italic.woff2` |
| [Instrument Sans](https://fonts.google.com/specimen/Instrument+Sans) — variable wght axis | [Fontsource](https://fontsource.org/fonts/instrument-sans) | `instrument-sans-wght-normal.woff2` |
| [Geist Mono](https://fonts.google.com/specimen/Geist+Mono) — variable wght axis | [Fontsource](https://fontsource.org/fonts/geist-mono) | `geist-mono-wght-normal.woff2` |

Full license text: https://openfontlicense.org/

---

## Code Inspirations

Portions of the display and weather code were inspired by:

| Project | License |
|---------|---------|
| [mendhak/waveshare-epaper-display](https://github.com/mendhak/waveshare-epaper-display) | [MIT](https://opensource.org/licenses/MIT) |
| [Jake Krajewski's e-Paper tutorial](https://medium.com/swlh/create-an-e-paper-display-for-your-raspberry-pi-with-python-2b0de7c8820c) | N/A |

---

## 3D-Printed Case (design + case print files)

The case design is Arthur Gassner's
[Time Teller](https://github.com/arthurgassner/timeteller) project
([project site](https://timeteller.arthurgassner.com)), licensed
**Creative Commons Attribution (CC BY)** per the author's
[Printables listing](https://www.printables.com/model/1398618-timeteller-a-literature-clock)
(also published under CC BY-SA 4.0 on
[Thingiverse](https://www.thingiverse.com/thing:7130877); the GitHub repository
itself carries no license file — this project relies on the Printables CC BY grant).

The files in `3d-models/` redistribute and build on the Time Teller case
under that work-level CC BY grant (a Creative Commons license attaches to
the WORK and is non-exclusive and irrevocable — the author's MakerWorld
listing carrying a CC BY-NC mark does not narrow the CC BY grant he made
on Printables for the same design; LitClock's copies rely on the
Printables grant regardless of which channel a mesh was downloaded
through):

- `RPIEnclosure.stl` — modified by Ankush Kapoor: bottom enclosure with an
  SD-card notch
- `RPIEnclosureCover_microUSB_Power.stl` — modified by Ankush Kapoor: back
  cover reworked for direct micro-USB power (replaces the original's
  USB-C-adapter mount) plus the notch
- `ScreenFrame.stl` — the author's UNMODIFIED front part, included for
  one-stop printing (re-exported through Bambu Studio, which does not
  preserve source-channel watermarks; the grant above is what licenses it)
- `LitClock_microUSB_Power.3mf` — print project by Ankush Kapoor arranging
  the three parts above

The author's own project files (his editable sources and his packaged
exports) are not mirrored here — LitClock links to his channels below for
those.

Original files (STL + SolveSpace source) remain available from the author on
[GitHub](https://github.com/arthurgassner/timeteller/tree/main/3d-models),
[Thingiverse](https://www.thingiverse.com/thing:7130877),
[Printables](https://www.printables.com/model/1398618-timeteller-a-literature-clock), and
[MakerWorld](https://makerworld.com/en/models/1744549-timeteller-telling-the-time-through-quotes).

The hardware assembly guide (`docs/hardware-assembly.md`) also reproduces case
design details and assembly instructions from the Time Teller project, with
attribution to the original author.