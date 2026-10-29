# DisCortex — Project Spec

**DisCortex** · Portable Discord AI Host · *Different brains. Different personalities. One memory.*

A portable Windows desktop app that links Discord **text** channels to an **Ollama** LLM (local or on another machine on the LAN).
When the owner is AFK, the bot reads channel messages and replies in character.
Uses an official **Discord bot account** (bot token). NO self-bot / user-account automation, ever.

## Core concepts (read this first)
- **Bot profile** = WHO the bot is and WHERE its brain lives: persona/system prompt, trigger words, behavior settings, channels to watch, Ollama URL, model. Exactly **one bot profile is active per run**, chosen at startup.
- **User profiles** = what the bot knows about each Discord user it has talked to. **Shared by ALL bot profiles.**
- **Memory database** = every conversation, searchable by keyword and meaning. **Shared by ALL bot profiles.**
- Switching bot profiles changes personality, behavior, or which Ollama server is used. It never splits or hides memory: every bot profile sees every user profile and every past conversation.
- Everything (config, bot profiles, memory, user profiles) lives in the app folder, so copying the folder to another PC moves the whole bot. Only the Discord token stays behind (see Token).

## Stack
- Python 3.11+
- `discord.py` 2.x (bot client, asyncio)
- `httpx` (async HTTP to Ollama)
- `PyYAML` (config + bot profiles)
- `numpy` (vector similarity)
- `tkinter` (control window) + `pystray` + `Pillow` (tray icon)
- `pynput` (global hotkeys)
- `keyring` (store bot token in Windows Credential Manager)
- `PyInstaller` (`--onefile` portable exe)

Ask before adding any dependency not on this list.

**`requirements.txt`** lives in the project root, lists every runtime dependency with pinned versions (`pip freeze`-style `==` pins for the packages above), and is updated in the same change whenever a dependency is added or bumped. Build-only tools (PyInstaller) go in `requirements-build.txt`.

**`setup.bat`** (run-from-source on another machine): `cd /d "%~dp0"`, check that Python 3.11+ is on PATH (clear message and exit if not), create `.venv` if missing, then `.venv\Scripts\python.exe -m pip install -r requirements.txt`. Safe to rerun.

## Ollama (local or remote)
- URL comes from the active bot profile. Default `http://localhost:11434`; can be any LAN box, e.g. `http://192.168.1.50:11434`.
- Chat: `POST /api/chat` with `"stream": false`
  - body: `{ "model": ..., "messages": [...], "stream": false, "options": {"temperature": ..., "num_ctx": ...}, "keep_alive": "30m" }`
  - reply text: `response["message"]["content"]`
- Model list: `GET /api/tags` → `models[].name`. Only models already pulled on that server can be selected.
- Embeddings: `POST /api/embed` on the same server, model from global `embed_model`.
- Startup health check: `GET /api/tags`. If unreachable, show a clear error naming the URL ("Can't reach Ollama at http://192.168.1.50:11434. Is it running and listening on the network?") and keep retrying. Do not crash.
- Also check at startup that the chosen chat model and `embed_model` exist on that server; warn clearly if not (e.g. "run: ollama pull nomic-embed-text on that machine").
- Timeouts: 120s+ on chat requests (first call loads the model into VRAM).
- Strip `<think>...</think>` blocks from replies (qwen3, deepseek-r1 and other reasoning models).
- All Ollama calls live in `llm.py`.
- README note for remote servers: on the Ollama machine set `OLLAMA_HOST=0.0.0.0` and allow TCP 11434 through its firewall.

## Discord setup (document in README)
- Create the app and bot in the Discord Developer Portal; copy the bot token
- **Enable "Message Content Intent"** under Bot → Privileged Gateway Intents
- Invite URL scopes: `bot`; permissions: View Channels, Send Messages, Read Message History
- Intents in code: `guilds`, `guild_messages`, `message_content`, `members` (for display names; also enable "Server Members Intent" in the portal)

## Token
- One Discord bot token, shared by all bot profiles.
- Stored via `keyring` (service `discord-afk-responder`), never written to disk in plaintext, never logged.
- Keyring is per-PC, so on a new machine the startup flow asks for the token once. Intended.

## Startup flow
Runs every launch (console version in Phase 2c; the GUI wizard in Phase 4 replaces it using the same `profiles.py` logic).
1. If no token in keyring → ask for it, save it.
2. List bot profiles in `bot_profiles/` → **Load existing** or **Create new** (also: Edit, Duplicate, Delete).
   - Remember the last-used profile and offer it as the default (Enter = load last).
