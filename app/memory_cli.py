"""Memory admin from the console (the Phase 4 GUI will offer the same actions).

  python -m app.memory_cli stats
  python -m app.memory_cli users [search]
  python -m app.memory_cli user <id or name>
  python -m app.memory_cli notes <id> "text"        # set owner notes ("" clears)
  python -m app.memory_cli search "text"           # keyword search over all messages
  python -m app.memory_cli wipe-user <id>
  python -m app.memory_cli wipe-channel <channel id>
  python -m app.memory_cli wipe-all
  python -m app.memory_cli reembed                 # rebuild all vectors with the current embed_model
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from app.config import base_dir
from app.memory import MemoryStore


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _pick_user(store: MemoryStore, who: str) -> dict | None:
    found = store.find_users(who)
    if not found:
        print(f"No user matching '{who}'.")
        return None
    if len(found) > 1:
        print(f"'{who}' matches {len(found)} users; use the id:")
        for u in found:
            print(f"  {u['author_id']}  {u['display_name']}")
        return None
    return store.get_user(found[0]["author_id"])


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} Type 'yes' to confirm: ").strip().lower() == "yes"


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect and manage the shared bot memory.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("users").add_argument("query", nargs="?", default="")
    sub.add_parser("user").add_argument("who")
    p = sub.add_parser("notes"); p.add_argument("who"); p.add_argument("text")
    sub.add_parser("search").add_argument("text")
    sub.add_parser("wipe-user").add_argument("who")
    sub.add_parser("wipe-channel").add_argument("channel_id", type=int)
    sub.add_parser("wipe-all")
    sub.add_parser("reembed")
    args = ap.parse_args()

    store = MemoryStore(base_dir() / "data" / "memory.db")
    try:
        if args.cmd == "stats":
            for k, v in store.stats().items():
                print(f"{k:>12}: {v}")
        elif args.cmd == "users":
            for u in store.find_users(args.query):
                print(f"{u['author_id']}  {u['display_name']:<20} msgs={u['message_count']:<5} "
                      f"last seen {_when(u['last_seen'])}")
        elif args.cmd == "user":
            u = _pick_user(store, args.who)
            if u:
                print(f"{u['display_name']} ({u['author_id']})")
                print(f"  also known as: {', '.join(u['names'])}")
                print(f"  first seen {_when(u['first_seen'])}, last seen {_when(u['last_seen'])}, "
                      f"{u['message_count']} messages")
                print(f"  summary: {u['summary'] or '-'}")
                print(f"  notes:   {u['notes'] or '-'}")
                print(f"  facts ({len(u['facts'])}):")
                for f in u["facts"]:
                    print(f"    - {f['fact']}")
        elif args.cmd == "notes":
            u = _pick_user(store, args.who)
            if u:
                store.set_notes(u["author_id"], args.text)
                print(f"Notes for {u['display_name']} saved.")
        elif args.cmd == "search":
            rows = store.keyword_search(args.text, 20, set())
            msgs = {r["id"]: r for r in store.conn.execute(
                f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(rows)) or 'NULL'})", [i for i, _ in rows])}
            for mid, _ in rows:
                m = msgs[mid]
                print(f"[{_when(m['timestamp'])} #{m['channel_name']}] {m['author_name']}: {m['text'][:200]}")
        elif args.cmd == "wipe-user":
            u = _pick_user(store, args.who)
            if u and _confirm(f"Delete {u['display_name']}'s profile, facts and messages?"):
                print(f"Deleted {store.wipe_user(u['author_id'])} messages and the profile.")
        elif args.cmd == "wipe-channel":
            if _confirm(f"Delete all stored messages from channel {args.channel_id}?"):
                print(f"Deleted {store.wipe_channel(args.channel_id)} messages.")
        elif args.cmd == "wipe-all":
            if _confirm("Delete ALL memory: every message, user profile and fact?"):
                store.wipe_all()
                print("Memory wiped.")
        elif args.cmd == "reembed":
            counts = store.embedding_counts()
            print(f"Stored vectors: {counts or 'none'}")
            if _confirm("Delete all vectors so the bot re-embeds every message with the current embed_model?"):
                print(f"Deleted {store.clear_embeddings()} vectors. The bot rebuilds them in the background "
                      f"(newest first) next time it runs; keyword search works meanwhile.")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
