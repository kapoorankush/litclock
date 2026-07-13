---
name: Bug report
about: Report a problem with the clock
labels: bug
---

## Describe the bug

A clear description of what's going wrong.

## Steps to reproduce

1. ...
2. ...
3. ...

## Expected behavior

What should happen instead.

## Hardware

- **Pi model**: (e.g., Pi Zero 2 W)
- **Display**: (e.g., Waveshare 7.5" V2)
- **OS**: (output of `cat /etc/os-release | head -2`)
- **Installation method**: (DIY install / pre-configured SD card)

## Logs / diagnostics

**Easiest — no shell needed:** open the Control PWA (`http://litclock.local`, or the
IP shown on the e-ink) → **Diagnostics** tab → tap **Copy support payload** (bottom of
the tab) and paste it here (or attach a screenshot). It includes version, render status,
WiFi/weather, error flags, and recent log tails, with secrets redacted.

**If you already have a shell** (SSH ships off — enable it from the console first;
see the [Recovery guide](https://github.com/kapoorankush/litclock/blob/master/docs/recovery.md)):

```
journalctl -u litclock.service --since today
journalctl -u litclock-firstboot.service --since today
```
