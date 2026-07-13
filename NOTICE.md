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
| [JohannesNE/literature-clock](https://github.com/JohannesNE/literature-clock) | [CC BY-NC-SA 2.5](https://creativecommons.org/licenses/by-nc-sa/2.5/) |
| [cdmoro/literature-clock](https://github.com/cdmoro/literature-clock) | [MIT](https://opensource.org/licenses/MIT) |
| [arthurgassner/timeteller](https://github.com/arthurgassner/timeteller) | No explicit license (repo); case design CC BY via [Printables](https://www.printables.com/model/1398618-timeteller-a-literature-clock) — see "3D-Printed Case" below |

Because the quote database includes material licensed under **Creative Commons
Attribution-NonCommercial-ShareAlike 2.5 Generic (CC BY-NC-SA 2.5)**, the
assembled database and derived images are subject to the following terms:

- **Attribution** — You must give appropriate credit to the original authors.
- **NonCommercial** — You may not use the material for commercial purposes.
- **ShareAlike** — If you remix, transform, or build upon the material, you must
  distribute your contributions under the same license.

Full license text: https://creativecommons.org/licenses/by-nc-sa/2.5/legalcode

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

### E-ink display (image-gen/`fonts/`)

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

## 3D-Printed Case (design + modified STLs)

The case design is Arthur Gassner's
[Time Teller](https://github.com/arthurgassner/timeteller) project
([project site](https://timeteller.arthurgassner.com)), licensed
**Creative Commons Attribution (CC BY)** per the author's
[Printables listing](https://www.printables.com/model/1398618-timeteller-a-literature-clock)
(also published under CC BY-SA 4.0 on
[Thingiverse](https://www.thingiverse.com/thing:7130877); the GitHub repository
itself carries no license file — this project relies on the Printables CC BY grant).

The STL files in `3d-models/` are **derivatives of Time Teller v3** by
Ankush Kapoor, redistributed under the same CC BY terms:

- `top-back-with-notch.stl` — modified: adds a notch to the top-back part
- `bottom-with-notch.stl` — modified: adds a notch to the bottom part
- `top-front.stl` — unmodified v3 part, included for one-stop printing

Original files (STL + SolveSpace source) remain available from the author on
[GitHub](https://github.com/arthurgassner/timeteller/tree/main/3d-models),
[Thingiverse](https://www.thingiverse.com/thing:7130877),
[Printables](https://www.printables.com/model/1398618-timeteller-a-literature-clock), and
[MakerWorld](https://makerworld.com/en/models/1744549-timeteller-telling-the-time-through-quotes).

The hardware assembly guide (`docs/hardware-assembly.md`) also reproduces case
design details and assembly instructions from the Time Teller project, with
attribution to the original author.