3. **Create new / Edit** asks, in order:
   1. Profile name
   2. Ollama URL (default `http://localhost:11434`) → test it immediately via `/api/tags`; on failure, let the user retry or re-enter
   3. Chat model → numbered list from that server's `/api/tags`; must pick one of them
   4. System prompt / persona (multi-line; finish with a blank line, or open in Notepad for long prompts)
   5. Trigger word(s), comma-separated
   6. Mode: `trigger` or `all`
   7. Channel ID(s) to watch, comma-separated
   8. Everything else gets defaults (editable in the YAML)
4. Run health checks (Ollama reachable, chat model present, embed model present), then connect to Discord.

## Files and layout
Base dir = `Path(sys.executable).parent` when frozen, else the project root. Everything is relative to it.

```
app/
  main.py          # entry: startup flow, then GUI (main thread) + bot (background thread)
  bot.py           # discord.py client, message handling
  llm.py           # Ollama client (chat, tags, embed)
  profiles.py      # bot profile load/save/validate/create
  engagement.py    # sticky conversation logic (pure, no Discord imports)
  memory.py        # SQLite memory + user profiles (no Discord/GUI imports)
  config.py        # global config load/save, base-dir logic
  gui.py           # control window, wizard, tray
  hotkeys.py       # pynput global hotkeys
config.yaml
bot_profiles/
  deep-thought.yaml
  dayz-grump.yaml
data/
  memory.db        # shared memory + user profiles
logs/
```

### config.yaml (global, applies to every bot profile)
```yaml
last_profile: deep-thought
embed_model: nomic-embed-text
enabled: false            # master on/off, starts OFF
hotkeys:
  toggle_enabled: "<ctrl>+<alt>+a"
  toggle_mode: "<ctrl>+<alt>+m"
```

### bot_profiles/<name>.yaml
```yaml
name: deep-thought
ollama_url: http://localhost:11434
model: llama3.1:8b
temperature: 0.8
system_prompt: |
  You are Deep Thought, the owner's AFK stand-in...
channel_ids: [123456789012345678]
mode: trigger                # "all" | "trigger"
triggers: ["deep thought", "dt"]
respond_to_mentions: true
respond_to_replies: true
context_messages: 15
cooldown_seconds: 8
max_reply_chars: 600

# engaged conversations
sticky_engage: true
engage_timeout_seconds: 120
disengage_phrases: ["not you", "wasn't talking to you", "i was talking to", "talking to someone else", "nobody asked you", "shut up bot", "go away", "stop talking"]
signoff_phrases: ["thanks deep thought", "bye deep thought", "later deep thought", "that's all"]

# memory (the data itself is always shared; these only tune retrieval)
memory_enabled: true
memory_k: 5
memory_max_chars: 1500
prefer_current_channel: true   # rank same-channel memories higher; never excludes others
```
Missing keys fall back to these defaults, so old profiles keep working.

