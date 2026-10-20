"""Stream thumbnail & avatar downloader, truecolor half-block renderer, and dynamic palette extractor."""

from __future__ import annotations

import colorsys
import io
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from rich.style import Style
from rich.text import Text


@dataclass(frozen=True)
class ThemePalette:
    name: str
    bg_main: str
    bg_panel: str
    bg_highlight: str
    primary: str
    secondary: str
    live: str
    offline: str
    fg: str
    fg_muted: str


DRACULA_PALETTE = ThemePalette(
    name="Dracula Twitch",
    bg_main="#1e1f29",
    bg_panel="#282a36",
    bg_highlight="#44475a",
    primary="#bd93f9",
    secondary="#ff79c6",
    live="#ff5555",
    offline="#6272a4",
    fg="#f8f8f2",
    fg_muted="#6272a4",
)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _clamp_hsv_to_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return _rgb_to_hex(int(r * 255), int(g * 255), int(b * 255))


def fetch_image(url: str, timeout: float = 3.0) -> Image.Image | None:
    if not url:
        return None
    try:
        if url.startswith("file://"):
            local_path = Path(urllib.parse.unquote(url[7:]))
            if local_path.exists():
                return Image.open(local_path).convert("RGB")
            return None
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) twitch-tui/0.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def extract_dynamic_palette(img: Image.Image) -> ThemePalette:
    """Extracts an Ambient dark palette from a stream preview or channel avatar."""
    small = img.convert("RGB").resize((36, 36), Image.Resampling.BILINEAR)
    pixels = list(small.getdata())

    scored: list[tuple[float, float, float, float]] = []
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if v < 0.15 or (s < 0.12 and v > 0.85):
            continue
        score = (s * 1.6) + (1.0 - abs(v - 0.65))
        scored.append((score, h, s, v))

    if not scored:
        return DRACULA_PALETTE

    scored.sort(key=lambda x: x[0], reverse=True)
    _, h1, s1, v1 = scored[0]

    h2, s2, v2 = (h1 + 0.12) % 1.0, max(0.55, s1), min(0.95, v1 + 0.1)
    for _, hc, sc, vc in scored[1:]:
        hue_dist = min(abs(hc - h1), 1.0 - abs(hc - h1))
        if hue_dist > 0.08 and sc > 0.25:
            h2, s2, v2 = hc, sc, vc
            break

    primary = _clamp_hsv_to_hex(h1, max(0.55, min(0.88, s1)), max(0.78, min(0.96, v1)))
    secondary = _clamp_hsv_to_hex(h2, max(0.50, min(0.85, s2)), max(0.78, min(0.95, v2)))
    bg_main = _clamp_hsv_to_hex(h1, min(0.38, s1 * 0.55), 0.11)
    bg_panel = _clamp_hsv_to_hex(h1, min(0.42, s1 * 0.60), 0.16)
    bg_highlight = _clamp_hsv_to_hex(h1, min(0.45, s1 * 0.65), 0.28)
    fg_muted = _clamp_hsv_to_hex(h1, min(0.30, s1 * 0.45), 0.62)

    return ThemePalette(
        name="Dynamic Stream",
        bg_main=bg_main,
        bg_panel=bg_panel,
        bg_highlight=bg_highlight,
        primary=primary,
        secondary=secondary,
        live="#ff5555",
        offline=fg_muted,
        fg="#f8f8f2",
        fg_muted=fg_muted,
    )


def _crop_aspect(img: Image.Image, target_ratio: float) -> Image.Image:
    """Center-crops image to match target width/height aspect ratio."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    if w <= 0 or h <= 0:
        return rgb
    curr_ratio = w / h
    if abs(curr_ratio - target_ratio) < 0.05:
        return rgb
    if curr_ratio > target_ratio:
        new_w = max(1, int(h * target_ratio))
        left = (w - new_w) // 2
        return rgb.crop((left, 0, left + new_w, h))
    new_h = max(1, int(w / target_ratio))
    top = (h - new_h) // 2
    return rgb.crop((0, top, w, top + new_h))


def render_image_halfblocks(
    img: Image.Image,
    width_cells: int = 26,
    height_cells: int = 9,
) -> Text:
    """Renders a PIL image into 24-bit Truecolor Unicode half-blocks (▀)."""
    px_w = max(4, width_cells)
    px_h = max(4, height_cells * 2)
    cropped = _crop_aspect(img, px_w / px_h)
    resized = cropped.resize((px_w, px_h), Image.Resampling.LANCZOS)

    out = Text()
    for y in range(0, px_h, 2):
        for x in range(px_w):
            r1, g1, b1 = resized.getpixel((x, y))
            if y + 1 < px_h:
                r2, g2, b2 = resized.getpixel((x, y + 1))
            else:
                r2, g2, b2 = r1, g1, b1
            style = Style(
                color=_rgb_to_hex(r1, g1, b1),
                bgcolor=_rgb_to_hex(r2, g2, b2),
            )
            out.append("▀", style=style)
        if y + 2 < px_h:
            out.append("\n")
    return out


def render_placeholder_tv(
    palette: ThemePalette = DRACULA_PALETTE,
    width_cells: int = 26,
    height_cells: int = 9,
    is_live: bool = False,
) -> Text:
    """Renders a stylized retro Twitch screen placeholder when no preview is loaded."""
    px_w = width_cells
    px_h = height_cells * 2

    img = Image.new("RGB", (px_w, px_h), (24, 25, 36))
    prim = tuple(int(palette.primary.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    sec = tuple(int(palette.secondary.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    bg = tuple(int(palette.bg_panel.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))

    for y in range(px_h):
        for x in range(px_w):
            # Outer TV frame
            if x == 0 or x == px_w - 1 or y == 0 or y == px_h - 1:
                img.putpixel((x, y), prim)
            elif x == 1 or x == px_w - 2 or y == 1 or y == px_h - 2:
                img.putpixel((x, y), bg)
            else:
                # Stylized Twitch glitch / test bars
                if is_live:
                    band = (x * 5) // px_w
                    colors = [
                        prim,
                        sec,
                        (80, 250, 123),
                        (139, 233, 253),
                        (255, 184, 108),
                    ]
                    c = colors[band % len(colors)]
                    # Darken slightly for scanline effect
                    factor = 0.85 if y % 2 == 0 else 0.65
                    img.putpixel(
                        (x, y),
                        (int(c[0] * factor), int(c[1] * factor), int(c[2] * factor)),
                    )
                else:
                    # Subtle diagonal scanlines + center Twitch eye icon
                    cx, cy = px_w // 2, px_h // 2
                    if abs(x - cx) <= 4 and abs(y - cy) <= 3:
                        if (x in (cx - 2, cx + 2)) and (cy - 1 <= y <= cy + 1):
                            img.putpixel((x, y), sec)
                        else:
                            img.putpixel((x, y), prim)
                    else:
                        shade = 32 if (x + y) % 3 == 0 else 24
                        img.putpixel((x, y), (shade, shade, shade + 10))

    return render_image_halfblocks(img, width_cells=width_cells, height_cells=height_cells)
