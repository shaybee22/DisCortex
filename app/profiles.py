"""Bot profiles (bot_profiles/<name>.yaml): load, save, validate, list, delete, duplicate.

A bot profile is who the bot is and where its brain lives. One is active per run.
No console or GUI code here, so the startup flow and the GUI can share it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

from app.config import base_dir
from app.engagement import EngageSettings, compile_phrases

log = logging.getLogger(__name__)

MODES = ("all", "trigger")
NAME_RE = re.compile(r"[A-Za-z0-9][\w .-]{0,63}")
DEFAULT_OLLAMA_URL = "http://localhost:11434"


def profiles_dir() -> Path:
    return base_dir() / "bot_profiles"


@dataclass
class Profile:
    name: str
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_api_key: str | None = None  # optional; sent as "Authorization: Bearer <key>" (reverse proxy)
    model: str = ""
    temperature: float = 0.8
    system_prompt: str = "You are a friendly AFK bot in a Discord channel. Keep replies short."
    channel_ids: list[int] = field(default_factory=list)
    mode: str = "trigger"
    triggers: list[str] = field(default_factory=list)
    respond_to_mentions: bool = True
    respond_to_replies: bool = True
    context_messages: int = 15
    cooldown_seconds: float = 8
    max_reply_chars: int = 600
    # engaged conversations
    sticky_engage: bool = True
    engage_timeout_seconds: float = 120
    disengage_phrases: list[str] = field(default_factory=lambda: [
        "not you", "wasn't talking to you", "i was talking to", "talking to someone else",
        "nobody asked you", "shut up bot", "go away", "stop talking"])
    signoff_phrases: list[str] = field(default_factory=lambda: ["that's all", "thanks bot", "bye bot"])
    # memory (the data itself is always shared; these only tune retrieval)
    memory_enabled: bool = True
    memory_k: int = 5
    memory_max_chars: int = 1500
    prefer_current_channel: bool = True

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        if not NAME_RE.fullmatch(self.name):
            raise ValueError(f"'name' must be 1-64 letters/digits/space/._- (got {self.name!r})")
        self.ollama_url = str(self.ollama_url or DEFAULT_OLLAMA_URL).strip().rstrip("/")
        if not re.match(r"https?://", self.ollama_url):
            raise ValueError(f"'ollama_url' must start with http:// or https:// (got {self.ollama_url!r})")
        self.ollama_api_key = str(self.ollama_api_key).strip() if self.ollama_api_key else None
        self.model = str(self.model or "").strip()
        if not self.model:
            raise ValueError("'model' is required")
        if self.mode not in MODES:
            raise ValueError(f"'mode' must be one of {MODES}, got {self.mode!r}")
        self.channel_ids = [int(c) for c in (self.channel_ids or [])]
        self.triggers = [str(t).strip() for t in (self.triggers or []) if str(t).strip()]
        self.context_messages = max(0, min(int(self.context_messages), 100))
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds))
        self.max_reply_chars = max(1, int(self.max_reply_chars))
        self.temperature = float(self.temperature)
        self.sticky_engage = bool(self.sticky_engage)
        self.engage_timeout_seconds = max(1.0, float(self.engage_timeout_seconds))
        self.disengage_phrases = [str(p) for p in (self.disengage_phrases or [])]
        self.signoff_phrases = [str(p) for p in (self.signoff_phrases or [])]
        self.memory_enabled = bool(self.memory_enabled)
        self.memory_k = max(0, min(int(self.memory_k), 50))
        self.memory_max_chars = max(0, int(self.memory_max_chars))
        self.prefer_current_channel = bool(self.prefer_current_channel)
        self._trigger_re = _compile_triggers(self.triggers)
        self.engage_settings = EngageSettings(
            sticky=self.sticky_engage,
            timeout_seconds=self.engage_timeout_seconds,
            disengage=compile_phrases(self.disengage_phrases),
            signoff=compile_phrases(self.signoff_phrases),
        )

    def matches_trigger(self, text: str) -> bool:
        return bool(self._trigger_re and self._trigger_re.search(text))

    def to_dict(self) -> dict:
        return asdict(self)  # only dataclass fields; compiled helpers are left out


def _compile_triggers(triggers: list[str]) -> re.Pattern | None:
    if not triggers:
        return None
    # Whole word/phrase, case-insensitive. Lookarounds instead of \b so triggers
    # that start/end with non-word chars still work.
    alts = "|".join(re.escape(t) for t in triggers)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def profile_from_dict(data: dict) -> Profile:
    """Build a Profile; missing keys get defaults, unknown keys are ignored."""
    known = {f.name for f in fields(Profile)}
    unknown = set(data) - known
    if unknown:
        log.debug("Profile '%s': ignoring keys not used yet: %s", data.get("name"), ", ".join(sorted(unknown)))
    return Profile(**{k: v for k, v in data.items() if k in known})


def profile_path(name: str, directory: Path | None = None) -> Path:
    return (directory or profiles_dir()) / f"{name}.yaml"


def list_profile_names(directory: Path | None = None) -> list[str]:
    directory = directory or profiles_dir()
    directory.mkdir(exist_ok=True)
    return sorted((p.stem for p in directory.glob("*.yaml") if not p.name.endswith(".example.yaml")),
                  key=str.lower)


def load_profile(name: str, directory: Path | None = None) -> Profile:
    """Load one profile by name. Raises ValueError/OSError/yaml.YAMLError on bad files."""
    path = profile_path(name, directory)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("name", path.stem)
    return profile_from_dict(data)


def save_profile(profile: Profile, directory: Path | None = None) -> Path:
    directory = directory or profiles_dir()
    directory.mkdir(exist_ok=True)
    path = profile_path(profile.name, directory)
    path.write_text(yaml.safe_dump(profile.to_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def delete_profile(name: str, directory: Path | None = None) -> None:
    profile_path(name, directory).unlink(missing_ok=True)


def duplicate_profile(name: str, new_name: str, directory: Path | None = None) -> Profile:
    if profile_path(new_name, directory).exists():
        raise ValueError(f"A profile named '{new_name}' already exists")
    data = load_profile(name, directory).to_dict()
    data["name"] = new_name
    copy = profile_from_dict(data)
    save_profile(copy, directory)
    return copy


def build_channel_map(profiles: list[Profile]) -> dict[int, Profile]:
    """channel_id -> profile. A channel claimed twice keeps the first profile."""
    mapping: dict[int, Profile] = {}
    for p in profiles:
        if not p.channel_ids:
            log.warning("Profile '%s' has no channel_ids; it is inactive", p.name)
        for cid in p.channel_ids:
            if cid in mapping:
                log.error("Channel %s is in both '%s' and '%s'; using '%s'",
                          cid, mapping[cid].name, p.name, mapping[cid].name)
                continue
            mapping[cid] = p
    return mapping
