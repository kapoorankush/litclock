# Hardware Assembly

## Parts List

| Part | Notes | Approx. Cost |
|------|-------|--------------|
| [Raspberry Pi Zero 2 W](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) | Pre-soldered header recommended (Zero 2 WH) | $15 |
| [Waveshare 7.5" e-Paper HAT (V2)](https://www.waveshare.com/7.5inch-e-paper-hat.htm) | 800x480, black/white — [buy on Amazon](https://www.amazon.com/dp/B075R4QY3L) | $60 |
| microSDHC card | 32 GB recommended | $10 |
| USB-C power cable | 5V/2A minimum | $10 |
| 3D-printed case *(optional)* | See [case section](#3d-printed-case) below | $5-30 |
| M2.5 threaded inserts + screws *(case only)* | For securing the case | $3 |
| Female USB-C to male Micro USB adapter *(case only)* | Mounts on case back for cleaner power input | $5 |

## Assembly

1. **Flash the SD card** with Raspberry Pi OS using [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. **Connect the HAT** to the Pi's 40-pin GPIO header — align pin 1 and press firmly
3. **Connect the e-Paper display** to the HAT via the flat ribbon cable — lift the connector latch, slide the cable in (contacts facing down), and close the latch
4. **Insert the SD card** into the Pi
5. **Enable SPI** — after first boot, run `sudo raspi-config` → Interface Options → SPI → Enable (the installer does this automatically if you use `install.sh`)
6. **Power on** via Micro USB (or USB-C if using the [3D-printed case](#3d-printed-case) adapter) — the display should show the boot splash within ~30 seconds

## E-ink Display Notes

- **Full refresh** takes ~4 seconds and briefly flashes black/white — this is normal and prevents ghosting
- **No backlight** — e-ink is reflective like paper, excellent in daylight but not readable in the dark
- The display retains its image with no power, so the last quote stays visible if the Pi loses power
- Operating temperature: 0-50°C — avoid direct sunlight and extreme cold

For troubleshooting display issues, see the [Waveshare wiki](https://www.waveshare.com/wiki/7.5inch_e-Paper_HAT) and [demo repository](https://github.com/waveshare/e-Paper).

## 3D-Printed Case

The case design comes from Arthur Gassner's [Time Teller](https://github.com/arthurgassner/timeteller) project (CC BY) — a literary clock built on the same hardware (RPi Zero 2W + Waveshare 7.5" e-Paper). The design is fully compatible with LitClock.

**Print-ready STLs ship in this repository** — [`3d-models/`](../3d-models/) holds LitClock's lightly modified v3 parts (notches added to the top-back and bottom pieces; top-front unmodified). Print those three and skip the downloads below.

### Original design downloads

The unmodified STL and SolveSpace source files are available from the original author:

- [GitHub](https://github.com/arthurgassner/timeteller/tree/main/3d-models) (STL + SolveSpace source)
- [Thingiverse](https://www.thingiverse.com/thing:7130877)
- [Printables](https://www.printables.com/model/1398618-timeteller-a-literature-clock)
- [MakerWorld](https://makerworld.com/en/models/1744549-timeteller-telling-the-time-through-quotes)

### Design Details

- **Software**: [SolveSpace](https://solvespace.com/) (open-source parametric CAD, runs on Linux)
- **Material**: PLA
- **Versions**: 3 design iterations (v1, v2, v3) — LitClock's `3d-models/` files derive from v3
- **Parts**: 3 pieces — `top-front.stl`, `top-back-with-notch.stl`, `bottom-with-notch.stl`
- **Cost**: ~5 CHF if you print at home, ~30 CHF via a print-on-demand service

### Case Assembly

1. **Install threaded inserts** into each printed part using a soldering iron — press the insert in while the iron heats the surrounding plastic
2. **Glue the two front parts** together (top-front + bottom) with super glue or PLA-compatible adhesive
3. **Mount the USB-C adapter** on the back part — this replaces the Pi's Micro USB port with a more common USB-C connector on the outside of the case
4. **Secure the back** with screws into the threaded inserts

### Preservation Notice

This documentation reproduces information from the [Time Teller project](https://timeteller.arthurgassner.com) by Arthur Gassner. The content is included here so that LitClock builders have a self-contained reference even if the original project site becomes unavailable. All credit belongs to the original author. See [NOTICE.md](../NOTICE.md) for full attribution.
