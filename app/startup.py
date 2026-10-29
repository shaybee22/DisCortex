"""Console startup flow: token -> pick/create bot profile -> health checks.

Phase 4's GUI wizard replaces this; both use profiles.py for the real work.
Ctrl+C inside the create/edit questions cancels back to the menu.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app import profiles as P
from app.config import Config, get_token, set_token
from app.llm import LLMError, OllamaClient, model_installed


# ---- small console helpers ---------------------------------------------------

def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer if answer else (default or "")


def ask_yes(prompt: str, default: bool = False) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def split_list(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def fetch_models(url: str, api_key: str | None = None) -> list[str]:
    return asyncio.run(OllamaClient(url, api_key=api_key).list_models())


# ---- token -------------------------------------------------------------------

_QUOTES = "'\"`‘’“”"
_INVISIBLE = dict.fromkeys(map(ord, "﻿​‌‍⁠"))


def clean_token(raw: str) -> str:
    """Strip whitespace, surrounding quotes, a 'Bot ' prefix and invisible characters."""
    t = raw.translate(_INVISIBLE).strip()
    t = t.strip(_QUOTES).strip()
    if t.lower().startswith("bot "):
        t = t[4:].strip().strip(_QUOTES).strip()
    return t


def describe_token(token: str) -> str:
    """Safe summary for error messages: length and first 4 chars only."""
    head = "".join(c if c.isprintable() else "?" for c in token[:4])
    return f"length {len(token)}, starts with '{head}'"


def token_problem(token: str) -> str | None:
    """Why `token` can't be a bot token, or None if it looks fine."""
    if any(ord(c) < 32 for c in token):
        return ("it contains control characters. Ctrl+V may not paste into hidden input here; "
                "try right-click to paste, or Ctrl+Shift+V")
    if any(c.isspace() for c in token):
        return "it contains spaces"
    if len(token) < 50:
        return "it's too short (bot tokens are about 70 characters)"
    if token.count(".") != 2:
        return "bot tokens have three parts separated by dots"
    return None


def ensure_token(force_new: bool = False) -> str:
    """Return the bot token. Order: Credential Manager, then ask (Enter = DISCORD_TOKEN env var).

    Whatever is accepted is saved to Credential Manager, so the env var is only
    ever a one-time convenience.
    """
    if not force_new:
        stored = get_token()
        if stored:
            cleaned = clean_token(stored)
            if cleaned != stored:  # saved by an older version with quotes/spaces
                set_token(cleaned)
            return cleaned

    env = clean_token(os.environ.get("DISCORD_TOKEN", ""))
    while True:
        print("\nPaste your Discord bot token (input is hidden). Right-click pastes in most consoles.")
        if env:
            print("  Or press Enter to use the DISCORD_TOKEN environment variable.")
        entered = clean_token(getpass.getpass("Token: ")) or env
        if not entered:
            continue
        problem = token_problem(entered)
        if problem:
            print(f"  Rejected ({describe_token(entered)}): {problem}.")
            print("  Copy it from Developer Portal > Bot > Reset Token.")
            continue
        set_token(entered)
        print(f"  Saved to Windows Credential Manager ({describe_token(entered)}).")
        return entered


# ---- profile menu ------------------------------------------------------------

def _describe(name: str) -> tuple[P.Profile | None, str]:
    try:
        p = P.load_profile(name)
        return p, f"{p.mode:<8} {p.model} @ {p.ollama_url}  ({len(p.channel_ids)} channel(s))"
    except Exception as e:
        return None, f"BROKEN: {e}"


