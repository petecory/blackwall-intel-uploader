#!/usr/bin/env python3
"""Blackwall Fortress — EVE intel uploader.

Runs alongside EVE, tails your in-game chat logs for the intel channels you
pick, and uploads new lines to the corp dashboard. It only ever READS your own
local EVE chat-log files — it never touches the game, your account, or ESI.

Double-click it for the window (GUI). Power users can run it headless:

    eve_intel_uploader --cli              terminal mode
    eve_intel_uploader --install-startup  run at login (minimised to tray)
    eve_intel_uploader --remove-startup   undo
    eve_intel_uploader --quiet | --verbose   (cli) console detail
    eve_intel_uploader --help

Pairing swaps a short dashboard code for a key that rotates itself against the
server, so there's no long-lived secret on disk. The GUI needs `pystray` and
`pillow` for the tray icon (bundled in the prebuilt binaries); `--cli` mode is
standard-library only.
"""

from __future__ import annotations

import argparse
import configparser
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://blackwallfortress.space"
POLL_SECONDS = 3
ROTATE_HOURS = 12
STARTUP_NAME = "eve-intel-uploader"
# Config sits next to the exe when frozen, else next to the script.
_HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = Path(_HERE) / "eve_intel_uploader.ini"

_CHAN_RE = re.compile(r"^(.*)_\d{8}_\d{6}_\d+\.txt$")


# --------------------------------------------------------------------------- #
#  Core — UI-agnostic. Both the GUI and the CLI drive these.
# --------------------------------------------------------------------------- #
def chatlog_dir() -> Path:
    return Path(os.path.expanduser("~")) / "Documents" / "EVE" / "logs" / "Chatlogs"


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
    if "main" not in cfg:
        cfg["main"] = {}
    m = cfg["main"]
    m.setdefault("base", DEFAULT_BASE)
    m.setdefault("channels", "")
    m.setdefault("logdir", str(chatlog_dir()))
    m.setdefault("verbosity", "normal")
    m.setdefault("watch_clipboard", "yes")
    m.setdefault("archive_local", "no")
    m.setdefault("local_system", "")
    m.setdefault("only_when_eve_focused", "yes")
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    with open(CONFIG_FILE, "w") as fh:
        cfg.write(fh)


def detect_channels(logdir: Path) -> list[str]:
    """Channel names you've logged, most-recently-active first."""
    seen: dict[str, float] = {}
    for f in glob.glob(str(logdir / "*.txt")):
        m = _CHAN_RE.match(os.path.basename(f))
        if not m:
            continue
        name = m.group(1)
        seen[name] = max(seen.get(name, 0), os.path.getmtime(f))
    return [n for n, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


def latest_log_for(logdir: Path, channel: str) -> Path | None:
    safe = channel.replace(" ", "_")
    matches = sorted(
        glob.glob(str(logdir / f"{safe}_*.txt")) + glob.glob(str(logdir / f"{channel}_*.txt")),
        key=lambda p: os.path.getmtime(p),
    )
    return Path(matches[-1]) if matches else None


def read_new(path: Path, pos: int) -> tuple[list[str], int]:
    with open(path, "rb") as fh:
        fh.seek(pos)
        data = fh.read()
        new_pos = fh.tell()
    text = data.decode("utf-16", errors="ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[")]
    return lines, new_pos


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
        r = _post(f"{base}/api/intel/pair", None, {"code": code.strip()})
        return r.get("key") if r.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def rotate(base: str, key: str) -> str | None:
    try:
        r = _post(f"{base}/api/intel/rotate", key, {})
        return r.get("key") if r.get("ok") else None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return None
        return key
    except Exception:  # noqa: BLE001
        return key


def upload(base: str, key: str, channel: str, lines: list[str]) -> int:
    try:
        r = _post(f"{base}/api/intel/ingest", key, {"channel": channel, "lines": lines})
        return int(r.get("stored", 0))
    except Exception:  # noqa: BLE001
        return -1  # signal a transient failure to the caller


