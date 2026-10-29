"""Control window (dashboard + live log) and tray icon.

Threading: tkinter owns the main thread. The bot runs in BotRunner's thread and
reaches us only through the `events` queue; the tray runs in pystray's thread and
reaches us only through `self._cmds`. Both queues are drained by root.after().
"""
from __future__ import annotations

import collections
import logging
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from pathlib import Path
from typing import Callable

from app import branding as B
from app import profiles as P
from app.config import Config, RuntimeState, base_dir, save_config, set_token
from app.hotkeys import HotkeyManager, pretty
from app.profile_editor import ProfileEditor
from app.profiles import Profile
from app.runner import BotRunner

log = logging.getLogger(__name__)

POLL_MS = 100
LOG_LINES = 200
FONT = ("Segoe UI", 10)
MONO = ("Consolas", 10)

GREEN, GREY, RED, AMBER = "#2e9d4f", "#8a8f98", "#c0392b", "#d68910"

# log line colouring: first matching rule wins
LOG_RULES = [
    ("error",      None,                                              "#c0392b"),
    ("warning",    None,                                              "#b9770e"),
    ("disengaged", r"\bDisengaged\b",                                 "#ca6f1e"),
    ("engaged",    r"\b(Engaged|Re-engaged)\b",                       "#1e8449"),
    ("replied",    r"\b(Replied to|Said goodbye to)\b",               "#1f5fa8"),
    ("skipped",    r"\b(Skipped|Cooldown)\b",                         "#7f8c8d"),
    ("memory",     r"\b(Learned about|Memory:|Updated summary)\b",   "#7d3c98"),
]


def make_icon(on: bool, size: int = 64) -> Image.Image:
    """Tray/window icon: DisCortex robot with a green (ON) / grey (OFF) dot.
    Plain generated dot if the logo is missing."""
    return B.status_icon(on, size)


# ---- token prompt (first run, and Settings > Change token) ----------------------

def ask_token_dialog(root: tk.Tk) -> str | None:
    """Masked token entry. Ctrl+V and right-click paste both work. Returns the saved token or None."""
    from app.startup import clean_token, describe_token, token_problem

    result: dict = {}
    win = tk.Toplevel(root)
    win.title("Discord bot token")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    frm = ttk.Frame(win, padding=16)
    frm.pack(fill="both")
    ttk.Label(frm, text="Paste your Discord bot token.\nIt is saved in Windows Credential Manager, "
                        "never in a file.", justify="left").pack(anchor="w")
    var = tk.StringVar()
    entry = ttk.Entry(frm, textvariable=var, show="•", width=60)
    entry.pack(fill="x", pady=(10, 4))
    msg = ttk.Label(frm, text="", foreground=RED, wraplength=420, justify="left")
    msg.pack(anchor="w")

    menu = tk.Menu(win, tearoff=0)
    menu.add_command(label="Paste", command=lambda: entry.event_generate("<<Paste>>"))
    entry.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    def save(_=None):
        token = clean_token(var.get())
        problem = token_problem(token)
        if problem:
            msg.config(text=f"Rejected ({describe_token(token)}): {problem}.")
            return
        set_token(token)
        result["token"] = token
        win.destroy()

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(10, 0))
    ttk.Button(btns, text="Save", command=save).pack(side="right")
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)
    entry.bind("<Return>", save)
    entry.focus_set()
    win.grab_set()
    root.wait_window(win)
    return result.get("token")


# ---- tray ---------------------------------------------------------------------

