"""Global config (config.yaml) load/save and base-dir logic."""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return base_dir() / "config.yaml"


def ensure_first_run_files() -> None:
    """Create data/ and logs/; copy the shipped *.example.yaml files to their real names if missing.

    Example bot profiles are copied only when there are no real profiles yet, so a profile
    the user deleted doesn't come back on the next launch.
    """
    import shutil
    base = base_dir()
    for d in ("data", "logs", "bot_profiles"):
        (base / d).mkdir(exist_ok=True)
    example = base / "config.example.yaml"
    if example.exists() and not config_path().exists():
        shutil.copyfile(example, config_path())
        log.info("Created config.yaml from config.example.yaml")
    prof_dir = base / "bot_profiles"
    if not any(not p.name.endswith(".example.yaml") for p in prof_dir.glob("*.yaml")):
        for ex in prof_dir.glob("*.example.yaml"):
            real = ex.with_name(ex.name.replace(".example.yaml", ".yaml"))
            shutil.copyfile(ex, real)
            log.info("Created bot_profiles/%s from %s", real.name, ex.name)


KEYRING_SERVICE = "discord-afk-responder"
KEYRING_USER = "bot_token"


def get_token() -> str | None:
    """Bot token from Windows Credential Manager, or None."""
    import keyring
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or None
    except Exception as e:
        log.error("Could not read token from Credential Manager: %s", type(e).__name__)
        return None


def set_token(token: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)


def delete_token() -> bool:
    """Remove the saved token. Returns False if there was none."""
    import keyring
    from keyring.errors import PasswordDeleteError
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        return True
    except PasswordDeleteError:
        return False


@dataclass
class Config:
    last_profile: str | None = None
    embed_model: str = "nomic-embed-text"
    enabled: bool = False  # master on/off, starts OFF
    start_minimized: bool = False  # GUI starts hidden in the tray
    hotkey_sound: bool = True      # short beep when a global hotkey is used
    hotkeys: dict = field(default_factory=lambda: {
        "toggle_enabled": "<ctrl>+<alt>+a",
        "toggle_mode": "<ctrl>+<alt>+m",
    })


def load_config(path: Path | None = None) -> Config:
    """Load config.yaml. Missing file -> write and return defaults."""
    path = path or config_path()
    if not path.exists():
        cfg = Config()
        save_config(cfg, path)
        log.info("Created default %s", path.name)
        return cfg
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = Config.__dataclass_fields__.keys()
    unknown = set(data) - set(known)
    if unknown:
        log.warning("%s: ignoring unknown keys: %s", path.name, ", ".join(sorted(unknown)))
    return Config(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: Config, path: Path | None = None) -> None:
    path = path or config_path()
    path.write_text(yaml.safe_dump(asdict(cfg), sort_keys=False), encoding="utf-8")


class RuntimeState:
    """Thread-safe switches the GUI/hotkeys will flip and the bot reads."""

    def __init__(self, enabled: bool):
        self._lock = threading.Lock()
        self._enabled = enabled
        self._mode_override: str | None = None  # None = use each profile's mode

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = value

    @property
    def mode_override(self) -> str | None:
        with self._lock:
            return self._mode_override

    def set_mode_override(self, mode: str | None) -> None:
        if mode not in (None, "all", "trigger"):
            raise ValueError(f"bad mode: {mode}")
        with self._lock:
            self._mode_override = mode
