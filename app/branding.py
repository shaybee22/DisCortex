"""DisCortex name, logo and icons. Never raises: a missing or broken logo falls back
to text-only headers and plain generated icons.

  python -m app.branding      # (re)generate assets/discortex.ico from assets/discortex.png
"""
from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from app.config import base_dir

log = logging.getLogger(__name__)

APP_NAME = "DisCortex"
SUBTITLE = "Portable Discord AI Host"
TAGLINE = "Different brains. Different personalities. One memory."

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
SMALL_MAX = 32                     # at this size and below use the robot head, not the full logo
HEAD_BOX = (0.27, 0.18, 0.74, 0.65)  # head crop, as fractions of the logo's width/height

GREEN, GREY = "#2e9d4f", "#8a8f98"


def asset_path(name: str) -> Path | None:
    """assets/<name> beside the exe / project root; inside a PyInstaller bundle as fallback."""
    for root in (base_dir(), Path(getattr(sys, "_MEIPASS", "")) if hasattr(sys, "_MEIPASS") else None):
        if root is not None and (root / "assets" / name).is_file():
            return root / "assets" / name
    return None


@functools.lru_cache(maxsize=1)
def logo() -> Image.Image | None:
    path = asset_path("discortex.png")
    if path is None:
        log.debug("Logo not found; using plain icons")
        return None
    try:
        im = Image.open(path).convert("RGBA")
        im.load()
        return im
    except Exception as e:
        log.warning("Could not load logo %s (%s); using plain icons", path, e)
        return None


def _square(im: Image.Image) -> Image.Image:
    w, h = im.size
    side = max(w, h)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(im, ((side - w) // 2, (side - h) // 2))
    return out


def logo_image(size: int, head: bool | None = None) -> Image.Image | None:
    """Square logo at `size` px: the full logo, or just the robot head
    (default: head for small sizes, where the wordmark would be unreadable)."""
    im = logo()
    if im is None:
        return None
    try:
        if head or (head is None and size <= SMALL_MAX):
            w, h = im.size
            l, t, r, b = HEAD_BOX
            im = im.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
        else:
            im = im.crop(im.getbbox() or (0, 0, *im.size))
        return _square(im).resize((size, size), Image.LANCZOS)
    except Exception as e:
        log.warning("Logo resize failed (%s); using plain icons", e)
        return None


def plain_icon(on: bool, size: int = 64) -> Image.Image:
    """Fallback icon: green dot when ON, grey when OFF."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size - 4, size - 4), fill=GREEN if on else GREY, outline="white", width=max(1, size // 20))
    d.ellipse((size * 0.36, size * 0.36, size * 0.64, size * 0.64), fill="white")
    return img


def status_icon(on: bool, size: int = 64) -> Image.Image:
    """Tray/window icon: the robot head with a green (ON) or grey (OFF) status dot;
    dimmed when OFF. Falls back to the plain dot icon."""
    base = logo_image(size, head=True)  # shown at 16-32 px by Windows, whatever the source size
    if base is None:
        return plain_icon(on, size)
    if not on:
        base = ImageEnhance.Color(base).enhance(0.25)
        alpha = base.getchannel("A").point(lambda a: a * 0.8)
        base.putalpha(alpha)
    d = ImageDraw.Draw(base)
    r = max(4, size * 0.2)
    x0, y0 = size - 2 * r - 1, size - 2 * r - 1
    d.ellipse((x0, y0, size - 1, size - 1), fill=GREEN if on else GREY, outline="white", width=max(1, size // 24))
    return base


def make_ico(dest: Path | None = None) -> Path | None:
    """Write assets/discortex.ico (all ICO_SIZES). Returns the path, or None without a logo."""
    images = [logo_image(s) for s in ICO_SIZES]
    if any(i is None for i in images):
        return None
    dest = dest or base_dir() / "assets" / "discortex.ico"
    images[-1].save(dest, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=images[:-1])
    return dest


def banner() -> str:
    return f"{APP_NAME} · {SUBTITLE} · {TAGLINE}"


if __name__ == "__main__":
    out = make_ico()
    print(f"Wrote {out}" if out else "assets/discortex.png not found or unreadable; no icon written.")
    sys.exit(0 if out else 1)
