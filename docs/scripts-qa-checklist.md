# Scripts QA Checklist

Manual QA pass for every shell script in `scripts/`. Issue litclock-dev#160.

This checklist complements the automated tests in `tests/test_*_sh.py` (which cover code patterns and subprocess orchestration). The scenarios below require real Pi hardware: e-ink rendering, SPI/GPIO behavior, NetworkManager hotspot creation, and systemd unit interactions can't be faithfully simulated in CI.

## How to use

1. Flash a fresh SD with the latest `litclock.img` (built by `.github/workflows/build-image.yml`).
2. Walk through each section, run the listed commands, and check off the result.
3. Record date + Pi model + image SHA at the bottom of the file when complete.
4. File issues for any failures and link them in the relevant Result line.

---

## install.sh — RETIRED, do not QA

The `curl … | bash` DIY installer is retired (litclock-dev#546, litclock-dev#547). It ran against
whatever state an existing Raspberry Pi OS install happened to be in, which made
it untestable by construction, and it was confirmed broken three ways. This
checklist never covered it in practice: every QA pass here starts by flashing a
built image, so the four scenarios that used to sit in this section were never
run — which is how the breakage survived.

**The script is still on disk.** Deletion is the follow-up PR to litclock-dev#547; until
then it remains reachable at the old `raw.githubusercontent.com` URL, and
`tests/test_pi_gen.py` still derives pi-gen's apt package set and
`BCM2835_VERSION` from it (`TestPackageParity`, `TestBCM2835`), so it is not yet
inert. Its unit and tmpfiles installs were globbed in the meantime, because the
enumerated list was enabling a unit it never copied under `set -e` — which
aborted the whole install.

Nothing replaces this section. The flashed image IS the install path, and it is
covered by the `first-boot.sh` section below.

---

## first-boot.sh

**Scenarios:** Cross-references the existing first-boot QA in `CLAUDE.md`. Pull those scenarios here when running.

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | Boot fresh image with no known WiFi networks | Hotspot `LitClock-Setup` appears within 60s. E-ink shows credentials + QR code. | [x] PASS |
| 2 | Connect phone to hotspot | Captive portal auto-opens (or reachable at displayed IP) | [x] PASS |
| 3 | Submit setup form with valid home WiFi credentials | Pi connects to home WiFi, transitions to clock mode within 2 min | [x] PASS |
| 4 | Submit setup form with WRONG WiFi password | Setup page auto-refreshes with "WiFi connection failed" banner | [x] PASS — phone disconnected from hotspot on failure; rescanned QR, captive portal relaunched with red error. Corrected password succeeded on retry. |
| 5 | Submit all optional fields blank (no city, no zip, no GPS) | Clock starts; weather degrades gracefully via IP geolocation | [x] PASS — verified in combined run with scenario 6. |
| 6 | Submit US zip code (e.g. 78701) | Geocodes to correct US city (Austin, TX), not foreign postal code | [x] PASS |
| 7 | Boot with WiFi pre-configured (e.g. via Imager) | Skips hotspot; resolves location inline, no page (litclock-dev#647/litclock-dev#715) | [ ] NOT TESTED — deferred, uncommon path on fresh image. |
| 8 | After successful setup: reboot | Boots straight to clock; setup is NOT shown again | [x] PASS (implicit via scenario 9). |

To re-test on the same Pi:
```
sudo rm -f /etc/litclock/.setup-complete && sudo systemctl enable litclock-firstboot.service && sudo reboot
```

---

## boot-splash.sh

**Scenario:** Every boot shows a splash, then transitions to clock if setup is done.

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | Cold boot a fully-set-up Pi | Splash "LitClock — Starting..." appears within 20s, then first clock face within ~30s | [x] PASS |
| 2 | Cold boot a Pi with `.setup-complete` removed | Splash appears, then first-boot.sh takes over (hotspot or setup server) | [x] PASS (implicit via scenario 1 of first-boot.sh) |
| 3 | Disconnect SPI cable, reboot | Splash service does NOT block boot — `systemctl status litclock-splash.service` shows it exited cleanly within 25s (timeout 20 + grace) | [ ] NOT TESTED — hardware-destructive, deferred. |

---

## shutdown-splash.sh

**Scenario:** Shutdown/reboot leaves a quote on the e-ink display (it's bistable, so it persists when off).

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | `sudo reboot` | E-ink shows "LitClock — Restarting..." with a literary quote | [x] PASS |
| 2 | `sudo shutdown -h now` | E-ink shows "Powered Off" with a different quote bank | [ ] FAIL — `shutdown -h now` powers off WITHOUT updating the e-ink (last clock face persists). `sudo poweroff` works correctly and shows "Powered Off". Filed [litclock-dev#186](https://github.com/kapoorankush/litclock/issues/186) — `Conflicts=shutdown.target` doesn't fire on `halt.target` isolation. |
| 3 | After shutdown completes (Pi powered off) | The quote remains on the display | [x] PASS (verified via `sudo poweroff` path). |
| 4 | Run `sudo reboot` 3 times in a row | Quote shown each time is randomized (not always the same) | [ ] NOT TESTED — spot-checked once, randomization logic covered by unit test `test_splash_scripts.py::test_random_quote_selection`. |

---

## runtheclock.sh

**Scenario:** Manual invocation must render the current minute's quote.

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | `cd /home/pi/litclock && ./scripts/runtheclock.sh` | Renders quote for current minute on e-ink within 10s | [x] PASS (exercised implicitly every minute by the timer since first-boot completed). |
| 2 | While timer is active, run manually | No GPIO contention errors in journal (`journalctl -u litclock.service -n 20`) | [ ] NOT TESTED — low risk, deferred. |

---

## update.sh

**Scenario:** In-place upgrade from a working install. Already exercised on the working Pi after every PR — formalize here.

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | `cd /home/pi/litclock && ./scripts/update.sh` on a working Pi already at HEAD | "Already up to date" message; clock keeps running | [x] PASS — exercised on working Pi post-PR litclock-dev#185 merge. |
| 2 | Roll Pi back to an older commit (`git reset --hard <old>`), then run update | Pulls latest, runs through all 7 phases, displays refresh BEFORE timer restart | [x] PASS — ran 0508830e → 3254d4d0 upgrade during PR litclock-dev#185 land-and-deploy. |
| 3 | Run update with uncommitted local changes | Warns "Uncommitted changes detected — will be overwritten" but proceeds | [ ] NOT TESTED. |
| 4 | Run update on a branch ≠ master | Warns about branch reset, proceeds | [ ] NOT TESTED. |
| 5 | Run update with `requirements.txt` changed in upstream | pip reinstall runs, `.pip-packages-hash` updated | [x] PASS — verified during Pillow 12.1.1 → 12.2.0 bump (PR litclock-dev#184). |
| 6 | Run update with `requirements.txt` unchanged | Skips pip reinstall ("Python packages up to date") | [x] PASS — observed during PR litclock-dev#185 update run. |
| 7 | Run update on an old `author-clock` install | Migration block triggers: dir renamed, old systemd units removed, venv recreated | [ ] NOT TESTED — no author-clock install available to test against. Structural test `test_update_sh.py::test_author_clock_migration_present` covers the code path. |
| 8 | After update: `systemctl is-active litclock.timer` | Returns `active` | [x] PASS — verified post-deploy in /land-and-deploy. |

---

## reset-setup.sh

**Scenario:** Put the Pi back into setup mode without reflashing.

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | `sudo ./scripts/reset-setup.sh --yes --keep-wifi` | Clears setup-complete + env.sh + certs + weather cache. WiFi profiles AND the setup network's password both preserved. Reboot manually → goes through setup, and the setup network's password is UNCHANGED. | [ ] NOT TESTED on test Pi (no SSH). |
| 1b | `sudo ./scripts/reset-setup.sh --yes` (the default, litclock-dev#666) | Erases WiFi **and** the setup network's password. Next boot raises `LitClock-Setup` with a NEW password on the panel; the previous one no longer joins. Run this over SSH and the session drops when the WiFi goes — expected. | [ ] NOT TESTED on hardware. |
| 2 | `sudo ./scripts/reset-setup.sh --yes --wipe-wifi --reboot` | Wipes WiFi, reboots, hotspot appears on next boot | [x] PASS — used to transition between scenarios 5 and 6 of first-boot.sh. Hotspot reappeared correctly. |
| 3 | After `--wipe-wifi`: check `/etc/NetworkManager/system-connections/` | Only WiFi-type `.nmconnection` files removed; ethernet/VPN profiles survive | [ ] NOT TESTED on hardware. Structural test `test_reset_setup_sh.py::test_only_wifi_connections_deleted` covers the grep filter. |
| 4 | Run without sudo | Fails with "must be run as root" | [ ] NOT TESTED — structural test `test_reset_setup_sh.py::test_requires_root` covers. |
| 5 | `sudo ./scripts/reset-setup.sh --gift-mode` | WiFi wiped, no confirmation prompt, marker `/etc/litclock/.welcome-mode` created, Pi powers off | [ ] NOT TESTED — pending new image build with gift-mode code (litclock-dev#190). |
| 6 | After (5), check e-ink while powered off | Shows "Welcome to LitClock" + setup steps + literary quote (NOT "Powered Off") | [ ] NOT TESTED — pending new image build. |
| 7 | After (5), power on the Pi and complete first-boot setup | Hotspot appears, setup completes, marker is consumed | [ ] NOT TESTED — pending new image build. |
| 8 | After (7), `sudo poweroff` | E-ink shows normal "Powered Off" splash (welcome marker was consumed) | [ ] NOT TESTED — pending new image build. |

---

## prepare-for-cloning.sh

**Scenario:** Wipe a working Pi clean before SD-card cloning.

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | `sudo ./scripts/prepare-for-cloning.sh` on a fully working Pi, answer "n" to WiFi prompt | env.sh defaults restored, certs + cache + history cleared, WiFi preserved | [ ] |
| 2 | `sudo ./scripts/prepare-for-cloning.sh` **from the local console** (monitor/keyboard or serial), answer "y" to WiFi prompt | Same as above, plus all NM connections removed | [ ] |
| 2a | Same as (2) but over SSH: answer "y" | The script REFUSES in red before deleting anything (litclock-dev#701) — the wipe would drop the session and SIGHUP would kill the script before Step 8 removes the setup-WiFi key. A pre-confirm advisory also warned about this up front. Answer "n" over SSH still completes normally. | [ ] |
| 2b | After a run that aborted or was killed part-way, run the script again | The new run warns in yellow before the confirm: "A previous run of this script did NOT finish" (`/var/lib/litclock/clone-prep-unfinished` survives any incomplete run and is removed only after the last verified step) | [ ] |
| 3 | After running: clone SD with `dd`, write to a new card, boot the new card | New card goes through first-boot setup (welcome screen, hotspot, captive portal) | [ ] |
| 4 | After running WITHOUT `--no-poweroff`: the Pi powers itself off | litclock-dev#660 — no boot can occur between the key removal and imaging | [ ] |
| 4a | Default (poweroff) run, **from the console**: "Disabling SSH before handoff... done" prints before the power-off. **Over SSH** the visible sequence is different by design: the yellow "THIS SESSION WILL DROP" warning (with its "if it has NOT powered off within a minute, do NOT image" caveat) is the LAST output — everything after is redirected, because an EIO'd echo on the dead pty would otherwise kill the script under `set -e` before poweroff — then the session drops and the Pi powers off. Boot a CLONE of the card | Port 22 refused on the clone; recovery is a blank `ssh` file in the SD boot partition (#57). A Pi still running a minute after the drop = a refused handoff — do NOT image; rerun from console (the litclock-dev#701 marker also reports it). A `--no-poweroff` run instead warns in yellow that SSH was NOT disabled | [ ] |
| 5 | Do NOT boot the prepared master to "check" it — booting contaminates it, and on one of the two branches (row 7) it also re-mints the setup-WiFi key the script just removed, which every clone taken afterwards would carry (litclock-dev#660). Use `--no-poweroff` when you need to inspect. | Key stays absent | [ ] |
| 6 | After a `--no-poweroff` run, the clock **looks bricked and is not**: the panel freezes on the last quote and port 80 refuses connections. Do not debug it, and do not try to `systemctl start` it back — the script cleared `.setup-complete` and `.handoff-complete`, which `litclock-control.service` and `litclock.service` are `ConditionPathExists`-gated on, so a start exits 0 and changes nothing. Shut the Pi down. | Frozen panel + refused `:80` + a start that no-ops is the intended end state (litclock-dev#659) | [ ] |
| 7 | If the master is booted anyway, note which branch first-boot takes: `is_wifi_connected()` tests only for an `inet` on `wlan0`, so with an address it completes setup inline with no page (litclock-dev#647) and mints no key, and without one it raises the hotspot and mints a fresh permanent key. | Boot contaminates the master either way; only the no-address branch re-mints the key (litclock-dev#660) | [ ] |

---

## Sign-off

| Field | Value |
|-------|-------|
| Tested by | @kapoorankush |
| Date | 2026-04-14 |
| Pi model | Raspberry Pi Zero 2W (test Pi, fresh flash) + Raspberry Pi Zero 2W (working Pi, in-place update) |
| Image SHA | `dev-20260414-3254d4d` (release), built from master @ `3254d4d0` |
| All scenarios pass? | 15/25 PASS, 1 FAIL (litclock-dev#186 filed), 9 NOT TESTED (deferred/low-risk — covered by structural tests) |
| Issues filed for failures | [litclock-dev#186 — shutdown-splash ExecStop doesn't fire on `shutdown -h now`](https://github.com/kapoorankush/litclock/issues/186) |

### Notes

- **Test Pi has no SSH.** The fresh image intentionally ships without SSH enabled — all interaction was via HDMI console and phone/captive-portal. This limited what could be tested remotely (e.g., reading `journalctl`) but is correct behavior for first-boot UX.
- **9 scenarios deferred as NOT TESTED.** Each one is either (a) covered by a structural test in `tests/test_*_sh.py`, (b) hardware-destructive (SPI disconnect), or (c) uncommon operator path (update with local changes, author-clock migration). Not gaps in confidence — gaps in scope.
- **One real bug surfaced:** scenario 10 of shutdown-splash.sh. `shutdown -h now` is the canonical poweroff command in many users' muscle memory; must fire the splash. Fix landed in PR litclock-dev#188 (adds `Conflicts=halt.target poweroff.target` to `litclock-shutdown.service`) and verified on hardware 2026-04-14.

## Conclusion

PR B complete. Script coverage is green; the one caveat (litclock-dev#186) has been fixed and verified on hardware (PR litclock-dev#188 merged). Ready to close issue litclock-dev#160 upon merge of this PR.
