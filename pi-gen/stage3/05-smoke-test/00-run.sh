#!/bin/bash
# In-chroot smoke test (#114, PR #202).
#
# Runs inside the pi-gen chroot jail via qemu-user-static binfmt. Because we're
# in the target's own filesystem namespace, symlink chains resolve natively and
# Python / systemd-analyze behave the way they will on the real Pi. Catches
# stage3 regressions (broken pip install, deleted files in 04-finalize, unit
# syntax errors, missing runtime files, Python dep import failure).
#
# A failure here fails pi-gen stage3, so no .img is ever produced from a broken
# rootfs. Post-export (loop-mount) smoke testing was tried and removed — see
# PR #202 and issue #114 for context.
set -e

on_chroot << 'CHROOT'
set -e

echo "=== In-chroot smoke test ==="

echo "-- Required files --"
required_files=(
    /home/pi/litclock/venv/bin/python3
    /home/pi/litclock/venv/pyvenv.cfg
    /home/pi/litclock/src/literary_clock.py
    /home/pi/litclock/src/setup_server.py
    /home/pi/litclock/src/eink_display.py
    /home/pi/litclock/scripts/runtheclock.sh
    /home/pi/litclock/scripts/first-boot.sh
    /home/pi/litclock/scripts/boot-splash.sh
    /home/pi/litclock/requirements.txt
    /etc/systemd/system/litclock.service
    /etc/systemd/system/litclock.timer
    /etc/systemd/system/litclock-splash.service
    /etc/systemd/system/litclock-firstboot.service
    /etc/systemd/system/litclock-shutdown.service
    /etc/systemd/system/wifi-watchdog.service
    /etc/systemd/system/wifi-watchdog.timer
    /usr/local/bin/wifi-watchdog.sh
)
for f in "${required_files[@]}"; do
    if [ ! -e "$f" ]; then
        echo "FAIL: $f missing"
        exit 1
    fi
done
echo "  OK: ${#required_files[@]} required files present"

echo "-- Systemd enable symlinks --"
# These are enabled at build time (stage3/03-install-services).
# litclock.timer is NOT — first-boot.sh enables it after setup.
enabled_units=(
    /etc/systemd/system/multi-user.target.wants/litclock-splash.service
    /etc/systemd/system/multi-user.target.wants/litclock-firstboot.service
    /etc/systemd/system/multi-user.target.wants/litclock-shutdown.service
    /etc/systemd/system/timers.target.wants/wifi-watchdog.timer
)
for link in "${enabled_units[@]}"; do
    if [ ! -L "$link" ] && [ ! -e "$link" ]; then
        echo "FAIL: expected enable symlink $link missing"
        exit 1
    fi
done
if [ -e /etc/systemd/system/timers.target.wants/litclock.timer ]; then
    echo "FAIL: litclock.timer is enabled at build time (must be deferred to first-boot.sh)"
    exit 1
fi
echo "  OK: expected units enabled, litclock.timer correctly deferred"

echo "-- Automatic OS updates disabled (appliance) --"
# 02-configure-system masks the apt-daily timers and zeroes the periodic knobs
# so a fielded/gift clock never auto-upgrades OS packages behind the owner.
# This is OS-only; litclock-update.timer (LitClock's own updater) stays enabled.
for t in apt-daily.timer apt-daily-upgrade.timer; do
    if [ "$(readlink -f "/etc/systemd/system/$t" 2>/dev/null)" != /dev/null ]; then
        echo "FAIL: $t is not masked — OS auto-updates could run behind the owner"
        exit 1
    fi
done
# Check the EFFECTIVE merged apt config (apt-config dump reflects all of
# apt.conf.d), so a later drop-in that re-enables a knob is caught — not just
# our own 20auto-upgrades file. All three periodic knobs must resolve to "0".
apt_periodic="$(apt-config dump 2>/dev/null)"
for knob in Update-Package-Lists Download-Upgradeable-Packages Unattended-Upgrade; do
    if ! printf '%s\n' "$apt_periodic" | grep -q "APT::Periodic::$knob \"0\";"; then
        echo "FAIL: APT::Periodic::$knob is not effectively \"0\" — OS auto-updates could run"
        exit 1
    fi
done
echo "  OK: apt-daily timers masked + all APT::Periodic knobs effectively zeroed"

echo "-- Systemd unit syntax --"
# Running inside the target's own namespace — no --root quirks, no
# cross-namespace symlink issues. Still tolerant of benign warnings.
fatal_patterns='Failed to parse|is not a valid unit name|Bad unit file setting|Unknown lvalue|Unknown section|Assignment outside of section'
for unit in litclock.service litclock.timer litclock-splash.service \
            litclock-firstboot.service litclock-shutdown.service \
            wifi-watchdog.service wifi-watchdog.timer; do
    echo "  checking $unit"
    if output=$(systemd-analyze verify "$unit" 2>&1); then
        :
    else
        echo "$output" | sed 's/^/    /'
        if echo "$output" | grep -qiE "$fatal_patterns"; then
            echo "FAIL: $unit has real errors"
            exit 1
        fi
        echo "    (warnings only — not fatal)"
    fi
done
echo "  OK: all unit files pass"

echo "-- Quote image corpus --"
# Quote images are fetched from a GitHub Release during the build
# (.github/workflows/build-image.yml calls scripts/download_images.sh). Verify
# the corpus actually landed in the image. The build workflow already has a
# count floor, but a chroot-side check catches any regression where the
# workflow step runs but stage3 doesn't see the files (e.g., cp path drift).
image_count=$(find /home/pi/litclock/images -maxdepth 2 -name '*.png' 2>/dev/null | wc -l)
if [ "$image_count" -lt 8000 ]; then
    echo "FAIL: only $image_count quote images under /home/pi/litclock/images (expected >=8000)"
    exit 1
fi
if [ ! -f /home/pi/litclock/images/.installed-version ]; then
    echo "FAIL: /home/pi/litclock/images/.installed-version is missing — image corpus was not staged"
    exit 1
fi
echo "  OK: $image_count quote images present, version $(cat /home/pi/litclock/images/.installed-version)"

echo "-- Python imports (venv, pure-Python deps) --"
# Validates pi-gen's pip install produced a usable venv. Hardware-specific
# imports (waveshare_epd, RPi.GPIO, spidev) are not checked here — they
# require real hardware probing and are verified on real Pis.
/home/pi/litclock/venv/bin/python3 -c '
import astral, PIL, qrcode, requests, pytz, timezonefinder, urllib3, certifi
# Successful import is the pass signal. Version-print is informational and
# must tolerate packages that do not expose __version__ at module level
# (e.g. qrcode).
def v(m): return getattr(m, "__version__", "unknown")
print(f"  OK: astral={v(astral)}, PIL={v(PIL)}, qrcode={v(qrcode)}, "
      f"requests={v(requests)}, pytz={v(pytz)}, tzf={v(timezonefinder)}, "
      f"urllib3={v(urllib3)}, certifi={v(certifi)}")
'

echo "=== In-chroot smoke test PASSED ==="
CHROOT
