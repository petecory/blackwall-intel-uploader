# EVE Intel Uploader

A tiny helper that runs alongside EVE Online and mirrors your **intel-channel
chat** to a corp dashboard, building an out-of-game intel board.

It only ever **reads your own local EVE chat-log files** — it does not touch the
game, your account, or ESI, and sends nothing back to EVE. The whole thing is
one short, dependency-free Python file you can read top to bottom.

## Quick start (any OS, with Python 3.12+)

1. **Turn on chat logging in EVE**: `Esc → General Settings → Log Chat to File`.
2. **Get a pairing code**: on the dashboard, open **Live intel → Connect uploader**.
3. **Run it**:
   ```
   python eve_intel_uploader.py
   ```
   It asks for the pairing code, then **lists the chat channels it found in your
   logs** so you can pick which intel ones to watch. Everything saves to
   `eve_intel_uploader.ini` next to the script.

Leave it running while you play — new intel uploads automatically. It never
stores a long-lived secret: pairing swaps a short code for a key that **rotates
itself** against the server.

## Prebuilt binaries (no Python needed)

Grab the latest from the **[Releases](../../releases)** page:

- **Windows** — `eve-intel-uploader.exe`
- **Debian/Ubuntu** — `eve-intel-uploader-debian`
- **RHEL/Rocky/Alma/Fedora** — `eve-intel-uploader-rhel`

On Linux, make it executable and run:
```
chmod +x eve-intel-uploader-*
./eve-intel-uploader-debian     # or the rhel one
```

## Build it yourself

CI (`.github/workflows/build.yml`) builds all three on every `v*` tag. To build
locally:

```
pip install pyinstaller
pyinstaller --onefile eve_intel_uploader.py     # binary lands in dist/
```
PyInstaller is per-OS — build the Windows exe on Windows, the Linux binary on
Linux. The Debian and RHEL binaries differ only by glibc; use the one matching
your distro family.

## Run at startup & console output

```
eve_intel_uploader --install-startup   # launch automatically at login (quiet)
eve_intel_uploader --remove-startup    # undo
eve_intel_uploader --quiet             # errors only
eve_intel_uploader --verbose           # everything (per-poll, rotations)
```
Startup uses your OS's normal mechanism — the Startup folder on Windows, an XDG
autostart entry on Linux — and runs quietly in the background.

## Pointing at a different dashboard

By default it uploads to Blackwall Fortress. To use your own instance, edit the
`base` line in `eve_intel_uploader.ini` after first run.

## Security

- Your key lives only in `eve_intel_uploader.ini` (git-ignored) and **rotates**,
  so a captured key stops working at the next rotation. Pairing codes are
  short-lived and single-use.
- All traffic is HTTPS; the server rate-limits per device.
- Lost a machine? An officer revokes that one device on the dashboard.
