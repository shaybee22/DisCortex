"""Runs the Discord bot on a background thread with its own asyncio loop.

The GUI talks to it only through:
  - RuntimeState (thread-safe on/off + mode switches), and
  - the `events` queue, which receives ("status", dict), ("log", levelno, text), ("fatal", text).
Phase 4b's live profile switching = stop() this runner and start a new one.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from pathlib import Path

import discord

from app.bot import AFKBot
from app.config import Config, RuntimeState
from app.llm import LLMError, OllamaClient, model_installed
from app.memory import MemoryStore
from app.profiles import Profile

log = logging.getLogger(__name__)

STATUS_SECONDS = 1        # how often the GUI gets a status snapshot
OLLAMA_CHECK_SECONDS = 30
DISCORD_RETRY_SECONDS = 15


class QueueLogHandler(logging.Handler):
    """Forwards log records to the GUI's event queue."""

    def __init__(self, events: queue.Queue):
        super().__init__(level=logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put_nowait(("log", record.levelno, self.format(record)))
        except Exception:
            self.handleError(record)


class BotRunner:
    def __init__(self, cfg: Config, profile: Profile, state: RuntimeState, token: str,
                 memory_path: Path, events: queue.Queue):
        self.cfg, self.profile, self.state = cfg, profile, state
        self._token = token
        self.memory_path = memory_path
        self.events = events
        self.bot: AFKBot | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self.ollama: dict = {"ok": None, "error": "", "model_ok": None, "embed_ok": None}
        self.discord_error = ""

    # ---- called from the GUI thread ------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="bot", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15) -> None:
        """Close the bot and wait for the thread to finish."""
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:
                pass  # loop already gone
        if self._thread is not None:
            self._thread.join(timeout)

    def memory_changed(self) -> None:
        """The GUI edited or wiped the shared database through its own connection:
        drop the bot's cached vectors and wake its embed worker (to re-embed if needed)."""
        loop, bot = self._loop, self.bot
        if loop is None or loop.is_closed() or bot is None or bot.memory is None:
            return
        try:
            bot._db_thread.submit(bot.memory.drop_cache)
            loop.call_soon_threadsafe(bot._embed_wake.set)
        except RuntimeError:
            pass  # bot is shutting down

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- bot thread ----------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("Bot thread crashed")
            self.events.put(("fatal", "The bot thread crashed; see the log."))

    def _request_stop(self) -> None:
        self._stopping.set()
        if self.bot is not None and not self.bot.is_closed():
            asyncio.ensure_future(self.bot.close())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        memory = MemoryStore(self.memory_path)
        s = memory.stats()
        log.info("Memory: %d messages, %d users, %d facts", s["messages"], s["users"], s["facts"])
        monitor = asyncio.create_task(self._monitor())
        try:
            while not self._stopping.is_set():
                self.bot = bot = AFKBot(self.cfg, self.profile, self.state, memory)
                try:
                    log.info("Connecting to Discord as bot profile '%s'...", self.profile.name)
                    await bot.start(self._token)  # returns when closed; reconnects by itself
                except discord.LoginFailure:
                    self._fatal("Discord rejected the bot token. Change it in Settings "
                                "(or run with --reset-token).")
                    break
                except discord.PrivilegedIntentsRequired:
                    self._fatal("Enable 'Message Content Intent' and 'Server Members Intent' in the "
                                "Developer Portal (Bot > Privileged Gateway Intents), then restart.")
                    break
                except Exception as e:
                    self.discord_error = f"{type(e).__name__}: {e}"
                    log.error("Can't connect to Discord (%s). Retrying in %ds.", self.discord_error,
                              DISCORD_RETRY_SECONDS)
                finally:
                    if not bot.is_closed():
                        await bot.close()
                if self._stopping.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stopping.wait(), DISCORD_RETRY_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            monitor.cancel()
            self._post_status()
            memory.close()
            log.info("Bot stopped")

    def _fatal(self, text: str) -> None:
        self.discord_error = text
        log.error(text)
        self.events.put(("fatal", text))

    async def _monitor(self) -> None:
        """Periodic Ollama health check + status snapshots for the GUI."""
        llm = OllamaClient(self.profile.ollama_url, api_key=self.profile.ollama_api_key)
        last_check = -OLLAMA_CHECK_SECONDS
        while True:
            if time.monotonic() - last_check >= OLLAMA_CHECK_SECONDS:
                last_check = time.monotonic()
                try:
                    models = await llm.list_models()
                    self.ollama = {"ok": True, "error": "",
                                   "model_ok": model_installed(self.profile.model, models),
                                   "embed_ok": model_installed(self.cfg.embed_model, models)}
                except LLMError as e:
                    if self.ollama.get("ok") is not False:
                        log.error("%s Will keep retrying.", e)
                    self.ollama = {"ok": False, "error": str(e), "model_ok": None, "embed_ok": None}
            self._post_status()
            await asyncio.sleep(STATUS_SECONDS)

    def _post_status(self) -> None:
        """Snapshot built on the bot thread, so the GUI never reads bot internals directly."""
        bot = self.bot
        connected = bool(bot and bot.connected and not bot.is_closed())
        engaged = []
        if bot is not None:
            for cid, uid in bot.engagement.engaged_keys():
                ch = bot.get_channel(cid)
                engaged.append(f"{bot._engaged_names.get((cid, uid), uid)} (#{getattr(ch, 'name', cid)})")
        self.events.put(("status", {
            "profile": self.profile.name,
            "mode": self.profile.mode,
            "model": self.profile.model,
            "ollama_url": self.profile.ollama_url,
            "embed_model": self.cfg.embed_model,
            "discord": "connected" if connected else ("error" if self.discord_error else "connecting"),
            "discord_user": str(bot.user) if bot and bot.user else "",
            "discord_error": self.discord_error if not connected else "",
            "ollama": dict(self.ollama),
            "engaged": engaged,
            "semantic_ok": bool(bot and bot.semantic_ok),
        }))
        if connected:
            self.discord_error = ""
