"""Entry point.

Usage:
  python -m app.main                            # GUI (dashboard + tray), bot on a background thread
  python -m app.main --profile dayz             # GUI, load that bot profile
  python -m app.main --console                  # console fallback: profile menu, then the bot
  python -m app.main --console --profile dayz   # console, skip the menu
  python -m app.main --reset-token              # forget the saved bot token and ask again
"""
from __future__ import annotations

import argparse
import logging
import queue
import sys

import discord

from app.bot import AFKBot
from app.branding import APP_NAME, SUBTITLE, banner
from app.config import (RuntimeState, base_dir, delete_token, ensure_first_run_files, get_token, load_config,
                        save_config)
from app.memory import MemoryStore
from app.profiles import list_profile_names, load_profile, profile_path
from app.startup import choose_profile, describe_token, ensure_token, health_check


def setup_logging() -> None:
    log_dir = base_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handlers: list[logging.Handler] = [logging.FileHandler(log_dir / "bot.log", encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # hides "voice will NOT be supported"
    logging.getLogger("httpx").setLevel(logging.WARNING)         # one INFO line per Ollama request


def memory_path():
    return base_dir() / "data" / "memory.db"


class _LazyErrorFile:
    """Opens logs/errors.log only when something is actually written."""

    def __init__(self, path):
        self.path, self._f = path, None

    def write(self, s: str) -> int:
        if s and self._f is None:
            try:
                self.path.parent.mkdir(exist_ok=True)
                self._f = open(self.path, "a", encoding="utf-8", buffering=1)
            except OSError:
                return len(s)
        return self._f.write(s) if self._f else len(s)

    def flush(self) -> None:
        if self._f:
            self._f.flush()


def _windowed_stderr() -> None:
    """pythonw / the windowed exe have no stderr: send stray tracebacks to logs/errors.log."""
    if sys.stderr is None:
        sys.stderr = _LazyErrorFile(base_dir() / "logs" / "errors.log")


def main() -> int:
    _windowed_stderr()
    ap = argparse.ArgumentParser(description=f"{APP_NAME}: {SUBTITLE}")
    ap.add_argument("--console", action="store_true", help="run without the GUI (console startup flow)")
    ap.add_argument("--profile", help="bot profile name to load (skips the console menu)")
    ap.add_argument("--reset-token", action="store_true",
                    help="delete the saved bot token from Credential Manager and ask for it again")
    args = ap.parse_args()

    setup_logging()
    log = logging.getLogger("main")
    log.info("===== %s =====", banner())
    try:
        ensure_first_run_files()
    except OSError as e:
        log.error("Could not create first-run files: %s", e)
    try:
        cfg = load_config()
    except Exception as e:
        log.error("config.yaml error: %s", e)
        return 1
    if args.console and sys.stdout is None:  # windowed exe: there is no console to talk to
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, "Console mode needs a console, which DisCortex.exe doesn't have.\n\n"
                                      "Use start-console.bat to watch the log, or run from source:\n"
                                      ".venv\\Scripts\\python.exe -m app.main --console")
        root.destroy()
        return 1
    return run_console(args, cfg, log) if args.console else run_gui_mode(args, cfg, log)


# ---- GUI (default) ---------------------------------------------------------------

def run_gui_mode(args, cfg, log) -> int:
    import tkinter as tk
    from tkinter import messagebox

    from app.gui import ask_token_dialog, run_gui
    from app.runner import BotRunner, QueueLogHandler

    try:  # crisp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    events: queue.Queue = queue.Queue()
    logging.getLogger().addHandler(QueueLogHandler(events))

    root = tk.Tk()
    root.withdraw()  # stay hidden until startup checks pass
    title = APP_NAME

    if args.reset_token:
        delete_token()
    token = get_token()
    if not token:
        token = ask_token_dialog(root)
        if not token:
            return 0

    # Pick the startup profile. No profile (or a broken one) still opens the GUI,
    # where the Profiles tab can create or fix one.
    names = list_profile_names()
    name = args.profile or (cfg.last_profile if cfg.last_profile in names else (names[0] if names else None))
    profile = None
    if name:
        if not profile_path(name).exists():
            messagebox.showerror(title, f"No bot profile named '{name}'.", parent=root)
        else:
            try:
                profile = load_profile(name)
            except Exception as e:
                messagebox.showerror(title, f"Bot profile '{name}' could not be loaded:\n\n{e}\n\n"
                                            "Fix it in the Profiles tab or load another one.", parent=root)

    state = RuntimeState(enabled=cfg.enabled)  # spec: starts OFF unless config says otherwise
    if profile:
        cfg.last_profile = profile.name
        save_config(cfg)
        log.info("Bot profile '%s' loaded (%s mode, %s @ %s). Master toggle is %s.", profile.name, profile.mode,
                 profile.model, profile.ollama_url,
                 "ON" if state.enabled else "OFF: click the button to start replying")

    def make_runner(p):
        # read the keyring each time, so Settings > Change token applies on the next (re)connect
        return BotRunner(cfg, p, state, get_token() or token, memory_path(), events)

    if profile is None or not cfg.start_minimized:
        root.deiconify()
    else:
        log.info("Started minimized to the tray. Double-click the tray icon to open the window.")
    run_gui(root, cfg, profile, state, make_runner, events, memory_path())
    return 0


# ---- console fallback -----------------------------------------------------------

def run_console(args, cfg, log) -> int:
    try:
        if args.reset_token:
            removed = delete_token()
            print("Saved token deleted." if removed else "No saved token to delete.")
        token = ensure_token(force_new=args.reset_token)
        if args.profile:
            if not profile_path(args.profile).exists():
                log.error("No bot profile named '%s'", args.profile)
                return 1
            profile = load_profile(args.profile)
        else:
            profile = choose_profile(cfg)
            if profile is None:
                return 0
    except KeyboardInterrupt:
        print()
        return 0
    except Exception as e:
        log.error("Could not load bot profile: %s", e)
        return 1

    cfg.last_profile = profile.name
    save_config(cfg)
    health_check(profile, cfg)

    try:
        memory = MemoryStore(memory_path())
    except Exception as e:
        log.error("Could not open memory database %s: %s", memory_path(), e)
        return 1
    s = memory.stats()
    log.info("Memory: %d messages, %d users, %d facts (%s)", s["messages"], s["users"], s["facts"], memory_path())

    print()
    log.info("Starting bot profile '%s' (Ctrl+C to stop). Console mode has no on/off toggle: the bot is ON.",
             profile.name)
    bot = AFKBot(cfg, profile, RuntimeState(enabled=True), memory)
    try:
        bot.run(token, log_handler=None)  # we configured logging ourselves
    except discord.LoginFailure:
        log.error("Discord rejected the token (%s). Copy a fresh one from Developer Portal > Bot > "
                  "Reset Token, then run with --reset-token.", describe_token(token))
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error("In the Developer Portal (Bot > Privileged Gateway Intents) enable both "
                  "'Message Content Intent' and 'Server Members Intent'.")
        return 1
    finally:
        memory.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
