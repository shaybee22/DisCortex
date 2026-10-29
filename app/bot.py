"""Discord client for one active bot profile: triggers, engagement, short-term context, cooldown."""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import discord

from app import memory as mem
from app.config import Config, RuntimeState
from app.engagement import IGNORE, SIGNOFF, EngagementTracker, MessageInfo
from app.llm import EmbedModelMissing, LLMError, OllamaClient, model_installed
from app.profiles import Profile, build_channel_map

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
CONTEXT_MSG_CHARS = 500  # per-message cap for history sent to the model
SWEEP_SECONDS = 2        # how often to check engagement timeouts / toggle changes
SIGNOFF_INSTRUCTION = ("The last user is signing off. Reply with one short, in-character goodbye "
                       "(one sentence).")
EMBED_BATCH = 32
EMBED_IDLE_SECONDS = 30       # how long the embed worker sleeps when there's nothing to do
EMBED_RECHECK_SECONDS = 300   # how often to look for a missing embed model again
QUERY_EMBED_TIMEOUT = 15      # semantic search gives up (keyword-only) after this


def _nomic(model: str) -> bool:
    return "nomic" in model.lower()  # nomic-embed-text wants task prefixes


def doc_text(model: str, author: str, text: str) -> str:
    return ("search_document: " if _nomic(model) else "") + f"{author}: {text}"


def query_text(model: str, text: str) -> str:
    return ("search_query: " if _nomic(model) else "") + text


def clean_reply(text: str, max_chars: int, names: tuple[str, ...] = ()) -> str:
    text = text.strip()
    # Models sometimes echo the "Name: text" format; drop our own prefix.
    for n in names:
        if n and text.lower().startswith(n.lower() + ":"):
            text = text[len(n) + 1:].lstrip()
            break
    limit = min(max_chars, DISCORD_LIMIT)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def message_to_turn(m: discord.Message, my_id: int) -> dict | None:
    """Discord message -> chat turn. Own messages are 'assistant'."""
    text = m.clean_content.strip()
    if m.attachments:
        text = (text + " [attachment]").strip()
    if not text:
        return None
    if len(text) > CONTEXT_MSG_CHARS:
        text = text[:CONTEXT_MSG_CHARS] + "…"
    if m.author.id == my_id:
        return {"role": "assistant", "content": text}
    return {"role": "user", "content": f"{m.author.display_name}: {text}"}


def build_messages(profile: Profile, bot_name: str, turns: list[dict], memory_block: str = "",
                   instruction: str = "") -> list[dict]:
    system = profile.system_prompt.strip()
    system += (
        f"\n\nYou are '{bot_name}' in this chat. User messages are formatted 'Name: text'. "
        "Reply with only your message text, without a name prefix."
    )
    if memory_block:
        system += "\n\n" + memory_block
    if instruction:
        system += "\n\n" + instruction
    return [{"role": "system", "content": system}, *turns]


