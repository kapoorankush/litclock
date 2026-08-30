# Hardware Assembly

## Parts List

| Part | Notes | Approx. Cost |
|------|-------|--------------|
| [Raspberry Pi Zero 2 W](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) | Must be a Zero 2 W (see below). Pre-soldered header (Zero 2 **WH**) is easiest; otherwise solder your own or fit a solderless **hammer header** | $15 list |
| [Waveshare 7.5" e-Paper HAT (V2)](https://www.waveshare.com/7.5inch-e-paper-hat.htm) | **V2 only** (see below) — 800x480, black/white — [buy on Amazon](https://www.amazon.com/dp/B075R4QY3L) | $60 |
| microSDHC card | 32 GB recommended | $10 |
| Micro-USB power cable | 5V/2A minimum | $10 |
| 3D-printed case *(optional)* | See [case section](#3d-printed-case) below | $5-30 |
| M2.5 threaded inserts + screws *(case only)* | For securing the case | $3 |

### Before you buy the Pi

**The Zero 2 W is the only board LitClock is tested on.** The image, the display code, the GPIO wiring and the case are all built and verified against that single board. No other Raspberry Pi is supported, and none has been tried — the case is cut for the Zero 2 W's 65x30mm footprint, and nothing in the e-ink path has been validated on other hardware. A larger 64-bit Pi (3/4/5) will probably boot the image, but that is unverified territory and you would be debugging it yourself.

**A 32-bit Pi will not work at all.** The image is 64-bit (arm64), so an original Pi Zero W or a Pi 1/2 (ARMv6/ARMv7) shows no boot activity whatsoever — no display, no WiFi, nothing on the network, and no error to read. If a freshly flashed card does absolutely nothing, check which board you have before anything else.

**On price and stock:** $15 is the list price, but as of July 2026 the Zero 2 W is frequently out of stock and often resold well above list. Check the authorized resellers first; marketplace listings at several times list are common.

### Before you buy the display

**It must be the Waveshare 7.5 inch e-Paper HAT (V2), 800x480, black/white.** That is the only panel LitClock is tested on; the clock drives it with Waveshare's `epd7in5_V2` driver and the whole layout is built for exactly 800x480.

To quote when ordering:

| | |
|---|---|
| Waveshare SKU | **13504** |
| Amazon ASIN | **B075R4QY3L** |
| Waveshare product page | [7.5inch e-Paper HAT](https://www.waveshare.com/7.5inch-e-paper-hat.htm) |
| Decisive specs | 800x480 resolution, two colours (black/white), SPI |

Waveshare has historically revised these panels while keeping the same SKU, so treat the SKU as a starting point and the **specs** as the real check: if a listing does not say 800x480 and black/white, it is not the right panel whatever it is called.

Waveshare sells several 7.5 inch panels under confusingly similar names, and they are **not** interchangeable:

| Panel | Resolution | Works? |
|-------|-----------|--------|
| 7.5" e-Paper HAT **V2** | 800x480, black/white | **Yes — this is the one** |
| 7.5" e-Paper HAT V1 | 640x384, black/white | No — wrong resolution |
| 7.5" e-Paper HAT **HD** | 880x528, black/white | No — wrong resolution |
| 7.5" e-Paper HAT **B** / **C** | tri-colour (black/white/red or yellow) | No — different driver and refresh model |

A mismatched panel does not degrade gracefully into a smaller or oddly-coloured clock; it will not work. If you already own a different Waveshare panel, treat adapting it as your own project — nothing here has been tested against one.

**Getting the 40-pin header on**, easiest first:

- **Buy the Zero 2 WH** — the header is already soldered on. No tools, nothing to go wrong.
- **Solder your own** — a standard 2x20 male header, forty joints.
- **Solderless hammer header** — press-fit pins you tap in with the small acrylic installation jig that comes with them. No iron, no solder; you seat the Pi in the jig and tap the header home with a few light hammer taps. Handy if you would rather not solder, and it comes apart again if you need the Pi for something else.

## Assembly

1. **Flash the SD card** with the latest LitClock image using [Raspberry Pi Imager](https://www.raspberrypi.com/software/) — see [Flash the SD card](../README.md#2-flash-the-sd-card)
2. **Connect the HAT** to the Pi's 40-pin GPIO header — align pin 1 and press firmly
3. **Connect the e-Paper display** to the HAT via the flat ribbon cable — lift the connector latch, slide the cable in (contacts facing down), and close the latch
4. **Insert the SD card** into the Pi
5. **Power on** via Micro USB — the display should show the boot splash within ~30 seconds

SPI is already enabled by the image, so there is nothing to configure. If the display stays blank after a couple of minutes, reseat the ribbon cable and the HAT first — that is the usual cause. If it is still blank you need a shell to go further: [Recovering a LitClock](recovery.md) covers getting console access, and from there `ls /dev/spidev0.0` confirms whether SPI came up.

## E-ink Display Notes

- **Full refresh** takes ~4 seconds and briefly flashes black/white — this is normal and prevents ghosting
- **No backlight** — e-ink is reflective like paper, excellent in daylight but not readable in the dark
- The display retains its image with no power, so the last quote stays visible if the Pi loses power
- Operating temperature: 0-50°C — avoid direct sunlight and extreme cold

For troubleshooting display issues, see the [Waveshare wiki](https://www.waveshare.com/wiki/7.5inch_e-Paper_HAT) and [demo repository](https://github.com/waveshare/e-Paper).

## 3D-Printed Case

The case design comes from Arthur Gassner's [Time Teller](https://github.com/arthurgassner/timeteller) project (CC BY) — a literary clock built on the same hardware (RPi Zero 2W + Waveshare 7.5" e-Paper). The design is fully compatible with LitClock.

**Print-ready files ship in this repository** — [`3d-models/`](../3d-models/) holds LitClock's micro-USB power edition of the case: the back cover exposes the Pi's own micro-USB port directly (no adapter), and the SD-card notches are incorporated. Print the three STLs — or open `LitClock_microUSB_Power.3mf`, a ready-to-slice Bambu Studio project with all parts arranged — and skip the downloads below.

### Original design downloads

The unmodified STL and SolveSpace source files are available from the original author:

- [GitHub](https://github.com/arthurgassner/timeteller/tree/main/3d-models) (STL + SolveSpace source)
- [Thingiverse](https://www.thingiverse.com/thing:7130877)
- [Printables](https://www.printables.com/model/1398618-timeteller-a-literature-clock)
- [MakerWorld](https://makerworld.com/en/models/1744549-timeteller-telling-the-time-through-quotes)

### Design Details

- **Software**: [SolveSpace](https://solvespace.com/) (open-source parametric CAD, runs on Linux) — the author's original design tool; LitClock's modified parts and the print project were produced in Bambu Studio
- **Material**: PLA
- **Versions**: 3 design iterations (v1, v2, v3) — LitClock's `3d-models/` files derive from the Time Teller case
- **Parts**: 3 pieces — `ScreenFrame.stl` (front), `RPIEnclosure.stl` (bottom), `RPIEnclosureCover_microUSB_Power.stl` (back cover)
- **Cost**: ~5 CHF if you print at home, ~30 CHF via a print-on-demand service

### Case Assembly

1. **Install threaded inserts** into each printed part using a soldering iron — press the insert in while the iron heats the surrounding plastic
2. **Glue the two front parts** together (`ScreenFrame` + `RPIEnclosure`) with super glue or PLA-compatible adhesive
3. **Route the power cable** — the back cover's opening lines up with the Pi's own micro-USB power port; no adapter is needed. (The original Time Teller design instead mounts a female-USB-C-to-micro-USB adapter on the back — if you prefer a USB-C outside connector, print the author's unmodified parts from the links above and add the $5 adapter.)
4. **Secure the back** with screws into the threaded inserts

### Preservation Notice

This documentation reproduces information from the [Time Teller project](https://timeteller.arthurgassner.com) by Arthur Gassner. The content is included here so that LitClock builders have a self-contained reference even if the original project site becomes unavailable. All credit belongs to the original author. See [NOTICE.md](../NOTICE.md) for full attribution.
