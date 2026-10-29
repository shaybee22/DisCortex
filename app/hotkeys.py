"""Global hotkeys (pynput). No GUI or Discord code.

pynput's own GlobalHotKeys misses Ctrl+Alt+<letter> on Windows (the key arrives
without its character), so combos are matched here by virtual-key code. On Windows
the modifier state is read from the OS at the moment the key is pressed, so a missed
key-up (e.g. after Win+L) can't leave a modifier "stuck".

Combo format (same as config.yaml): "<ctrl>+<alt>+a", "<ctrl>+<shift>+<f9>", "<pause>".
Callbacks run on pynput's listener thread: keep them tiny (e.g. put on a queue).
"""
from __future__ import annotations

import logging
import sys
from typing import Callable

log = logging.getLogger(__name__)

MODS = ("ctrl", "alt", "shift", "cmd")                 # canonical order
MOD_ALIASES = {
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl", "control": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd", "win": "cmd", "super": "cmd",
}
MOD_VKS = {
    "ctrl": (0x11, 0xA2, 0xA3), "alt": (0x12, 0xA4, 0xA5),
    "shift": (0x10, 0xA0, 0xA1), "cmd": (0x5B, 0x5C),
}
VK_TO_MOD = {vk: m for m, vks in MOD_VKS.items() for vk in vks}
PRETTY_MODS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win"}
# keys that are fine without a modifier (rarely typed)
BARE_OK = {f"f{i}" for i in range(1, 25)} | {"pause", "scroll_lock", "insert"}

_WIN = sys.platform == "win32"


class HotkeyError(ValueError):
    pass


def _named_vks() -> dict[str, int]:
    """pynput's named keys (<f1>, <space>, <home>, ...) -> Windows virtual-key code."""
    out: dict[str, int] = {}
    try:
        from pynput.keyboard import Key
        for k in Key:
            vk = getattr(k.value, "vk", None)
            if vk is not None and vk not in VK_TO_MOD:
                out.setdefault(k.name, vk)
    except Exception:
        pass
    for i in range(1, 25):
        out.setdefault(f"f{i}", 0x6F + i)
    return out


NAMED = _named_vks()
VK_TO_NAME = {vk: name for name, vk in NAMED.items()}


def _char_vk(ch: str) -> int | None:
    if ch.isascii() and ch.isalnum():
        return ord(ch.upper())
    if _WIN:
        import ctypes
        r = ctypes.windll.user32.VkKeyScanW(ord(ch))
        if r != -1 and (r & 0xFF) != 0xFF:
            return r & 0xFF
    return None


def parse_combo(text: str) -> tuple[frozenset[str], int, str]:
    """'<ctrl>+<alt>+a' -> ({'ctrl','alt'}, vk, canonical text). Raises HotkeyError."""
    raw = (text or "").strip().lower().replace(" ", "")
    if not raw:
        raise HotkeyError("empty")
    tokens = raw.split("+")
    if raw.endswith("++"):                       # "<ctrl>++" means the + key
        tokens = tokens[:-2] + ["+"]
    mods: set[str] = set()
    key = None
    for t in tokens:
        name = t[1:-1] if t.startswith("<") and t.endswith(">") and len(t) > 2 else t
        if name in MOD_ALIASES:
            mods.add(MOD_ALIASES[name])
            continue
        if key is not None:
            raise HotkeyError(f"'{text}' has more than one non-modifier key")
        key = name
    if key is None:
        raise HotkeyError(f"'{text}' needs a key besides the modifiers")
    if len(key) == 1:
        vk = _char_vk(key)
        canon_key = key
    else:
        vk = NAMED.get(key)
        canon_key = f"<{key}>"
    if vk is None:
        raise HotkeyError(f"unknown key '{key}' in '{text}'")
    if not mods and key not in BARE_OK:
        raise HotkeyError(f"'{text}' needs a modifier (Ctrl, Alt, Shift or Win); "
                          "only F-keys, Pause, Scroll Lock and Insert work alone")
    canon = "+".join([f"<{m}>" for m in MODS if m in mods] + [canon_key])
    return frozenset(mods), vk, canon


