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
# Non-unit runtime files stay an explicit curated list: their absence is not
# derivable from anywhere else in the tree. Systemd units are NOT listed here
# any more — see the derived check below.
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
    /usr/local/bin/wifi-watchdog.sh
    # Load-bearing units, asserted by ABSOLUTE PATH on purpose. The derived
    # check below reads the same source tree that 03-install-services copies
    # from, so if that tree is wrong both gates agree and both pass. These
    # three do not depend on the source tree at all, so the two checks fail on
    # different causes: the derived one catches drift, this one catches source
    # corruption.
    #
    # Why these three: only 10 of the 20 units are named in a `systemctl enable`
    # line, which is what makes a missing file abort stage 03. The other 10 have
    # no such backstop, and litclock.service + litclock.timer are the ones whose
    # absence is silent AND fatal — first-boot.sh guards the timer with
    # `if systemctl list-unit-files | grep -q litclock.timer`, a soft if, so a
    # missing timer is skipped with no error and no journal line. The clock
    # finishes setup, shows the handoff splash, and never paints a quote, on a
    # device with no keyboard attached.
    #
    # wifi-watchdog.service is the third for a different reason: it carries no
    # [Install] section, so no `systemctl enable` line names it and the derived
    # enable check below skips it. wifi-watchdog.timer's Unit= reference to it is
    # resolved by systemd at RUNTIME, so its absence is invisible to every
    # build-time check and surfaces only as a timer that fails on the device.
    /etc/systemd/system/litclock.service
    /etc/systemd/system/litclock.timer
    /etc/systemd/system/wifi-watchdog.service
    # Same independent-backstop role, for the tmpfiles drop-in. It creates
    # /run/litclock and /var/lib/litclock. When it is absent the root/sudo
    # writers survive (they mkdir their own parent — see the note in
    # 03-install-services), but the PI-OWNED /run writers cannot: the render
    # heartbeat in src/literary_clock.py and the weather cache in
    # src/weather_providers/base_provider.py both fail, and /var/lib/litclock
    # loses its ownership normalisation. There is no journal line for a
    # directory that was never asked for.
    /etc/tmpfiles.d/litclock.conf
)
for f in "${required_files[@]}"; do
    if [ ! -e "$f" ]; then
        echo "FAIL: $f missing"
        exit 1
    fi
done
echo "  OK: ${#required_files[@]} required runtime files present"

echo "-- Installed systemd units (derived from the source tree) --"
# DERIVED, not enumerated. The previous hardcoded list named only a third of the
# units in systemd/, so this smoke test — the last gate before an image is produced —
# passed green while litclock-reresolve-location.service reached no flashed
# image at all (litclock-dev#547).
#
# Comparing the installed set against the SOURCE tree is the only check that
# cannot drift as units are added, and it runs in the repo->image direction,
# which is the direction the bug actually travels. The old
# test_pi_gen.py::test_all_copied_units_exist_in_repo checked image->repo,
# which structurally cannot catch a unit that was never installed.
# Unconditional on purpose. These were briefly `${VAR:-default}` so tests could
# redirect them, but that bought nothing: the sentinel-extracted blocks below
# do not include these lines, and tests/test_pi_gen.py injects its own
# assignments ahead of the extracted block. All the indirection did was let a
# stray environment variable redirect where a ROOT-run build script reads its
# source of truth and verifies the installed result — point ETC_SYSTEMD_DIR at
# the source tree and every `cmp -s` compares a file to itself, so all three
# checks pass green on an image nobody inspected. That is the exact
# silently-wrong-image class litclock-dev#547 exists to close.
SRC_SYSTEMD_DIR=/home/pi/litclock/systemd
ETC_SYSTEMD_DIR=/etc/systemd/system

