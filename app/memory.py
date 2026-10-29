"""Shared long-term memory + user profiles (SQLite). No Discord, GUI or network code.

One database (data/memory.db) serves every bot profile. A MemoryStore holds one
SQLite connection: use it from one thread at a time (the bot runs all calls on a
single worker thread). Other threads (e.g. the GUI) open their own MemoryStore;
WAL mode lets them share the file.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1
MIN_EMBED_CHARS = 15          # shorter messages ("lol", "ok") are keyword-search only
MIN_SIMILARITY = 0.5          # semantic hits below this cosine similarity are dropped
SNIPPET_CHARS = 300
FIRST_SUMMARY_AT = 5          # write a first summary once a user has this many facts
SUMMARY_EVERY = 20            # then refresh it after this many new/updated facts
MAX_FACT_CHARS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,     -- Discord message id
    channel_id   INTEGER NOT NULL,
    guild_id     INTEGER,
    channel_name TEXT,
    author_id    INTEGER NOT NULL,
    author_name  TEXT NOT NULL,
    text         TEXT NOT NULL,
    timestamp    REAL NOT NULL,           -- unix seconds
    is_bot_reply INTEGER NOT NULL DEFAULT 0,
    bot_profile  TEXT                     -- persona that said it (bot replies only)
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel_id, timestamp);
CREATE INDEX IF NOT EXISTS messages_author  ON messages(author_id, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS embeddings (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    embed_model TEXT NOT NULL,
    vector      BLOB NOT NULL,            -- float32
    PRIMARY KEY (message_id, embed_model)
);

CREATE TABLE IF NOT EXISTS users (
    author_id           INTEGER PRIMARY KEY,
    display_name        TEXT NOT NULL,
    names               TEXT NOT NULL DEFAULT '[]',   -- JSON list of known names/aliases
    first_seen          REAL NOT NULL,
    last_seen           REAL NOT NULL,
    message_count       INTEGER NOT NULL DEFAULT 0,
    summary             TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',     -- owner's manual notes
    facts_checked_until REAL NOT NULL DEFAULT 0,      -- fact extraction has read messages up to here
    facts_since_summary INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_facts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id         INTEGER NOT NULL,
    fact              TEXT NOT NULL,
    source_message_id INTEGER,
    created           REAL NOT NULL,
    updated           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS user_facts_author ON user_facts(author_id);
"""

_STOPWORDS = set("""
a an and are as at be been but by can could did do does doing for from had has have he her
hers him his how i i'm im if in into is it its it's just me my no not of on or our ours she so
some than that the their them then there these they this those to too up us was we were what
when where which who whom why will with would you your yours yeah yes ok okay lol hey hi hello
bot about know remember tell said say whats
""".split())


@dataclass
class StoredMessage:
    id: int
    channel_id: int
    guild_id: int | None
    channel_name: str | None
    author_id: int
    author_name: str
    text: str
    timestamp: float
    is_bot_reply: bool = False
    bot_profile: str | None = None


@dataclass
class UserSeen:
    """Name info for updating a user's profile when one of their messages is stored."""
    author_id: int
    display_name: str
    names: list[str]


@dataclass
class Hit:
    id: int
    channel_id: int
    channel_name: str | None
    author_name: str
    text: str
    timestamp: float
    score: float