## Message handling rules
1. Ignore if master toggle is off
2. Ignore own messages and all other bots
3. Ignore channels not in the active bot profile's `channel_ids`
4. Store the message in memory and update that user's profile (last seen, names) for every message in watched channels, whether or not the bot replies
5. Decide whether to reply: mode `all` → yes; mode `trigger` → trigger word (case-insensitive, whole word/phrase), @mention, reply to the bot, or an active engagement (see below)
6. Per-channel cooldown; at most one in-flight LLM request per channel (no stacking)
7. Build the prompt: system prompt + user profile block + retrieved memories + last N channel messages as `user`/`assistant` turns prefixed `DisplayName: text`
8. Typing indicator while waiting on Ollama
9. Clean the reply (strip think tags, trim to `max_reply_chars`, respect Discord's 2000-char limit), reply to the triggering message, store the reply in memory
10. On LLM error: log it, post nothing

## Engaged conversations (sticky trigger)
In `trigger` mode, a trigger (keyword, @mention, or reply to the bot) **engages** that author. While engaged, every message from that author in that channel gets a reply, no trigger needed.

- In-memory state: `engaged[(channel_id, author_id)] = last_activity_time`. Several users can be engaged at once.
- `last_activity_time` updates on the user's message AND on the bot's reply.
- **Disengage** when:
  1. **Timeout:** no message from that user for `engage_timeout_seconds`
  2. **Addressed someone else:** @mentions another member (not the bot) or Discord-replies to someone else. Don't reply.
  3. **Name-addressed someone else:** message starts with another channel member's display name/username followed by `,` `:` or whitespace. Don't reply.
  4. **Disengage phrase:** contains any `disengage_phrases`. Don't reply. If it's a `signoff_phrases` match, send one short in-character goodbye, then disengage.
  5. Master toggle off or mode switched clears all engagements.
- A trigger always (re)engages, even if a disengage rule matched in the same message.
- Log every engage/disengage with the reason.

## Memory and user profiles (shared, portable, local)
All in `data/memory.db` (SQLite). Bot profiles never get their own copy; one database serves all of them.

**Short-term:** last N channel messages fetched live from Discord history.

**User profiles** (`users` table): `author_id` (primary key), current display name, known names/aliases (JSON), first_seen, last_seen, message_count, `summary` (short paragraph), `notes` (manual, editable by the owner).
**User facts** (`user_facts` table): author_id, fact, source_message_id, created, updated.
- After the bot replies to someone, a background task asks the LLM to extract durable facts about that author ("main is a Glock", "lives in Ohio", "name is Mike"), dedupes against existing facts, and inserts/updates. Never blocks a reply.
- Periodically (e.g. every 20 new facts for a user) regenerate that user's `summary`.

**Conversation memory:**
- `messages`: id, channel_id, guild_id, author_id, author_name, text, timestamp, is_bot_reply, bot_profile (which persona said it, for bot replies)
- `messages_fts`: FTS5 over `text`
- `embeddings`: message_id, embed_model, vector (float32 BLOB). Embedded asynchronously after storing.
- Retrieval (hybrid): top-k semantic (numpy cosine) + top-k FTS5 keyword, deduped, searched across ALL channels and ALL bot profiles; same-channel results get a ranking boost when `prefer_current_channel` is on.
- Only compare vectors made with the current `embed_model`. If `embed_model` changes, keep working keyword-only for older rows and offer a "re-embed all" maintenance action.
- If the embed model is missing on the current Ollama server, warn and fall back to keyword-only. Don't crash.

**Injected into the prompt** (capped by `memory_max_chars`):
```
About <DisplayName>: <summary>
Known facts: <fact>; <fact>; ...
Relevant past conversation:
[2026-10-20 #dayz] Mike: ...
[2026-10-20 #dayz] Deep Thought: ...
```

**Admin:** view/edit a user's profile and notes; wipe one user, one channel, or everything.

## GUI (control window + tray)
The GUI is the normal way to run and configure the app. The console startup flow stays as a fallback (`--console`).

**Main window (dashboard):**
- Big ON/OFF toggle (master)
- Mode toggle: All / Trigger
- Status: Discord connected, Ollama URL + reachable, model, active bot profile, currently engaged users
- Bot profile dropdown + Load (live switch: reconnects to the new Ollama server/model without restarting)
- Live log pane: last ~200 events (incoming message, engaged, disengaged + reason, replied, skipped, memory saved, errors), color-coded, with a pause/clear button

**Profile editor (tab or dialog):** New / Edit / Duplicate / Delete, with fields for every bot profile key:
- Name, system prompt (large multi-line box), trigger words, mode, channel IDs (list with add/remove)
- Ollama URL (localhost, LAN IP like a Jetson, or a WAN/https address), optional API key / auth header (for a reverse proxy in front of Ollama), **Test connection** button
- Model: dropdown filled from that server's `/api/tags` after Test connection; Refresh button
- Engagement: sticky on/off, timeout seconds, disengage phrases, signoff phrases
- Memory tuning: enabled, k, max chars, prefer current channel
- Save validates (URL reachable warning, model exists, channel IDs numeric) but lets the user save anyway with a warning

**Settings tab:**
- Discord bot token: shows "token saved ✓" (never the token itself), **Change token** button (paste into a masked field, Ctrl+V and right-click both work), reconnects after change
- Embed model (dropdown from the active server), re-embed all
- Hotkeys
- "Start minimized to tray" option

**User profiles tab:** search, view facts/summary, edit notes, delete a fact, wipe a user. Plus wipe channel / wipe all (with confirmation).

**Tray:** minimize to tray; menu: Toggle, Mode, Open, Quit. Hotkeys mirror the GUI toggles.

## Remote Ollama (LAN / Jetsons / WAN)
- Any profile can point at any Ollama: `http://localhost:11434`, `http://192.168.1.x:11434` (e.g. a Jetson), or `https://ollama.example.com`.
- Support an optional `ollama_api_key` per profile, sent as `Authorization: Bearer <key>` when set (for a reverse proxy).
- README warning: Ollama has NO authentication. Never port-forward 11434 to the internet. For WAN use a VPN (Tailscale, WireGuard, SoftEther) or an https reverse proxy with auth.
- Network errors show the URL and keep retrying; switching profiles in the GUI is how you move the brain to another box.

## Branding
- Name: **DisCortex** (capital D, capital C). Tagline: "Different brains. Different personalities. One memory." Subtitle: "Portable Discord AI Host".
- Logo: `assets/discortex.png` (supplied by the owner). Generate `assets/discortex.ico` from it with Pillow (sizes 16, 24, 32, 48, 64, 128, 256).
- Use the name/logo for: window title, app header (logo + name + subtitle + tagline), tray icon + tooltip, exe icon and filename (`DisCortex.exe`), README title, log/startup banner.
- If the logo file is missing, fall back to a text-only header and a plain generated tray icon. Never crash over branding.
- Do NOT rename the keyring service (`discord-afk-responder`) or move any data paths; existing tokens, profiles and memory must survive the rename untouched.

## Threading
- tkinter owns the main thread; discord.py runs its asyncio loop in a daemon thread
- GUI → bot via lock-guarded shared state; bot → GUI via `queue.Queue` polled with `root.after()`
- Never touch tkinter widgets from the bot thread
- SQLite: one connection per thread, or a single writer task; enable WAL mode

## Build phases (do ONE phase at a time; stop and report after each)
1. **Ollama + bot core** ✅ DONE
2. **Triggers + short-term context** ✅ DONE
   - 2b. **Engaged conversations** ✅ DONE: `engagement.py`, wired in. Accept: trigger once, keep chatting without the trigger; 2 min silence, an @ to someone else, or "not you" drops back to monitoring.
   - 2c. **Bot profiles + startup flow** ✅ DONE: move settings into `bot_profiles/*.yaml` per the schema (migrate the existing config into the first profile), console startup flow (token → load/create profile → health checks). Ollama URL and model come from the profile. Accept: create a second profile pointing at a different model, switch between them at startup.
3. **Memory + user profiles** ✅ DONE: `memory.py`, all tables above, storing every watched message, hybrid retrieval, user fact extraction and summaries, prompt injection. Accept: tell the bot a fact under profile A, restart under profile B, ask about it; profile B recalls it and knows who you are.
4. **GUI + tray + wizard** ✅ DONE: everything in the GUI section (dashboard, profile editor with Test connection + model dropdown, settings with Change token, user profiles tab, tray), GUI version of the startup flow, live profile switching, optional `ollama_api_key`. Accept: configure and run everything without touching a YAML file or the console.
5. **Hotkeys** ✅ DONE: global toggles synced with the GUI. Accept: hotkeys work while a game has focus.
6. **Branding + packaging + portable launch**:
   - Apply the Branding section everywhere listed (rename user-visible strings only; no module/package renames)
   - PyInstaller onefile exe `DisCortex.exe` (windowed, no console, `--icon assets/discortex.ico`), `config.yaml` + `bot_profiles/` + `data/` + `logs/` beside the exe
   - `start.bat` in the app folder: `cd /d "%~dp0"` then launches the exe if present, else falls back to `.venv\Scripts\pythonw.exe -m app.main` (dev mode). Also `start-console.bat` for troubleshooting (shows the console/log output)
   - All paths relative to the exe's folder, so it runs from any drive letter (F:\, a USB stick, etc.)
   - Note in README: the `.venv` is NOT portable (it hardcodes paths), so on another machine use the exe
   - README covering Discord setup, Ollama setup (local, LAN, Jetson), WAN safety, first run
   - Accept: copy the folder to a USB stick, run `start.bat` on another Windows PC with no Python installed, enter the token once, point a profile at a remote Ollama box; memories and user profiles are intact.

## Working rules for CC
- Surgical changes. Don't refactor earlier phases while building later ones unless asked.
- Keep modules in their lane (`llm.py`, `memory.py`, `engagement.py` know nothing about Discord or the GUI)
- Log, don't crash: network failures on either side should recover
- Never log or print the bot token
- Test against a private test server/channel before anything else