def choose_profile(cfg: Config) -> P.Profile | None:
    """Interactive menu. Returns the chosen profile, or None to quit."""
    while True:
        names = P.list_profile_names()
        if not names:
            print("\nNo bot profiles yet. Let's create one.")
            p = _run_wizard(None)
            if p:
                return p
            if not ask_yes("No profile created. Try again?", True):
                return None
            continue

        default = cfg.last_profile if cfg.last_profile in names else names[0]
        print("\nBot profiles:")
        loaded: dict[str, P.Profile | None] = {}
        for i, name in enumerate(names, 1):
            loaded[name], desc = _describe(name)
            mark = "  <- last used" if name == cfg.last_profile else ""
            print(f"  {i}) {name:<16} {desc}{mark}")
        print(f"\n  Enter = load '{default}'   #  = load   N = new   E# = edit   C# = duplicate"
              "   D# = delete   T = change token   Q = quit")
        choice = input("> ").strip().lower()

        if choice == "":
            choice = str(names.index(default) + 1)
        if choice == "q":
            return None
        if choice == "n":
            p = _run_wizard(None)
            if p and ask_yes(f"Load '{p.name}' now?", True):
                return p
            continue
        if choice == "t":
            ensure_token(force_new=True)
            continue

        m = re.fullmatch(r"([ecd]?)\s*(\d+)", choice)
        if not m or not 1 <= int(m.group(2)) <= len(names):
            print("  Not a valid choice.")
            continue
        action, name = m.group(1), names[int(m.group(2)) - 1]
        prof = loaded[name]

        if action == "":
            if prof is None:
                print(f"  '{name}' can't be loaded. Fix {P.profile_path(name)} or delete it.")
                continue
            return prof
        if action == "e":
            if prof is None:
                print(f"  '{name}' is broken; edit {P.profile_path(name)} by hand or delete it.")
                continue
            p = _run_wizard(prof)
            if p and ask_yes(f"Load '{p.name}' now?", True):
                return p
        elif action == "c":
            new_name = ask("Name for the copy", f"{name}-copy")
            try:
                P.duplicate_profile(name, new_name)
                print(f"  Created '{new_name}'.")
            except Exception as e:
                print(f"  Couldn't duplicate: {e}")
        elif action == "d":
            if ask_yes(f"Delete bot profile '{name}'? Memories are not affected.", False):
                P.delete_profile(name)
                print(f"  Deleted '{name}'.")


# ---- create / edit -----------------------------------------------------------

def _run_wizard(existing: P.Profile | None) -> P.Profile | None:
    try:
        return profile_wizard(existing)
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        return None


def profile_wizard(existing: P.Profile | None) -> P.Profile:
    """Ask the create/edit questions in spec order, save, and return the profile."""
    title = f"Editing '{existing.name}'" if existing else "New bot profile"
    print(f"\n--- {title} (Ctrl+C cancels) ---")
    base = existing.to_dict() if existing else {}
    taken = set(P.list_profile_names()) - ({existing.name} if existing else set())

    # 1. name
    while True:
        name = ask("1. Profile name", base.get("name"))
        if not P.NAME_RE.fullmatch(name):
            print("   Use letters, digits, space, . _ - (max 64), starting with a letter or digit.")
        elif name in taken:
            print(f"   '{name}' already exists.")
        else:
            break

    # 2. Ollama URL, tested now
    url = base.get("ollama_url", P.DEFAULT_OLLAMA_URL)
    reenter = True
    while True:
        if reenter:
            url = ask("2. Ollama URL", url).rstrip("/")
            if not re.match(r"https?://", url):
                print("   Must start with http:// (e.g. http://192.168.1.50:11434)")
                continue
        reenter = True
        print(f"   Testing {url} ...")
        try:
            models = fetch_models(url)
        except LLMError as e:
            print(f"   {e}")
            reenter = input("   R = retry, Enter = re-enter URL: ").strip().lower() != "r"
            continue
        models = [m for m in models if "embed" not in m.lower()]  # embedding models can't chat
        if not models:
            print("   Reachable, but no chat models are installed there. Run 'ollama pull <model>' on that machine.")
            input("   Press Enter to test again...")
            reenter = False
            continue
        print(f"   OK, {len(models)} model(s) available.")
        break

    # 3. chat model, must be one on that server
    print("3. Chat model:")
    for i, mname in enumerate(models, 1):
        print(f"   {i}) {mname}")
    current = base.get("model")
    default_idx = str(models.index(current) + 1) if current in models else None
    while True:
        pick = ask("   Number", default_idx)
        if pick.isdigit() and 1 <= int(pick) <= len(models):
            model = models[int(pick) - 1]
            break
        print("   Pick one of the numbers above.")

    # 4. system prompt
    system_prompt = ask_system_prompt(base.get("system_prompt"))

    # 5. triggers
    triggers = split_list(ask("5. Trigger word(s), comma-separated", ", ".join(base.get("triggers", []))))

    # 6. mode
    while True:
        mode = ask("6. Mode: trigger or all", base.get("mode", "trigger")).lower()
        if mode in P.MODES:
            break
        print("   Type 'trigger' or 'all'.")
    if mode == "trigger" and not triggers:
        print("   Note: no trigger words, so only @mentions and replies to the bot will engage it.")

    # 7. channel IDs
    while True:
        raw = ask("7. Channel ID(s) to watch, comma-separated", ", ".join(map(str, base.get("channel_ids", []))))
        ids = split_list(raw)
        if ids and all(re.fullmatch(r"\d{15,21}", c) for c in ids):
            channel_ids = [int(c) for c in ids]
            break
        print("   Right-click a channel in Discord > Copy Channel ID (needs Developer Mode). Numbers only.")

    # 8. everything else: keep existing values or defaults
    data = {**base, "name": name, "ollama_url": url, "model": model, "system_prompt": system_prompt,
            "triggers": triggers, "mode": mode, "channel_ids": channel_ids}
    profile = P.profile_from_dict(data)
    path = P.save_profile(profile)
    if existing and existing.name != name:
        P.delete_profile(existing.name)
    print(f"   Saved {path}. Other settings (cooldown, engagement, ...) can be edited in that file.")
    return profile


