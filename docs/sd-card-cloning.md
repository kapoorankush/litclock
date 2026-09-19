# Creating SD Cards for Friends & Family

> **Tip:** The easiest way to create SD cards is to download the pre-built image from [Releases](https://github.com/kapoorankush/litclock/releases/latest) and flash it directly. The steps below are only needed if you want to clone a customized setup.

If you want to make pre-configured SD cards from a working clock:

## 1. Set Up a Working Clock First

Complete the full installation on one Pi by flashing the released image (see [Flash the SD card](../README.md#2-flash-the-sd-card)). Verify everything works.

## 2. Prepare for Cloning

Run this as the **only open session** on the Pi: log out of every other shell
first (SSH sessions, tmux panes, a `sudo -i` root shell). The script clears the
bash history, but any interactive shell still holding history when the Pi
powers off writes it back to disk on exit, and that is what gets imaged onto
every card.

That rule cannot cover **the shell you run it from** — it is alive for the
whole run and writes its own history as it exits. The script covers that one
itself, by locking both history files against write-back until the clone's
first boot, and it now stops rather than continue if it cannot apply the lock.

Running over SSH is otherwise supported; only the WiFi question below requires
a local console.

```bash
sudo ./scripts/prepare-for-cloning.sh
```

This script will (and then power the Pi off):
- Stop the services that write setup state, so nothing re-creates it mid-run
- Remove both setup-state markers, `.setup-complete` and `.handoff-complete`
- Clear your API key and location
- Optionally clear WiFi credentials
- Re-enable the first-boot setup service
- Clear logs and caches
- Clear the bash history, and lock both history files so no shell still open at power-off — including the one you are typing in — can write it back (the clone's first boot unlocks them). The run stops if either path cannot be cleared or locked.
- Clear the SSL certificates
- Delete the persisted setup-hotspot password, so no clone carries your key
- Disable SSH, so clones ship in the same posture as a fresh flash (to get back into a clone: put a blank file named `ssh` in the SD card's boot partition)

If any of those steps cannot finish, the script says so in red and stops rather
than reporting success. Do not clone a card it refused. If the API-key and
location wipe itself fails, it stops right there, before asking about WiFi, and
nothing on the card has been changed at all — the clock still works and still
boots normally. Fix the cause (usually the control PWA still holding
`env.sh.lock`) and run it again; the PWA comes back on the next boot. The one refusal you may
not SEE is the final SSH-disable check when running over the network (output is
cut before it, deliberately) — its tell is a Pi that has not powered itself off
within a minute of your session dropping; do not image that card either, and
the next run of the script will say the previous one did not finish.

Answering "y" to the WiFi question requires a **local console** (monitor/keyboard
or serial). Over SSH the script refuses that answer: deleting the connections
would drop your own session and kill the script before it removes the
setup-hotspot key — the half-prepared card would look exactly like a finished
one. If a run ever dies part-way for any reason, the next run tells you so
before you confirm; do not clone until a run completes.

## 3. Clone the SD Card

The script powers the Pi off itself when it finishes (litclock-dev#660), so just wait for the activity LED to stop and remove the SD card.

**Do not power the card on again before you image it.** A single boot re-creates the setup-WiFi password the script just removed, and every clone taken afterwards would share it. If you need to inspect the prepared card, re-run the script with `--no-poweroff`.

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
2. If no WiFi: they join the "LitClock-Setup" network from their phone
3. Display shows QR code to scan
4. They fill out the form with their location and API key
5. Clock starts!