def looks_like_dscan(text: str) -> bool:
    """Cheap client-side check so we only upload d-scan-shaped clipboards."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    tabbed = sum(1 for ln in lines if ln.count("\t") >= 2)
    return tabbed >= max(3, int(len(lines) * 0.6))


def upload_dscan(base: str, key: str, text: str) -> int:
    try:
        r = _post(f"{base}/api/intel/dscan", key, {"text": text})
        return int(r.get("ships", 0)) if r.get("ok") else 0
    except Exception:  # noqa: BLE001
        return 0


def upload_local(base: str, key: str, system: str, lines: list[str]) -> int:
    try:
        r = _post(f"{base}/api/intel/local", key, {"system": system, "lines": lines})
        return int(r.get("stored", 0)) if r.get("ok") else 0
    except Exception:  # noqa: BLE001
        return 0


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'\-]{2,36}$")


def looks_like_member_list(text: str) -> bool:
    """Plain character names, one per line — a Local member-list copy. Distinct
    from d-scan (which has tabs) and from prose (validated server-side too)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or any("\t" in ln for ln in lines):
        return False
    named = sum(1 for ln in lines if _NAME_RE.match(ln) and len(ln.split()) <= 3)
    return named >= 2 and named >= len(lines) * 0.8


def upload_presence(base: str, key: str, system: str, names: list[str]) -> int:
    try:
        r = _post(f"{base}/api/intel/local-presence", key, {"system": system, "names": names})
        return int(r.get("present", 0)) if r.get("ok") else 0
    except Exception:  # noqa: BLE001
        return 0


def looks_like_probe(text: str) -> bool:
    return "Cosmic Anomaly" in text or "Cosmic Signature" in text


def upload_probe(base: str, key: str, system: str, text: str) -> int:
    try:
        r = _post(f"{base}/api/intel/probe", key, {"system": system, "text": text})
        return int(r.get("sites", 0)) if r.get("ok") else 0
    except Exception:  # noqa: BLE001
        return 0


