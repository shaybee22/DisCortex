"""Settings tab: Discord bot token, embed model + re-embed all, start minimized.

The token is never shown: only whether one is saved. Changing it (or the embed model)
reconnects the running bot profile.
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk

from app import hotkeys as HK
from app.config import get_token
from app.llm import model_installed
from app.profile_editor import fetch_models
from app.profiles import DEFAULT_OLLAMA_URL

log = logging.getLogger(__name__)

FONT = ("Segoe UI", 10)
GREEN, RED, AMBER, GREY = "#1e8449", "#c0392b", "#b9770e", "#7f8c8d"
PROGRESS_MS = 3000
HOTKEY_ACTIONS = (("toggle_enabled", "Turn bot ON / OFF"), ("toggle_mode", "Switch mode (All / Trigger)"))


def embed_first(models: list[str]) -> list[str]:
    """Embedding models first (they're the useful choices), then the rest."""
    emb = [m for m in models if "embed" in m.lower()]
    return emb + [m for m in models if m not in emb]


class SettingsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self._models: list[str] = []
        self._models_from: tuple | None = None
        self._tick_job = None
        self._build()
        self._refresh_token()

    # ---- layout -----------------------------------------------------------------

    def _build(self) -> None:
        head = ("Segoe UI Semibold", 10)

        tok = ttk.LabelFrame(self, text="Discord bot token", padding=(12, 8))
        tok.pack(fill="x")
        row = ttk.Frame(tok)
        row.pack(fill="x")
        self.token_lbl = tk.Label(row, text="", font=("Segoe UI Semibold", 11))
        self.token_lbl.pack(side="left")
        ttk.Button(row, text="Change token…", command=self.change_token).pack(side="left", padx=16)
        ttk.Label(tok, text="Stored in Windows Credential Manager on this PC only; never shown and never "
                            "written to a file. Changing it reconnects the bot.",
                  foreground=GREY, wraplength=900, justify="left").pack(anchor="w", pady=(6, 0))

        emb = ttk.LabelFrame(self, text="Memory search (embed model)", padding=(12, 8))
        emb.pack(fill="x", pady=12)
        row = ttk.Frame(emb)
        row.pack(fill="x")
        ttk.Label(row, text="Embed model", font=head).pack(side="left")
        self.embed_var = tk.StringVar(value=self.app.cfg.embed_model)
        self.embed_box = ttk.Combobox(row, textvariable=self.embed_var, width=34, font=FONT)
        self.embed_box.pack(side="left", padx=8)
        ttk.Button(row, text="Refresh list", command=lambda: self.load_models(force=True)).pack(side="left")
        self.apply_btn = ttk.Button(row, text="Apply", command=self.apply_embed)
        self.apply_btn.pack(side="left", padx=6)
        self.server_lbl = ttk.Label(emb, text="", foreground=GREY)
        self.server_lbl.pack(anchor="w", pady=(6, 0))
        self.progress_lbl = ttk.Label(emb, text="", font=FONT)
        self.progress_lbl.pack(anchor="w", pady=(6, 0))
        self.other_lbl = ttk.Label(emb, text="", foreground=GREY)
        self.other_lbl.pack(anchor="w")
        row = ttk.Frame(emb)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Re-embed all…", command=self.reembed_all).pack(side="left")
        ttk.Label(row, text="Deletes every stored vector; the running bot rebuilds them with the current "
                            "embed model in the background (keyword search keeps working meanwhile).",
                  foreground=GREY, wraplength=760, justify="left").pack(side="left", padx=10)
        ttk.Label(emb, text="Applies to every bot profile. After changing the model the bot embeds old "
                            "messages with it automatically; Re-embed all is only needed to start over.",
                  foreground=GREY, wraplength=900, justify="left").pack(anchor="w", pady=(8, 0))

        hk = ttk.LabelFrame(self, text="Global hotkeys (work in any app, including games)", padding=(12, 8))
        hk.pack(fill="x", pady=(0, 12))
        self.hk_vars: dict[str, tk.StringVar] = {}
        for r, (action, label) in enumerate(HOTKEY_ACTIONS):
            ttk.Label(hk, text=label, font=head).grid(row=r, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=self.app.cfg.hotkeys.get(action, ""))
            self.hk_vars[action] = var
            ttk.Entry(hk, textvariable=var, width=26, font=FONT).grid(row=r, column=1, padx=8, pady=2)
            ttk.Button(hk, text="Record…", command=lambda a=action: self.record(a)).grid(row=r, column=2)
            ttk.Button(hk, text="Clear", command=lambda v=var: v.set("")).grid(row=r, column=3, padx=6)
            pl = ttk.Label(hk, text="", foreground=GREY)
            pl.grid(row=r, column=4, sticky="w")
            var.trace_add("write", lambda *_, v=var, l=pl: l.config(text=self._pretty_or_error(v.get())))
            pl.config(text=self._pretty_or_error(var.get()))
        self.sound_var = tk.BooleanVar(value=self.app.cfg.hotkey_sound)
        ttk.Checkbutton(hk, text="Beep when a hotkey is used (rising = ON, falling = OFF; "
                                 "two beeps = All mode, one = Trigger)",
                        variable=self.sound_var, command=self.toggle_sound).grid(
            row=len(HOTKEY_ACTIONS), column=0, columnspan=5, sticky="w", pady=(6, 0))
        row = ttk.Frame(hk)
        row.grid(row=len(HOTKEY_ACTIONS) + 1, column=0, columnspan=5, sticky="we", pady=(8, 0))
        ttk.Button(row, text="Apply hotkeys", command=self.apply_hotkeys).pack(side="left")
        self.hk_status = ttk.Label(row, text="", font=FONT)
        self.hk_status.pack(side="left", padx=10)
        ttk.Label(hk, text="Format: <ctrl>+<alt>+a, <ctrl>+<shift>+<f9>, <pause>. Leave empty to disable. "
                           "If a game runs as administrator, Windows only passes keys to this app when it "
                           "runs as administrator too.",
                  foreground=GREY, wraplength=900, justify="left").grid(
            row=len(HOTKEY_ACTIONS) + 2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        hk.columnconfigure(4, weight=1)
        self._show_hotkey_status()

        win = ttk.LabelFrame(self, text="Window", padding=(12, 8))
        win.pack(fill="x")
        self.min_var = tk.BooleanVar(value=self.app.cfg.start_minimized)
        ttk.Checkbutton(win, text="Start minimized to tray", variable=self.min_var,
                        command=self.toggle_minimized).pack(anchor="w")
        ttk.Label(win, text="Next launch opens only the tray icon (the window still opens if no bot profile "
                            "can be loaded).", foreground=GREY).pack(anchor="w", pady=(4, 0))

    # ---- visibility ---------------------------------------------------------------

    def on_show(self) -> None:
        self._refresh_token()
        self._show_hotkey_status()
        if not self._embed_edited():
            self.embed_var.set(self.app.cfg.embed_model)
        self.load_models()
        self._tick()

    def _visible(self) -> bool:
        try:
            return self.app.tabs.select() == str(self)
        except tk.TclError:
            return False

    def _tick(self) -> None:
        if self._tick_job:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self._refresh_progress()
        if self._visible():
            self._tick_job = self.after(PROGRESS_MS, self._tick)

    # ---- token --------------------------------------------------------------------

    def _refresh_token(self) -> None:
        if get_token():
            self.token_lbl.config(text="✓ token saved", fg=GREEN)
        else:
            self.token_lbl.config(text="✗ no token saved", fg=RED)

    def change_token(self) -> None:
        from app.gui import ask_token_dialog
        if not ask_token_dialog(self.app.root):
            return  # cancelled: old token unchanged
        log.info("Bot token changed (saved in Credential Manager).")
        self._refresh_token()
        if self.app.profile is None:
            messagebox.showinfo("Token saved", "Token saved. Load a bot profile to connect.", parent=self)
        elif not self.app.restart_bot("new bot token"):
            messagebox.showinfo("Token saved", "Token saved. A profile switch is in progress; it will "
                                               "connect with the new token.", parent=self)

    # ---- embed model ----------------------------------------------------------------

    def _server(self) -> tuple[str, str | None]:
        p = self.app.profile
        return (p.ollama_url, p.ollama_api_key) if p else (DEFAULT_OLLAMA_URL, None)

    def _embed_edited(self) -> bool:
        return self.embed_var.get().strip() not in ("", self.app.cfg.embed_model)

    def load_models(self, force: bool = False) -> None:
        url, key = self._server()
        if not force and self._models_from == (url, key) and self._models:
            return
        self._models_from = (url, key)
        self.server_lbl.config(text=f"Checking {url}…", foreground=GREY)
        self.app.run_bg(lambda: fetch_models(url, key), lambda models, err: self._got_models(url, models, err))

    def _got_models(self, url: str, models, err) -> None:
        name = self.app.profile.name if self.app.profile else "no profile loaded"
        if err:
            self._models = []
            self._models_from = None
            self.server_lbl.config(text=f"{err}", foreground=RED)
            return
        self._models = list(models)
        self.embed_box.configure(values=embed_first(self._models))
        ok = model_installed(self.app.cfg.embed_model, self._models)
        self.server_lbl.config(
            text=f"Models from {url} (active bot profile: {name}). "
                 + ("Current embed model is installed." if ok else
                    f"'{self.app.cfg.embed_model}' is NOT installed there: run  ollama pull "
                    f"{self.app.cfg.embed_model}  on that machine (keyword-only search until then)."),
            foreground=GREY if ok else AMBER)

    def apply_embed(self) -> None:
        name = self.embed_var.get().strip()
        cfg = self.app.cfg
        if not name or name == cfg.embed_model:
            return
        if self._models and not model_installed(name, self._models):
            if not messagebox.askyesno(
                    "Embed model", f"'{name}' is not installed on {self._server()[0]}.\n\n"
                                   f"Run  ollama pull {name}  on that machine. Until then memory search "
                                   "is keyword-only.\n\nUse it anyway?", icon="warning", parent=self):
                return
        elif self._models and "embed" not in name.lower():
            if not messagebox.askyesno(
                    "Embed model", f"'{name}' doesn't look like an embedding model. Chat models usually "
                                   "can't embed (or embed poorly).\n\nUse it anyway?", icon="warning", parent=self):
                return
        old = cfg.embed_model
        cfg.embed_model = name
        self.app.save_cfg()
        log.info("Embed model changed from %s to %s. Older messages are re-embedded with it in the "
                 "background; keyword search covers them meanwhile.", old, name)
        if self._models:
            self._got_models(self._server()[0], self._models, None)  # refresh the "installed?" line
        self._refresh_progress()
        self.app.restart_bot("embed model changed")

    def _refresh_progress(self) -> None:
        store = self.app.users_tab.store
        if store is None:
            self.progress_lbl.config(text="Memory database unavailable (see log).", foreground=RED)
            return
        model = self.app.cfg.embed_model
        done, total = store.embed_progress(model)
        pct = f" ({done * 100 // total}%)" if total else ""
        self.progress_lbl.config(
            text=f"{done} of {total} messages have a '{model}' vector{pct}"
                 + ("" if done >= total else ": the bot fills in the rest while it runs"),
            foreground=GREEN if done >= total else AMBER)
        others = {m: n for m, n in store.embedding_counts().items() if m != model}
        self.other_lbl.config(text=("Unused vectors from other models: "
                                    + ", ".join(f"{m}: {n}" for m, n in others.items())) if others else "")

    def reembed_all(self) -> None:
        store = self.app.users_tab.store
        if store is None:
            return
        n = sum(store.embedding_counts().values())
        if not messagebox.askyesno(
                "Re-embed all", f"Delete all {n} stored vectors and rebuild them with "
                                f"'{self.app.cfg.embed_model}'?\n\nThe bot re-embeds in the background while "
                                "it runs (newest messages first). Keyword search works meanwhile.",
                icon="warning", default="no", parent=self):
            return
        deleted = store.clear_embeddings()
        log.info("Memory: re-embed all: deleted %d vectors; rebuilding with %s in the background",
                 deleted, self.app.cfg.embed_model)
        self.app.memory_changed()
        self._refresh_progress()

    # ---- hotkeys ----------------------------------------------------------------------

    @staticmethod
    def _pretty_or_error(text: str) -> str:
        if not text.strip():
            return "disabled"
        try:
            HK.parse_combo(text)
            return HK.pretty(text)
        except HK.HotkeyError as e:
            return f"⚠ {e}"

    def _show_hotkey_status(self, errors: list[str] | None = None) -> None:
        if errors:
            self.hk_status.config(text="; ".join(errors), foreground=RED)
        elif self.app.hotkeys.active:
            self.hk_status.config(text="✓ active", foreground=GREEN)
        else:
            self.hk_status.config(text="off (no hotkeys set)", foreground=GREY)

    def apply_hotkeys(self) -> None:
        new = {a: self.hk_vars[a].get().strip() for a, _ in HOTKEY_ACTIONS}
        _, errors = HK.validate(new)
        if errors:
            messagebox.showerror("Hotkeys", "Not saved:\n\n" + "\n".join(errors), parent=self)
            return
        # store the canonical spelling
        self.app.cfg.hotkeys = {a: (HK.parse_combo(c)[2] if c else "") for a, c in new.items()}
        for a, var in self.hk_vars.items():
            var.set(self.app.cfg.hotkeys[a])
        self.app.save_cfg()
        self._show_hotkey_status(self.app.apply_hotkeys())

    def toggle_sound(self) -> None:
        self.app.cfg.hotkey_sound = bool(self.sound_var.get())
        self.app.save_cfg()

    def record(self, action: str) -> None:
        """Capture the next key combination pressed in a small dialog."""
        label = dict(HOTKEY_ACTIONS)[action]
        win = tk.Toplevel(self)
        win.title("Record hotkey")
        win.resizable(False, False)
        win.transient(self.winfo_toplevel())
        frm = ttk.Frame(win, padding=18)
        frm.pack()
        ttk.Label(frm, text=f"Press the new key combination for:\n{label}", justify="center",
                  font=("Segoe UI Semibold", 10)).pack()
        shown = ttk.Label(frm, text="…", font=("Segoe UI Semibold", 16))
        shown.pack(pady=12)
        msg = ttk.Label(frm, text="Esc to cancel", foreground=GREY, wraplength=360, justify="center")
        msg.pack()
        self.app.hotkeys.stop()  # don't fire the current hotkeys while recording

        def mods_held(event) -> set[str]:
            m = HK.current_mods()
            if m is not None:
                return set(m)
            st = event.state  # non-Windows fallback
            return {n for n, bit in (("shift", 0x1), ("ctrl", 0x4), ("alt", 0x20000)) if st & bit}

        def on_key(event):
            mods = mods_held(event)
            if event.keycode in HK.VK_TO_MOD:
                shown.config(text="+".join(HK.PRETTY_MODS[m] for m in HK.MODS if m in mods) + "+…")
                return "break"
            if event.keysym == "Escape" and not mods:
                win.destroy()
                return "break"
            combo = HK.combo_from_vk(mods, event.keycode)
            if combo is None:
                msg.config(text="That key can't be used. Try another.", foreground=RED)
                return "break"
            try:
                HK.parse_combo(combo)
            except HK.HotkeyError as e:
                shown.config(text=HK.pretty(combo))
                msg.config(text=str(e), foreground=RED)
                return "break"
            self.hk_vars[action].set(combo)
            win.destroy()
            return "break"

        win.bind("<KeyPress>", on_key)
        win.bind("<Destroy>", lambda e: e.widget is win and self.after(50, self._resume_hotkeys))
        win.grab_set()
        win.focus_force()

    def _resume_hotkeys(self) -> None:
        if not self.app.hotkeys.active:
            self.app.apply_hotkeys()
        self._show_hotkey_status()
        if self._hotkeys_edited():
            self.hk_status.config(text=self.hk_status.cget("text") + "   (press Apply hotkeys to save changes)")

    def _hotkeys_edited(self) -> bool:
        return any(self.hk_vars[a].get().strip() != (self.app.cfg.hotkeys.get(a) or "") for a, _ in HOTKEY_ACTIONS)

    # ---- window -----------------------------------------------------------------------

    def toggle_minimized(self) -> None:
        self.app.cfg.start_minimized = bool(self.min_var.get())
        self.app.save_cfg()
        log.info("Start minimized to tray: %s", "on" if self.app.cfg.start_minimized else "off")
