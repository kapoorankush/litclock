# Creating SD Cards for Friends & Family

> **Tip:** The easiest way to create SD cards is to download the pre-built image from [Releases](https://github.com/kapoorankush/litclock/releases/latest) and flash it directly. The steps below are only needed if you want to clone a customized setup.

If you want to make pre-configured SD cards from a working clock:

## 1. Set Up a Working Clock First

Complete the full installation on one Pi using [Option 2 (DIY Installation)](../README.md#2-flash-the-sd-card). Verify everything works.

## 2. Prepare for Cloning

```bash
sudo ./scripts/prepare-for-cloning.sh
```

This script will:
- Remove the setup-complete flag
- Clear your API key and location
- Optionally clear WiFi credentials
- Re-enable the first-boot setup service
- Clear logs and caches

## 3. Clone the SD Card

Shut down the Pi and remove the SD card.

**On Windows:**
- Use [Win32 Disk Imager](https://win32diskimager.org/) to read the SD card to an `.img` file
- Use Raspberry Pi Imager or [balenaEtcher](https://etcher.balena.io/) to write the image to new SD cards

**On Linux/Mac:**
```bash
# Read from SD card (find device with lsblk)
sudo dd if=/dev/sdX of=litclock.img bs=4M status=progress

# Write to new SD card
sudo dd if=litclock.img of=/dev/sdX bs=4M status=progress
```

## 4. Give to Recipient

When they insert the cloned card and power on:
1. Display shows "Welcome!"
2. If no WiFi: they connect phone to "LitClock-Setup" hotspot
3. Display shows QR code to scan
4. They fill out the form with their location and API key
5. Clock starts!