class AFKBot(discord.Client):
    def __init__(self, cfg: Config, profile: Profile, state: RuntimeState,
                 memory: mem.MemoryStore | None = None):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.members = True  # member names, for "addressed someone else by name"
        super().__init__(intents=intents)
        self.cfg = cfg
        self.state = state
        self.profile = profile
        self.llm = OllamaClient(profile.ollama_url, api_key=profile.ollama_api_key)
        self._channels: dict[int, Profile] = build_channel_map([profile])
        # Per-channel: latest message waiting for a reply, the worker answering it,
        # and when we last replied (for cooldown).
        self._pending: dict[int, tuple[discord.Message, str]] = {}  # (message, "reply"|"signoff")
        self._workers: dict[int, asyncio.Task] = {}
        self._last_reply: dict[int, float] = {}
        # Sticky conversations: (channel_id, author_id) -> engaged
        self.engagement = EngagementTracker()
        self._engaged_names: dict[tuple[int, int], str] = {}  # for log lines only
        self._seen_state = (state.enabled, state.mode_override)
        # Long-term memory: all SQLite work runs on one worker thread.
        self.memory = memory
        self._db_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-db")
        self.semantic_ok = True              # False while the embed model is missing
        self._embed_wake = asyncio.Event()
        self._extract_lock = asyncio.Lock()  # one fact-extraction job at a time
        self._extract_pending: set[int] = set()
        self._bg_tasks: set[asyncio.Task] = set()
        self.connected = False               # for the GUI status line

    # ---- startup --------------------------------------------------------

    async def setup_hook(self) -> None:
        self._health_task = asyncio.create_task(self._wait_for_ollama())
        self._sweep_task = asyncio.create_task(self._engagement_sweep())
        if self.memory is not None:
            self._embed_task = asyncio.create_task(self._embed_worker())

    async def close(self) -> None:
        self._closing_on_purpose = True
        await super().close()
        self._db_thread.shutdown(wait=True)

    async def _db(self, fn, *args):
        """Run a MemoryStore call on the memory thread."""
        return await asyncio.get_running_loop().run_in_executor(self._db_thread, fn, *args)

    # ---- engagement bookkeeping -----------------------------------------

    def _check_state_change(self) -> None:
        """Master toggle or mode switch clears every engagement."""
        now = (self.state.enabled, self.state.mode_override)
        if now != self._seen_state:
            what = "master toggle" if now[0] != self._seen_state[0] else "mode switch"
            self._seen_state = now
            for key in self.engagement.clear():
                self._log_disengage(key, what)

    async def _engagement_sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                self._check_state_change()
                for key in self.engagement.expire():
                    p = self._channels.get(key[0])
                    secs = f"{p.engage_timeout_seconds:g}s " if p else ""
                    self._log_disengage(key, f"timeout ({secs}silence)")
            except Exception:
                log.exception("Engagement sweep failed")

    def _log_disengage(self, key: tuple[int, int], reason: str) -> None:
        p = self._channels.get(key[0])
        name = self._engaged_names.pop(key, str(key[1]))
        log.info("[%s] Disengaged %s: %s", p.name if p else key[0], name, reason)

    async def _wait_for_ollama(self) -> None:
        while True:
            try:
                models = await self.llm.list_models()
                break
            except LLMError as e:
                log.error("%s Retrying in 10s.", e)
                await asyncio.sleep(10)
        log.info("Ollama reachable at %s (%d models)", self.profile.ollama_url, len(models))
        if not model_installed(self.profile.model, models):
            log.warning("Chat model '%s' is not on that server (run: ollama pull %s on that machine)",
                        self.profile.model, self.profile.model)
        if self.memory is not None and not model_installed(self.cfg.embed_model, models):
            self._embed_missing()

    def _embed_missing(self) -> None:
        if self.semantic_ok:
            log.warning("Embed model '%s' is not on %s; memory uses keyword search only. "
                        "Run: ollama pull %s on that machine", self.cfg.embed_model,
                        self.profile.ollama_url, self.cfg.embed_model)
        self.semantic_ok = False

    async def on_disconnect(self) -> None:
        if self.connected and not getattr(self, "_closing_on_purpose", False):
            log.warning("Disconnected from Discord; reconnecting automatically...")
        self.connected = False

    async def on_resumed(self) -> None:
        if not self.connected:
            log.info("Reconnected to Discord")
        self.connected = True

    async def on_ready(self) -> None:
        self.connected = True
        p = self.profile
        log.info("Logged in to Discord as %s (id %s)", self.user, self.user.id)
        log.info("Bot profile '%s': %s mode, model %s @ %s", p.name, p.mode, p.model, p.ollama_url)
        log.info("Master toggle is %s", "ON" if self.state.enabled else "OFF")
        for cid in self._channels:
            ch = self.get_channel(cid)
            where = f"#{ch.name} in {ch.guild.name}" if ch else "NOT VISIBLE (check ID / permissions)"
            log.info("Watching channel %s: %s", cid, where)

    # ---- message handling -----------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:  # own messages and all other bots
            return
        profile = self._channels.get(message.channel.id)
        if profile is None:
            return
        self._check_state_change()
        if not self.state.enabled:
            return
        preview = message.clean_content.replace("\n", " ")
        log.info("[%s] #%s %s: %s", profile.name, getattr(message.channel, "name", message.channel.id),
                 message.author.display_name, preview if len(preview) <= 120 else preview[:120] + "…")
        await self._observe(message, profile)
        kind = await self._decide(message, profile)
        if kind is None:
            return

        cid = message.channel.id
        superseded = self._pending.get(cid)
        if superseded is not None:
            log.info("[%s] Skipped msg from %s (newer message queued)", profile.name, superseded[0].author.display_name)
        self._pending[cid] = (message, kind)
        if cid not in self._workers:
            self._workers[cid] = asyncio.create_task(self._channel_worker(cid))

    async def _decide(self, message: discord.Message, profile: Profile) -> str | None:
        """Return "reply", "signoff", or None (don't reply)."""
        who = message.author.display_name
        mode = self.state.mode_override or profile.mode
        if mode == "all":
            log.info("[%s] Message from %s (all mode)", profile.name, who)
            return "reply"

        target = await self._reply_target(message)
        trigger_reason = None
        if profile.respond_to_replies and target is not None and target.author.id == self.user.id:
            trigger_reason = "reply to bot"
        elif profile.respond_to_mentions and self.user in message.mentions:
            trigger_reason = "@mention"
        elif profile.matches_trigger(message.content):
            trigger_reason = "trigger word"

        skip_ids = {self.user.id, message.author.id}
        info = MessageInfo(
            text=message.content,
            trigger_reason=trigger_reason,
            mentions_other=any(m.id not in skip_ids for m in message.mentions),
            replies_to_other=target is not None and target.author.id not in skip_ids,
            other_names=lambda: self._other_member_names(message.channel, skip_ids),
        )
        key = (message.channel.id, message.author.id)
        outcome = self.engagement.evaluate(key, info, profile.engage_settings)

        if outcome.event in ("engaged", "re-engaged"):
            self._engaged_names[key] = who
            log.info("[%s] %s %s (%s)", profile.name, outcome.event.capitalize(), who, outcome.reason)
        elif outcome.event == "disengaged":
            self._engaged_names[key] = who
            self._log_disengage(key, outcome.reason)
        elif outcome.action != IGNORE:
            log.info("[%s] Message from %s (%s)", profile.name, who, outcome.reason)

        if outcome.action == IGNORE:
            return None
        return "signoff" if outcome.action == SIGNOFF else "reply"

    async def _reply_target(self, message: discord.Message) -> discord.Message | None:
        """The message this one Discord-replies to, if any."""
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        target = ref.resolved
        if target is None:
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        return None if isinstance(target, discord.DeletedReferencedMessage) else target

    @staticmethod
    def _other_member_names(channel, skip_ids: set[int]) -> list[str]:
        names: list[str] = []
        for m in getattr(channel, "members", []):
            if m.id in skip_ids:
                continue
            names += [n for n in (m.display_name, m.name, getattr(m, "global_name", None)) if n]
        return names

    async def _channel_worker(self, cid: int) -> None:
        """One worker per channel: answers the latest pending message, one at a time."""
        try:
            while cid in self._pending:
                profile = self._channels.get(cid)
                if profile is None:  # profile removed while queued
                    self._pending.pop(cid, None)
                    break
                wait = self._last_reply.get(cid, 0) + profile.cooldown_seconds - time.monotonic()
                if wait > 0:
                    log.info("[%s] Cooldown: waiting %.1fs", profile.name, wait)
                    await asyncio.sleep(wait)
                    continue  # a newer message may have replaced the pending one
                message, kind = self._pending.pop(cid)
                if not self.state.enabled:
                    log.info("[%s] Skipped (bot was turned off)", profile.name)
                    continue
                try:
                    await self._respond(message, profile, kind)
                except Exception:
                    log.exception("[%s] Unexpected error while replying", profile.name)
                finally:
                    self._last_reply[cid] = time.monotonic()
        finally:
            self._workers.pop(cid, None)

    async def _respond(self, message: discord.Message, profile: Profile, kind: str = "reply") -> None:
        async with message.channel.typing():
            turns, history = await self._history_turns(message, profile)
            await self._store(history, profile)  # messages seen before we were running
            # Skip memories the model already sees in the short-term window.
            memory_block = await self._recall(message, profile, {m.id for m in history} | {message.id})
            me = message.guild.me if message.guild else self.user
            instruction = SIGNOFF_INSTRUCTION if kind == "signoff" else ""
            messages = build_messages(profile, me.display_name, turns, memory_block, instruction)
            try:
                text = await self.llm.chat(profile.model, messages, temperature=profile.temperature)
            except LLMError as e:
                log.error("[%s] LLM error, not replying: %s", profile.name, e)
                return
        text = clean_reply(text, profile.max_reply_chars, (me.display_name, self.user.name))
        if not text:
            log.warning("[%s] Empty reply from model, not posting", profile.name)
            return
        try:
            sent = await message.reply(text, mention_author=False)
        except discord.HTTPException as e:
            log.error("[%s] Failed to send reply: %s", profile.name, e)
            return
        # The bot's reply counts as activity, so the timeout runs from here.
        self.engagement.touch((message.channel.id, message.author.id))
        log.info("[%s] %s %s (%d chars, %d context msgs)", profile.name,
                 "Said goodbye to" if kind == "signoff" else "Replied to",
                 message.author.display_name, len(text), len(turns) - 1)
        await self._observe(sent, profile)
        self._schedule_fact_extraction(message.author.id, message.author.display_name, profile)

    async def _history_turns(self, message: discord.Message,
                             profile: Profile) -> tuple[list[dict], list[discord.Message]]:
        """Last N channel messages before `message`, oldest first, then `message`.
        Returns (chat turns, the history messages)."""
        history: list[discord.Message] = []
        if profile.context_messages > 0:
            try:
                async for m in message.channel.history(limit=profile.context_messages, before=message):
                    history.append(m)
            except discord.HTTPException as e:
                log.warning("[%s] Could not read history (need Read Message History?): %s", profile.name, e)
        history.reverse()
        turns = [t for m in (*history, message) if (t := message_to_turn(m, self.user.id))]
        return turns, history

    # ---- long-term memory ---------------------------------------------------

    async def _observe(self, message: discord.Message, profile: Profile) -> None:
        """Store a watched message (or our own reply) and update the author's profile."""
        await self._store([message], profile)

    def _to_stored(self, m: discord.Message, profile: Profile) -> tuple[mem.StoredMessage, mem.UserSeen | None] | None:
        mine = m.author.id == self.user.id
        if m.author.bot and not mine:
            return None  # other bots are never stored
        text = m.clean_content.strip()
        if m.attachments:
            text = (text + " [attachment]").strip()
        if not text:
            return None
        stored = mem.StoredMessage(
            id=m.id, channel_id=m.channel.id, guild_id=m.guild.id if m.guild else None,
            channel_name=getattr(m.channel, "name", None), author_id=m.author.id,
            author_name=m.author.display_name, text=text, timestamp=m.created_at.timestamp(),
            is_bot_reply=mine, bot_profile=profile.name if mine else None)
        user = None if mine else mem.UserSeen(
            m.author.id, m.author.display_name,
            [m.author.display_name, m.author.name, getattr(m.author, "global_name", None)])
        return stored, user

    async def _store(self, messages: list[discord.Message], profile: Profile) -> None:
        if self.memory is None or not messages:
            return
        try:
            items = [it for m in messages if (it := self._to_stored(m, profile))]
            if items and await self._db(self.memory.store, items):
                self._embed_wake.set()
        except Exception:
            log.exception("[%s] Could not store message(s) in memory", profile.name)

    async def _recall(self, message: discord.Message, profile: Profile, exclude: set[int]) -> str:
        """Memory block for the system prompt: user profile + relevant past conversation."""
        if self.memory is None or not profile.memory_enabled:
            return ""
        try:
            text = message.clean_content
            vec = None
            if self.semantic_ok and profile.memory_k > 0:
                try:
                    vec = (await self.llm.embed(self.cfg.embed_model, [query_text(self.cfg.embed_model, text)],
                                                timeout=QUERY_EMBED_TIMEOUT))[0]
                except EmbedModelMissing:
                    self._embed_missing()
                except LLMError as e:
                    log.warning("[%s] Query embedding failed, keyword-only this time: %s", profile.name, e)
            me = message.guild.me if message.guild else self.user
            ignore = {w for t in profile.triggers for w in t.split()} | {*me.display_name.split(), self.user.name}
            hits = await self._db(self.memory.recall, text, vec, self.cfg.embed_model, profile.memory_k,
                                  message.channel.id, profile.prefer_current_channel, exclude, ignore)
            block = await self._db(self.memory.memory_block, message.author.id,
                                   message.author.display_name, hits, profile.memory_max_chars)
            if block:
                log.info("[%s] Memory: %d past message(s)%s, %d chars", profile.name, len(hits),
                         "" if vec is not None else " (keyword-only)", len(block))
            return block
        except Exception:
            log.exception("[%s] Memory recall failed; replying without it", profile.name)
            return ""

    async def _embed_worker(self) -> None:
        """Embed stored messages in the background (newest first; also backfills old rows
        and rows from before an embed_model change). Never blocks replies."""
        model = self.cfg.embed_model
        while True:
            try:
                if not self.semantic_ok:
                    await asyncio.sleep(EMBED_RECHECK_SECONDS)
                    if model_installed(model, await self.llm.list_models()):
                        log.info("Embed model '%s' found; semantic memory search is back on", model)
                        self.semantic_ok = True
                    continue
                rows = await self._db(self.memory.missing_embeddings, model, EMBED_BATCH)
                if not rows:
                    self._embed_wake.clear()
                    try:
                        await asyncio.wait_for(self._embed_wake.wait(), EMBED_IDLE_SECONDS)
                    except asyncio.TimeoutError:
                        pass
                    continue
                vectors = await self.llm.embed(model, [doc_text(model, r["author_name"], r["text"]) for r in rows])
                await self._db(self.memory.add_embeddings, model, [(r["id"], v) for r, v in zip(rows, vectors)])
            except asyncio.CancelledError:
                raise
            except EmbedModelMissing:
                self._embed_missing()
            except LLMError as e:
                log.warning("Embedding failed, retrying in %ds: %s", EMBED_IDLE_SECONDS, e)
                await asyncio.sleep(EMBED_IDLE_SECONDS)
            except Exception:
                log.exception("Embed worker error")
                await asyncio.sleep(EMBED_IDLE_SECONDS)

    def _schedule_fact_extraction(self, author_id: int, name: str, profile: Profile) -> None:
        if self.memory is None or author_id in self._extract_pending:
            return
        self._extract_pending.add(author_id)
        task = asyncio.create_task(self._extract_facts(author_id, name, profile))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _extract_facts(self, author_id: int, name: str, profile: Profile) -> None:
        """Background: learn durable facts about `author_id` from their unread messages."""
        try:
            async with self._extract_lock:
                rows = await self._db(self.memory.unchecked_messages, author_id)
                if not rows:
                    return
                user = await self._db(self.memory.get_user, author_id)
                existing = [f["fact"] for f in (user or {}).get("facts", [])]
                raw = await self.llm.chat(profile.model, mem.fact_prompt(name, existing, [r["text"] for r in rows]),
                                          temperature=0.2, json_output=True)
                new, updated = mem.parse_fact_reply(raw)
                added, changed = await self._db(self.memory.apply_fact_changes, author_id, new, updated,
                                                rows[-1]["id"], rows[-1]["timestamp"])
                if added or changed:
                    log.info("[%s] Learned about %s: %s", profile.name, name,
                             "; ".join([*added, *(f"(updated) {c}" for c in changed)]))
                if await self._db(self.memory.needs_summary, author_id):
                    user = await self._db(self.memory.get_user, author_id)
                    summary = await self.llm.chat(
                        profile.model, mem.summary_prompt(name, [f["fact"] for f in user["facts"]], user["notes"]),
                        temperature=0.3)
                    if summary.strip():
                        await self._db(self.memory.set_summary, author_id, summary)
                        log.info("[%s] Updated summary for %s", profile.name, name)
        except LLMError as e:
            log.warning("[%s] Fact extraction for %s failed (will retry after the next reply): %s",
                        profile.name, name, e)
        except Exception:
            log.exception("[%s] Fact extraction for %s crashed", profile.name, name)
        finally:
            self._extract_pending.discard(author_id)
