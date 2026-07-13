# Recovering a LitClock

LitClock ships **hardened by default**: the `pi` account uses the standard
`raspberry` password and **SSH is turned off**. There is no network path to a
shell, which is deliberate — the only way in is physical. This guide covers how
to get a shell, reset the clock, or recover a device that is misbehaving.

If you are a non-technical owner and the clock is simply showing the wrong thing,
start with **[Read-only diagnostics](#read-only-diagnostics-no-shell-needed)** —
you probably do not need any of the shell steps below.

---

## Getting a shell (console)

The clock has no keyboard or screen of its own, so "console" means plugging the
Pi into a monitor (micro-HDMI) and a USB keyboard.

1. Power off, connect a monitor + keyboard, power on.
2. Log in at the prompt: user `pi`, password `raspberry`.
3. You now have a shell. `sudo` works with the same password if prompted.

To enable SSH for the rest of a session (openssh-server is installed, just
disabled — this works with no network):

```bash
sudo systemctl enable --now ssh      # or: sudo raspi-config  → Interface Options → SSH
```

SSH stays enabled until you turn it back off (`sudo systemctl disable --now ssh`)
or reflash. It is off on every fresh image; enabling it is always a deliberate,
physically-present act.

---

## Enabling SSH without a keyboard (SD card)

If you cannot attach a keyboard (e.g. the Pi is framed) but you can reach the SD
card, enable SSH by editing the boot partition on another computer:

1. Power off, remove the SD card, insert it into your laptop. The small FAT
   partition (`bootfs` / `boot`) mounts on any OS.
2. Create an empty file named `ssh` (no extension) in that partition.
3. Eject, reinsert into the Pi, power on. SSH is enabled on next boot; log in
   with `pi` / `raspberry` over the network.

This needs physical access to the card and is the recommended path when a screen
isn't practical. Turn SSH back off afterward if you don't want it standing.

---

## Reset the clock (back to first-boot setup)

From a shell (console or SSH), re-run the setup flow — this clears location,
weather, and setup markers and drops the clock back into the WiFi-setup captive
portal on next boot:

```bash
sudo /home/pi/litclock/scripts/reset-setup.sh          # keeps WiFi
sudo /home/pi/litclock/scripts/reset-setup.sh --wipe-wifi --reboot   # full fresh-start
```

Use `--gift-mode` to prepare a device for shipping to someone else (wipes WiFi +
config, writes a welcome splash, powers off). See
[SD Card Cloning](sd-card-cloning.md) for duplicating a configured card.

---

## Read-only diagnostics (no shell needed)

The Control PWA has a **Diagnostics** tab that shows version, last render time,
WiFi + weather status, error flags, and recent log tails — everything a helper
needs to triage, with nothing to type and no shell. Open the PWA
(`http://litclock.local` or the IP shown on the e-ink handoff splash) and screenshot
the Diagnostics tab. This is the first thing to check when a clock misbehaves, and
the intended way to get help without SSH.

---

## Last resort: reflash or replace the card

If the software is wedged past reset (a bad state, corrupted SD), the clean fix is
to reflash:

1. Download the latest image and flash it (Raspberry Pi Imager / Etcher).
2. Boot; the clock re-runs first-boot and reprovisions over WiFi.

Config lives on the card, so reflashing is a true clean slate. For a gift
recipient who can't do this, shipping a freshly-flashed replacement card is the
simplest recovery of all.
