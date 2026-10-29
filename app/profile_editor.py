"""Profiles tab: list + editor for every bot profile key. All file work goes through profiles.py.

Network calls (Test connection, save-time checks) run on a worker thread via app.run_bg();
their results come back on the GUI thread.
"""
from __future__ import annotations

import asyncio
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from app import profiles as P
from app.llm import OllamaClient, model_installed

FONT = ("Segoe UI", 10)
GREEN, RED, AMBER, GREY = "#1e8449", "#c0392b", "#b9770e", "#7f8c8d"

# key -> (label, kind). kind: str/int/float/bool
SCALARS = {
    "name": ("Name", str), "mode": ("Mode", str), "triggers": ("Triggers (comma-separated)", str),
    "respond_to_mentions": ("Reply to @mentions", bool), "respond_to_replies": ("Reply to replies to the bot", bool),
    "ollama_url": ("Ollama URL", str), "ollama_api_key": ("API key (optional)", str), "model": ("Model", str),
    "temperature": ("Temperature", float), "context_messages": ("Context messages", int),
    "cooldown_seconds": ("Cooldown (s)", float), "max_reply_chars": ("Max reply chars", int),
    "sticky_engage": ("Sticky conversations", bool), "engage_timeout_seconds": ("Engage timeout (s)", float),
    "memory_enabled": ("Use memory", bool), "memory_k": ("Memories per reply (k)", int),
    "memory_max_chars": ("Memory max chars", int), "prefer_current_channel": ("Prefer current channel", bool),
}
TEXTS = ("system_prompt", "disengage_phrases", "signoff_phrases")


def fetch_models(url: str, api_key: str | None) -> list[str]:
    return asyncio.run(OllamaClient(url, api_key=api_key or None).list_models())


def chat_models(models: list[str]) -> list[str]:
    return [m for m in models if "embed" not in m.lower()]


class ScrollFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, padding=(4, 4, 12, 4))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if isinstance(w, tk.Text):
                return  # let text boxes scroll themselves
            if w is self:
                self.canvas.yview_scroll(int(-e.delta / 120), "units")
                return
            w = w.master


