"""Generate brand artwork for the Orbit B-Hyve BLE integration.

Run: ``python3 assets/generate_icons.py`` (regenerates all four PNGs in place).

Outputs (sibling to this script):
  icon.png       256x256
  icon@2x.png    512x512
  logo.png       256x128
  logo@2x.png    512x256

The same files, copied to ``custom_integrations/orbit_bhyve/`` in
home-assistant/brands, are what HACS and the HA UI render. No HA tooling
reads from this repo's ``assets/`` directory; it only exists so the
artwork is reproducible.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent

GRADIENT_TOP = (14, 116, 144, 255)     # #0E7490
GRADIENT_BOTTOM = (34, 211, 238, 255)  # #22D3EE
DROPLET_COLOR = (255, 255, 255, 255)
TEXT_COLOR = (14, 116, 144, 255)
WORDMARK_COLOR = (255, 255, 255, 255)

SUPERSAMPLE = 4

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _gradient(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), GRADIENT_TOP)
    px = img.load()
    for y in range(height):
        t = y / max(height - 1, 1)
        r = round(GRADIENT_TOP[0] + (GRADIENT_BOTTOM[0] - GRADIENT_TOP[0]) * t)
        g = round(GRADIENT_TOP[1] + (GRADIENT_BOTTOM[1] - GRADIENT_TOP[1]) * t)
        b = round(GRADIENT_TOP[2] + (GRADIENT_BOTTOM[2] - GRADIENT_TOP[2]) * t)
        for x in range(width):
            px[x, y] = (r, g, b, 255)
    return img


def _rounded_tile(size: int, radius_ratio: float = 0.22) -> Image.Image:
    """Square teal/cyan gradient tile with rounded corners and transparent outside."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = _gradient(size, size)
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    radius = int(size * radius_ratio)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    canvas.paste(gradient, (0, 0), mask)
    return canvas


def _droplet_polygon(cx: float, cy: float, scale: float, samples: int = 360) -> list[tuple[float, float]]:
    """Teardrop pointing up: narrow point at top, rounded bottom.

    Parametric form: x(t) = sin(t), y(t) = -cos(t) + sin(t)^2 / 2 (image y-down).
    """
    pts: list[tuple[float, float]] = []
    for i in range(samples):
        t = 2 * math.pi * i / samples
        x = math.sin(t)
        y = -math.cos(t) + (math.sin(t) ** 2) / 2
        pts.append((cx + x * scale, cy + y * scale))
    return pts


def _draw_droplet(layer: Image.Image, cx: int, cy: int, scale: int) -> None:
    draw = ImageDraw.Draw(layer)
    draw.polygon(_droplet_polygon(cx, cy, scale), fill=DROPLET_COLOR)


def _draw_ble_text(layer: Image.Image, cx: int, droplet_cy: int, scale: int) -> None:
    """Stamp 'BLE' inside the droplet's wide lower lobe."""
    font_size = int(scale * 0.65)
    font = _load_font(font_size)
    text = "BLE"
    draw = ImageDraw.Draw(layer)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = cx - text_w / 2 - bbox[0]
    text_y = droplet_cy + scale * 0.35 - text_h / 2 - bbox[1]
    draw.text((text_x, text_y), text, font=font, fill=TEXT_COLOR)


def _icon_image(target_size: int) -> Image.Image:
    big = target_size * SUPERSAMPLE
    tile = _rounded_tile(big)
    droplet_scale = int(big * 0.28)
    cx = big // 2
    cy = int(big * 0.50)
    _draw_droplet(tile, cx, cy, droplet_scale)
    _draw_ble_text(tile, cx, cy, droplet_scale)
    return tile.resize((target_size, target_size), Image.LANCZOS)


def _rounded_gradient_rect(width: int, height: int, radius_ratio: float = 0.22) -> Image.Image:
    """Horizontal rounded-corner tile with the same teal gradient as the square icon."""
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gradient = _gradient(width, height)
    mask = Image.new("L", (width, height), 0)
    radius = int(min(width, height) * radius_ratio)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=radius, fill=255
    )
    canvas.paste(gradient, (0, 0), mask)
    return canvas


def _logo_image(target_w: int, target_h: int) -> Image.Image:
    big_w = target_w * SUPERSAMPLE
    big_h = target_h * SUPERSAMPLE

    canvas = _rounded_gradient_rect(big_w, big_h)

    droplet_scale = int(big_h * 0.28)
    droplet_cx = big_h // 2
    droplet_cy = int(big_h * 0.50)
    _draw_droplet(canvas, droplet_cx, droplet_cy, droplet_scale)
    _draw_ble_text(canvas, droplet_cx, droplet_cy, droplet_scale)

    wordmark = "B-Hyve"
    pad_left = int(big_h * 0.18)
    pad_right = int(big_h * 0.18)
    text_region_x = big_h
    available_w = big_w - text_region_x - pad_left - pad_right
    available_h = int(big_h * 0.60)
    font_size = _fit_font_size(wordmark, available_w, available_h)
    font = _load_font(font_size)
    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), wordmark, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = text_region_x + pad_left + (available_w - text_w) // 2 - bbox[0]
    text_y = (big_h - text_h) // 2 - bbox[1]
    draw.text((text_x, text_y), wordmark, font=font, fill=WORDMARK_COLOR)

    return canvas.resize((target_w, target_h), Image.LANCZOS)


def _fit_font_size(text: str, max_w: int, max_h: int) -> int:
    """Largest integer font size that fits ``text`` inside (max_w, max_h)."""
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lo, hi = 8, max_h
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(mid)
        bbox = measure.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= max_w and h <= max_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets: list[tuple[str, Image.Image]] = [
        ("icon.png", _icon_image(256)),
        ("icon@2x.png", _icon_image(512)),
        ("logo.png", _logo_image(256, 128)),
        ("logo@2x.png", _logo_image(512, 256)),
    ]
    for name, img in targets:
        path = OUT_DIR / name
        img.save(path, format="PNG", optimize=True)
        print(f"wrote {path} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