def norm_fact(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


class MemoryStore:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()
        # embed_model -> (ids, unit-length float32 matrix); built lazily, appended to on insert
        self._vectors: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def close(self) -> None:
        self.conn.close()

    # ---- storing ------------------------------------------------------------

    def store(self, items: list[tuple[StoredMessage, UserSeen | None]]) -> list[int]:
        """Insert messages (ignoring ones already stored) and update user profiles
        for newly stored human messages. Returns the ids that were new."""
        new_ids = []
        with self.conn:
            for m, user in items:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO messages (id, channel_id, guild_id, channel_name, author_id,"
                    " author_name, text, timestamp, is_bot_reply, bot_profile) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (m.id, m.channel_id, m.guild_id, m.channel_name, m.author_id, m.author_name,
                     m.text, m.timestamp, int(m.is_bot_reply), m.bot_profile))
                if cur.rowcount != 1:
                    continue
                new_ids.append(m.id)
                if user is not None:
                    self._touch_user(user, m.timestamp)
        return new_ids

    def _touch_user(self, u: UserSeen, ts: float) -> None:
        row = self.conn.execute("SELECT names, first_seen, last_seen FROM users WHERE author_id=?",
                                (u.author_id,)).fetchone()
        if row is None:
            names = sorted({n for n in u.names if n})
            self.conn.execute(
                "INSERT INTO users (author_id, display_name, names, first_seen, last_seen, message_count)"
                " VALUES (?,?,?,?,?,1)", (u.author_id, u.display_name, json.dumps(names), ts, ts))
            return
        names = sorted(set(json.loads(row["names"] or "[]")) | {n for n in u.names if n})
        newest = ts >= row["last_seen"]
        self.conn.execute(
            "UPDATE users SET names=?, message_count=message_count+1, first_seen=MIN(first_seen, ?),"
            " last_seen=MAX(last_seen, ?)" + (", display_name=?" if newest else "") + " WHERE author_id=?",
            (json.dumps(names), ts, ts, *( [u.display_name] if newest else [] ), u.author_id))

    # ---- embeddings ---------------------------------------------------------

    def missing_embeddings(self, embed_model: str, limit: int = 32) -> list[sqlite3.Row]:
        """Messages long enough to embed that have no vector for `embed_model` (newest first)."""
        return self.conn.execute(
            "SELECT m.id, m.author_name, m.text FROM messages m"
            " LEFT JOIN embeddings e ON e.message_id = m.id AND e.embed_model = ?"
            " WHERE e.message_id IS NULL AND length(m.text) >= ? ORDER BY m.id DESC LIMIT ?",
            (embed_model, MIN_EMBED_CHARS, limit)).fetchall()

    def add_embeddings(self, embed_model: str, items: list[tuple[int, list[float]]]) -> None:
        rows = [(mid, embed_model, np.asarray(v, dtype=np.float32).tobytes()) for mid, v in items]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO embeddings (message_id, embed_model, vector) VALUES (?,?,?)", rows)
        if embed_model in self._vectors and items:
            ids, mat = self._vectors[embed_model]
            new = _unit(np.array([np.asarray(v, dtype=np.float32) for _, v in items]))
            if mat.size and new.shape[1] != mat.shape[1]:
                self._vectors.pop(embed_model)  # dimension changed; rebuild on next search
                return
            keep = ~np.isin(ids, [mid for mid, _ in items])
            self._vectors[embed_model] = (np.concatenate([ids[keep], [mid for mid, _ in items]]),
                                          np.vstack([mat[keep], new]) if mat.size else new)

    def _matrix(self, embed_model: str) -> tuple[np.ndarray, np.ndarray]:
        if embed_model not in self._vectors:
            rows = self.conn.execute("SELECT message_id, vector FROM embeddings WHERE embed_model=?",
                                     (embed_model,)).fetchall()
            vecs = [np.frombuffer(r["vector"], dtype=np.float32) for r in rows]
            if vecs:
                dim = max(set(len(v) for v in vecs), key=[len(v) for v in vecs].count)
                pairs = [(r["message_id"], v) for r, v in zip(rows, vecs) if len(v) == dim]
                ids = np.array([p[0] for p in pairs], dtype=np.int64)
                mat = _unit(np.vstack([p[1] for p in pairs]))
            else:
                ids, mat = np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
            self._vectors[embed_model] = (ids, mat)
        return self._vectors[embed_model]

    def clear_embeddings(self) -> int:
        """'Re-embed all': drop every stored vector. The bot's embed worker rebuilds them
        with the current embed_model in the background; keyword search works meanwhile."""
        with self.conn:
            n = self.conn.execute("DELETE FROM embeddings").rowcount
        self._vectors.clear()
        return n

    def embedding_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT embed_model, COUNT(*) FROM embeddings GROUP BY embed_model")}

    # ---- retrieval ----------------------------------------------------------

    def keyword_search(self, text: str, k: int, exclude: set[int],
                       ignore_words: set[str] = frozenset()) -> list[tuple[int, float]]:
        skip = _STOPWORDS | {w.lower() for w in ignore_words}
        words = [w for w in re.findall(r"\w+", text.lower()) if len(w) >= 3 and w not in skip]
        words = list(dict.fromkeys(words))[:12]
        if not words or k <= 0:
            return []
        query = " OR ".join(f'"{w}"' for w in words)
        rows = self.conn.execute(
            "SELECT rowid, bm25(messages_fts) AS rank FROM messages_fts WHERE messages_fts MATCH ?"
            " ORDER BY rank LIMIT ?", (query, k + len(exclude) + 10)).fetchall()
        return [(r[0], -r[1]) for r in rows if r[0] not in exclude][:k]

    def semantic_search(self, embed_model: str, query_vec: list[float], k: int,
                        exclude: set[int]) -> list[tuple[int, float]]:
        ids, mat = self._matrix(embed_model)
        if not len(ids) or k <= 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        if q.shape[0] != mat.shape[1]:
            return []
        sims = mat @ (q / (np.linalg.norm(q) or 1.0))
        if exclude:
            sims = np.where(np.isin(ids, list(exclude)), -1.0, sims)
        top = np.argsort(-sims)[:k]
        return [(int(ids[i]), float(sims[i])) for i in top if sims[i] >= MIN_SIMILARITY]

    def recall(self, text: str, query_vec: list[float] | None, embed_model: str, k: int,
               channel_id: int, prefer_channel: bool, exclude: set[int],
               ignore_words: set[str] = frozenset()) -> list[Hit]:
        """Hybrid search across ALL channels and bot profiles, merged by reciprocal rank.
        `ignore_words` (e.g. the bot's trigger words) are left out of the keyword query."""
        if k <= 0:
            return []
        scores: dict[int, float] = {}
        for ranked in (self.keyword_search(text, k * 2, exclude, ignore_words),
                       self.semantic_search(embed_model, query_vec, k * 2, exclude) if query_vec else []):
            for rank, (mid, _) in enumerate(ranked):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
        if not scores:
            return []
        rows = {r["id"]: r for r in self.conn.execute(
            f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(scores))})", list(scores))}
        hits = []
        for mid, s in scores.items():
            r = rows.get(mid)
            if r is None:
                continue
            if prefer_channel and r["channel_id"] == channel_id:
                s *= 1.25
            hits.append(Hit(mid, r["channel_id"], r["channel_name"], r["author_name"], r["text"], r["timestamp"], s))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def memory_block(self, author_id: int, display_name: str, hits: list[Hit], max_chars: int) -> str:
        """Compact text for the system prompt, capped at max_chars."""
        if max_chars <= 0:
            return ""
        lines: list[str] = []
        user = self.get_user(author_id)
        if user:
            if user["summary"]:
                lines.append(f"About {display_name}: {user['summary']}")
            if user["facts"]:
                lines.append("Known facts: " + "; ".join(f["fact"] for f in user["facts"]))
            if user["notes"]:
                lines.append(f"Owner's notes about {display_name}: {user['notes']}")
        if hits:
            lines.append("Relevant past conversation:")
            for h in sorted(hits, key=lambda h: h.timestamp):
                date = datetime.fromtimestamp(h.timestamp).strftime("%Y-%m-%d")
                chan = f" #{h.channel_name}" if h.channel_name else ""
                text = h.text if len(h.text) <= SNIPPET_CHARS else h.text[:SNIPPET_CHARS] + "…"
                lines.append(f"[{date}{chan}] {h.author_name}: {text}")
        if not lines:
            return ""
        header = "Memory from earlier conversations (use it naturally when relevant; don't recite it):"
        out, size = [header], len(header)
        for line in lines:
            if size + 1 + len(line) > max_chars:
                room = max_chars - size - 2
                if room > 40 and not line.startswith("["):  # trim a long facts/summary line
                    out.append(line[:room] + "…")
                break
            out.append(line)
            size += 1 + len(line)
        return "\n".join(out) if len(out) > 1 else ""

    # ---- users and facts ----------------------------------------------------

    def get_user(self, author_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM users WHERE author_id=?", (author_id,)).fetchone()
        if row is None:
            return None
        user = dict(row)
        user["names"] = json.loads(user["names"] or "[]")
        user["facts"] = [dict(r) for r in self.conn.execute(
            "SELECT id, fact, source_message_id, created, updated FROM user_facts"
            " WHERE author_id=? ORDER BY updated", (author_id,))]
        return user

    def find_users(self, query: str = "") -> list[dict]:
        q = query.strip()
        if q.isdigit():
            rows = self.conn.execute("SELECT * FROM users WHERE author_id=?", (int(q),))
        else:
            like = f"%{q.lower()}%"
            rows = self.conn.execute(
                "SELECT * FROM users WHERE lower(display_name) LIKE ? OR lower(names) LIKE ?"
                " ORDER BY last_seen DESC", (like, like))
        return [dict(r) for r in rows]

    def set_notes(self, author_id: int, notes: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET notes=? WHERE author_id=?", (notes.strip(), author_id))

    def set_summary(self, author_id: int, summary: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET summary=?, facts_since_summary=0 WHERE author_id=?",
                              (summary.strip(), author_id))

    def unchecked_messages(self, author_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """The author's messages that fact extraction hasn't read yet (oldest first)."""
        return self.conn.execute(
            "SELECT m.id, m.text, m.timestamp FROM messages m JOIN users u ON u.author_id = m.author_id"
            " WHERE m.author_id=? AND m.is_bot_reply=0 AND m.timestamp > u.facts_checked_until"
            " ORDER BY m.timestamp DESC LIMIT ?", (author_id, limit)).fetchall()[::-1]

    def apply_fact_changes(self, author_id: int, new_facts: list[str], updates: list[tuple[str, str]],
                           source_message_id: int | None, checked_until: float) -> tuple[list[str], list[str]]:
        """Insert new facts and rewrite updated ones, skipping duplicates.
        Returns (added, updated) fact texts."""
        now = time.time()
        added, changed = [], []
        with self.conn:
            existing = {r["id"]: r["fact"] for r in self.conn.execute(
                "SELECT id, fact FROM user_facts WHERE author_id=?", (author_id,))}
            by_norm = {norm_fact(f): fid for fid, f in existing.items()}

            # A new fact that is a more detailed version of an existing one
            # ("plays dayz" -> "plays dayz on pc") becomes an update instead.
            for fact in list(new_facts):
                n = norm_fact(fact)
                broader = next((e for e in by_norm if len(e) > 8 and e != n and f" {e} " in f" {n} "), None)
                if broader:
                    updates = [*updates, (existing[by_norm[broader]], fact)]
                    new_facts = [f for f in new_facts if f is not fact]

            def is_dup(fact: str) -> bool:
                n = norm_fact(fact)
                return not n or n in by_norm or any(f" {n} " in f" {e} " for e in by_norm if len(n) > 8)

            for old, new in updates:
                fid = by_norm.get(norm_fact(old))
                n = norm_fact(new)
                if fid is None or not n or n == norm_fact(old):
                    continue
                self.conn.execute("UPDATE user_facts SET fact=?, source_message_id=?, updated=? WHERE id=?",
                                  (new, source_message_id, now, fid))
                by_norm.pop(norm_fact(old), None)
                by_norm[n] = fid
                changed.append(new)
            for fact in new_facts:
                if is_dup(fact):
                    continue
                cur = self.conn.execute(
                    "INSERT INTO user_facts (author_id, fact, source_message_id, created, updated)"
                    " VALUES (?,?,?,?,?)", (author_id, fact, source_message_id, now, now))
                by_norm[norm_fact(fact)] = cur.lastrowid
                added.append(fact)
            self.conn.execute(
                "UPDATE users SET facts_checked_until=MAX(facts_checked_until, ?),"
                " facts_since_summary=facts_since_summary+? WHERE author_id=?",
                (checked_until, len(added) + len(changed), author_id))
        return added, changed

    def needs_summary(self, author_id: int) -> bool:
        row = self.conn.execute(
            "SELECT u.summary, u.facts_since_summary, COUNT(f.id) AS n FROM users u"
            " LEFT JOIN user_facts f ON f.author_id = u.author_id WHERE u.author_id=?",
            (author_id,)).fetchone()
        if row is None or row["n"] == 0:
            return False
        if not row["summary"]:
            return row["n"] >= FIRST_SUMMARY_AT
        return row["facts_since_summary"] >= SUMMARY_EVERY

    # ---- admin --------------------------------------------------------------

    def wipe_user(self, author_id: int) -> int:
        """Delete a user's profile, facts and messages. Returns messages deleted."""
        with self.conn:
            n = self.conn.execute("DELETE FROM messages WHERE author_id=?", (author_id,)).rowcount
            self.conn.execute("DELETE FROM user_facts WHERE author_id=?", (author_id,))
            self.conn.execute("DELETE FROM users WHERE author_id=?", (author_id,))
        self._vectors.clear()
        return n

    def wipe_channel(self, channel_id: int) -> int:
        with self.conn:
            n = self.conn.execute("DELETE FROM messages WHERE channel_id=?", (channel_id,)).rowcount
        self._vectors.clear()
        return n

    def wipe_all(self) -> None:
        with self.conn:
            for table in ("embeddings", "user_facts", "users", "messages"):
                self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute("VACUUM")
        self._vectors.clear()

    def delete_fact(self, fact_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM user_facts WHERE id=?", (fact_id,)).rowcount == 1

    def fact_counts(self) -> dict[int, int]:
        return dict(self.conn.execute("SELECT author_id, COUNT(*) FROM user_facts GROUP BY author_id").fetchall())

    def channels(self) -> list[dict]:
        """Channels that have stored messages: channel_id, channel_name, messages, last (timestamp)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT channel_id, MAX(channel_name) AS channel_name, COUNT(*) AS messages,"
            " MAX(timestamp) AS last FROM messages GROUP BY channel_id ORDER BY last DESC")]

    def embed_progress(self, embed_model: str) -> tuple[int, int]:
        """(messages with a vector for embed_model, messages long enough to embed)."""
        total = self.conn.execute("SELECT COUNT(*) FROM messages WHERE length(text) >= ?",
                                  (MIN_EMBED_CHARS,)).fetchone()[0]
        done = self.conn.execute("SELECT COUNT(*) FROM embeddings WHERE embed_model=?",
                                 (embed_model,)).fetchone()[0]
        return done, total

    def drop_cache(self) -> None:
        """Forget cached vectors (another connection changed the database)."""
        self._vectors.clear()

    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "messages": one("SELECT COUNT(*) FROM messages"),
            "bot_replies": one("SELECT COUNT(*) FROM messages WHERE is_bot_reply=1"),
            "users": one("SELECT COUNT(*) FROM users"),
            "facts": one("SELECT COUNT(*) FROM user_facts"),
            "channels": one("SELECT COUNT(DISTINCT channel_id) FROM messages"),
            "embeddings": self.embedding_counts(),
        }


def _unit(mat: np.ndarray) -> np.ndarray:
    mat = np.atleast_2d(mat).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


# ---- prompts for the background fact/summary jobs (pure text, no LLM calls) ----

FACT_SYSTEM = """You extract durable facts about one Discord user from their own messages.
Durable means still true in a few weeks: name, location, job, age, games and platforms they play,
their main weapon/class/build, preferences, pets, family, skills, schedule.
Do NOT include: moods, what they're doing right now, jokes, hypotheticals, questions,
or anything about other people.
Write each fact as a short third-person phrase without the user's name, e.g. "lives in Ohio",
"main DayZ weapon is a Mosin", "plays on PC".
Do not repeat existing facts. If a message changes an existing fact, put it in "updated" with the
existing fact copied exactly as "old".
Reply with JSON only: {"new": ["..."], "updated": [{"old": "...", "new": "..."}]}
Use empty lists when there is nothing to add."""


def fact_prompt(display_name: str, existing: list[str], messages: list[str]) -> list[dict]:
    known = "\n".join(f"- {f}" for f in existing) or "(none)"
    said = "\n".join(f"- {m}" for m in messages)
    return [{"role": "system", "content": FACT_SYSTEM},
            {"role": "user", "content": f"User: {display_name}\n\nExisting facts:\n{known}\n\n"
                                        f"Their recent messages:\n{said}"}]


def parse_fact_reply(raw: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse the model's JSON. Bad output -> ([], [])."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0) if m else raw)
    except (ValueError, TypeError):
        return [], []
    if not isinstance(data, dict):
        return [], []

    def clean(s) -> str:
        return str(s).strip().strip("-•* ").strip()[:MAX_FACT_CHARS] if isinstance(s, (str, int, float)) else ""

    new = [c for c in (clean(f) for f in data.get("new") or [] if not isinstance(f, (dict, list))) if c][:10]
    updated = []
    for u in data.get("updated") or []:
        if isinstance(u, dict) and clean(u.get("old")) and clean(u.get("new")):
            updated.append((clean(u["old"]), clean(u["new"])))
    return new, updated[:10]


def summary_prompt(display_name: str, facts: list[str], notes: str) -> list[dict]:
    body = "\n".join(f"- {f}" for f in facts)
    if notes:
        body += f"\n\nOwner's notes: {notes}"
    return [{"role": "system", "content":
             "Write a 2-3 sentence profile of a Discord user, for a chatbot that talks with them. "
             "Plain prose, third person, no lists, no speculation beyond the facts given."},
            {"role": "user", "content": f"User: {display_name}\nFacts:\n{body}"}]
