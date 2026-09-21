# LitClock

## Pre-commit checklist

Before every commit, run these in order:

1. **Lint**: `ruff check .` — lint the WHOLE repo, not a path list. CI's
   `astral-sh/ruff-action` appends the repository root to whatever `args`
   names, so it effectively lints everything; a path list here silently
   skips `tools/` and any future top-level directory, and local green then
   means nothing. That gap shipped a red CI on the v0.226.0 port
   (`tools/render_invariants.py` E501). Full rule set, not just `--select F`.
2. **Shell lint** (when the commit touches any `.sh`): `find scripts pi-gen docs/manual -name '*.sh' -type f -print0 | xargs -0 shellcheck --severity=warning` — the exact CI invocation. CI runs this on every push; a local green that skipped it shipped six red Lint runs on 2026-08-21 (orphaned SC2034 constants from litclock-dev#647's branch cleanup).
3. **Tests**: `python3 -m pytest tests/ --ignore=tests/test_eink_display.py -q`
   — needs `requirements-dev.txt` installed. Since litclock-dev#769 pytest
   REFUSES to start without `pytest-timeout` (`required_plugins` in
   `pyproject.toml`) rather than silently running with no per-test timeout,
   which is what it did for months. If you see
   `ERROR: Missing required plugins`, that is this: install with
   `pip install --user --break-system-packages -r requirements-dev.txt` on a
   PEP 668 box (Debian/Ubuntu mark the system Python externally-managed;
   `--user` writes to `~/.local`, where the other dev tools already live).
4. **JS tests** (when `node_modules/` is present): `npm run test:js` — covers `src/control_server/static/js/*.js` via vitest + jsdom (litclock-dev#338). Dev/CI only; skip if you haven't run `npm install`. Never required on the Pi.

The eink_display tests require hardware-specific dependencies (Pillow + waveshare display drivers) that aren't available on dev machines. Weather tests now run in CI (astral is lazy-imported inside `is_daytime()`). All non-hardware tests must pass.

Do NOT commit if either step fails.

If the commit touches `image-gen/litclock_annotated.csv`, ship it with `python3 image-gen/corpus_edit.py ship "<message>"` instead of hand-sequencing image regen + `.images-version` bump + release (see CONTRIBUTING.md → "Editing the quote corpus", issue litclock-dev#211).

## Testing

- Framework: pytest (Python) + vitest (JS, control PWA only — litclock-dev#338)
- Python test command: `python3 -m pytest tests/ --ignore=tests/test_eink_display.py -q`
- JS test command: `npm run test:js` (requires `npm install` first; Node 20+)
- Python tests live in `tests/`; JS tests live in `tests/js/`
- Hardware-dependent tests (`test_eink_display.py`) are skipped on dev machines — they need Pillow + waveshare drivers
- JS test infra: vitest + jsdom; loader helpers in `tests/js/helpers/loadScript.js`. See CONTRIBUTING.md → "JavaScript tests" for usage.

## Linting

- Linter: ruff (configured in `pyproject.toml`)
- Check: `ruff check .` (whole repo — CI's ruff-action appends the repo root, so a path list under-covers; see the pre-commit checklist)
- Max line length: 120

## First-boot QA checklist

The first-boot flow (`scripts/first-boot.sh`) provisions WiFi via a web UI; everything else (location, timezone, units) auto-populates from IP geolocation after WiFi connects (EPIC litclock-dev#383). Must be tested on real Pi hardware — CI cannot fully simulate hotspot creation, captive portal, IP-geo retry, or e-ink display.

**Critical scenarios to test:**

- **WiFi-only hotspot form**: Verify the setup page shows ONLY the WiFi network picker + password field + Submit button (plus, on a multi-language fleet only, the Language select — dormant while English is the sole active registry language, litclock-dev#532). No Location, Timezone, Temperature, or Mature-content sections — those are PWA-only post-handoff. The hidden-network "Network name" box is NOT visible at load: since litclock-dev#848 it appears only after picking "My network isn't listed" in the dropdown (and is focused then — not at load), and picking a real network afterwards hides it again AND blanks anything typed. Two exceptions arrive with the option pre-selected and the box showing: a retry that echoes a hand-typed name, and an empty scan. Also check **Refresh**: with the manual option selected and a name typed, tap Refresh — the option must still be selected and the name still there when the list lands; and separately, tap Refresh from the placeholder and pick "My network isn't listed" DURING the scan (it runs 2-20s) — the box must NOT vanish when the response arrives. With JavaScript OFF the disclosure is present in every state, as it was before — but on a clean render it is CLOSED, so only its summary line shows and the box needs one tap to reveal; the retry-echo and empty-scan renders arrive open. That is the no-JS fallback, and the server's rule (a picked network wins over typed text) is the only guard there.
- **Hotspot creation**: Power on with no known WiFi networks. Verify the Pi creates a hotspot and displays credentials + QR code on the e-ink screen.
- **litclock-dev#620 hotspot-password block — RUN IN ORDER, and only on a device you can re-flash.** Checks 1-4 are sequential and destructive: check 2 needs the phone state check 1 leaves behind, check 3 destroys the password both depend on, and check 4 needs its own fresh flash. Out of order they need a re-flash to redo. Check 5 needs a shell AND the setup-hotspot path — it reads `/var/lib/litclock/hotspot-password`, which exists only when the setup network was raised — so run it right after check 1's first cycle, before check 3 rotates the key and check 6's SSH-off takes your shell. (These steps describe the litclock-dev#620 feature: if that PR has not merged, none of the paths below exist yet.)
  1. **The password is STABLE across cycles** — the core invariant, invisible without hardware. Provision, note the password on the e-ink, then re-enter setup with `sudo systemctl start --no-block litclock-wifi-reset.service`. **Do NOT run `litclock-wifi-reset.sh` foregrounded over SSH**: it deletes every WiFi profile before it clears `.setup-complete`, so your own connection dies mid-script and SIGHUP kills it before it finishes — leaving a device with no WiFi, no hotspot, and the look of a brick. Verify the panel shows **the same password**, then `sudo stat -c '%a %U:%G' /var/lib/litclock/hotspot-password` returns exactly `600 pi:pi`. Ownership matters as much as the mode: `litclock-firstboot.service` is `User=pi`, so a `600 root:root` file (what a maintainer running the CLI under sudo leaves behind) is unreadable to the real writer and silently rotates the password every cycle — the exact bug this feature removes. Both verification commands need a shell, and the reset you just triggered deleted the WiFi profile your SSH session was riding on — **reconnect over the `LitClock-Setup` hotspot and SSH to its gateway IP** (or run this step on an Ethernet-attached rig) before expecting them to work. If cycle 2 differs, get the cause for free with `journalctl -u litclock-firstboot | grep -iE 'hotspot.password'`: the code emits distinguishable lines for unreadable ("minting a REPLACEMENT"), invalid ("regenerating"), unwritable ("this cycle only") and race ("adopting the stored value"). Use that regex, not `'hotspot password'` — the race line is the one message that spells it `hotspot-password` with a hyphen, so a literal-space grep silently hides the single case this list exists to diagnose.
  2. **The phone actually rejoins** — the user-visible payoff. Using a phone that joined during check 1's FIRST cycle (do NOT forget the network), tap `LitClock-Setup`. **The pass condition is that the setup page LOADS** — captive sheet rises, or the gateway IP serves the WiFi picker. **"It did not ask for a password" is NOT evidence on its own** (litclock-dev#878 review, from the 2026-09-20 row below): on iOS 27.0 a stale-credential FAILURE from Control Centre also asks for nothing — no prompt, no banner, it just reverts to searching. So the absence of a password prompt is consistent with both outcomes there, and only the page actually loading separates them. On Android the absence still carries information, because a stale credential produces an explicit failure string. Test both platforms.
     - **Judging the result.** Do not use "No Internet Access" to judge a *success*: Android shows that for any AP without an uplink, so it is also what a successful join to a captive setup network looks like. On a genuinely stale credential, current Android is unambiguous — expect an explicit **"Connection failed — Wrong password for `LitClock-Setup`"**. That string is a clean fail signal, so treat its absence, plus the setup page loading, as the pass.
     - **Both measurements, stamped.** These disagree, and the disagreement is the point — record device + OS + date for anything you add here, because Android's WiFi error surfaces have changed materially across releases and an undated claim cannot be told apart from drift.

       | measured | device | OS | on a stale saved credential |
       |---|---|---|---|
       | ~2026-08-10 (litclock-dev#620) | OnePlus 6T | *not recorded* | "No Internet Access"; no password field offered; **a QR scan did not override the saved entry** (the phone did not register an attempt) |
       | 2026-08-15 (bench, `dev-20260815-b0c0590`) | *not recorded* | *not recorded* | "Connection failed — Wrong password for `LitClock-Setup`"; **offered "Change password"**; **a QR scan connected on the first try**, captive portal followed, provisioning completed normally |
       | 2026-09-18 (bench, `dev-20260918-787d307`) | iPhone | **iOS 27** | **QR scan joined first try** — camera scan raised a "join LitClock-Setup?" prompt, tapped Join, ~10s, captive portal opened by itself. No password prompt, no failure banner. First iOS measurement; the stale credential was two rotations old (a gift-mode prep and a PWA factory reset had each re-minted the key that same afternoon). **Do not record key values in this file** — it is published; the rotation COUNT is the measurement, the keys are a credential |
       | 2026-09-20 (bench, `dev-20260920-512c291`) | iPhone 16 Pro Max | **iOS 27.0** | **a plain tap does NOT join** — and the failure is SILENT from Control Centre: tapping `LitClock-Setup` there shows no error at all, then reverts to searching the moment you tap away. From **Settings** the same tap prompts for the password, which the owner can type to recover. So recovery on iOS exists but is not where a recipient taps first. Stale by ONE rotation (a PWA factory reset minted the key minutes earlier). The QR path was not re-run on this build |

       The 2026-08-15 run was on a device whose hotspot password had just been rotated by `--gift-mode` — functionally the same condition the original describes. All three of the original's Android claims failed to reproduce. That the device and OS columns are half empty is itself the finding: fill them in next time.

       **The 2026-09-18 row is the first with both columns filled, and the first
       iOS one.** It matters that the platform is recorded: the two Android rows
       disagree with each other, so a third undated row would have been
       unattributable to either a platform difference or OS drift. iOS 27 simply
       joined — no prompt, no banner, no recovery needed. It was a QR scan, so it
       settles the QR-override sub-check below on iOS. **The inverse was measured
       on 2026-09-20 and the answer is no**: a plain tap does not join. What makes
       that row worth reading twice is WHERE it fails. Control Centre reports
       nothing — no prompt, no error, it simply goes back to searching — while
       Settings offers the password field that recovers it. Any judgement about
       whether a recipient can recover has to name which of the two they used.

       **Do not read the OS off the captive-portal user agent.** On that same
       iOS 27.0 handset the probe logged `CPU iPhone OS 18_7 like Mac OS X`,
       because Apple freezes the UA in the captive WebView. The 2026-09-20 handset was
       checked in Settings -> General -> About and reads 27.0, which is what
       that row records; the 2026-09-18 row's method was not written down at
       the time, so treat its version as consistent-with rather than
       independently confirmed.

       **The owner reports iOS has behaved this way on earlier devices too**
       (2026-09-18, recollection rather than a dated run — deliberately NOT given
       a table row, since undated claims are the thing this table exists to
       replace). It does change where re-test effort belongs: the two Android
       rows contradict each other on whether recovery is discoverable at all,
       while iOS appears consistent and always has. Treat the uncertainty as
       **Android-side** — vary the device and the OS version there.

       **"iOS is settled" was QR-path evidence only, and the 2026-09-20 row
       narrows it** (litclock-dev#878 review). A QR scan joins; a plain tap does not, and
       fails SILENTLY from Control Centre. Those are different interaction
       paths, not contradictory measurements — but it means the platform is
       settled for the recovery we document and unsettled for the one a
       recipient reaches for first. Keep plain-tap coverage on iOS.
     - **Severity, corrected (litclock-dev#648).** The original block argued Android left *"no user-discoverable recovery short of Forget This Network, which the intended recipient will not find."* On the 2026-08-15 device recovery was one tap, and a QR scan bypassed the saved entry entirely. Treat the strong version as unproven rather than as established fact. **This does not weaken litclock-dev#620 itself**, which stays: a stable per-device password means a normal owner never reaches any of these screens, and that is the right outcome however gracefully a given Android build degrades. Only the severity narrative was stale.
     - **New sub-check: a QR scan overrides a stale saved entry.** Now the interesting case, and previously untested anywhere. Every LitClock broadcasts the same SSID with a per-device password, so a phone that set up clock A meets clock B with the wrong key — the recovery path a real recipient is most likely to stumble into. Scan the panel QR on a phone holding a stale credential for `LitClock-Setup` and confirm it joins. It worked on 2026-08-15; it did not on the original measurement, so this is worth re-running per device rather than assuming. **iOS 27, 2026-09-18: worked.** Camera scan raised a "join LitClock-Setup?" prompt; Join, ~10s, captive portal auto-opened — against a credential two rotations stale. The sub-check now stands at one PASS on iOS, one PASS and one FAIL on Android — the same Android-side split the rest of this block shows. Incidentally the panel says "wait about 20 seconds"; 10 sufficed, so that copy errs the right way.
  3. **Which resets keep the setup password and which rotate it — since litclock-dev#666 the DEFAULT rotates.** The old rule (wipe AND power-off) is gone; erasing both passwords is now what a bare reset does, and `--keep-wifi` is the only way to preserve either. Run the sub-checks in this order, because each destroys state the next would need. First **`--keep-wifi`** (the "same owner, moved house" case): `sudo cat /var/lib/litclock/hotspot-password`, keep the value, `sudo ./scripts/reset-setup.sh --keep-wifi --poweroff`, power back on, `sudo cat` again and confirm it is **UNCHANGED**. Read it from the file, not the panel — with the WiFi kept, the device boots straight onto its saved network and never raises a setup network, so there is no panel password on that path. Then a **bare `sudo ./scripts/reset-setup.sh --yes`** over SSH: expect the session to DROP when the WiFi goes (that is the documented behaviour, not a fault), power-cycle, and confirm the panel password is **DIFFERENT**. Since litclock-dev#833 there is a second thing to check on this path, and it needs a console, not a power-cycle: run the same bare reset from the console (or Ethernet), then `sudo reboot` — the panel must paint "Restarting…" (a `sudo poweroff` would paint "Powered Off") instead of carrying the stale quote across. A power-cycle fires no stop edge, so it cannot test this, and the SSH session a WiFi-wipe drops leaves you no shell to type the reboot from. The plain arm re-arms `litclock-shutdown.service` inside Step 1, right after its own stop consumed the edge, so this holds even when the reset aborts later or the WiFi wipe SIGHUPs it. Then the **PWA Factory reset** (`litclock-reset.service` runs `--wipe-wifi --strict-env-wipe --poweroff --yes`): record the password, trigger it from PWA → System, power on, confirm the panel password is **DIFFERENT** — this is the litclock-dev#660 path and the only one that proves it end to end. Also confirm the PWA's confirm modal and the in-progress screen both say the password will be new and that a phone holding the old one must forget the network. **Both handoff arms now also wipe the shell history (litclock-dev#868):** before triggering the PWA reset, run one throwaway command in an INTERACTIVE shell (console or a normal SSH session). A scripted `ssh host 'cmd'` writes no history, and neither does `ssh -t host 'cmd'` — a pty alone does not make a shell interactive. A bare `ssh -t host`, which drops you at a prompt, DOES save on exit (litclock-dev#878 review corrected the broader claim). Confirm the marker actually landed with `tail -2 ~/.bash_history` before you reset, or you will be testing nothing, then exit that shell so it saves. **Do not try to inspect the lock by mounting the card** — that was the first version of this check and it is impractical from a Windows box and unnecessary. The device removes the lock on its own first boot, so by the time you can look it is already gone, and an absent history file looks identical whether the lock was placed or never placed at all. Read the journal instead, after the reset and the next boot (verified 2026-09-20):

     ```bash
     MARKER=dev868-marker            # whatever you echoed before the reset
     sudo journalctl -b -1 | grep -E 'Clearing shell history before handoff\.\.\. .*(done|FAILED)'
     sudo grep -rl "$MARKER" /home/pi /root --exclude-dir={venv,lib,images,.git} 2>/dev/null   # nothing
     sudo ls -ld /home/pi/.bash_history /root/.bash_history                                    # both absent
     ```

     **The journal line is the load-bearing one, and the only one that can fail.** `clear_and_lock_bash_history`
     returns 0 only when BOTH paths were emptied AND both `mkdir` locks were placed; anything else prints `FAILED`
     and the red do-not-hand-this-on banner. It reaches the journal because `litclock-reset.service` sets
     `StandardOutput=journal`, so it survives the power-off. Three details that each turn this back into a check
     that cannot fail if you get them wrong (litclock-dev#878 review): **grep for `done|FAILED`, not for the prefix**, because
     the prefix matches both outcomes; **bound it with `-b -1`**, the reset's own boot, because journald here is
     `Storage=persistent` and an unbounded grep on a second run happily matches the FIRST run's line; and
     **quote a real `$MARKER`**, because a literal `<your-marker>` pasted into a shell is a redirect that produces
     empty output, which is exactly what the PASS looks like.

     **The `--gift-mode` repeat needs the PWA, not the CLI.** `sudo ./scripts/reset-setup.sh --gift-mode` writes
     that line to your terminal and nowhere else, so there is no journal to read afterwards. Trigger gift prep from
     the PWA (`litclock-prepare-for-gift.service`, which also sets `StandardOutput=journal`), or capture the
     terminal output yourself before the device powers off.

     **Do NOT use the `rmdir` lines from first-boot as proof the lock existed** (litclock-dev#878 review). `first-boot.sh`
     calls `sudo rmdir` unconditionally on both paths with errors suppressed, so the audit lines appear whether or
     not a directory was ever there — "the lock was never placed" passes that check exactly as cleanly as
     "the lock was placed and cleared". They corroborate the restore step running; they prove nothing about the
     lock. The exclusions on the marker search matter too: a bare `grep -r /home` walks the 1.9GB library tree
     and looks like a hang. Repeat for `--gift-mode`. A plain `--yes` reset or `--reboot` must leave the history file untouched; those hand control back to an operator, not to a new owner. Finally `sudo ./scripts/reset-setup.sh --gift-mode`, power on, confirm the panel password is **DIFFERENT**, and that `--gift-mode --keep-wifi` is **REFUSED** in both orderings. Gift prep must abort loudly ("do NOT ship this device") rather than print "done" if the file cannot be removed; an ordinary failing reset must say "Reset FAILED" instead, not tell you to stop passing the device on. After this sub-check the device has no saved WiFi, so check 4 needs a re-provision or a fresh flash regardless. **Every sub-step here now ends your SSH session** (litclock-dev#657): `--keep-wifi --poweroff` is permitted and still takes the poweroff arm, so it disables SSH too — and that sub-step explicitly rules out the panel and requires a shell, with no hotspot to fall back on because the WiFi was kept. Check 6's advice ("run anything needing SSH before a reset check") cannot rescue this one, because the check IS a before/after comparison across a reset. Either run the whole block from the console, or restore access between sub-steps by putting a blank `ssh` file in the SD card's boot partition.
  4. **The SD-cloning path rotates it too** — the highest-fanout distribution channel, and the one gift mode does NOT cover. `docs/sd-card-cloning.md` is the "SD Cards for Friends & Family" flow: without this step every clone broadcasts `LitClock-Setup` with the SAME key, known to whoever made the cards and never rotated on any recipient. On a provisioned clock run `sudo ./scripts/prepare-for-cloning.sh --no-poweroff` — **the `--no-poweroff` matters**: since litclock-dev#660 the script powers the Pi off when it finishes, so without the flag the device is already down before you can inspect anything, and booting it to look is the exact action that re-mints the key. Confirm `/var/lib/litclock/hotspot-password` is gone along with any `.hotspot-password.*` staging files, and that the script aborts rather than reporting success if it cannot remove them. Separately, run it WITHOUT the flag once and confirm the Pi powers itself off.
     - **After the `--no-poweroff` run the clock looks bricked. It is not — do not debug it, and do not try to restart it back to life.** The panel freezes on whatever quote was last painted and port 80 refuses connections, indefinitely, with `/` still `rw`, load idle and nothing failed. The script stops `litclock-control.service` (Step 1) and `litclock.timer` (Step 4), but the stops are the transient half: it also clears `/etc/litclock/.setup-complete` and `.handoff-complete`, and `litclock-control.service` and `litclock.service` are `ConditionPathExists`-gated on those, so `systemctl start` on either exits 0 and changes nothing. Each half of the symptom has a familiar fault behind it — a frozen panel is what the litclock-dev#531 lgpio wedge looks like, a refused `:80` is what a bind failure looks like — so the pair reads as two faults at once rather than one intended state. (The pair is in fact a specific signature: `litclock-control.service` has no dependency on `litclock.service`, so neither lookalike produces both. That precision is no help at 1am.) It cost 20 minutes on 2026-08-15 (`dev-20260815-b0c0590`), hours after the fact, when the terminal holding the closing banner was long gone. **Shut the Pi down** (`sudo shutdown -h now`) — the card is a clone master and there is nothing left to test on it.
     - **The default (no-flag) run reaches the same state only if its power-off fails.** It normally halts, so there is no panel to misread; on the `poweroff || …` recovery path it prints "Power-off FAILED", exits 1, and leaves the identical frozen panel. Both arms of the banner say so.
     - **Answering `y` to the WiFi prompt over SSH-on-WiFi kills the run.** Step 3 deletes the profile you are connected over, the session drops, and `SIGHUP` takes the script with it — before Step 8 removes the hotspot key, so the card is NOT prepared even though nothing said otherwise. Pre-existing, not introduced by any litclock-dev#659 change. The delete loop walks **every** NM connection, wired included, so ethernet is no refuge — run the `y` path from a local console, and if a session ever drops mid-run, re-run the script rather than assuming it finished.
     - **The bash history is a directory afterwards, and that is the lock, not damage (litclock-dev#834).** `sudo ls -ld /home/pi/.bash_history /root/.bash_history` must show two EMPTY directories after the `--no-poweroff` run. `history -c` reaches only the script's own shell; the console or SSH shell you ran it from writes its history back on exit, during the power-off — on the bench the file was back eight seconds after "Clearing bash history... done", holding the operator's last command. A directory at the path fails that write with EISDIR for root and pi alike (a mode-0 file does not: root bypasses it, and `history -w` renames over it). Exit your shell and confirm both paths are STILL directories, then boot a CLONE (never the master) and confirm `first-boot.sh` removed both (`ls -ld` shows nothing, and the recipient's first `exit` creates a normal file). Run the script as the only open session regardless — but note the rule cannot cover the shell you run it FROM, which is why the lock exists at all. **The lock is now a hard gate**: stage a failure (`sudo mkdir /home/pi/.bash_history && sudo touch /home/pi/.bash_history/x` before a run, which is what an earlier aborted run plus a stray file looks like) and confirm the step prints `FAILED` + `Do NOT clone this card` and exits 1 rather than a yellow note. Remove the stray file and re-run; it must complete.
     - **A failed env.sh wipe stops BEFORE the WiFi question (litclock-dev#839).** Stage it by holding the sidecar lock from a second shell — `sudo flock /home/pi/litclock/env.sh.lock sleep 120` — then run the script. It must print `Clearing configuration (env.sh)... FAILED` and the `Do NOT clone this card` banner and exit 1 **without** asking `Clear saved WiFi networks?`; afterwards `/var/lib/litclock/hotspot-password` must still exist, `nmcli connection show` must still list your network, and `env.sh` must be byte-identical. Pre-fix the same run printed "Clearing setup-hotspot password... done" BEFORE the red env.sh line, having already wiped everything else. **The device is NOT in the looks-bricked state the `--no-poweroff` bullet below describes**, and that is the point of the fix: the setup-state markers are removed only after the wipe succeeds, so an aborted run leaves a fully provisioned clock that still paints quotes and still boots normally. What IS true until the next boot: Step 1 already stopped `litclock-control.service` (the PWA) and the updater, so port 80 refuses connections in the meantime — reboot, or `sudo systemctl start litclock-control.service`, restores it. Release the lock and re-run; it must complete normally. Also confirm the aborted run did NOT leave `/var/lib/litclock/clone-prep-unfinished` behind: it changed nothing, so the next run must not open with the "previous run did not finish" warning.
     - **Do not reboot it to "check" it — that contaminates the master.** `first-boot.sh` runs again on that card, and whether the boot also re-mints the setup-WiFi key depends on which branch it takes. `is_wifi_connected()` is literally `ip addr show wlan0 | grep -q 'inet '`, and `litclock-firstboot.service` is ordered `After=NetworkManager.service`, **not** `network-online.target` — so the branch turns on whether `wlan0` happens to hold an address at that instant, not on whether a profile is saved. With **no** address it raises the setup hotspot, and `create_hotspot()` mints a fresh permanent key (litclock-dev#660) that every clone taken afterwards would share. With an address it completes setup inline — no page, no hotspot (litclock-dev#647), **no new key** — and runs straight through to the handoff splash. The script's own closing banner states the key hazard unconditionally, which is the safe direction; do not read the second branch as permission to boot the master.
  5. **The password must be absent from the RAW journal — that is the check, not the redacted bundle (rewritten for litclock-dev#867).** The original wording — grep the support payload and the deep logs for the setup password; it must not appear — passed VACUOUSLY on a fresh `dev-20260918-787d307` flash: the password never reached the journal at all — not because of litclock-dev#620's redaction but because litclock-dev#654 moved the PSK off the argv (`_build_hotspot_profile` feeds it to `nmcli connection edit` on stdin), so `redact_text()` had nothing to scrub and the check read identically whether redaction worked or had been deleted outright (the litclock-dev#860 / litclock-dev#864 class). The property worth holding is litclock-dev#654's invariant, and it CAN fail: sudo's command-audit line records the full argv, journald is persistent on the image, and the deep-logs bundle exports the last 50 lines of five units from whatever boots journald retains (no `-b`), so a future change that passes the password on a `sudo` argv puts it in the raw journal for good. If the grep ever fires, the place to look is `src/wifi_provision.py`'s profile writer, not the redactor. (The key IS still on a non-sudo argv during provisioning — `first-boot.sh` passes `--hotspot-password` to `setup_server.py`, readable in `/proc/<pid>/cmdline` for the life of that process — so "never reaches the journal" is not "never leaves the process".) Preconditions: a provisioning that raised the setup hotspot (the pre-connected litclock-dev#647 path never mints the file, and then this check does not apply), and NO `--hotspot-password` override on that run (a bench override leaves the stored file unchanged, so you would be searching for the wrong key). Over SSH, as separate steps — one pipeline hides three failures behind a `0`:

     ```bash
     PW=$(sudo cat /var/lib/litclock/hotspot-password) && [ "${#PW}" -ge 8 ] && echo "key ok (${#PW} chars)"
     sudo journalctl --no-pager > /tmp/journal.txt && wc -l < /tmp/journal.txt   # ALL boots, default short format (keeps the `sudo[pid]:` identifier); must be non-empty
     grep -cF 'sudo[' /tmp/journal.txt      # >= 1: the audit-line class that leaked pre-fix is actually in what you searched
     grep -cF -e "$PW" /tmp/journal.txt    # must print 0
     grep -cF 'LitClock-Setup' /tmp/journal.txt   # >= 1: the CONTROL — proves the search works
     ```

     Each line guards the next. An empty `PW` makes `grep -F` match every line (a false FAIL that reads like a catastrophic leak); a failed `journalctl` prints `0` matches (a false PASS); a journal with no `sudo[` lines proves nothing either way. `-e` keeps a key that begins with `-` from being parsed as an option. The last line's `grep` exits non-zero on the PASS — read the printed count, not `$?`. **Never put the value on a `sudo` argv while you are here** (no `sudo grep -r "$PW" /`, no retyping it under `sudo`): sudo would audit-log the password into the persistent journal by your own hand, and check 5 would fail forever after on this device. Only `journalctl` and `cat` run privileged above; the greps are unprivileged on purpose. Redaction itself is NOT a bench check: `tests/test_redaction.py::TestNmcliArgvSecrets` executes `redact_text()` against a real sudo audit line carrying an `nmcli … password …` argv, and `tests/test_diagnostics_no_secrets.py::test_synthetic_journal_secrets_do_not_leak` proves the exporter applies it. Only if the raw count is NON-zero does the bundle become worth reading: then PWA → Diagnostics → copy the deep logs, and the key's absence there tells you whether the redactor caught the shape — with the caveat that the bundle is a 50-line tail per unit, so a leak early in `litclock-firstboot.service`'s output can already have scrolled out of it by export time; the raw journal is the only read that sees the whole provisioning window. Baseline, 2026-09-18: `0` in the raw journal, `0` in all three endpoints, 39 `sudo[` lines of 196 in the bundle. **Re-run 2026-09-20 on `dev-20260920-512c291`, and this time it is demonstrably not vacuous:** 2120 journal lines, **427 `sudo[` audit lines** inside the very file being searched, zero password hits — plus a control string that IS present (`LitClock-Setup`, 15 hits), which is what proves the search works at all. Run that control every time: a `0` from a broken grep is indistinguishable from a `0` from a clean device, and that confusion is the whole reason this check was rewritten. `rm /tmp/journal.txt` when done — it is the unredacted journal.
  6. **A factory reset now turns SSH OFF on dev images too, and that is new.** litclock-dev#657 removed the
     dev-image exception: `disable_ssh_for_handoff` runs on both terminal arms of `reset-setup.sh` — gift mode and
     the litclock-dev#627 `--poweroff` reset — on every image, and the PWA's Factory reset button takes the second
     of those. So **the SSH session you ran the reset from is the last one you get** until you restore access. Two
     ways back in, both cheap, which is why the exception was not worth a dev/public divergence: put a blank `ssh`
     file in the SD card's boot partition from any machine that can read it (`sshswitch.service` re-enables SSH at
     the next boot), or use the console on a monitor-attached bench Pi. Plan the order of your checks accordingly —
     anything you need SSH for should run BEFORE a reset check, not after.
- **Captive portal**: Connect a phone to the hotspot. The setup page should auto-open (or be reachable at the displayed IP). On iOS, the journal should show the litclock-dev#526 coax sequence: a `CAPTIVE-PROBE` line with branch `cna-302` or `cna-302-hostmatch` (grep `-> cna-302`, matches both) `status=302` for the `CaptiveNetworkSupport wispr` UA, then (if the sheet rises) a Safari-family UA fetching `/cna` (bridge, 200). A `cna-bridge` 200 answered to a wispr UA means the UA split regressed. Ideally also verify no regression on one iOS-18-class device and macOS (their detection UA is also CaptiveNetworkSupport).
- **WiFi provisioning**: Select a network from the setup page, enter credentials. Verify the Pi connects and transitions to clock mode.
- **WiFi provisioning failure + retry**: Enter a wrong WiFi password. The page should auto-refresh and show an error banner ("Couldn't join your WiFi…"). Fix the password and resubmit. The Pi should connect on retry. Banner must NOT use the deprecated "home WiFi" phrasing.
- **IP-geo auto-populate (US residential)**: On a US residential WiFi, after submit verify env.sh contains `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, `WEATHER_LOCATION_NAME` (City, State), `WEATHER_UNITS=imperial`, and `timedatectl` reports the correct timezone — all from one ip-api.com call.
- **IP-geo auto-populate (non-US)**: On a non-US WiFi (or via VPN), verify `WEATHER_UNITS=metric` and tz matches the egress country.
- **IP-geo hard failure**: Block `ip-api.com` (firewall rule or DNS block) during provisioning. Resolver retries 4 times with 1/3/9s backoff. After hard failure: env.sh location keys stay empty, timezone unset. PR2 handoff splash will surface the browser-tz fallback.
- **Connecting splash (PR2)**: After submitting WiFi creds, the e-ink should swap the hotspot QR for a "Connecting to {SSID}…" splash while WiFi joins + IP-geo runs (~30s), before the handoff splash.
- **Handoff splash + quote gate (PR2)**: After IP-geo succeeds, the e-ink shows the "Ready to read." handoff splash (settings summary block: Location/Timezone/Units/Mature + PWA QR top-right encoding `http://<IP>` — port 80, no port shown, litclock-dev#343). Quotes must NOT start yet — `litclock.service` is gated on `/etc/litclock/.handoff-complete`. Verify the QR scans to the PWA and the Status tab shows the "Setup complete" top-sheet banner with a "Done — Start the Clock" button.
- **Handoff completion paths (PR2)**: Verify ALL of: (a) tapping "Done" in the PWA, (b) saving any setting in PWA Settings, (c) waiting 120s — each writes `.handoff-complete` and the e-ink paints the first quote within ~1 min. The AtHS hint must stay suppressed while the banner is up, then appear after Done.
- **Handoff failure (IP-geo blocked, PR2)**: With `ip-api.com` blocked, the e-ink shows "Almost ready." + "Not detected" rows; the PWA banner shows "Almost there." with a "Use {browser-tz}" button. Tapping it sets the system tz and starts quotes. Quotes must NOT start on the 120s timer when tz is unknown (a wrong-time clock is worse than no clock).
- **Handoff fallback timer (PR2)**: If control_server never completes the handoff but a location WAS detected, `litclock-handoff-fallback.timer` writes `.handoff-complete` ~10 min after boot so the clock isn't stuck on the splash. With NO location detected it must leave the splash up.
- **Upgrade migration (PR2)**: On an already-provisioned Pi, run `update.sh` and confirm quotes keep painting — the new `litclock.service` gate is satisfied by update.sh touching `.handoff-complete` (it never runs the handoff flow). This is the brick-prevention check for authorclock.
- **Clock starts after setup**: After completing setup + handoff, confirm the e-ink display shows a literary quote within ~2 minutes (assuming tz resolved).
- **Pre-connected WiFi path (litclock-dev#647)**: Boot with WiFi already configured — via `reset-setup.sh --keep-wifi`, the marker-deletion quick re-test below, or a wpa_supplicant-preseeded card. (A plain `--poweroff` reset wipes WiFi by default since litclock-dev#666 and lands on the hotspot path; and `is_wifi_connected()` checks `wlan0` only, so ethernet does NOT reach this branch.) NO setup page is served and no QR to `https://<IP>:8443` appears — the e-ink shows "Setting Up / Detecting your location...", the IP-geo resolver runs inline, and the flow lands directly on the "Ready to read." handoff splash (PWA QR + Done / 120s fallback). A Specific location saved in the PWA must survive this path untouched (the resolver gates on `WEATHER_LOCATION_MODE=auto`). With `ip-api.com` blocked, the handoff splash must show "Almost ready." and the PWA the browser-tz fallback — same degraded path as the hotspot flow.
- **Post-setup PWA Settings overrides**: After first-boot, open the Control PWA → Settings → Weather and verify auto-populated values are editable. If a city is set, a "Clear" link appears next to the "Currently: …" hint. Tap Clear → city, latitude, longitude all clear together. Weather should stop rendering on the e-ink within ~1 min. Toggling `WEATHER_ENABLED` off WITHOUT tapping Clear must preserve the saved city (regression test for the toggle-only flow). **litclock-dev#337 supersedes this — see the litclock-dev#337 IA section below.**

### Render timing — the minute lead (litclock-dev#762)

Added because the first-boot checklist above covers provisioning and says
nothing about the painted frame's TIMING, which is what litclock-dev#762 changed. The
timer now fires at `:56` (`systemd/litclock.timer` → `OnCalendar=*-*-* *:*:56`)
and the painter renders the **upcoming** minute, `RENDER_LEAD_DEFAULT_S = 4.0`
seconds ahead. **The two halves are one change**: the timer at `:56` without
the offset renders the previous minute's quote permanently, and the offset
without the timer renders 4s into the future. `tests/test_timer_lead.py` pins
them together, but the payoff is only observable on hardware.

- **The quote lands ON the minute.** Put a phone stopwatch (or any second-
  accurate clock) next to the panel and watch one transition. The refresh
  blackout should START at about `:56` and the new frame should be settled at
  or just after `:00` — NOT around `:06`, which is what the old `:00` timer
  produced (p50 refresh 10.45s over 6942 on-device runs). Watch at least three
  transitions: the panel's own refresh time varies, and one sample cannot tell
  a fixed offset from a slow refresh.
- **The timestring matches the wall clock during the blackout window.** The
  interesting moment is `:56`–`:00`, when the panel is mid-refresh painting a
  minute that has not arrived yet. Immediately after it settles, the quote's
  timestring must equal the CURRENT minute, not the next one. If it reads one
  minute ahead once settled, the lead is too large for this device's refresh.
- **Runtime-render devices need the override checked separately.** Runtime
  render costs ~1.3s more prep, so on a device with `LITCLOCK_RUNTIME_RENDER`
  on, confirm the frame still settles by `:00`. If it does not, raise
  `LITCLOCK_RENDER_LEAD_S` for that device — do NOT edit the default.
- **The nightly full clear still fires — this one fails SILENTLY.** `epd.Clear()`
  is gated on `now.minute == 0 and now.hour == DISPLAY_CLEAR_HOUR` (default 2),
  and a live clock read is never minute 0 at `:56`. The code reads the OFFSET
  target for this (at `01:59:56` the target is `02:00:00`), so it works — but
  if that ever regresses, the clear simply stops happening and ghosting
  accumulates over WEEKS with nothing in the journal. Verify directly: set
  `DISPLAY_CLEAR_HOUR` to the next hour, wait for the top of it, and confirm
  the panel performs a full white flush rather than a normal partial refresh.
  Do not skip this because the unit tests cover it; the unit tests cover the
  target arithmetic, not that the panel actually flushed.
- **Midnight rollover — every downstream site must read the SAME target.** At
  `23:59:56` the target is tomorrow. Watch that transition (or fake it by
  setting the clock a minute before midnight with `sudo timedatectl set-ntp
  false && sudo timedatectl set-time "..."`, then restore NTP). The masthead
  DATE, the quote's timestring, and the status file must all agree on tomorrow.
  A split here shows as a panel whose date says yesterday while its quote says
  tomorrow.
- **A malformed `LITCLOCK_RENDER_LEAD_S` must not brick the painter.** `export
  KEY=` is `env.sh.sample`'s own idiom for an unset key and `update.sh` merges
  sample keys into every device's `env.sh`, so the empty value is not
  hypothetical. Set `export LITCLOCK_RENDER_LEAD_S=` (empty), then `=abc`, then
  `=0`, restarting `litclock.timer` each time. Every one must keep painting on
  the 4.0s default; `=abc` and `=0` must log a warning naming the variable,
  while the EMPTY value is silent BY DESIGN — it is the sample's own unset
  idiom and `update.sh` merges it onto every device, so a warning there would
  fire every minute on every clock and train you to ignore the line that
  matters (v0.227.0 port review). **`=0` is the dangerous one** — it is what
  an operator reaching for "turn this off" would set, and it is accepted-looking
  but produces the previous minute's quote forever, so confirm the journal
  actually says it fell back. If the guard ever regressed, the frozen panel
  would look exactly like the litclock-dev#531 lgpio wedge — but the journal would carry
  a traceback naming the variable (`litclock.service` sets
  `StandardError=journal`), so the panel is ambiguous and the journal is not.
  That is why the check is "read the journal", and why it is worth three
  restarts.

### Shutdown splash vs. boot splash (litclock-dev#856)

The only part of litclock-dev#856 unit files cannot prove. `litclock-shutdown.service` is
now `Before=litclock-splash.service`, which in the stop direction means its
`ExecStop` (`shutdown-splash.sh`) waits for the boot splash's stop job — and
that stop is what releases GPIO17/SPI, because it SIGTERMs a control group
rather than running an `ExecStop`. Run on a device you can re-flash.

**VERIFIED END TO END on the bench 2026-09-17 23:43–23:49 CDT**
(bench device, address and build deliberately not recorded here — this file
is public), control-fails-then-fixed-passes,
after this recipe had failed to test anything twice (litclock-dev#860). The numbers below
are that run; a follow-up pass on 2026-09-18 07:42–07:46 added the panel
corroboration from the journal and found litclock-dev#862 doing it (last two bullets).
Three things the earlier versions got wrong are corrected here —
**read all three before touching the device**, because each one on its own is
enough to make the check pass on broken code.

**Where the delay has to go: inside the painter, after `epd.init()`.** The
paint is a synchronous `timeout 20 "$PYTHON" src/eink_display.py status ...` in
`scripts/boot-splash.sh`. A `sleep` placed *before* that line runs when Python
has not yet acquired GPIO; placed *after* it, Python has already exited and
released it. **Neither creates contention, and neither can fail** — a delay
around the paint tests nothing. GPIO17/SPI is held only between `epd.init()`
and `epd.sleep()` inside `display_image()` (`src/eink_display.py`, ~line 476).

**Correction 1 — a plain `sleep` hold cannot reproduce the race, in either
configuration.** Measured: with both directives REVERTED and a 20s
`time.sleep()` hold, the shutdown splash **painted cleanly**. The splash's
python dies on SIGTERM in under a second (`time.sleep` is an interruptible
point, and SIGTERM's default action needs no Python at all), while
`shutdown-splash.sh` spends ~1.5s in Python startup and image generation before
it reaches `epd.init()`. It loses the race by default. **The hold must survive
SIGTERM** — which is also the honest model of the hazard, since litclock-dev#856's own
unit comment names "a python wedged in uninterruptible kernel I/O on the SPI
transfer" as the residual. Use this, not the old one-liner:

```python
# QA HARNESS (litclock-dev#860) — remove after testing.
import os as _os, time as _t, signal as _sig
_h = float(_os.environ.get("LITCLOCK_QA_PANEL_HOLD_S") or "0")   # `or`: an EMPTY value is this repo's unset idiom
if _h:
    _sig.signal(_sig.SIGTERM, lambda *a: logging.warning("QA hold: SIGTERM ignored, GPIO still held"))
    logging.warning("QA hold %ss (post-init, SIGTERM-resistant)", _h)
    _end = _t.monotonic() + _h
    while _t.monotonic() < _end:
        _t.sleep(0.2)
    logging.warning("QA hold over")
```

Off by default, so a forgotten line does not change a normal boot. Two more
device edits, all three reverted by the next `update.sh` — undo them yourself
with `git checkout -- src/eink_display.py scripts/boot-splash.sh
scripts/shutdown-splash.sh` (the third file only if you also add the
journal-corroboration probe in the second-to-last bullet):

1. The block above, in `display_image()`, immediately after `epd.init()`.
2. `sudo systemctl edit litclock-splash.service` → `[Service]` →
   `Environment=LITCLOCK_QA_PANEL_HOLD_S=20`. Confirm it lands:
   `systemctl show litclock-splash.service -p Environment`.
3. In `scripts/boot-splash.sh`, raise the wrapper to `timeout 40` — at
   `timeout 20` the hold is killed before it does anything.

**Correction 2 — use `systemctl restart litclock-splash.service`, and fire it
clear of the minute tick.** Do not race a boot window; the unit has no
`Condition*=` and restarts by hand cleanly into the same start state holding
GPIO. `restart`, not `start`: it is a `RemainAfterExit=yes` oneshot, so `start`
on an already-active unit is a silent no-op. **And the painter it spawns loses
the panel to `litclock.service` if it lands on the `:56` tick** — measured at
23:46:58, `boot-splash.sh` exited in 1s with `Could not initialize display:
'GPIO busy'` and the run proved nothing. This is almost certainly what happened
on 2026-09-17 (litclock-dev#860): an ExecStart that finishes in ~1s with the painter
already gone is this, not a misplaced hold. Fire between `:10` and `:45` of a
minute, and **guard the reboot on the hold actually being live**:

```bash
S=$(date +%S); sleep $(( (75 - 10#$S) % 60 ))      # land at ~:15
sudo systemctl restart --no-block litclock-splash.service
sleep 12
N=$(pgrep -fc "eink_display.py statu[s]")          # bracket: do not self-match
if [ "$N" -ge 1 ]; then sudo systemctl reboot; else echo "ABORT: hold not live"; fi
```

(No `exit` in that block on purpose — it is meant to be pasted into an
interactive SSH session, and an `exit` on the guard path closes the session you
are about to need.)

**Budget — use 20s, and do NOT exceed it.** The hold is squeezed from both
ends. Above it: `~7–11s paint + hold` must stay inside `TimeoutStartSec=45`,
and the paint is 7s warm but **10s cold** (measured on a first boot), so a 30s
hold leaves only ~4s of margin — trip it and systemd kills `ExecStart`, the unit
fails, and the harness quietly becomes a start-timeout test instead. That is the
same "cannot fail" class this whole section exists to prevent, so take the
margin: **20s**. Below it: the hold must outlast `TimeoutStopSec=10s` measured
from the reboot at `t+12`, i.e. it must still be running at `t+22`; a 20s hold
started at `t+7..11` runs to `t+27..31`. 8s would not. Do not raise
`TimeoutStartSec` to buy room — 45s is the bound the whole thing lives in.
(The 2026-09-17/18 runs used 30s and did not trip it; 20s is the same test with
margin.)

- **Run the CONTROL first, on the unfixed units, or you have not tested
  anything.** Revert both directives on the device — drop
  `litclock-splash.service` from `Before=` in
  `/etc/systemd/system/litclock-shutdown.service` and the `TimeoutStopSec=10s`
  from `/etc/systemd/system/litclock-splash.service` — `daemon-reload`, then run
  the block above. **Correction 3 — the expected failure string is NOT
  `KeyError: PinInfo(... 'GPIO17' ...)`.** That shape was inferred in litclock-dev#856
  ("no hardware reproduction yet") and never observed; `get_display()` catches
  the lgpio error at `epd7in5.EPD()` construction, so what
  `journalctl -b -1 -u litclock-shutdown` actually carries is:
  ```
  WARNING: Could not initialize display: 'GPIO busy'
  ERROR: No display available
  ```
  A tester grepping for `KeyError` finds nothing and calls the control clean —
  a third way this check could not fail. **Expected FAILURE, measured:** the
  shutdown unit's `Stopping` and the splash's stop run CONCURRENTLY
  (both 23:45:15), `'GPIO busy'` 1.5s later at 23:45:16.7, no shutdown paint,
  and the panel powers off still showing **"Starting…"**. `shutdown-splash.sh`
  swallows it on its `|| true` tail, so the journal is the only place it is
  visible. If the control does not fail, fix the harness before reading
  anything into the fixed run.
- **Then the fixed units: the payoff.** Restore both directives,
  `daemon-reload`, re-run the block. **Measured PASS:** SIGKILL at exactly
  `TimeoutStopSec` after the reboot (23:48:55.3 → 23:49:05), splash `Stopped`,
  and only THEN `Stopping litclock-shutdown.service` — the ordering, in
  reverse, visible as a timestamp gap the control does not have. ExecStop then
  paints with no error and finishes 23:49:12. The e-ink must end on the
  **reboot splash** ("To sleep, perchance to dream." and friends) while the Pi
  is off.
- **The bound.** Time that same reboot. Measured: **~17s** with
  `TimeoutStopSec=10s`, versus **26s** in the control (and up to 90s + 90s
  unbounded, if the hold outlives `DefaultTimeoutStopSec`). A
  reboot-during-splash that suddenly takes over a minute means the directive is
  gone — and a minute is long enough that a real owner pulls the power and gets
  no splash at all, which is the whole reason it is capped.
- **The start-direction half.** With the hold running, from a second SSH
  session: `systemctl is-active litclock-shutdown.service` must read `active`
  while `systemctl is-active litclock-splash.service` still reads `activating`.
  **PASS 2026-09-17.** If the shutdown unit reads `activating` or `inactive`,
  the `Before=` edge is not being honoured and a reboot in that window paints
  **nothing** — a `Type=oneshot` stopped out of its start state never reaches
  `ExecStop`.
- **The ordinary case still works.** Undo all three edits and the drop-in,
  reboot normally, confirm the reboot splash paints as always. This is the
  boot-direction regression check: a cycle would have had systemd drop an edge
  at random. `systemd-analyze verify /etc/systemd/system/litclock-*.service` on
  the device must report no ordering cycle.
- **What this run did NOT show, and it matters for how you read litclock-dev#856.** The
  ordinary reboot-during-splash does not race at all (Correction 1): the fix
  only changes the outcome once the splash needs longer than ~1.5s to die. So
  the window litclock-dev#856 describes is real but narrower than the issue implies, and
  the directive's day-to-day value is the 10s bound as much as the ordering.
  Both are still right; neither is load-bearing on a healthy panel.
- **The fixed run paints the WRONG splash — litclock-dev#862, found 2026-09-18.** The
  ordering works, and the paint it enables resolves `action=poweroff` on a
  `sudo reboot`: the panel gets a final-state farewell ("So we beat on, boats
  against the current") instead of "Restarting…". `shutdown-splash.sh` tier 3
  is `systemctl list-jobs | grep -q reboot.target`, and by the time the DELAYED
  `ExecStop` runs the system bus is gone — captured at resolution time, the
  command returns `Failed to connect to bus: Connection refused`, so the grep
  fails and tier 4 falls through to poweroff. Deterministic, n=2; the same
  reboot with the splash idle resolves `action=reboot` correctly. **Do not read
  this as a reason to revert litclock-dev#856** — pre-fix, that window painted NOTHING
  and carried "Starting…" across the power-off. When QAing this section, expect
  the wrong variant until litclock-dev#862 lands, and judge litclock-dev#856 on the ordering and
  the 10s bound, which are the two things it claims.
- **Corroborate the panel from the journal, not the glass.** The variant is not
  logged today (litclock-dev#861), so the 2026-09-18 pass added two temporary `echo`s to
  `scripts/shutdown-splash.sh` — one for `$SHUTDOWN_ACTION` before the `case`,
  one for `${SPLASH_ARGS[*]}` before the paint — and dumped `systemctl
  list-jobs` to a NON-tmpfs path (`/run/litclock` is tmpfs and does not survive
  the reboot you are about to take). That turns a 10s eyes-on window into a
  `journalctl -b -1` grep, and is how litclock-dev#862 was found at all.
- **Observe, do not fix here (Codex, out of scope for litclock-dev#856).** A SIGTERM
  landing inside `display_image()` skips `epd.sleep()`, so the panel is left
  out of its sleep state; whether it recovers cleanly from an interrupted SPI
  transfer versus an interrupted BUSY waveform is unproven either way. This
  predates litclock-dev#856 and the control run above deliberately provokes it. Note
  what the panel does — ghosting, a partial frame, a refusal to take the next
  paint — rather than treating it as a litclock-dev#856 regression. On the 2026-09-17
  run the next scheduled paint succeeded normally (`picked_at_age_s` 0.27s
  after the following tick), so at least one SIGKILL mid-hold left no lasting
  damage.

### OTA smoke gate (litclock-dev#763, litclock-dev#773)

Not in the checklist above because it is not a first-boot flow — but it is the
highest-blast-radius surface in the tree, and **the failure direction is
asymmetric**. A false GREEN ships a broken update once. A false RED reverts
every update, on every device, every weekly tick, forever: nothing writes a
blocked-sha on the smoke-revert path, so the next tick resolves the same target
and reverts again. Test on a device you can re-flash.

Force a run rather than waiting for the Sunday timer:

```bash
sudo systemctl start litclock-update.service && journalctl -fu litclock-update
```

- **The happy path keeps the update.** A normal run must log `Smoke test
  passed`, `Catalog smoke passed`, `Splash-triplet smoke passed` and
  `Catalog size smoke passed (NNN keys)`, and must NOT log `[revert]`. Confirm
  the PWA's System tab shows the new version afterwards.
- **A non-English device still takes the update (litclock-dev#763).** This is the one
  that reverted forever. Set a non-English `LITCLOCK_LANGUAGE` in `env.sh` and
  run an update. The catalog probes are pinned to English (`LITCLOCK_LANGUAGE=en`
  as a command prefix), so they must still pass. Before the pin they resolved
  in the owner's language and compared against English literals. **Latent while
  the registry lists one active language** — it only bites once a release
  activates a second and an owner selects it, so this needs a fleet with a
  second active language to be a real test rather than a shape check.
- **A missing venv interpreter FAILS the gate, it does not skip it (litclock-dev#773
  item 1) — but do NOT try to stage it with `mv`. That recipe was wrong
  (litclock-dev#864).** `sudo mv /home/pi/litclock/venv/bin/python3{,.bak}` and
  run an update and you get **`Update Complete`**, not a revert: the Phase-4
  venv guard (`update.sh:1325`, `! "$PYTHON" -c "import PIL, requests"`) fails
  on a *missing* interpreter exactly as on a broken one, rebuilds the venv on
  the spot, and the gate then runs against a healthy interpreter. Measured
  2026-09-18. Every other hand-staged variant lands somewhere else too: if you
  also stop the rebuild from succeeding, `NEED_PIP` is already true and pip —
  whose shebang points at the interpreter you removed — fails first, so the
  **pip-failure arm** reverts, not this one. The `update.sh:1636` arm is
  reachable only when Phase 4 gets far enough to skip or finish pip and still
  leaves no interpreter (a pip run that breaks its own venv, or an interrupted
  Phase 4) — which you cannot stage from a shell in one line.
  **So do not hardware-test this one.** It is covered where it belongs, by
  `tests/test_update_sh.py` (the executed arm assertion at ~line 2371 pins
  `smoke_rc=1` AND `smoke_no_interpreter=1` together). Spend the bench time on
  the catalog case below, which is both reachable and the one that shipped
  green with 435 strings gone.
- **A truncated catalog FAILS the gate (litclock-dev#773 item 2) — and the truncation
  must be in the TARGET tree, not on the device.** Phase 2 does
  `git reset --hard <target>` before the gate runs, so a file you truncate on
  the device is restored before anything looks at it; the old wording here said
  "truncate … then run an update", which tests nothing (litclock-dev#864).
  Publish a deliberately bad release instead — see the local-remote harness in
  a local bare remote — with `languages/en/strings.json` cut to a handful
  of keys, **keeping** `status.relative.just_now`, `boot.splash.starting.title`
  and `firstboot.splash.setup_incomplete.title`, the three the VALUE probes
  read. Build it on top of the release the device is already running, because
  releases are cumulative and the gate that runs is the *target's* gate, not the
  device's. The value probes must pass and the COUNT probe must fail with
  `Catalog smoke failed: catalog-count returned 'N' (want >= 400)`. That
  combination is the whole point: before the count probe, exactly this bundle
  passed green with 435 strings gone. Verified 2026-09-18 (`returned '8'`).
- **A revert leaves a working clock, not a brick.** After any of the failing
  cases above, confirm the panel is still painting quotes on the old SHA and
  the PWA Updates view shows the terminal banner **"Update failed verification —
  rolled back. Your clock is running normally."** (state `failed_reverted`).
  **Then run the tick again — this is the half that used to be missing.** Since
  litclock-dev#865 the revert records the failing SHA in
  `/var/lib/litclock/blocked-sha`, so the second tick must log
  `Latest Release SHA … is blocked (a previous run reverted from it) — skipping
  update`, leave `HEAD` untouched, finish `inactive` rather than `failed`, and
  report `update_state: complete` (a deliberate no-op is not a failure). Before
  that fix it re-fetched, re-applied, re-failed and reverted again — every week,
  forever, with the clock stopped and a full pip install each time. Then publish
  a NEWER healthy release and confirm it installs and CLEARS the block: a device
  that can never be updated again would be a worse bug than the loop. All three
  ticks verified on hardware 2026-09-18. The pip hash stays deleted across the
  blocked ticks (the revert removes `HASH_FILE`) so the recovering release
  re-runs pip once; that is expected, not a second fault.
- **The runtime-render self-test runs after the marker, and its verdict lands in the memo (litclock-dev#871 Stage A — INERT this release).** It runs only on an APPLIED release: a tick with nothing new exits at the no-op early-out before Phase 4.5, so publish one from a local bare remote — and build that release so it touches NONE of the six proof inputs (`fonts/`, the measurement dump, `requirements.txt`, `quote_renderer.py`, `gd_measure.py`, `validate_measurement.py`), or Phase 2b's revoke block removes the marker you are about to tamper with, the KEEP arm re-stamps a fresh one, and the self-test passes; a `runtime-render validation marker removed:` line in the journal means the staging was undone and the run proves nothing. A normal run on a device with a marker must log `Running the runtime-render self-test`, then `[selftest] dry-run: rendered 800x480 image, render_mode=runtime`, then `runtime-render self-test PASSED in N.Ns`, and `/var/lib/litclock/runtime-render-selftest.json` holding `"result":"passed"` with `duration_s` and the release `sha` — the durable pass record Stage B will gate on (the journal is capped at 7 days, so the log line alone would be gone before the next release); read `duration_s`: Stage B will need devices that render inside the 4s lead — and `LITCLOCK_RUNTIME_RENDER` in `env.sh` must be UNCHANGED afterwards, because Stage A changes nothing on the device; that is the point. A failure is silent by design, so stage one to prove the harness can fail — **in the marker, not in the tree**: `sudo sed -i 's/digest=[0-9a-f]*/digest=deadbeef/' /home/pi/litclock/.runtime-render-validated`. The marker is gitignored, so Phase 2's `git reset --hard` leaves it alone; it is still PRESENT, so the KEEP arm does not re-stamp it and clears the memo; and the painter's digest check declines the text tier, so the self-test paints a PNG and exits 3. Expect `[selftest] ... render_mode=image`, then `self-test did not pass: the painter fell back`, then `jq .result,.rc /var/lib/litclock/runtime-render-validation.json` printing `"selftest-failed"` and `3` (the file is compact JSON — do not grep for a spaced literal), `/var/lib/litclock/runtime-render-selftest.json` GONE (a fail retires an earlier pass), and `/api/status` showing the memo as `runtime_render_validation`. **The update must still end in `Update Complete`** and the panel must keep painting — a failed self-test is not an update failure. Two stagings that CANNOT fail, recorded so nobody re-derives them: moving the marker aside (the KEEP arm re-stamps an absent marker before the self-test runs, so that run passes), and `sudo mv fonts{,.bak}` (four tracked files — Phase 2 restores them before anything looks, and even if it did not, the PNG tier needs the same fonts for the masthead, so the smoke gate's own dry-run would revert the release before reaching the self-test). Then restore the marker — `sudo -u pi /home/pi/litclock/venv/bin/python3 tools/validate_measurement.py check --stamp` re-earns it — and apply another release: the present marker clears the memo at the top of the KEEP arm, the self-test passes, and the memo file must be gone. Three more things to confirm while there: with a non-English `LITCLOCK_LANGUAGE` in `env.sh` the `[selftest]` lines render THAT corpus (the catalog probes above are pinned to English; this one deliberately is not); `/run/litclock/current-quote.png` keeps its pre-tick mtime (the self-test renders into a throwaway directory); and no weather request leaves the device during the self-test (`WEATHER_ENABLED` is forced off for it — a capability probe has no business on the network).

     **First field measurement, 2026-09-20 (`dev-20260920-512c291`, Pi Zero 2 W):** the self-test recorded **3.8s** during the update; five hand-run repeats on an idle system gave **3.4 / 3.5 / 3.5 / 3.5 / 3.7s**. The gap is update-time load, not instrument bias, and it errs the safe way.

     **Read what `duration_s` actually contains before you build a threshold on it** (litclock-dev#878 review). It is WHOLE-PROCESS wall time: `t0` is stamped before the subshell, so it includes sourcing `env.sh`, spawning the interpreter, and importing PIL — a material share of 3.5s on a Zero 2 W — and only then the render. It is **not** render time, and it is **not** comparable to a frame-settle figure, which includes a panel refresh the dry-run never performs (it exits before `epd.init()`).

     Worse for a naive gate: the painter pays that same startup BEFORE it computes its target instant, so that share is not covered by the 4.0s lead at all. The lead is not a render budget. What actually makes a frame late — timer fire through `epd.display()` returning, panel included — is measured by neither this number nor the lead, so Stage B needs to decide what it is really gating before picking a value. Two things that ARE settled: render duration cannot change WHICH minute is painted, because the target is computed once and threaded through the quote pick, masthead, status file and clear gate; but a stall BEFORE that computation (a hanging weather fetch, say) can push the target into the next minute and leave one unpainted, so "slow is harmless" is true only on the render side.
- **`catalog-count` is stdout-compared, so check its contract directly.** On the
  device: `sudo -u pi /home/pi/litclock/venv/bin/python3 src/eink_display.py
  catalog-count` must print an integer and **exit 0**. Break the bundle
  (`echo '{' > languages/en/strings.json`) and it must print `0` and STILL exit
  0 — a non-zero exit with empty stdout is indistinguishable from a dead
  interpreter, and the gate would have to guess. Restore the bundle afterwards.

### litclock-dev#337 — Location/Weather/Temperature IA (post-design-review, A9-A18)

After litclock-dev#337 lands, the Settings tab IA changes substantially. Replace the pre-litclock-dev#337 expectations with these:

- **Section order top-down**: Location → Weather → Temperature → Advanced. ("Units" renamed to "Temperature.")
- **MODE=specific preserved across reboots (CRITICAL)**: PWA → Location → tap Specific pill → type "Austin, TX" → Save. SSH into Pi, reboot. Verify `WEATHER_LOCATION_NAME` is still "Austin, Texas" (not overwritten by on-boot reresolve). `cat /home/pi/litclock/env.sh | grep WEATHER_LOCATION_MODE` returns `specific`.
- **Cold boot at "moved" location (Automatic mode)**: prefer the deterministic test — `sudo ./scripts/qa-reresolve-hw-test.sh prepare` → `sudo reboot` → `sudo ./scripts/qa-reresolve-hw-test.sh verify` (litclock-dev#549). It injects a synthetic prior country (ZZ) so the country-change branch fires without a VPN, and asserts the boot ordering (resolver started after NetworkManager-wait-online finished) plus the country flip, with distinguishable failure messages. The quick no-reboot variant is `... run`. The old VPN/relocation method still works for a full end-to-end (real tz/city change on the e-ink within ~5 min) but is no longer the QA gate.
- **Worldwide checkbox on US WiFi**: PWA → Location → Specific → type "SW1A 1AA" → tab out → preview shows "Couldn't find that location." Check the worldwide checkbox → preview re-fires automatically → "Buckingham Palace, London, England" appears. Save → reboot → city persists, on-boot reresolve no-ops (MODE=specific).
- **Failed Specific save = zero env trace (A15)**: PWA → Specific → type "Moon" → Save. Verify 422 + red banner + inline "Location not found." + "Moon" retained in input. Close PWA → reopen → Location section shows Automatic mode + Austin, Texas (last persisted state — no "Moon" anywhere). Optional: `cat /home/pi/litclock/env.sh` before/after diff is empty.
- **Stale preview dim (A14)**: PWA → Specific → type "Mumbai" → tab out → Currently sublabel shows "Mumbai, India" (no `is-stale` class). Type "x" (so input becomes "Mumbaix") → Currently sublabel gets dimmed (visible `is-stale` class). Tab out again → Currently re-resolves and dim clears.
- **Advanced auto-expand (A17)**: PWA → Specific → open `<details>Advanced` → type lat=28.62, lon=77.22 → Save. Reload page. Advanced section is OPEN by default (because `WEATHER_LOCATION_NAME` is empty + lat/lon populated); Currently sublabel shows the coords; Place input is empty.
- **Advanced overrides Place (A17)**: PWA → Specific → Place="Tokyo" + Advanced lat=28.62, lon=77.22 → Save. Verify env.sh holds 28.62/77.22 (Advanced won) and `WEATHER_LOCATION_NAME` is empty.
- **Pill switch to Auto clears Advanced (A17)**: PWA → Specific → type Advanced lat/lon → tap Automatic pill. Verify Advanced lat/lon inputs blank visually (form-state only, no save yet).
- **Save disabled on Specific + empty Place (A10 + A14)**: PWA → Specific → leave Place input empty + Advanced empty → Save button is grayed with tooltip "Type a place or pick Automatic." Type any character → Save enables.
- **Temperature auto-save (A13)**: PWA → Temperature → tap Celsius. Verify `cat /home/pi/litclock/env.sh | grep WEATHER_UNITS` returns `metric` within ~1s (no Save tap). Reload PWA. Celsius is still selected.
- **Country-change UNITS auto-flip (A16)**: Pi in US (WEATHER_IP_COUNTRY=US, WEATHER_UNITS=imperial). PWA → Temperature → manually pick Celsius (WEATHER_UNITS=metric). Reboot. On-boot reresolve detects same country (US still) → UNITS preserved as metric. Now VPN to UK + reboot → on-boot reresolve detects country change US→GB → WEATHER_UNITS auto-flips to metric (already was).
- **Browser-tz fallback (A18)**: Block ip-api.com (firewall rule on test Pi) → reboot → wait for first-boot to complete with empty location → open PWA. Verify Location section shows "Couldn't detect location. [Use my browser's timezone (America/Chicago)] — clock will work, weather stays off." Tap the link. Verify `timedatectl` reports America/Chicago (or whichever your browser detected); quotes resume painting at correct local time within ~1 minute.
- **Gift mode → fresh-flash recipient (A3)**: `sudo ./scripts/reset-setup.sh --gift-mode` on Pi A → SD card to Pi B (or reflash Pi A) → connect to new WiFi → verify first-boot resolver writes `WEATHER_LOCATION_MODE=auto` + `WEATHER_IP_COUNTRY` populated + UNITS matches recipient's country (regardless of what the gifter had).
- **No Clear button anywhere**: PWA → Location section (any state). Verify no "Clear location" button renders. (Per A10 — Automatic pill IS the reset.)
- **No-JS form fallback still works**: Disable JavaScript in browser (Safari → Advanced → "Show Develop menu" → Develop → Disable JavaScript). Reload Settings tab. All forms should still POST; Save buttons render for every section (including Weather and Temperature whose JS-enabled flow is auto-save). Server-side validation (A14) backstops the empty-Specific-Place case with a 422 + "Type a place or pick Automatic." inline error.

**Quick way to re-test first-boot:**

```
sudo rm -f /etc/litclock/.setup-complete /etc/litclock/.handoff-complete && sudo systemctl enable litclock-firstboot.service && sudo reboot
```

(Clear `.handoff-complete` too, or the post-WiFi handoff splash is skipped and quotes start immediately — PR2 litclock-dev#388.)

## Design System (Control PWA)

Always read `DESIGN.md` before making any visual or UI decision in the Control PWA workstream. (`DESIGN.md` is maintainer-local and gitignored — excluded from the public repo per the litclock-dev#82 PII decision, along with `PRD-`/`PLAN-LitClock-Control-PWA.md`.) All font choices, colors, spacing, motion, and aesthetic direction are defined there. Do not deviate without explicit user approval.

In code review and `/design-review` mode, flag any code that doesn't match `DESIGN.md`. The hardware-side e-ink rendering (`image-gen/quote_to_image.php`) is governed by its own constraints and is NOT subject to `DESIGN.md`.