class ProfileEditor(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app = app
        self.loaded_name: str | None = None   # file the form came from (None = new, unsaved)
        self._is_new = False                  # form holds a new profile that isn't saved yet
        self._baseline = None
        self._models: list[str] = []
        self._build()
        self.refresh_list(select=app.active_profile_name())
        self.after(500, self._dirty_tick)

    # ---- layout ---------------------------------------------------------------

    def _build(self) -> None:
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(left, text="Bot profiles", font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.listbox = tk.Listbox(left, width=24, height=18, font=FONT, exportselection=False, activestyle="none")
        self.listbox.pack(fill="y", expand=True, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        for text, cmd in (("New", self.new), ("Duplicate", self.duplicate), ("Delete", self.delete)):
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=1)

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(right)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.save_btn = ttk.Button(bar, text="Save", command=self.save)
        self.save_btn.pack(side="left")
        ttk.Button(bar, text="Revert", command=self.revert).pack(side="left", padx=6)
        self.dirty_lbl = ttk.Label(bar, text="", foreground=AMBER)
        self.dirty_lbl.pack(side="left", padx=6)
        self.load_btn = ttk.Button(bar, text="Load this profile now", command=self.load_now)
        self.load_btn.pack(side="right")

        scroll = ScrollFrame(right)
        scroll.pack(fill="both", expand=True)
        form = scroll.inner
        form.columnconfigure(0, weight=1, uniform="col")
        form.columnconfigure(1, weight=1, uniform="col")

        self.v: dict[str, tk.Variable] = {}
        for key, (_, kind) in SCALARS.items():
            self.v[key] = tk.BooleanVar() if kind is bool else tk.StringVar()

        # -- column 0 --
        ident = self._section(form, "Identity and triggers", 0, 0)
        self._entry(ident, "name")
        r = self._row(ident, "mode")
        for val, lbl in (("trigger", "Trigger"), ("all", "All")):
            ttk.Radiobutton(r, text=lbl, value=val, variable=self.v["mode"]).pack(side="left", padx=(0, 10))
        self._entry(ident, "triggers")
        self._check(ident, "respond_to_mentions")
        self._check(ident, "respond_to_replies")

        chans = self._section(form, "Channels to watch", 1, 0)
        self.chan_list = tk.Listbox(chans, height=4, font=FONT, exportselection=False)
        self.chan_list.pack(fill="x")
        cr = ttk.Frame(chans)
        cr.pack(fill="x", pady=(4, 0))
        self.chan_entry = ttk.Entry(cr)
        self.chan_entry.pack(side="left", fill="x", expand=True)
        self.chan_entry.bind("<Return>", lambda e: self._add_channel())
        ttk.Button(cr, text="Add", command=self._add_channel).pack(side="left", padx=4)
        ttk.Button(cr, text="Remove", command=self._remove_channel).pack(side="left")
        ttk.Label(chans, text="Discord: right-click a channel > Copy Channel ID (Developer Mode on).",
                  foreground=GREY).pack(anchor="w")

        brain = self._section(form, "Ollama server and model", 2, 0)
        self._entry(brain, "ollama_url")
        r = self._row(brain, "ollama_api_key")
        self.key_entry = ttk.Entry(r, textvariable=self.v["ollama_api_key"], show="•")
        self.key_entry.pack(side="left", fill="x", expand=True)
        self._show_key = tk.BooleanVar()
        ttk.Checkbutton(r, text="show", variable=self._show_key,
                        command=lambda: self.key_entry.config(show="" if self._show_key.get() else "•")
                        ).pack(side="left", padx=4)
        ttk.Label(brain, text="Only for a reverse proxy in front of Ollama. Stored in the profile file.",
                  foreground=GREY).pack(anchor="w")
        tr = ttk.Frame(brain)
        tr.pack(fill="x", pady=4)
        ttk.Button(tr, text="Test connection", command=self.test_connection).pack(side="left")
        self.test_lbl = ttk.Label(tr, text="", wraplength=330)
        self.test_lbl.pack(side="left", padx=8)
        r = self._row(brain, "model")
        self.model_box = ttk.Combobox(r, textvariable=self.v["model"], state="readonly")
        self.model_box.pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="Refresh", command=self.test_connection).pack(side="left", padx=4)
        self._spin(brain, "temperature", 0, 2, 0.1)

        # -- column 1 --
        persona = self._section(form, "Persona / system prompt", 0, 1)
        self.t: dict[str, tk.Text] = {}
        self.t["system_prompt"] = self._text(persona, 9)

        reply = self._section(form, "Replies", 1, 1)
        self._spin(reply, "context_messages", 0, 100, 1)
        self._spin(reply, "cooldown_seconds", 0, 600, 1)
        self._spin(reply, "max_reply_chars", 50, 2000, 50)

        eng = self._section(form, "Engaged conversations (trigger mode)", 2, 1)
        self._check(eng, "sticky_engage")
        self._spin(eng, "engage_timeout_seconds", 5, 3600, 15)
        ttk.Label(eng, text="Disengage phrases (one per line)").pack(anchor="w", pady=(4, 0))
        self.t["disengage_phrases"] = self._text(eng, 4)
        ttk.Label(eng, text="Sign-off phrases: one goodbye, then disengage (one per line)").pack(anchor="w", pady=(4, 0))
        self.t["signoff_phrases"] = self._text(eng, 3)

        mem = self._section(form, "Memory (shared by all profiles; these only tune recall)", 3, 1)
        self._check(mem, "memory_enabled")
        self._spin(mem, "memory_k", 0, 50, 1)
        self._spin(mem, "memory_max_chars", 0, 10000, 100)
        self._check(mem, "prefer_current_channel")

    def _section(self, parent, title, row, col):
        f = ttk.LabelFrame(parent, text=title, padding=(10, 6))
        f.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
        return f

    def _row(self, parent, key):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=SCALARS[key][0], width=26).pack(side="left")
        return r

    def _entry(self, parent, key):
        ttk.Entry(self._row(parent, key), textvariable=self.v[key]).pack(side="left", fill="x", expand=True)

    def _spin(self, parent, key, lo, hi, step):
        ttk.Spinbox(self._row(parent, key), textvariable=self.v[key], from_=lo, to=hi, increment=step,
                    width=10).pack(side="left")

    def _check(self, parent, key):
        ttk.Checkbutton(parent, text=SCALARS[key][0], variable=self.v[key]).pack(anchor="w", pady=1)

    def _text(self, parent, height):
        t = tk.Text(parent, height=height, wrap="word", font=FONT, undo=True, relief="solid", borderwidth=1)
        t.pack(fill="both", expand=True, pady=(2, 0))
        return t

    # ---- channels ---------------------------------------------------------------

    def _add_channel(self) -> None:
        raw = self.chan_entry.get().replace(",", " ").split()
        bad = [c for c in raw if not c.isdigit() or not 15 <= len(c) <= 21]
        if bad:
            messagebox.showerror("Channel ID", f"Not a channel ID: {', '.join(bad)}\n\nChannel IDs are "
                                 "17-20 digit numbers.", parent=self)
            return
        existing = set(self.chan_list.get(0, "end"))
        for c in raw:
            if c not in existing:
                self.chan_list.insert("end", c)
        self.chan_entry.delete(0, "end")

    def _remove_channel(self) -> None:
        for i in reversed(self.chan_list.curselection()):
            self.chan_list.delete(i)

    # ---- form <-> data ------------------------------------------------------------

    def _populate(self, data: dict) -> None:
        for key, (_, kind) in SCALARS.items():
            val = data.get(key)
            if key == "triggers":
                val = ", ".join(val or [])
            if kind is bool:
                self.v[key].set(bool(val))
            else:
                self.v[key].set("" if val is None else (f"{val:g}" if isinstance(val, float) else str(val)))
        self.t["system_prompt"].delete("1.0", "end")
        self.t["system_prompt"].insert("1.0", (data.get("system_prompt") or "").rstrip("\n"))
        for key in ("disengage_phrases", "signoff_phrases"):
            self.t[key].delete("1.0", "end")
            self.t[key].insert("1.0", "\n".join(data.get(key) or []))
        self.chan_list.delete(0, "end")
        for c in data.get("channel_ids") or []:
            self.chan_list.insert("end", str(c))
        model = data.get("model") or ""
        self.model_box.configure(values=self._models or ([model] if model else []))
        self._baseline = self._raw()

    def _raw(self) -> tuple:
        return (tuple(str(v.get()) for v in self.v.values()),
                tuple(self.t[k].get("1.0", "end-1c") for k in TEXTS),
                tuple(self.chan_list.get(0, "end")))

    def dirty(self) -> bool:
        return self._baseline is not None and self._raw() != self._baseline

    def _collect(self) -> tuple[P.Profile | None, list[str]]:
        errors, data = [], {}
        for key, (label, kind) in SCALARS.items():
            val = self.v[key].get()
            if kind in (int, float):
                try:
                    val = kind(str(val).strip())
                except ValueError:
                    errors.append(f"{label}: '{val}' is not a {'whole ' if kind is int else ''}number")
                    continue
            data[key] = val
        data["triggers"] = [t.strip() for t in str(data.get("triggers", "")).split(",") if t.strip()]
        data["ollama_api_key"] = str(data.get("ollama_api_key") or "").strip() or None
        sp = self.t["system_prompt"].get("1.0", "end-1c").strip()
        data["system_prompt"] = sp + "\n" if sp else ""
        for key in ("disengage_phrases", "signoff_phrases"):
            data[key] = [l.strip() for l in self.t[key].get("1.0", "end-1c").splitlines() if l.strip()]
        data["channel_ids"] = [int(c) for c in self.chan_list.get(0, "end")]
        if not sp:
            errors.append("System prompt is empty")
        name = str(data.get("name", "")).strip()
        if name != self.loaded_name and name in P.list_profile_names():
            errors.append(f"A profile named '{name}' already exists")
        if errors:
            return None, errors
        try:
            return P.profile_from_dict(data), []
        except (ValueError, TypeError) as e:
            return None, [str(e)]

    # ---- list ---------------------------------------------------------------------

    def refresh_list(self, select: str | None = None) -> None:
        names = P.list_profile_names()
        active = self.app.active_profile_name()
        self.listbox.delete(0, "end")
        for n in names:
            self.listbox.insert("end", f"{n}   (running)" if n == active else n)
        self._names = names
        if select in names:
            i = names.index(select)
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(i)
            self.listbox.see(i)
            if select != self.loaded_name:  # never reload (and lose edits) for the profile already open
                self._load_into_form(select)
        elif self._is_new:
            self.listbox.selection_clear(0, "end")  # keep the unsaved new profile in the form
        elif names and self.loaded_name is None:
            self.listbox.selection_set(0)
            self._load_into_form(names[0])

    def _selected_name(self) -> str | None:
        sel = self.listbox.curselection()
        return self._names[sel[0]] if sel else None

    def _confirm_discard(self) -> bool:
        return not (self._is_new or self.dirty()) or messagebox.askyesno(
            "Unsaved changes", "Discard your unsaved changes to this profile?", parent=self)

    def _on_select(self, _=None) -> None:
        name = self._selected_name()
        if name is None or name == self.loaded_name:
            return
        if not self._confirm_discard():
            self._reselect(self.loaded_name)
            return
        self._load_into_form(name)

    def _reselect(self, name: str | None) -> None:
        self.listbox.selection_clear(0, "end")
        if name in self._names:
            self.listbox.selection_set(self._names.index(name))

    def _load_into_form(self, name: str) -> None:
        try:
            prof = P.load_profile(name)
        except Exception as e:
            messagebox.showerror("Profile", f"'{name}' can't be loaded:\n\n{e}\n\nFix or delete "
                                 f"{P.profile_path(name)}.", parent=self)
            return
        self.loaded_name = name
        self._is_new = False
        self._models = []
        self.test_lbl.config(text="")
        self._populate(prof.to_dict())
        self.test_connection(quiet=True)

    # ---- buttons --------------------------------------------------------------------

    def new(self) -> None:
        if not self._confirm_discard():
            return
        names = set(P.list_profile_names())
        name, i = "new-profile", 2
        while name in names:
            name, i = f"new-profile-{i}", i + 1
        defaults = {f: getattr(P.Profile(name=name, model="placeholder"), f) for f in P.Profile.__dataclass_fields__}
        defaults["model"] = ""
        self.loaded_name = None
        self._is_new = True
        self.listbox.selection_clear(0, "end")
        self._models = []
        self._populate(defaults)
        self._baseline = None  # brand-new form counts as unsaved
        self.test_lbl.config(text="")
        self.test_connection()

    def duplicate(self) -> None:
        src = self._selected_name()
        if not src:
            return
        new = simpledialog.askstring("Duplicate profile", "Name for the copy:", initialvalue=f"{src}-copy",
                                     parent=self)
        if not new:
            return
        try:
            P.duplicate_profile(src, new.strip())
        except Exception as e:
            messagebox.showerror("Duplicate profile", str(e), parent=self)
            return
        self.app.on_profiles_changed()
        self.loaded_name = None
        self.refresh_list(select=new.strip())

    def delete(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if name == self.app.active_profile_name():
            messagebox.showinfo("Delete profile", f"'{name}' is the running profile. Load another profile "
                                "first, then delete this one.", parent=self)
            return
        if not messagebox.askyesno("Delete profile", f"Delete bot profile '{name}'?\n\nMemories and user "
                                   "profiles are shared and are NOT affected.", parent=self):
            return
        P.delete_profile(name)
        self.loaded_name, self._baseline = None, None
        self.app.on_profiles_changed()
        self.refresh_list(select=self.app.active_profile_name())
        if not self._names:
            self.new()

    def revert(self) -> None:
        if self.loaded_name:
            self._load_into_form(self.loaded_name)
        else:
            self.new()

    def load_now(self) -> None:
        name = self.loaded_name
        if name is None:
            messagebox.showinfo("Load profile", "Save this profile first.", parent=self)
            return
        if self.dirty():
            if not messagebox.askyesno("Unsaved changes", "Load the saved version of this profile and discard "
                                       "your unsaved changes?", parent=self):
                return
            self._load_into_form(name)
        self.app.switch_profile(name)

    # ---- Test connection / Save -------------------------------------------------------

    def test_connection(self, quiet: bool = False) -> None:
        url = self.v["ollama_url"].get().strip().rstrip("/")
        key = self.v["ollama_api_key"].get().strip()
        if not url.startswith(("http://", "https://")):
            self.test_lbl.config(text="URL must start with http:// or https://", foreground=RED)
            return
        if not quiet:
            self.test_lbl.config(text=f"Testing {url}…", foreground=GREY)
        self.app.run_bg(lambda: fetch_models(url, key), lambda models, err: self._tested(url, models, err))

    def _tested(self, url: str, models, err) -> None:
        if self.v["ollama_url"].get().strip().rstrip("/") != url:
            return  # URL changed while testing
        if err is not None:
            self.test_lbl.config(text=f"✗ {err}", foreground=RED)
            return
        self._models = chat_models(models)
        self.model_box.configure(values=self._models)
        current = self.v["model"].get()
        if not current and self._models:
            self.v["model"].set(self._models[0])
            current = self._models[0]
        if not self._models:
            self.test_lbl.config(text="✓ reachable, but no chat models installed there", foreground=AMBER)
        elif current and current not in self._models:
            self.test_lbl.config(text=f"✓ reachable ({len(self._models)} models), but '{current}' is not one "
                                      "of them", foreground=AMBER)
        else:
            self.test_lbl.config(text=f"✓ reachable, {len(self._models)} chat model(s)", foreground=GREEN)

    def save(self) -> None:
        prof, errors = self._collect()
        if errors:
            messagebox.showerror("Can't save", "\n".join(f"• {e}" for e in errors), parent=self)
            return
        self.save_btn.config(state="disabled")
        self.test_lbl.config(text=f"Checking {prof.ollama_url}…", foreground=GREY)
        self.app.run_bg(lambda: fetch_models(prof.ollama_url, prof.ollama_api_key),
                        lambda models, err: self._finish_save(prof, models, err))

    def _finish_save(self, prof: P.Profile, models, err) -> None:
        self.save_btn.config(state="normal")
        warnings = []
        if err is not None:
            warnings.append(str(err))
            self.test_lbl.config(text=f"✗ {err}", foreground=RED)
        else:
            self._tested(prof.ollama_url, models, None)
            if not model_installed(prof.model, models):
                warnings.append(f"Model '{prof.model}' is not on {prof.ollama_url}.")
        if not prof.channel_ids:
            warnings.append("No channel IDs: this profile won't watch any channel.")
        if warnings and not messagebox.askyesno(
                "Save anyway?", "\n\n".join(warnings) + "\n\nSave the profile anyway?", parent=self):
            return
        old = self.loaded_name
        P.save_profile(prof)
        if old and old != prof.name:
            P.delete_profile(old)
        self.loaded_name = prof.name
        self._is_new = False
        self._baseline = self._raw()
        self.app.on_profiles_changed(renamed=(old, prof.name) if old and old != prof.name else None)
        self.refresh_list(select=prof.name)
        self.app.log(f"Saved bot profile '{prof.name}'")
        if old is not None and old == self.app.active_profile_name():
            if messagebox.askyesno("Apply changes", f"'{prof.name}' is running. Apply the changes now?\n\n"
                                   "The bot reconnects with the new settings.", parent=self):
                self.app.switch_profile(prof.name)

    # ---- unsaved-changes indicator -------------------------------------------------------

    def _dirty_tick(self) -> None:
        unsaved = self._is_new or self.dirty()
        self.dirty_lbl.config(text="● unsaved changes" if unsaved else "")
        self.load_btn.config(state="normal" if self.loaded_name else "disabled")
        self.after(500, self._dirty_tick)
