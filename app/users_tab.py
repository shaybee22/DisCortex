"""User profiles tab: search users, view summary/facts, edit notes, delete facts,
wipe a user / a channel / everything. All database work goes through memory.py.

The GUI opens its own MemoryStore connection (WAL lets it share data/memory.db with
the bot). After any change it calls app.memory_changed() so the running bot drops
its cached vectors.
"""
from __future__ import annotations

import logging
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from app.memory import MemoryStore

log = logging.getLogger(__name__)

FONT = ("Segoe UI", 10)
GREEN, RED, AMBER, GREY = "#1e8449", "#c0392b", "#b9770e", "#7f8c8d"


def when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


class UsersTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app = app
        self._store: MemoryStore | None = None
        self.user_id: int | None = None     # user shown on the right
        self._notes_saved = ""
        self._search_job = None
        self._channels: list[dict] = []
        self._build()

    # ---- database ---------------------------------------------------------------

    @property
    def store(self) -> MemoryStore | None:
        if self._store is None:
            try:
                self._store = MemoryStore(self.app.memory_path)
            except Exception as e:
                log.error("Could not open memory database %s: %s", self.app.memory_path, e)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    # ---- layout -----------------------------------------------------------------

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.search_var, width=30, font=FONT)
        e.pack(side="left", padx=6)
        e.bind("<KeyRelease>", lambda _: self._search_soon())
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Label(top, text="name, alias or Discord user ID", foreground=GREY).pack(side="left", padx=8)
        self.stats_lbl = ttk.Label(top, text="", foreground=GREY)
        self.stats_lbl.pack(side="right")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=8)

        # user list
        left = ttk.Frame(body)
        self.tree = ttk.Treeview(left, columns=("msgs", "facts", "seen"), show="tree headings",
                                 selectmode="browse", height=20)
        self.tree.heading("#0", text="User")
        self.tree.heading("msgs", text="Msgs")
        self.tree.heading("facts", text="Facts")
        self.tree.heading("seen", text="Last seen")
        self.tree.column("#0", width=170)
        self.tree.column("msgs", width=55, anchor="e")
        self.tree.column("facts", width=50, anchor="e")
        self.tree.column("seen", width=125)
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        body.add(left, weight=2)

        # details
        right = ttk.Frame(body, padding=(12, 0, 0, 0))
        body.add(right, weight=3)
        self.title_lbl = ttk.Label(right, text="Select a user", font=("Segoe UI Semibold", 13))
        self.title_lbl.pack(anchor="w")
        self.info_lbl = ttk.Label(right, text="", foreground=GREY, justify="left", wraplength=560)
        self.info_lbl.pack(anchor="w", pady=(2, 6))

        ttk.Label(right, text="Summary (written by the bot from the facts)",
                  font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.summary = tk.Text(right, height=4, wrap="word", font=FONT, relief="flat",
                               background="#f4f5f7", state="disabled", padx=6, pady=4)
        self.summary.pack(fill="x", pady=(2, 8))

        fh = ttk.Frame(right)
        fh.pack(fill="x")
        ttk.Label(fh, text="Known facts", font=("Segoe UI Semibold", 10)).pack(side="left")
        self.del_fact_btn = ttk.Button(fh, text="Delete selected fact(s)", command=self.delete_facts)
        self.del_fact_btn.pack(side="right")
        ff = ttk.Frame(right)
        ff.pack(fill="both", expand=True, pady=(2, 8))
        self.facts = ttk.Treeview(ff, columns=("updated",), show="tree headings", selectmode="extended", height=7)
        self.facts.heading("#0", text="Fact")
        self.facts.heading("updated", text="Updated")
        self.facts.column("#0", width=380)
        self.facts.column("updated", width=125, stretch=False)
        fsb = ttk.Scrollbar(ff, command=self.facts.yview)
        self.facts.configure(yscrollcommand=fsb.set)
        fsb.pack(side="right", fill="y")
        self.facts.pack(side="left", fill="both", expand=True)
        self.facts.bind("<Delete>", lambda _: self.delete_facts())

        nh = ttk.Frame(right)
        nh.pack(fill="x")
        ttk.Label(nh, text="Your notes (the bot sees these)", font=("Segoe UI Semibold", 10)).pack(side="left")
        self.notes_dirty_lbl = ttk.Label(nh, text="", foreground=AMBER)
        self.notes_dirty_lbl.pack(side="left", padx=8)
        self.save_notes_btn = ttk.Button(nh, text="Save notes", command=self.save_notes)
        self.save_notes_btn.pack(side="right")
        self.revert_notes_btn = ttk.Button(nh, text="Revert", command=self.revert_notes)
        self.revert_notes_btn.pack(side="right", padx=6)
        self.notes = tk.Text(right, height=4, wrap="word", font=FONT, undo=True, padx=6, pady=4)
        self.notes.pack(fill="x", pady=(2, 8))
        self.notes.bind("<<Modified>>", self._notes_modified)

        self.wipe_user_btn = ttk.Button(right, text="Wipe this user…", command=self.wipe_user)
        self.wipe_user_btn.pack(anchor="e")

        # whole-database actions
        danger = ttk.LabelFrame(self, text="Wipe memory", padding=(10, 6))
        danger.pack(fill="x")
        ttk.Label(danger, text="Channel").pack(side="left")
        self.chan_var = tk.StringVar()
        self.chan_box = ttk.Combobox(danger, textvariable=self.chan_var, state="readonly", width=44, font=FONT)
        self.chan_box.pack(side="left", padx=6)
        ttk.Button(danger, text="Wipe channel…", command=self.wipe_channel).pack(side="left")
        ttk.Button(danger, text="Wipe ALL memory…", command=self.wipe_all).pack(side="right")
        ttk.Label(danger, text="Deleting can't be undone.", foreground=GREY).pack(side="right", padx=10)
        self._show_user(None)

    # ---- list -------------------------------------------------------------------

    def on_show(self) -> None:
        self.refresh()

    def _search_soon(self) -> None:
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(250, self.refresh)

    def refresh(self) -> None:
        self._search_job = None
        store = self.store
        if store is None:
            self.stats_lbl.config(text="memory database unavailable (see log)", foreground=RED)
            return
        s = store.stats()
        self.stats_lbl.config(text=f"{s['users']} users · {s['messages']} messages · {s['facts']} facts · "
                                   f"{s['channels']} channels", foreground=GREY)
        users = store.find_users(self.search_var.get())
        fact_counts = store.fact_counts() if users else {}
        self.tree.delete(*self.tree.get_children())
        for u in users:
            self.tree.insert("", "end", iid=str(u["author_id"]), text=u["display_name"],
                             values=(u["message_count"], fact_counts.get(u["author_id"], 0), when(u["last_seen"])))
        if self.user_id is not None and self.tree.exists(str(self.user_id)):
            self.tree.selection_set(str(self.user_id))
            self.tree.see(str(self.user_id))
            self._show_user(self.user_id, keep_notes=self._notes_dirty())
        elif not self._notes_dirty():
            self._show_user(None)
        self._refresh_channels()

    def _refresh_channels(self) -> None:
        self._channels = self.store.channels() if self.store else []
        labels = [f"#{c['channel_name'] or '?'}  ({c['channel_id']}) · {c['messages']} msgs" for c in self._channels]
        cur = self.chan_box.current()
        self.chan_box.configure(values=labels)
        if labels:
            self.chan_box.current(cur if 0 <= cur < len(labels) else 0)
        else:
            self.chan_var.set("")

    def _on_select(self, _=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return  # list was refreshed/filtered; refresh() decides what to show
        uid = int(sel[0])
        if uid == self.user_id:
            return
        if self._notes_dirty() and not messagebox.askyesno(
                "Unsaved notes", "Discard your unsaved notes for this user?", parent=self):
            if self.user_id is not None and self.tree.exists(str(self.user_id)):
                self.tree.selection_set(str(self.user_id))
            return
        self._show_user(uid)

    # ---- details ----------------------------------------------------------------

    def _show_user(self, uid: int | None, keep_notes: bool = False) -> None:
        u = self.store.get_user(uid) if (uid is not None and self.store) else None
        self.user_id = u["author_id"] if u else None
        state = "normal" if u else "disabled"
        for b in (self.del_fact_btn, self.save_notes_btn, self.revert_notes_btn, self.wipe_user_btn):
            b.config(state=state)
        self.title_lbl.config(text=f"{u['display_name']}" if u else "Select a user")
        self.info_lbl.config(text=(
            f"Discord ID {u['author_id']}   ·   {u['message_count']} messages\n"
            f"Also known as: {', '.join(u['names']) or '-'}\n"
            f"First seen {when(u['first_seen'])}   ·   last seen {when(u['last_seen'])}") if u else "")
        self.summary.config(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", (u["summary"] or "(none yet: written after a few facts are learned)") if u else "")
        self.summary.config(state="disabled")
        self.facts.delete(*self.facts.get_children())
        for f in (u["facts"] if u else []):
            self.facts.insert("", "end", iid=str(f["id"]), text=f["fact"], values=(when(f["updated"]),))
        if not keep_notes:
            self._notes_saved = u["notes"] if u else ""
            self.notes.config(state="normal")
            self._set_notes(self._notes_saved)
            self.notes.config(state="normal" if u else "disabled")
        self._notes_modified()

    def _set_notes(self, text: str) -> None:
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text)
        self.notes.edit_reset()

    def _notes_text(self) -> str:
        return self.notes.get("1.0", "end-1c").strip()

    def _notes_dirty(self) -> bool:
        return self.user_id is not None and self._notes_text() != self._notes_saved.strip()

    def _notes_modified(self, _=None) -> None:
        self.notes.edit_modified(False)
        self.notes_dirty_lbl.config(text="● unsaved" if self._notes_dirty() else "")

    def save_notes(self) -> None:
        if self.user_id is None or self.store is None:
            return
        text = self._notes_text()
        self.store.set_notes(self.user_id, text)
        self._notes_saved = text
        self._notes_modified()
        log.info("Memory: notes saved for %s", self.title_lbl.cget("text"))

    def revert_notes(self) -> None:
        self._set_notes(self._notes_saved)
        self._notes_modified()

    # ---- deleting ---------------------------------------------------------------

    def delete_facts(self) -> None:
        ids = self.facts.selection()
        if self.user_id is None or not ids:
            return
        texts = [self.facts.item(i, "text") for i in ids]
        preview = "\n".join(f"• {t}" for t in texts[:8]) + ("\n…" if len(texts) > 8 else "")
        if not messagebox.askyesno("Delete fact", f"Delete {len(ids)} fact(s)?\n\n{preview}",
                                   icon="warning", default="no", parent=self):
            return
        n = sum(self.store.delete_fact(int(i)) for i in ids)
        log.info("Memory: deleted %d fact(s) about %s", n, self.title_lbl.cget("text"))
        self.app.memory_changed()
        self.refresh()

    def wipe_user(self) -> None:
        u = self.store.get_user(self.user_id) if (self.user_id is not None and self.store) else None
        if not u:
            return
        if not messagebox.askyesno(
                "Wipe user",
                f"Delete everything stored about {u['display_name']}?\n\n"
                f"• their profile, summary and notes\n• {len(u['facts'])} facts\n"
                f"• their {u['message_count']} stored messages\n\n"
                "The bot's own replies to them are kept. If they talk again, a new profile starts.\n"
                "This can't be undone.", icon="warning", default="no", parent=self):
            return
        n = self.store.wipe_user(u["author_id"])
        log.info("Memory: wiped user %s (%d messages)", u["display_name"], n)
        self.user_id = None
        self._notes_saved = ""
        self.app.memory_changed()
        self.refresh()

    def wipe_channel(self) -> None:
        i = self.chan_box.current()
        if self.store is None or not (0 <= i < len(self._channels)):
            return
        c = self._channels[i]
        name = f"#{c['channel_name'] or '?'} ({c['channel_id']})"
        if not messagebox.askyesno(
                "Wipe channel",
                f"Delete all {c['messages']} stored messages from {name}?\n\n"
                "User profiles and facts are kept. This can't be undone.",
                icon="warning", default="no", parent=self):
            return
        n = self.store.wipe_channel(c["channel_id"])
        log.info("Memory: wiped channel %s (%d messages)", name, n)
        self.app.memory_changed()
        self.refresh()

    def wipe_all(self) -> None:
        if self.store is None:
            return
        s = self.store.stats()
        answer = simpledialog.askstring(
            "Wipe ALL memory",
            f"This deletes ALL memory shared by every bot profile:\n"
            f"{s['messages']} messages, {s['users']} user profiles, {s['facts']} facts and all notes.\n"
            "It can't be undone.\n\nType WIPE to confirm:", parent=self)
        if (answer or "").strip() != "WIPE":
            if answer is not None:
                messagebox.showinfo("Wipe ALL memory", "Nothing was deleted.", parent=self)
            return
        self.store.wipe_all()
        log.info("Memory: wiped ALL memory (%d messages, %d users, %d facts)",
                 s["messages"], s["users"], s["facts"])
        self.user_id = None
        self._notes_saved = ""
        self.app.memory_changed()
        self.refresh()