def ask_system_prompt(current: str | None) -> str:
    print("4. System prompt / persona.")
    if current:
        preview = current.strip().splitlines()[0][:80]
        print(f"   Current: {preview}{' ...' if len(current.strip()) > 80 or chr(10) in current.strip() else ''}")
        print("   Press Enter to keep it, type 'notepad' to edit it in Notepad, or type a new prompt.")
    else:
        print("   Type it below and finish with an empty line, or type 'notepad' to write it in Notepad.")
    first = input("   > ").rstrip()
    if not first:
        if current:
            return current
        print("   A system prompt is required.")
        return ask_system_prompt(current)
    if first.strip().lower() == "notepad":
        return _edit_in_notepad(current or "") or ask_system_prompt(current)
    lines = [first]
    while (line := input("   > ").rstrip()):
        lines.append(line)
    return "\n".join(lines) + "\n"


def _edit_in_notepad(text: str) -> str:
    fd, name = tempfile.mkstemp(suffix=".txt", prefix="system_prompt_")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        print("   Opening Notepad. Save and close it to continue...")
        subprocess.run(["notepad.exe", str(path)], check=False)
        result = path.read_text(encoding="utf-8-sig").strip()
        return result + "\n" if result else ""
    finally:
        path.unlink(missing_ok=True)


# ---- health checks -----------------------------------------------------------

def health_check(profile: P.Profile, cfg: Config) -> None:
    """Report Ollama/model status. Never blocks startup: the bot keeps retrying."""
    print(f"\nHealth check for '{profile.name}':")
    try:
        models = fetch_models(profile.ollama_url, profile.ollama_api_key)
    except LLMError as e:
        print(f"  [!!] {e}")
        print("       Starting anyway; the bot will keep retrying.")
        return
    print(f"  [ok] Ollama reachable at {profile.ollama_url}")
    if model_installed(profile.model, models):
        print(f"  [ok] Chat model {profile.model}")
    else:
        print(f"  [!!] Chat model '{profile.model}' is not on that server. "
              f"Run: ollama pull {profile.model} on that machine")
    if model_installed(cfg.embed_model, models):
        print(f"  [ok] Embed model {cfg.embed_model}")
    else:
        print(f"  [!!] Embed model '{cfg.embed_model}' is not on {profile.ollama_url}. Memory will use "
              f"keyword search only. Run: ollama pull {cfg.embed_model} on that machine")