# BEGIN unit-install-check (tests/test_pi_gen.py extracts between these sentinels)
shopt -s nullglob
src_units=( "$SRC_SYSTEMD_DIR"/*.service "$SRC_SYSTEMD_DIR"/*.timer )
shopt -u nullglob
# Same floor as 03-install-services. tests/test_pi_gen.py asserts the two values
# stay equal, so they cannot drift apart.
MIN_EXPECTED_UNITS=10
if [ "${#src_units[@]}" -lt "$MIN_EXPECTED_UNITS" ]; then
    echo "FAIL: only ${#src_units[@]} unit files in ${SRC_SYSTEMD_DIR} — repo staging is broken"
    exit 1
fi
for src in "${src_units[@]}"; do
    name=$(basename "$src")
    if [ ! -e "${ETC_SYSTEMD_DIR}/${name}" ]; then
        echo "FAIL: ${name} exists in systemd/ but was never installed to ${ETC_SYSTEMD_DIR}/"
        exit 1
    fi
    # Existence is not enough. A stale unit left in /etc/systemd/system/ by a
    # reused pi-gen work directory (CLEAN=0) satisfies -e while differing from
    # what this build staged, so the image would ship a unit nobody reviewed.
    if ! cmp -s "$src" "${ETC_SYSTEMD_DIR}/${name}"; then
        echo "FAIL: ${ETC_SYSTEMD_DIR}/${name} differs from the staged source ${src}"
        exit 1
    fi
done
# END unit-install-check
echo "  OK: all ${#src_units[@]} source units installed to ${ETC_SYSTEMD_DIR}/ and match the source"

echo "-- systemd-tmpfiles drop-ins (derived from the source tree) --"
# Same derivation, same direction (repo -> image), for the one part of systemd/
# the unit globs cannot see. *.service/*.timer at the first level skips the
# tmpfiles.d/ subdirectory entirely, so before this block the drop-in that
# creates /run/litclock and /var/lib/litclock was checked by nothing at all —
# not the derived unit check, not required_files, not tests/test_pi_gen.py.
SRC_TMPFILES_DIR="${SRC_SYSTEMD_DIR}/tmpfiles.d"
ETC_TMPFILES_DIR=/etc/tmpfiles.d

# BEGIN tmpfiles-install-check (tests/test_pi_gen.py extracts between these sentinels)
shopt -s nullglob
src_tmpfiles=( "$SRC_TMPFILES_DIR"/*.conf )
shopt -u nullglob
# Same floor as 03-install-services. tests/test_pi_gen.py asserts the two values
# stay equal, so they cannot drift apart.
MIN_EXPECTED_TMPFILES=1
if [ "${#src_tmpfiles[@]}" -lt "$MIN_EXPECTED_TMPFILES" ]; then
    echo "FAIL: only ${#src_tmpfiles[@]} tmpfiles.d drop-ins in ${SRC_TMPFILES_DIR} — repo staging is broken"
    exit 1
fi
for src in "${src_tmpfiles[@]}"; do
    name=$(basename "$src")
    if [ ! -e "${ETC_TMPFILES_DIR}/${name}" ]; then
        echo "FAIL: ${name} exists in systemd/tmpfiles.d/ but was never installed to ${ETC_TMPFILES_DIR}/"
        exit 1
    fi
    if ! cmp -s "$src" "${ETC_TMPFILES_DIR}/${name}"; then
        echo "FAIL: ${ETC_TMPFILES_DIR}/${name} differs from the staged source ${src}"
        exit 1
    fi
done
# END tmpfiles-install-check
echo "  OK: all ${#src_tmpfiles[@]} tmpfiles.d drop-in(s) installed to ${ETC_TMPFILES_DIR}/ and match the source"

echo "-- Systemd enable symlinks (derived from [Install]) --"
# Every unit carrying an [Install] section must have its .wants symlink, EXCEPT
# the deliberate exclusions below. tests/test_pi_gen.py asserts this list stays
# byte-identical to BUILD_ENABLE_EXCLUSIONS in 03-install-services/00-run.sh,
# so the two cannot drift apart.
#
# litclock.timer — first-boot.sh enables it after setup completes. Enabling at
# build time would race splash/firstboot for GPIO before setup is done, which
# is why this is checked as a hard negative rather than merely skipped.
SMOKE_ENABLE_EXCLUSIONS=(
    litclock.timer
)
# BEGIN enable-symlink-check (tests/test_pi_gen.py extracts between these sentinels)
#
# This block verifies ENABLEMENT only — the .wants/.requires symlinks, which
# exist only on a built image and so are the one thing pytest cannot see.
#
# Directive VALIDITY (does this [Install] section install anything at all?) was
# briefly checked here as a hard failure and has been moved to
# tests/test_pi_gen.py::TestInstallSectionValidity. Reason: a false positive in
# this file blocks every release ~38 minutes into a 40-minute build, and the
# hard-fail version had four separate ways to reject a legal unit — `UpheldBy=`
# (valid since systemd 249) and `DefaultInstance=` were absent from its
# allow-list, indented directives are legal because systemd strstrip()s every
# line, and `WantedBy=a.target \` continuations parsed into a literal `\`
# target. Validity is a property of a file in the repo, so it belongs in a
# 0.3-second test that cannot block a build. Enablement needs the image.
#
# src_units is set by the unit-install-check block above. Asserted non-empty
# here rather than assumed: an empty array makes every loop below a no-op and
# this section would print its success line having verified nothing, which is
# the cannot-fail-guard shape this file exists to remove. The harness injects
# its own src_units, so without this assert the dependency is covered nowhere.
if [ "${#src_units[@]}" -eq 0 ]; then
    echo "FAIL: src_units is empty — the enable check would verify nothing and pass"
    exit 1
fi
unverified=0
for src in "${src_units[@]}"; do
    name=$(basename "$src")
    # Normalise leading whitespace ONCE, then match. systemd's parser
    # strstrip()s every line, so `  [Install]` and `  WantedBy=x` are legal
    # units. Column-0 anchors silently skipped the former — litclock-dev#547's own
    # failure mode arriving through whitespace instead of a name list — and
    # hard-failed the latter. Line continuations are joined in the same pass so
    # `WantedBy=a.target \` + `b.target` yields both targets rather than a
    # literal backslash.
    #
    # COMMENTS ARE DELETED BEFORE THE JOIN, and the order matters. systemd does
    # NOT continue a comment line: a unit whose line above [Install] ends in a
    # backslash is still enabled by `systemctl enable` (verified against a real
    # --root tree). Joining it here produced `# comment [Install]`, so the grep
    # below failed and the unit was skipped by this entire check with no output
    # — the silent-skip this normalisation exists to close, reintroduced by the
    # normalisation itself. Same bug one level in: a `# note \` INSIDE [Install]
    # swallowed the following WantedBy= and downgraded the unit to a NOTE.
    # TWO passes. `N` inside the :a loop appends the next RAW line without
    # re-running the delete, so a single pass still joined a comment that
    # FOLLOWS a continued directive: `WantedBy=a.target \` + `# note` became
    # `WantedBy=a.target # note`, and the shipped check then hard-failed a unit
    # real systemd enables fine. Comments must be gone before the join starts.
    norm=$(tr -d '\r' < "$src" \
        | sed -e 's/^[[:space:]]*//' -e '/^[#;]/d' \
        | sed -e :a -e '/\\$/N; s/\\\n[[:space:]]*//; ta' \
        | sed -e 's/[[:space:]]*\\[[:space:]]*$//')
    printf '%s\n' "$norm" | grep -q '^\[Install\]' || continue

    skip=false
    for excluded in "${SMOKE_ENABLE_EXCLUSIONS[@]}"; do
        if [ "$name" = "$excluded" ]; then
            skip=true
            break
        fi
    done
    if [ "$skip" = true ]; then
        # Every *.wants and *.requires directory, not a hardcoded pair — an
        # excluded unit wanted by sockets.target or a custom target would
        # otherwise pass while actually being enabled.
        if compgen -G "${ETC_SYSTEMD_DIR}/*.wants/${name}" > /dev/null \
            || compgen -G "${ETC_SYSTEMD_DIR}/*.requires/${name}" > /dev/null; then
            echo "FAIL: ${name} is a deliberate build-time exclusion but was enabled:"
            compgen -G "${ETC_SYSTEMD_DIR}/*.wants/${name}" || true
            compgen -G "${ETC_SYSTEMD_DIR}/*.requires/${name}" || true
            exit 1
        fi
        continue
    fi

    # Scope the read to the [Install] section — a stray or mistyped WantedBy=
    # earlier in the file would otherwise win. Iterate EVERY target, since
    # `WantedBy=a.target b.target` enables into both and checking only the
    # first would half-verify it.
    install_block=$(printf '%s\n' "$norm" | sed -n '/^\[Install\]/,/^\[/p')
    wanted_by=$(printf '%s\n' "$install_block" \
        | sed -n 's/^WantedBy[[:space:]]*=[[:space:]]*//p' \
        | tr -s '[:space:]' ' ')
    # RequiredBy= is verified the same way: it creates <target>.requires/<unit>,
    # exactly as checkable as .wants/. It used to be lumped in with Alias= and
    # Also= as "not verifiable", which would have let a future RequiredBy= unit
    # ship un-enabled behind a NOTE — litclock-dev#547 surviving inside the check built
    # to eliminate it.
    required_by=$(printf '%s\n' "$install_block" \
        | sed -n 's/^RequiredBy[[:space:]]*=[[:space:]]*//p' \
        | tr -s '[:space:]' ' ')
    if [ -z "${wanted_by// /}" ] && [ -z "${required_by// /}" ]; then
        # An [Install] section with NO recognised directive at all installs
        # nothing and is always a typo — the litclock-dev#547 shape. Hard-failed here as
        # well as in pytest, deliberately belt-and-braces: pytest gates the build
        # only via the workflow step added alongside this, and a gate that lives
        # in exactly one place is how this went missing the first time.
        #
        # This is narrow enough to be safe now, which it was NOT when it was
        # removed. All four of its false-positive causes are fixed above: the
        # directive list is the FULL documented set (UpheldBy= and
        # DefaultInstance= were missing), leading whitespace is stripped,
        # continuations are joined, and comments are deleted before the join.
        if ! printf '%s\n' "$install_block" \
            | grep -qE '^(Alias|WantedBy|RequiredBy|UpheldBy|Also|DefaultInstance)[[:space:]]*=[[:space:]]*[^[:space:]]'; then
            echo "FAIL: ${name} has an [Install] section with no recognised install directive"
            echo "      That section installs nothing. Check for a typo (e.g. WnatedBy=)."
            exit 1
        fi
        # Alias=, Also=, UpheldBy= and DefaultInstance= are legal but their
        # enablement is not verifiable by the .wants/.requires convention.
        echo "  NOTE: ${name} has [Install] but no WantedBy=/RequiredBy= — enablement not verified here"
        unverified=$(( unverified + 1 ))
        continue
    fi
    for target in $wanted_by; do
        if [ ! -e "${ETC_SYSTEMD_DIR}/${target}.wants/${name}" ]; then
            echo "FAIL: ${name} has [Install] WantedBy=${target} but no enable symlink was created"
            exit 1
        fi
    done
    for target in $required_by; do
        if [ ! -e "${ETC_SYSTEMD_DIR}/${target}.requires/${name}" ]; then
            echo "FAIL: ${name} has [Install] RequiredBy=${target} but no .requires symlink was created"
            exit 1
        fi
    done
done
# Report the unverified count rather than claiming "every [Install] unit
# enabled" unconditionally. The old line said that even when a NOTE had just
# been printed, so a build log ended with a success claim that contradicted
# the line above it. Inside the sentinels so the claim itself is tested.
if [ "$unverified" -gt 0 ]; then
    echo "  OK: every WantedBy= [Install] unit enabled, ${#SMOKE_ENABLE_EXCLUSIONS[@]} deliberate exclusion(s) verified un-enabled, ${unverified} unit(s) NOT verified (see NOTEs above)"
else
    echo "  OK: every [Install] unit enabled, ${#SMOKE_ENABLE_EXCLUSIONS[@]} deliberate exclusion(s) verified un-enabled"
fi
# END enable-symlink-check

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
# BEGIN unit-syntax-check (tests/test_pi_gen.py extracts between these sentinels)
# The patterns live INSIDE the sentinels on purpose. Outside them the harness
# ran with both unset, and `grep -qiE ""` matches every line — so the tests
# would have exercised a classifier that calls everything fatal while the
# shipped one classified nothing. Same outside-the-sentinel blind spot this PR
# exists to close.
# `Unknown key name` is the message that matters and it was missing. systemd has
# emitted it since v246 (bookworm ships 252); `Unknown lvalue` is pre-246 wording
# and is dead here, kept only so an older host still matches.
#
# THE EXIT CODE IS NOT A CLASSIFIER. Verified on systemd 255: a unit containing
# `WnatedBy=multi-user.target` — the literal litclock-dev#547 typo this gate is named for —
# produces `Unknown key name 'WnatedBy' in section 'Install', ignoring.` and exits
# ZERO. So the old `if output=$(...); then :; else <classify>; fi` never reached
# the classifier for it: no output echoed, neither pattern consulted, the counter
# not incremented, and the success line printed. Output is now captured and
# classified on EVERY run regardless of rc.
fatal_patterns='Failed to parse|is not a valid unit name|Bad unit file setting|Unknown key name|Unknown lvalue|Unknown section|Assignment outside of section'
# A SECOND fatal class, and the reason this loop is now load-bearing rather than
# advisory. Widening it from 7 hardcoded units to the whole source tree means
# systemd-analyze now resolves every ExecStart= in the tree — so it reports
# "Command ... is not executable" or "No such file or directory" for a helper
# that never reached the image. That is exactly the litclock-dev#547 evidence, and it was
# being collected and then thrown away: the message matches none of the patterns
# above, so the old code printed "(warnings only — not fatal)" and then "OK: all
# unit files pass".
#
# It is reachable today. litclock-set-timezone, reset-setup.sh,
# litclock-mark-collected.sh, litclock-wifi-reset.sh and the NM dispatcher are
# all referenced by units and none of them appear in required_files, so nothing
# else in this file would notice their absence. On the device it surfaces as
# status=203/EXEC in a journal nobody reads on a keyboardless appliance.
# Anchored to the message systemd actually emits, not the bare errno string.
# `No such file or directory` on its own is generic: any chroot-level failure
# carrying that errno (dbus, /proc, machine-id) would convert this from
# advisory into a hard fail on EVERY unit, 38 minutes into a 40-minute build,
# on the one gate whose false positives block all releases.
exec_fatal_patterns='Command .* is not executable|Command .* No such file or directory'
syntax_warned=0
for src in "${src_units[@]}"; do
    unit=$(basename "$src")
    echo "  checking $unit"
    # 2>&1 is load-bearing: systemd-analyze writes ALL diagnostics to stderr and
    # nothing to stdout (verified on 255). Without it $output is always empty and
    # this whole gate is a no-op.
    #
    # rc is captured SEPARATELY rather than discarded with `|| true`. Round 3
    # replaced rc-gating with an output-emptiness test, which laundered a
    # non-zero exit with no output into "OK: all unit files pass" — a
    # cannot-fail guard introduced while fixing a cannot-fail guard. Reachable
    # in the chroot: systemd-analyze under qemu-user-static binfmt dying on a
    # signal exits 132/139 and prints nothing.
    analyze_rc=0
    output=$(systemd-analyze verify -- "$src" 2>&1) || analyze_rc=$?
    if [ "$analyze_rc" -ne 0 ] && [ -z "$output" ]; then
        echo "FAIL: systemd-analyze verify exited ${analyze_rc} for $unit with no output"
        echo "      The verifier itself failed; this gate cannot vouch for the unit."
        exit 1
    fi
    if [ -n "$output" ]; then
        echo "$output" | sed 's/^/    /'
        if echo "$output" | grep -qiE "$fatal_patterns"; then
            echo "FAIL: $unit has real errors"
            exit 1
        fi
        if echo "$output" | grep -qiE "$exec_fatal_patterns"; then
            echo "FAIL: $unit references a command that is missing or not executable in the image"
            echo "      This is the litclock-dev#547 shape: the unit shipped, the thing it runs did not."
            exit 1
        fi
        # Anything left is genuinely advisory, but it is COUNTED so the success
        # line below cannot claim a clean pass over a unit that printed output.
        echo "    (warnings only — not fatal)"
        syntax_warned=$(( syntax_warned + 1 ))
    fi
done
if [ "$syntax_warned" -gt 0 ]; then
    echo "  OK: all unit files parse, ${syntax_warned} with non-fatal warnings (see above)"
else
    echo "  OK: all unit files pass"
fi
# END unit-syntax-check

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

echo "-- Waveshare e-Paper driver (submodule) staged --"
# The e-ink driver lives in the lib/e-Paper git SUBMODULE. A checkout or
# staging path that silently drops submodule contents (e.g. `git archive`,
# a checkout without submodules: true) produces an image that boots, serves
# the PWA, and never paints a single quote — the worst possible fielded
# failure. File-presence is checkable in the chroot even though the driver
# itself needs real hardware to import.
EPD_DRIVER=/home/pi/litclock/lib/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd7in5_V2.py
if [ ! -s "$EPD_DRIVER" ]; then
    echo "FAIL: waveshare driver missing/empty at $EPD_DRIVER — lib/e-Paper submodule was not staged"
    exit 1
fi
echo "  OK: waveshare epd7in5_V2 driver present ($(wc -c < "$EPD_DRIVER") bytes)"

echo "-- Python imports (venv, pure-Python deps) --"
# Validates pi-gen's pip install produced a usable venv. Hardware-specific
# imports (waveshare_epd, RPi.GPIO, spidev) are not checked here — they
# require real hardware probing and are verified on real Pis.
/home/pi/litclock/venv/bin/python3 -c '
import astral, PIL, qrcode, requests, timezonefinder, urllib3, certifi
# Successful import is the pass signal. Version-print is informational and
# must tolerate packages that do not expose __version__ at module level
# (e.g. qrcode).
def v(m): return getattr(m, "__version__", "unknown")
print(f"  OK: astral={v(astral)}, PIL={v(PIL)}, qrcode={v(qrcode)}, "
      f"requests={v(requests)}, tzf={v(timezonefinder)}, "
      f"urllib3={v(urllib3)}, certifi={v(certifi)}")
'

echo "=== In-chroot smoke test PASSED ==="
CHROOT
