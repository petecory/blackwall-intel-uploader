#!/usr/bin/env python3
"""Blackwall Fortress — EVE intel uploader.

Runs alongside EVE, tails your in-game chat logs for the intel channels you
name, and uploads new lines to the corp dashboard. It only ever READS your own
local EVE chat-log files — it does not touch the game, your account, or ESI.

Usage:
    python eve_intel_uploader.py

First run asks for your upload token (get it from the dashboard) and which
channels to watch, and saves them to eve_intel_uploader.ini next to this file.
"""

from __future__ import annotations

import configparser
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://blackwallfortress.space"
POLL_SECONDS = 3
ROTATE_HOURS = 12
CONFIG_FILE = Path(__file__).with_name("eve_intel_uploader.ini")


def chatlog_dir() -> Path:
    # EVE's default log location per OS.
    if sys.platform.startswith("win"):
        return Path(os.path.expanduser("~")) / "Documents" / "EVE" / "logs" / "Chatlogs"
    if sys.platform == "darwin":
        return Path(os.path.expanduser("~")) / "Documents" / "EVE" / "logs" / "Chatlogs"
    return Path(os.path.expanduser("~")) / "Documents" / "EVE" / "logs" / "Chatlogs"


_CHAN_RE = re.compile(r"^(.*)_\d{8}_\d{6}_\d+\.txt$")


def detect_channels(logdir: Path) -> list[str]:
    """Channel names you've logged, newest-active first, from the log filenames."""
    seen: dict[str, float] = {}
    for f in glob.glob(str(logdir / "*.txt")):
        m = _CHAN_RE.match(os.path.basename(f))
        if not m:
            continue
        name = m.group(1)
        seen[name] = max(seen.get(name, 0), os.path.getmtime(f))
    return [n for n, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


def pick_channels(logdir: Path) -> str:
    """Let the user tick which detected channels to watch."""
    chans = detect_channels(logdir)
    if not chans:
        print("No chat logs found yet. Open your intel channels in EVE with logging on,")
        print("then type the channel names here, comma-separated:")
        return input("Channels: ").strip()
    print("\nChat channels found in your logs:")
    for i, c in enumerate(chans, 1):
        print(f"  {i:2}. {c.replace('_', ' ')}")
    print("Enter the numbers of the intel channels to watch (e.g. 1,3,5):")
    raw = input("> ").strip()
    picked = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit() and 1 <= int(part) <= len(chans):
            picked.append(chans[int(part) - 1])
    if not picked:  # fall back to typed names
        return raw
    print("Watching:", ", ".join(c.replace("_", " ") for c in picked))
    return ", ".join(picked)


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
    if "main" not in cfg:
        cfg["main"] = {}
    m = cfg["main"]
    if not m.get("base"):
        # Point at your own instance here if you're not on Blackwall Fortress.
        m["base"] = DEFAULT_BASE
    if not m.get("key"):
        # Pair this device: type the short code from the dashboard.
        code = input("Enter the pairing code from the dashboard (Live intel → Connect): ").strip()
        key = pair(m["base"], code)
        if not key:
            print("Pairing failed — code wrong or expired. Get a fresh one and try again.")
            sys.exit(1)
        m["key"] = key
        print("Paired!")
    if not m.get("channels"):
        m["channels"] = pick_channels(Path(m.get("logdir") or chatlog_dir()))
    if not m.get("logdir"):
        m["logdir"] = str(chatlog_dir())
    with open(CONFIG_FILE, "w") as fh:
        cfg.write(fh)
    return cfg


def _post(url: str, token: str | None, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def pair(base: str, code: str) -> str | None:
    try:
        r = _post(f"{base}/api/intel/pair", None, {"code": code})
        return r.get("key") if r.get("ok") else None
    except Exception as exc:  # noqa: BLE001
        print(f"  ! pair failed: {exc}")
        return None


def rotate(base: str, key: str) -> str | None:
    """Swap the current key for a fresh one; the old one dies server-side."""
    try:
        r = _post(f"{base}/api/intel/rotate", key, {})
        return r.get("key") if r.get("ok") else None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return None  # key no longer valid — needs re-pairing
        raise
    except Exception:  # noqa: BLE001
        return key  # transient; keep the current key


def latest_log_for(logdir: Path, channel: str) -> Path | None:
    # EVE names files "Channel Name_YYYYMMDD_HHMMSS_charid.txt"; pick the newest.
    safe = channel.replace(" ", "_")
    matches = sorted(
        glob.glob(str(logdir / f"{safe}_*.txt")) + glob.glob(str(logdir / f"{channel}_*.txt")),
        key=lambda p: os.path.getmtime(p),
    )
    return Path(matches[-1]) if matches else None


def read_new(path: Path, pos: int) -> tuple[list[str], int]:
    # EVE chat logs are UTF-16. Read from the last byte position.
    with open(path, "rb") as fh:
        fh.seek(pos)
        data = fh.read()
        new_pos = fh.tell()
    text = data.decode("utf-16", errors="ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[")]
    return lines, new_pos


def upload(base: str, key: str, channel: str, lines: list[str]) -> int:
    try:
        r = _post(f"{base}/api/intel/ingest", key, {"channel": channel, "lines": lines})
        return r.get("stored", 0)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! upload failed: {exc}")
        return 0


def main() -> None:
    cfg = load_config()
    base = cfg["main"].get("base", DEFAULT_BASE)
    key = cfg["main"]["key"]
    channels = [c.strip() for c in cfg["main"]["channels"].split(",") if c.strip()]

    def save_key(new_key: str) -> None:
        cfg["main"]["key"] = new_key
        with open(CONFIG_FILE, "w") as fh:
            cfg.write(fh)

    # Rotate on startup — a fresh key each session limits how long a leaked one
    # from disk stays useful.
    rk = rotate(base, key)
    if rk is None:
        print("!! Stored key rejected. Re-pair: delete the 'key' line in "
              "eve_intel_uploader.ini and run again with a new code.")
        sys.exit(1)
    key = rk
    save_key(key)
    last_rotate = time.time()
    logdir = Path(cfg["main"]["logdir"])
    print(f"Intel uploader — watching {channels}")
    print(f"Log dir: {logdir}")
    if not logdir.exists():
        print("!! Log directory not found. Enable chat logging in EVE (Esc → General → Log Chat to File),")
        print("   or fix 'logdir' in eve_intel_uploader.ini.")
    # Track file + read position per channel. Start at end of the current file so
    # we only send new lines from launch onward.
    state: dict[str, tuple[str, int]] = {}
    for ch in channels:
        f = latest_log_for(logdir, ch)
        state[ch] = (str(f), f.stat().st_size) if f else ("", 0)
    print("Watching for new intel… (Ctrl-C to stop)\n")
    while True:
        if time.time() - last_rotate > ROTATE_HOURS * 3600:
            rk = rotate(base, key)
            if rk:
                key = rk
                save_key(key)
            last_rotate = time.time()
        for ch in channels:
            f = latest_log_for(logdir, ch)
            if f is None:
                continue
            cur_path, cur_pos = state.get(ch, ("", 0))
            if str(f) != cur_path:  # EVE rolled to a new log file (relog)
                cur_path, cur_pos = str(f), 0
            try:
                lines, new_pos = read_new(f, cur_pos)
            except OSError:
                continue
            state[ch] = (str(f), new_pos)
            if lines:
                n = upload(base, key, ch, lines)
                if n:
                    print(f"  [{ch}] uploaded {n} new line(s)")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