def eve_is_focused() -> bool:
    """True if EVE is the foreground window — so we only read the clipboard when
    you're actually in the game, never while you're copying things elsewhere.
    Fail-open: if we can't tell (no tools/permission), don't block."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            u = ctypes.windll.user32
            hwnd = u.GetForegroundWindow()
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            return "eve" in buf.value.lower()
        if sys.platform == "darwin":
            import subprocess

            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=1)
            return out.returncode == 0 and "eve" in out.stdout.lower()
        import subprocess

        out = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                             capture_output=True, text=True, timeout=1)
        if out.returncode == 0:
            return "eve" in out.stdout.lower()
    except Exception:  # noqa: BLE001
        pass
    return True


def _launch_cmd(extra: str = "") -> str:
    if getattr(sys, "frozen", False):
        base = f'"{sys.executable}"'
    else:
        py = sys.executable
        if sys.platform.startswith("win") and py.lower().endswith("python.exe"):
            py = py[: -len("python.exe")] + "pythonw.exe"
        base = f'"{py}" "{os.path.abspath(__file__)}"'
    return f"{base} {extra}".strip()


def install_startup() -> str:
    cmd = _launch_cmd("--minimized")
    if sys.platform.startswith("win"):
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
        startup.mkdir(parents=True, exist_ok=True)
        entry = startup / f"{STARTUP_NAME}.cmd"
        entry.write_text(f'@echo off\r\nstart "" {cmd}\r\n')
    else:
        autostart = Path(os.path.expanduser("~/.config/autostart"))
        autostart.mkdir(parents=True, exist_ok=True)
        entry = autostart / f"{STARTUP_NAME}.desktop"
        entry.write_text(
            "[Desktop Entry]\nType=Application\nName=EVE Intel Uploader\n"
            f"Exec={cmd}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n"
        )
    return str(entry)


def remove_startup() -> str | None:
    if sys.platform.startswith("win"):
        entry = Path(os.environ.get("APPDATA", "")) / f"Microsoft/Windows/Start Menu/Programs/Startup/{STARTUP_NAME}.cmd"
    else:
        entry = Path(os.path.expanduser(f"~/.config/autostart/{STARTUP_NAME}.desktop"))
    if entry.exists():
        entry.unlink()
        return str(entry)
    return None


def startup_installed() -> bool:
    if sys.platform.startswith("win"):
        return (Path(os.environ.get("APPDATA", "")) / f"Microsoft/Windows/Start Menu/Programs/Startup/{STARTUP_NAME}.cmd").exists()
    return Path(os.path.expanduser(f"~/.config/autostart/{STARTUP_NAME}.desktop")).exists()


# --------------------------------------------------------------------------- #
#  Worker — the poll/upload loop, in a background thread. Reports via callback.
# --------------------------------------------------------------------------- #
class Worker(threading.Thread):
    def __init__(self, cfg: configparser.ConfigParser, on_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.on_event = on_event  # (kind, text); kind in status|upload|error|info
        self._stop = threading.Event()

    @property
    def base(self) -> str:
        return self.cfg["main"].get("base", DEFAULT_BASE)

    def channels(self) -> list[str]:
        return [c.strip() for c in self.cfg["main"].get("channels", "").split(",") if c.strip()]

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        key = self.cfg["main"].get("key", "")
        if not key:
            self.on_event("error", "Not paired yet.")
            return
        rk = rotate(self.base, key)
        if rk is None:
            self.on_event("reauth", "Key no longer valid — re-pair.")
            return
        key = rk
        self.cfg["main"]["key"] = key
        save_config(self.cfg)
        self.on_event("status", "Connected")
        logdir = Path(self.cfg["main"].get("logdir") or chatlog_dir())
        state: dict[str, tuple[str, int]] = {}
        for ch in self.channels():
            f = latest_log_for(logdir, ch)
            state[ch] = (str(f), f.stat().st_size) if f else ("", 0)
        lf = latest_log_for(logdir, "Local")
        local_state = (str(lf), lf.stat().st_size) if lf else ("", 0)
        last_rotate = time.time()
        while not self._stop.is_set():
            if time.time() - last_rotate > ROTATE_HOURS * 3600:
                nk = rotate(self.base, key)
                if nk is None:
                    self.on_event("reauth", "Key no longer valid — re-pair.")
                    return
                key = nk
                self.cfg["main"]["key"] = key
                save_config(self.cfg)
                self.on_event("info", "Key rotated")
                last_rotate = time.time()
            for ch in self.channels():
                f = latest_log_for(logdir, ch)
                if f is None:
                    continue
                cur_path, cur_pos = state.get(ch, ("", 0))
                if str(f) != cur_path:
                    cur_path, cur_pos = str(f), 0
                try:
                    lines, new_pos = read_new(f, cur_pos)
                except OSError:
                    continue
                state[ch] = (str(f), new_pos)
                if lines:
                    n = upload(self.base, key, ch, lines)
                    if n > 0:
                        self.on_event("upload", f"[{ch}] {n} line(s)")
                    elif n < 0:
                        self.on_event("error", f"[{ch}] upload failed (retrying)")
            # Archive the home system's Local chat, if switched on.
            if self.cfg["main"].get("archive_local") == "yes" and self.cfg["main"].get("local_system"):
                lf = latest_log_for(logdir, "Local")
                if lf is not None:
                    lp, lpos = local_state
                    if str(lf) != lp:
                        lp, lpos = str(lf), 0
                    try:
                        llines, lnew = read_new(lf, lpos)
                        local_state = (str(lf), lnew)
                        if llines:
                            m = upload_local(self.base, key, self.cfg["main"]["local_system"], llines)
                            if m > 0:
                                self.on_event("upload", f"[Local {self.cfg['main']['local_system']}] {m} line(s)")
                    except OSError:
                        pass
            self._stop.wait(POLL_SECONDS)
        self.on_event("status", "Stopped")


# --------------------------------------------------------------------------- #
#  CLI frontend
# --------------------------------------------------------------------------- #
_LEVELS = {"quiet": 0, "normal": 1, "verbose": 2}


def run_cli(cfg: configparser.ConfigParser, verbosity: str) -> None:
    m = cfg["main"]
    if not m.get("key"):
        code = input("Enter the pairing code from the dashboard (Live intel → Connect): ").strip()
        key = pair(m.get("base", DEFAULT_BASE), code)
        if not key:
            print("Pairing failed — code wrong or expired.")
            sys.exit(1)
        m["key"] = key
        print("Paired!")
    if not m.get("channels"):
        chans = detect_channels(Path(m.get("logdir") or chatlog_dir()))
        if chans:
            print("\nChannels found in your logs:")
            for i, c in enumerate(chans, 1):
                print(f"  {i:2}. {c.replace('_', ' ')}")
            raw = input("Numbers to watch (e.g. 1,3,5): ").strip()
            picked = [chans[int(p) - 1] for p in raw.replace(" ", "").split(",")
                      if p.isdigit() and 1 <= int(p) <= len(chans)]
            m["channels"] = ", ".join(picked) if picked else raw
        else:
            m["channels"] = input("Channel names (comma-separated): ").strip()
    save_config(cfg)

    lvl = _LEVELS.get(verbosity, 1)

    def show(kind: str, text: str) -> None:
        want = {"error": 0, "status": 0, "upload": 1, "info": 2}.get(kind, 1)
        if want <= lvl:
            tag = {"upload": "↑", "error": "!", "status": "•", "info": "·"}.get(kind, " ")
            print(f"  {tag} {text}")

    reauth = {"needed": False}

    def cli_event(kind, text):
        if kind == "reauth":
            reauth["needed"] = True
        show(kind, text)

    print(f"Uploader running — watching {m['channels']}. Ctrl-C to stop.")
    while True:
        w = Worker(cfg, cli_event)
        w.start()
        try:
            while w.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopping…")
            w.stop(); w.join(timeout=5)
            return
        if not reauth["needed"]:
            return
        # Key was rejected — re-pair inline rather than making them edit the ini.
        reauth["needed"] = False
        code = input("\nKey rejected. Enter a new pairing code (or Ctrl-C to quit): ").strip()
        key = pair(m.get("base", DEFAULT_BASE), code)
        if not key:
            print("Pairing failed."); return
        m["key"] = key; save_config(cfg); print("Re-paired — resuming.")


# --------------------------------------------------------------------------- #
#  GUI frontend (tkinter + optional pystray tray)
# --------------------------------------------------------------------------- #
def run_gui(cfg: configparser.ConfigParser, minimized: bool = False) -> None:
    import queue
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    events: "queue.Queue[tuple[str, str]]" = queue.Queue()
    worker: list[Worker | None] = [None]

    root = tk.Tk()
    root.title("EVE Intel Uploader")
    root.geometry("560x520")
    root.configure(bg="#0f172a")

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background="#0f172a", foreground="#e2e8f0", fieldbackground="#0b1220")
    style.configure("TCheckbutton", background="#0f172a", foreground="#cbd5e1")
    style.configure("TButton", background="#4f46e5", foreground="#ffffff")
    style.map("TButton", background=[("active", "#6366f1")])

    top = tk.Frame(root, bg="#0f172a")
    top.pack(fill="x", padx=14, pady=(12, 4))
    status_dot = tk.Label(top, text="●", fg="#64748b", bg="#0f172a", font=("Segoe UI", 14))
    status_dot.pack(side="left")
    status_lbl = tk.Label(top, text="Not paired", fg="#cbd5e1", bg="#0f172a", font=("Segoe UI", 11))
    status_lbl.pack(side="left", padx=6)
    ttk.Button(top, text="Re-pair", command=lambda: do_reauth()).pack(side="right")

    body = tk.Frame(root, bg="#0f172a")
    body.pack(fill="both", expand=True, padx=14, pady=6)

    # pairing (shown only when unpaired)
    pair_frame = tk.Frame(body, bg="#0f172a")
    tk.Label(pair_frame, text="Pairing code (dashboard → Live intel → Connect):",
             fg="#cbd5e1", bg="#0f172a").pack(anchor="w")
    code_var = tk.StringVar()
    prow = tk.Frame(pair_frame, bg="#0f172a")
    prow.pack(fill="x", pady=4)
    code_entry = tk.Entry(prow, textvariable=code_var, bg="#0b1220", fg="#e2e8f0",
                          insertbackground="#e2e8f0", relief="flat", font=("Consolas", 13))
    code_entry.pack(side="left", fill="x", expand=True, ipady=4)

    # channels
    chan_frame = tk.LabelFrame(body, text=" Intel channels ", fg="#94a3b8", bg="#0f172a", labelanchor="nw")
    chan_vars: dict[str, "tk.BooleanVar"] = {}
    chan_inner = tk.Frame(chan_frame, bg="#0f172a")
    chan_inner.pack(fill="x", padx=6, pady=4)

    # activity log
    log_frame = tk.LabelFrame(body, text=" Activity ", fg="#94a3b8", bg="#0f172a", labelanchor="nw")
    log = scrolledtext.ScrolledText(log_frame, height=8, bg="#0b1220", fg="#cbd5e1",
                                    relief="flat", font=("Consolas", 9), state="disabled")
    log.pack(fill="both", expand=True, padx=6, pady=6)
    log.tag_config("upload", foreground="#34d399")
    log.tag_config("error", foreground="#f87171")
    log.tag_config("status", foreground="#818cf8")
    log.tag_config("info", foreground="#64748b")

    def append(kind: str, text: str) -> None:
        log.configure(state="normal")
        log.insert("end", f"{time.strftime('%H:%M')}  {text}\n", kind)
        if int(log.index("end-1c").split(".")[0]) > 300:
            log.delete("1.0", "100.0")
        log.see("end")
        log.configure(state="disabled")

    def save_channels() -> None:
        cfg["main"]["channels"] = ", ".join(c for c, v in chan_vars.items() if v.get())
        save_config(cfg)

    def build_channels() -> None:
        for w in chan_inner.winfo_children():
            w.destroy()
        chan_vars.clear()
        logdir = Path(cfg["main"].get("logdir") or chatlog_dir())
        found = detect_channels(logdir)
        watched = {c.strip() for c in cfg["main"].get("channels", "").split(",") if c.strip()}
        names = list(dict.fromkeys(found + list(watched)))
        if not names:
            tk.Label(chan_inner, text="No chat logs found yet — open your intel channels in EVE "
                     "with logging on, then Refresh.", fg="#64748b", bg="#0f172a",
                     wraplength=480, justify="left").pack(anchor="w")
            return
        for i, name in enumerate(names):
            var = tk.BooleanVar(value=name in watched)
            chan_vars[name] = var
            ttk.Checkbutton(chan_inner, text=name.replace("_", " "), variable=var,
                            command=save_channels).grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)

    ttk.Button(chan_frame, text="Refresh channels", command=build_channels).pack(anchor="e", padx=6, pady=(0, 6))

    bottom = tk.Frame(root, bg="#0f172a")
    bottom.pack(fill="x", padx=14, pady=(4, 12))
    startup_var = tk.BooleanVar(value=startup_installed())

    def toggle_startup() -> None:
        if startup_var.get():
            append("info", "Added to startup")
            install_startup()
        else:
            remove_startup()
            append("info", "Removed from startup")

    ttk.Checkbutton(bottom, text="Start with system", variable=startup_var,
                    command=toggle_startup).pack(side="left")

    watch_var = tk.BooleanVar(value=cfg["main"].get("watch_clipboard", "yes") == "yes")

    def toggle_watch() -> None:
        cfg["main"]["watch_clipboard"] = "yes" if watch_var.get() else "no"
        save_config(cfg)

    ttk.Checkbutton(bottom, text="Watch clipboard (d-scan + local)", variable=watch_var,
                    command=toggle_watch).pack(side="left", padx=(12, 0))
    focus_var = tk.BooleanVar(value=cfg["main"].get("only_when_eve_focused", "yes") == "yes")

    def toggle_focus() -> None:
        cfg["main"]["only_when_eve_focused"] = "yes" if focus_var.get() else "no"
        save_config(cfg)

    ttk.Checkbutton(bottom, text="only when EVE focused", variable=focus_var,
                    command=toggle_focus).pack(side="left", padx=(6, 0))

    local_row = tk.Frame(root, bg="#0f172a")
    local_row.pack(fill="x", padx=14, pady=(0, 10))
    local_var = tk.BooleanVar(value=cfg["main"].get("archive_local", "no") == "yes")
    local_sys = tk.StringVar(value=cfg["main"].get("local_system", ""))

    def save_local() -> None:
        cfg["main"]["archive_local"] = "yes" if local_var.get() else "no"
        cfg["main"]["local_system"] = local_sys.get().strip()
        save_config(cfg)

    ttk.Checkbutton(local_row, text="Archive Local chat (parked alt) — system:",
                    variable=local_var, command=save_local).pack(side="left")
    le = tk.Entry(local_row, textvariable=local_sys, width=10, bg="#0b1220", fg="#e2e8f0",
                  insertbackground="#e2e8f0", relief="flat")
    le.pack(side="left", padx=(4, 0), ipady=2)
    le.bind("<FocusOut>", lambda e: save_local())

    def set_status(text: str) -> None:
        colors = {"Connected": "#22c55e", "Stopped": "#64748b", "Connecting…": "#f59e0b"}
        status_dot.config(fg=colors.get(text, "#f59e0b"))
        status_lbl.config(text=text)

    def start_worker() -> None:
        if worker[0] and worker[0].is_alive():
            return
        if not cfg["main"].get("key"):
            return
        w = Worker(cfg, lambda kind, text: events.put((kind, text)))
        worker[0] = w
        w.start()

    def refresh_paired() -> None:
        paired = bool(cfg["main"].get("key"))
        for w in (pair_frame, chan_frame, log_frame):
            w.pack_forget()
        if paired:
            build_channels()
            chan_frame.pack(fill="x", pady=(4, 6))
            log_frame.pack(fill="both", expand=True, pady=(4, 0))
            set_status("Connecting…")
        else:
            pair_frame.pack(fill="x", pady=6)
            set_status("Not paired")

    def do_pair() -> None:
        key = pair(cfg["main"].get("base", DEFAULT_BASE), code_var.get())
        if key:
            cfg["main"]["key"] = key
            save_config(cfg)
            refresh_paired()
            append("status", "Paired")
            start_worker()
        else:
            append("error", "Pairing failed — code wrong or expired")

    ttk.Button(prow, text="Pair", command=do_pair).pack(side="left", padx=(6, 0))

    def do_reauth() -> None:
        cfg["main"]["key"] = ""
        save_config(cfg)
        refresh_paired()
        set_status("Re-pair needed")
        append("error", "Enter a new pairing code from the dashboard.")

    def pump() -> None:
        try:
            while True:
                kind, text = events.get_nowait()
                if kind == "status":
                    set_status(text)
                elif kind == "reauth":
                    append("error", text)
                    do_reauth()
                    continue
                append(kind, text)
        except Exception:  # noqa: BLE001 - queue empty
            pass
        root.after(250, pump)

    last_clip = [""]

    def watch_clipboard() -> None:
        # Reads the clipboard only when the box is ticked AND (by default) EVE is
        # the focused window — so nothing you copy elsewhere is ever looked at.
        gate = cfg["main"].get("only_when_eve_focused", "yes") == "yes"
        if watch_var.get() and (not gate or eve_is_focused()):
            try:
                text = root.clipboard_get()
            except Exception:  # noqa: BLE001 - empty / non-text clipboard
                text = ""
            base = cfg["main"].get("base", DEFAULT_BASE)
            key = cfg["main"].get("key", "")
            parked = cfg["main"].get("local_system", "") if cfg["main"].get("archive_local") == "yes" else ""
            if text and text != last_clip[0] and key:
                if looks_like_probe(text) and parked:
                    last_clip[0] = text
                    n = upload_probe(base, key, parked, text)
                    if n > 0:
                        append("upload", f"probe: {n} sites in {parked}")
                elif looks_like_dscan(text):
                    last_clip[0] = text
                    n = upload_dscan(base, key, text)
                    if n > 0:
                        append("upload", f"d-scan: {n} ships")
                elif parked and looks_like_member_list(text):
                    last_clip[0] = text
                    names = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    m = upload_presence(base, key, parked, names)
                    if m > 0:
                        append("upload", f"local: {m} in {parked}")
        root.after(1500, watch_clipboard)

    # system tray (optional — needs pystray + pillow)
    tray = {"icon": None}

    def make_tray():
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:  # noqa: BLE001
            return None
        img = Image.new("RGB", (64, 64), "#0f172a")
        d = ImageDraw.Draw(img)
        d.ellipse((16, 16, 48, 48), fill="#4f46e5")

        def show(icon=None, item=None):
            root.after(0, root.deiconify)

        def quit_all(icon=None, item=None):
            if worker[0]:
                worker[0].stop()
            if tray["icon"]:
                tray["icon"].stop()
            root.after(0, root.destroy)

        menu = pystray.Menu(pystray.MenuItem("Show", show, default=True),
                            pystray.MenuItem("Quit", quit_all))
        return pystray.Icon(STARTUP_NAME, img, "EVE Intel Uploader", menu)

    def hide_to_tray() -> None:
        if tray["icon"] is None:
            tray["icon"] = make_tray()
            if tray["icon"] is not None:
                threading.Thread(target=tray["icon"].run, daemon=True).start()
        if tray["icon"] is not None:
            root.withdraw()
        else:
            root.iconify()  # no tray lib → just minimise

    root.protocol("WM_DELETE_WINDOW", hide_to_tray)

    refresh_paired()
    start_worker()
    root.after(250, pump)
    root.after(1500, watch_clipboard)
    if minimized:
        root.after(200, hide_to_tray)
    root.mainloop()


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(prog="eve_intel_uploader",
                                 description="Upload EVE intel-channel chat to the corp dashboard.")
    ap.add_argument("--cli", action="store_true", help="run in the terminal instead of a window")
    ap.add_argument("--minimized", action="store_true", help="(gui) start hidden in the system tray")
    ap.add_argument("--quiet", action="store_true", help="(cli) errors only")
    ap.add_argument("--verbose", action="store_true", help="(cli) show everything")
    ap.add_argument("--install-startup", action="store_true", help="run automatically at login")
    ap.add_argument("--remove-startup", action="store_true", help="undo --install-startup")
    args = ap.parse_args()

    if args.install_startup:
        print("Installed at:", install_startup())
        return
    if args.remove_startup:
        where = remove_startup()
        print("Removed:", where if where else "(nothing installed)")
        return

    cfg = load_config()
    if args.cli:
        verbosity = "quiet" if args.quiet else "verbose" if args.verbose else cfg["main"].get("verbosity", "normal")
        run_cli(cfg, verbosity)
    else:
        try:
            run_gui(cfg, minimized=args.minimized)
        except Exception as exc:  # noqa: BLE001 - no display / tkinter missing → fall back
            print(f"GUI unavailable ({exc}); falling back to --cli.")
            run_cli(cfg, cfg["main"].get("verbosity", "normal"))


if __name__ == "__main__":
    main()