class Tray:
    def __init__(self, app: "App"):
        import pystray
        self.app = app
        state = app.state
        M = pystray.MenuItem
        menu = pystray.Menu(
            M("Open", lambda: app.post("open"), default=True),
            M(lambda _: "Turn OFF" if state.enabled else "Turn ON", lambda: app.post("toggle")),
            M("Mode", pystray.Menu(
                M("All", lambda: app.post("mode:all"), checked=lambda _: app.effective_mode() == "all", radio=True),
                M("Trigger", lambda: app.post("mode:trigger"),
                  checked=lambda _: app.effective_mode() == "trigger", radio=True),
            )),
            pystray.Menu.SEPARATOR,
            M("Quit", lambda: app.post("quit")),
        )
        self.icon = pystray.Icon("discord-afk-responder", make_icon(state.enabled), B.APP_NAME, menu)
        self._thread = threading.Thread(target=self.icon.run, name="tray", daemon=True)
        self._thread.start()

    def refresh(self, on: bool, tooltip: str) -> None:
        try:
            self.icon.icon = make_icon(on)
            self.icon.title = tooltip[:127]
            self.icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass


# ---- main window --------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk, cfg: Config, profile: Profile | None, state: RuntimeState,
                 make_runner: Callable[[Profile], BotRunner], events: queue.Queue,
                 memory_path: Path | None = None):
        self.root, self.cfg, self.profile, self.state, self.events = root, cfg, profile, state, events
        self.make_runner = make_runner
        self.memory_path = memory_path or base_dir() / "data" / "memory.db"
        self.runner: BotRunner | None = make_runner(profile) if profile else None
        self._cmds: queue.Queue = queue.Queue()
        self._status: dict = {}
        self._paused = False
        self._held: collections.deque = collections.deque(maxlen=LOG_LINES)
        self._quitting = False
        self._switching = False
        self._shown_state = None

        root.geometry("1180x800")
        root.minsize(760, 520)
        self._icons = {on: [ImageTk.PhotoImage(make_icon(on, s)) for s in (32, 16, 48)] for on in (True, False)}
        root.iconphoto(True, *self._icons[state.enabled])
        root.protocol("WM_DELETE_WINDOW", self.quit)
        # A lone Alt tap would start Windows keyboard-menu mode, which pauses Tk's timers (and so the
        # log, status and queued hotkey actions) until the next click/key. We have no menus: skip it.
        for key in ("<KeyRelease-Alt_L>", "<KeyRelease-Alt_R>"):
            root.bind_all(key, lambda e: "break")
        root.bind("<Unmap>", self._on_unmap)

        self.hotkeys = HotkeyManager(lambda action: self.post(f"hotkey:{action}"))  # called on pynput's thread
        self._build()
        self.tray = Tray(self)
        self.apply_hotkeys()
        self._show_profile()
        self._refresh_controls()
        root.after(POLL_MS, self._poll)

    def start(self) -> None:
        if self.runner is not None:
            self.runner.start()
        else:
            self.tabs.select(self.editor)
            log.info("No bot profile loaded yet. Create one in the Profiles tab, then press Load.")

    # -- layout --

    def _build(self) -> None:
        style = ttk.Style()
        style.configure("Status.TLabel", font=FONT)
        style.configure("Head.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("TNotebook.Tab", padding=(14, 4), font=FONT)

        self._build_header()
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        dash = ttk.Frame(self.tabs)
        self.tabs.add(dash, text="Dashboard")

        top = ttk.Frame(dash, padding=(12, 12, 12, 6))
        top.pack(fill="x")

        self.toggle_btn = tk.Button(top, font=("Segoe UI Semibold", 16), width=16, relief="flat",
                                    fg="white", activeforeground="white", cursor="hand2",
                                    command=lambda: self.post("toggle"))
        self.toggle_btn.pack(side="left", ipady=8)

        mode = ttk.Frame(top)
        mode.pack(side="left", padx=20)
        ttk.Label(mode, text="Mode", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(mode)
        row.pack()
        self.mode_var = tk.StringVar()
        for value, label in (("all", "All"), ("trigger", "Trigger")):
            tk.Radiobutton(row, text=label, value=value, variable=self.mode_var, indicatoron=False,
                           width=9, font=FONT, selectcolor="#d6eaf8", cursor="hand2",
                           command=lambda v=value: self.post(f"mode:{v}")).pack(side="left")
        self.mode_hint = ttk.Label(mode, text="", foreground=GREY)
        self.mode_hint.pack(anchor="w")

        sw = ttk.Frame(top)
        sw.pack(side="left", padx=(10, 0))
        ttk.Label(sw, text="Bot profile", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(sw)
        row.pack()
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(row, textvariable=self.profile_var, state="readonly", width=22, font=FONT)
        self.profile_box.pack(side="left")
        self.load_btn = ttk.Button(row, text="Load", command=lambda: self.switch_profile(self.profile_var.get()))
        self.load_btn.pack(side="left", padx=4)
        self.switch_lbl = ttk.Label(sw, text="", foreground=GREY)
        self.switch_lbl.pack(anchor="w")

        ttk.Button(top, text="Quit", command=self.quit).pack(side="right")
        ttk.Button(top, text="Hide to tray", command=self.hide).pack(side="right", padx=6)

        # status grid
        st = ttk.LabelFrame(dash, text="Status", padding=(12, 6))
        st.pack(fill="x", padx=12, pady=6)
        self.status_rows: dict[str, tuple[tk.Label, ttk.Label]] = {}
        for i, (key, title) in enumerate((("profile", "Bot profile"), ("discord", "Discord"),
                                          ("ollama", "Ollama"), ("model", "Model"),
                                          ("embed", "Memory search"), ("engaged", "Engaged"))):
            r, c = divmod(i, 2)
            dot = tk.Label(st, text="●", fg=GREY, font=FONT)
            dot.grid(row=r, column=c * 3, sticky="w")
            ttk.Label(st, text=title, style="Head.TLabel").grid(row=r, column=c * 3 + 1, sticky="w", padx=(4, 10))
            val = ttk.Label(st, text="…", style="Status.TLabel")
            val.grid(row=r, column=c * 3 + 2, sticky="w", padx=(0, 24))
            self.status_rows[key] = (dot, val)
        st.columnconfigure(2, weight=1)
        st.columnconfigure(5, weight=1)

        # log pane
        lf = ttk.LabelFrame(dash, text="Live log", padding=(8, 4))
        lf.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        bar = ttk.Frame(lf)
        bar.pack(fill="x")
        self.pause_btn = ttk.Button(bar, text="Pause", command=self._toggle_pause)
        self.pause_btn.pack(side="right")
        ttk.Button(bar, text="Clear", command=self._clear_log).pack(side="right", padx=6)
        self.held_lbl = ttk.Label(bar, text="", foreground=GREY)
        self.held_lbl.pack(side="right", padx=6)
        legend = ttk.Frame(bar)
        legend.pack(side="left")
        for name, _, color in LOG_RULES:
            tk.Label(legend, text=name, fg=color, font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))

        body = ttk.Frame(lf)
        body.pack(fill="both", expand=True, pady=(4, 0))
        self.log_text = tk.Text(body, font=MONO, wrap="word", state="disabled", height=10,
                                background="#fbfbfc", relief="flat", padx=6, pady=4)
        sb = ttk.Scrollbar(body, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for name, _, color in LOG_RULES:
            self.log_text.tag_configure(name, foreground=color)

        self.editor = ProfileEditor(self.tabs, self)
        self.tabs.add(self.editor, text="Profiles")
        from app.settings_tab import SettingsTab
        from app.users_tab import UsersTab
        self.users_tab = UsersTab(self.tabs, self)
        self.tabs.add(self.users_tab, text="User profiles")
        self.settings_tab = SettingsTab(self.tabs, self)
        self.tabs.add(self.settings_tab, text="Settings")
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _=None) -> None:
        tab = self.root.nametowidget(self.tabs.select())
        if hasattr(tab, "on_show"):
            tab.on_show()

    def _build_header(self) -> None:
        """Logo + name + subtitle + tagline. Text-only if the logo is missing."""
        head = ttk.Frame(self.root, padding=(12, 8, 12, 6))
        head.pack(fill="x")
        img = B.logo_image(76, head=False)  # the full logo
        if img is not None:
            self._logo = ImageTk.PhotoImage(img)
            ttk.Label(head, image=self._logo).pack(side="left", padx=(0, 10))
        text = ttk.Frame(head)
        text.pack(side="left")
        row = ttk.Frame(text)
        row.pack(anchor="w")
        tk.Label(row, text=B.APP_NAME, font=("Segoe UI Semibold", 20), fg="#1f2a5a").pack(side="left")
        tk.Label(row, text=B.SUBTITLE, font=("Segoe UI", 11), fg=GREY).pack(side="left", padx=(10, 0), pady=(8, 0))
        tk.Label(text, text=B.TAGLINE, font=("Segoe UI", 10, "italic"), fg="#6b46c1").pack(anchor="w")

    def _set_row(self, key: str, color: str, text: str) -> None:
        dot, val = self.status_rows[key]
        dot.config(fg=color)
        val.config(text=text)

    # -- commands (from buttons and the tray thread) --

    def post(self, cmd: str) -> None:
        """Thread-safe: queue a command for the GUI thread."""
        self._cmds.put(cmd)

    def effective_mode(self) -> str:
        return self.state.mode_override or (self.profile.mode if self.profile else "trigger")

    def save_cfg(self) -> None:
        save_config(self.cfg)

    def log(self, text: str) -> None:
        log.info(text)

    def run_bg(self, fn: Callable, done: Callable) -> None:
        """Run fn() on a worker thread; done(result, error) is called on the GUI thread."""
        def work():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, e
            self._cmds.put(("bg", done, res, err))
        threading.Thread(target=work, daemon=True).start()

    # -- profiles --

    def active_profile_name(self) -> str | None:
        return self.profile.name if self.profile else None

    def on_profiles_changed(self, renamed: tuple[str, str] | None = None) -> None:
        """Called by the editor after save/duplicate/delete."""
        if renamed and self.profile and renamed[0] == self.profile.name:
            self.cfg.last_profile = renamed[1]
            save_config(self.cfg)
        self._show_profile()

    def _show_profile(self) -> None:
        names = P.list_profile_names()
        self.profile_box.configure(values=names)
        cur = self.profile.name if self.profile else ""
        if self.profile_var.get() not in names or not self.profile_var.get():
            self.profile_var.set(cur if cur in names else (names[0] if names else ""))
        self.root.title(f"{B.APP_NAME} — {cur}" if cur else f"{B.APP_NAME} — {B.SUBTITLE}")
        if self.profile:
            self._set_row("profile", GREEN, f"{cur}  ({self.profile.mode} mode)")
        else:
            self._set_row("profile", GREY, "none loaded: create one in the Profiles tab")
            for key in ("discord", "ollama", "model", "embed", "engaged"):
                self._set_row(key, GREY, "-")
        self.load_btn.config(state="disabled" if self._switching or not names else "normal")

    def switch_profile(self, name: str) -> None:
        """Live switch: stop the running bot, start a new one with this profile."""
        if self._switching or not name:
            return
        try:
            new = P.load_profile(name)
        except Exception as e:
            messagebox.showerror("Load profile", f"'{name}' can't be loaded:\n\n{e}", parent=self.root)
            return
        log.info("Switching to bot profile '%s' (%s @ %s)...", new.name, new.model, new.ollama_url)
        self._replace_runner(new, keep_mode=False, label=f"switching to {name}…")

    def restart_bot(self, reason: str) -> bool:
        """Reconnect the running profile (same settings and mode), e.g. after a token or
        embed model change. Returns False if nothing is running or a switch is in progress."""
        if self._switching or self.profile is None:
            return False
        log.info("Reconnecting (%s)...", reason)
        self._replace_runner(self.profile, keep_mode=True, label="reconnecting…")
        return True

    def _replace_runner(self, new: Profile, keep_mode: bool, label: str) -> None:
        self._switching = True
        self.load_btn.config(state="disabled")
        self.switch_lbl.config(text=label)
        old = self.runner
        self.run_bg(lambda: old.stop() if old else None, lambda _r, _e: self._finish_switch(new, keep_mode))

    def memory_changed(self) -> None:
        """The User profiles / Settings tab changed the shared database."""
        if self.runner is not None:
            self.runner.memory_changed()

    def _finish_switch(self, new: Profile, keep_mode: bool = False) -> None:
        self.profile = new
        if not keep_mode:
            self.state.set_mode_override(None)  # new profile starts in its own mode
        self.cfg.last_profile = new.name
        save_config(self.cfg)
        self._status = {}
        for key in ("discord", "ollama", "model", "embed", "engaged"):
            self._set_row(key, AMBER, "…")
        self.runner = self.make_runner(new)
        self.runner.start()
        self._switching = False
        self.switch_lbl.config(text="")
        self.profile_var.set(new.name)
        self._show_profile()
        self._shown_state = None
        self._refresh_controls()
        self.editor.refresh_list(select=self.editor.loaded_name)

    def apply_hotkeys(self) -> list[str]:
        """(Re)start global hotkeys from cfg.hotkeys. Returns error messages."""
        errors = self.hotkeys.start(self.cfg.hotkeys)
        for e in errors:
            log.warning("Hotkey problem: %s", e)
        names = {"toggle_enabled": "on/off", "toggle_mode": "mode"}
        active = [f"{pretty(c)} = {names.get(a, a)}" for a, c in self.cfg.hotkeys.items() if c and c.strip()]
        if self.hotkeys.active:
            log.info("Global hotkeys active: %s", ", ".join(active))
        elif not errors:
            log.info("Global hotkeys are off (none set).")
        return errors

    def _beep(self, tones: tuple[tuple[int, int], ...]) -> None:
        """Audible feedback for hotkeys (the window is usually hidden behind a game)."""
        if not self.cfg.hotkey_sound:
            return
        def play():
            try:
                import winsound
                for freq, ms in tones:
                    winsound.Beep(freq, ms)
            except Exception:
                pass
        threading.Thread(target=play, daemon=True).start()

    def _run_cmd(self, cmd) -> None:
        if isinstance(cmd, tuple) and cmd[0] == "bg":
            _, done, res, err = cmd
            done(res, err)
            return
        if cmd.startswith("hotkey:"):
            action = cmd.split(":", 1)[1]
            log.info("Hotkey %s pressed", pretty(self.cfg.hotkeys.get(action, "") or action))
            if action == "toggle_enabled":
                self._run_cmd("toggle")
                self._beep(((660, 90), (990, 120)) if self.state.enabled else ((660, 90), (440, 120)))
            elif action == "toggle_mode":
                self._run_cmd("mode:" + ("trigger" if self.effective_mode() == "all" else "all"))
                self._beep(((880, 80),) * (2 if self.effective_mode() == "all" else 1))
            return
        if cmd == "toggle":
            self.state.set_enabled(not self.state.enabled)
            log.info("Bot turned %s", "ON: replying" if self.state.enabled else "OFF: ignoring all messages")
        elif cmd.startswith("mode:"):
            mode = cmd.split(":", 1)[1]
            if mode != self.effective_mode():
                self.state.set_mode_override(None if self.profile and mode == self.profile.mode else mode)
                log.info("Mode switched to %s", mode.upper())
        elif cmd == "open":
            self.show()
        elif cmd == "quit":
            self.quit()
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        on, mode = self.state.enabled, self.effective_mode()
        if self._shown_state == (on, mode):
            return
        self._shown_state = (on, mode)
        self.toggle_btn.config(text="● ON" if on else "OFF", bg=GREEN if on else GREY,
                               activebackground=GREEN if on else GREY)
        self.mode_var.set(mode)
        self.mode_hint.config(text="profile default" if self.state.mode_override is None or not self.profile
                              else f"overriding profile ({self.profile.mode})")
        self.root.iconphoto(True, *self._icons[on])
        self.tray.refresh(on, f"{B.APP_NAME}: {'ON' if on else 'OFF'}, {mode} mode "
                              f"({self.profile.name if self.profile else 'no profile'})")

    # -- window / tray --

    def _on_unmap(self, event) -> None:
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after(10, self.hide)  # minimise = hide to tray

    def hide(self) -> None:
        self.root.withdraw()

    def show(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("Shutting down...")
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        if self.runner is not None:
            self.runner.stop()
        self.users_tab.close()
        self.hotkeys.stop()
        self.tray.stop()
        self.root.destroy()

    # -- polling --

    def _poll(self) -> None:
        if self._quitting:
            return
        try:
            while True:
                self._run_cmd(self._cmds.get_nowait())
                if self._quitting:
                    return
        except queue.Empty:
            pass
        for _ in range(500):
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "log":
                self._add_log(ev[1], ev[2])
            elif ev[0] == "status":
                self._show_status(ev[1])
            elif ev[0] == "fatal":
                self.show()
                messagebox.showerror(B.APP_NAME, ev[1], parent=self.root)
        self._refresh_controls()
        self.root.after(POLL_MS, self._poll)

    def _show_status(self, s: dict) -> None:
        if s == self._status or not self.profile or s.get("profile") != self.profile.name or self._switching:
            return  # unchanged, or a late update from the runner we just stopped
        self._status = s
        if s["discord"] == "connected":
            self._set_row("discord", GREEN, f"connected as {s['discord_user']}")
        elif s["discord"] == "error":
            self._set_row("discord", RED, s["discord_error"][:90])
        else:
            self._set_row("discord", AMBER, "connecting…")
        o = s["ollama"]
        if o["ok"] is None:
            self._set_row("ollama", AMBER, f"{s['ollama_url']}  checking…")
        elif o["ok"]:
            self._set_row("ollama", GREEN, f"{s['ollama_url']}  reachable")
        else:
            self._set_row("ollama", RED, f"{s['ollama_url']}  unreachable, retrying")
        if o["model_ok"] is False:
            self._set_row("model", RED, f"{s['model']}  not on this server")
        else:
            self._set_row("model", GREEN if o["model_ok"] else AMBER, s["model"])
        if o["embed_ok"] is False or (o["ok"] and not s["semantic_ok"]):
            self._set_row("embed", AMBER, f"{s['embed_model']} missing: keyword-only")
        else:
            self._set_row("embed", GREEN if o["embed_ok"] else AMBER, f"keyword + meaning ({s['embed_model']})")
        eng = s["engaged"]
        self._set_row("engaged", GREEN if eng else GREY, ", ".join(eng) if eng else "nobody")

    # -- log pane --

    def _add_log(self, level: int, text: str) -> None:
        if self._paused:
            self._held.append((level, text))
            self.held_lbl.config(text=f"{len(self._held)} new while paused")
            return
        self._write_lines([(level, text)])

    def _write_lines(self, lines) -> None:
        t = self.log_text
        at_bottom = t.yview()[1] >= 0.999
        t.configure(state="normal")
        for level, text in lines:
            t.insert("end", text + "\n", self._tag_for(level, text))
        excess = int(t.index("end-1c").split(".")[0]) - 1 - LOG_LINES
        if excess > 0:
            t.delete("1.0", f"{excess + 1}.0")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")

    @staticmethod
    def _tag_for(level: int, text: str) -> str:
        if level >= logging.ERROR:
            return "error"
        if level >= logging.WARNING:
            return "warning"
        for name, pattern, _ in LOG_RULES[2:]:
            if re.search(pattern, text):
                return name
        return ""

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_btn.config(text="Resume" if self._paused else "Pause")
        if not self._paused:
            self._write_lines(list(self._held))
            self._held.clear()
            self.held_lbl.config(text="")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._held.clear()
        self.held_lbl.config(text="")


def run_gui(root: tk.Tk, cfg: Config, profile: Profile | None, state: RuntimeState,
            make_runner: Callable[[Profile], BotRunner], events: queue.Queue,
            memory_path: Path | None = None) -> None:
    app = App(root, cfg, profile, state, make_runner, events, memory_path)
    app.start()
    root.mainloop()