def combo_from_vk(mods: set[str], vk: int) -> str | None:
    """Build a combo string from a key press (used by the GUI's Record button)."""
    if vk in VK_TO_MOD:
        return None
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        key = chr(vk).lower()
    elif vk in VK_TO_NAME:
        key = f"<{VK_TO_NAME[vk]}>"
    elif _WIN:
        import ctypes
        ch = ctypes.windll.user32.MapVirtualKeyW(vk, 2) & 0xFFFF  # MAPVK_VK_TO_CHAR
        if not ch or not chr(ch).isprintable() or chr(ch) == "+":
            return None
        key = chr(ch).lower()
    else:
        return None
    return "+".join([f"<{m}>" for m in MODS if m in mods] + [key])


def pretty(combo: str) -> str:
    """'<ctrl>+<alt>+a' -> 'Ctrl+Alt+A' (falls back to the raw text)."""
    try:
        mods, _, canon = parse_combo(combo)
    except HotkeyError:
        return combo
    key = canon.split("+")[-1] if not canon.endswith("++") else "+"
    key = key.strip("<>").replace("_", " ").title() if key.startswith("<") else key.upper()
    return "+".join([PRETTY_MODS[m] for m in MODS if m in mods] + [key])


def validate(bindings: dict[str, str]) -> tuple[dict[tuple[frozenset[str], int], str], list[str]]:
    """Parse {action: combo}. Empty combos are skipped (= disabled).
    Returns ({(mods, vk): action}, [error messages])."""
    table: dict[tuple[frozenset[str], int], str] = {}
    errors: list[str] = []
    for action, combo in (bindings or {}).items():
        if not (combo or "").strip():
            continue
        try:
            mods, vk, _ = parse_combo(combo)
        except HotkeyError as e:
            errors.append(f"{action}: {e}")
            continue
        if (mods, vk) in table:
            errors.append(f"{action}: {pretty(combo)} is already used by {table[(mods, vk)]}")
            continue
        table[(mods, vk)] = action
    return table, errors


def current_mods() -> frozenset[str] | None:
    """Modifiers held right now according to Windows (None elsewhere)."""
    if not _WIN:
        return None
    import ctypes
    down = ctypes.windll.user32.GetAsyncKeyState
    return frozenset(m for m, vks in MOD_VKS.items() if any(down(vk) & 0x8000 for vk in vks))


class HotkeyManager:
    def __init__(self, on_action: Callable[[str], None]):
        self.on_action = on_action
        self._table: dict[tuple[frozenset[str], int], str] = {}
        self._listener = None
        self._held: set[str] = set()     # fallback modifier tracking (non-Windows)
        self._down: set[int] = set()     # keys already fired, until released (no auto-repeat)

    @property
    def active(self) -> bool:
        return self._listener is not None and self._listener.running

    def start(self, bindings: dict[str, str]) -> list[str]:
        """(Re)start with these bindings. Returns error messages for bad combos."""
        self.stop()
        self._table, errors = validate(bindings)
        if not self._table:
            return errors
        try:
            from pynput import keyboard
            self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)
            self._listener.daemon = True
            self._listener.start()
            self._listener.wait()
        except Exception as e:
            self._listener = None
            errors.append(f"global hotkeys unavailable: {type(e).__name__}: {e}")
        return errors

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._held.clear()
        self._down.clear()

    # ---- listener thread -----------------------------------------------------

    @staticmethod
    def _vk(key) -> int | None:
        vk = getattr(key, "vk", None)
        if vk is None:
            vk = getattr(getattr(key, "value", None), "vk", None)
        return vk

    def _mods_now(self) -> frozenset[str]:
        mods = current_mods()
        return mods if mods is not None else frozenset(self._held)

    def _press(self, key) -> None:
        try:
            vk = self._vk(key)
            if vk is None:
                return
            if vk in VK_TO_MOD:
                self._held.add(VK_TO_MOD[vk])
                return
            if vk in self._down:
                return  # auto-repeat while held
            action = self._table.get((self._mods_now(), vk))
            if action:
                self._down.add(vk)
                self.on_action(action)
        except Exception:
            log.exception("Hotkey handler error")

    def _release(self, key) -> None:
        vk = self._vk(key)
        if vk in VK_TO_MOD:
            self._held.discard(VK_TO_MOD[vk])
        self._down.discard(vk)
