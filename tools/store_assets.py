#!/usr/bin/env python3
"""Build the Snap Store listing media from the app screenshots.

The store page (https://snapcraft.io/dotdeck/listing) takes up to five
screenshots and a 3:1 banner, separately from the AppStream metainfo. This
frames data/screenshots/*.png on the brand gradient with short headlines:

    python3 tools/store_assets.py             # writes data/store/*.png

Needs Pillow and librsvg (gir1.2-rsvg-2.0); text uses the Inter font.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "screenshots"
ICON_PNG = ROOT / "data" / "icons" / "hicolor" / "512x512" / "apps" / "io.github.euvinicios.Dotdeck.png"

SLIDE_SIZE = (1760, 1100)  # 16:10
BANNER_SIZE = (1800, 600)  # 3:1, as the store asks for the featured banner
MAX_BYTES = 2 * 1024 * 1024  # store limit per image (2 MB)
RADIUS = 14  # GNOME window corners at 1×


@dataclass(frozen=True)
class Window:
    file: str
    x: int
    y: int
    scale: float = 1.0
    width: int | None = None


@dataclass(frozen=True)
class Slide:
    name: str
    title: str
    subtitle: str
    windows: tuple[Window, ...]


SLIDES = (
    Slide("1-workspace", "Every Dot, one click away",
          "A native sidebar with all your Dots: their pets, names and jobs, like a real team.",
          (Window("workspace.png", 260, 230),)),
    Slide("2-pets", "A pet for every Dot",
          "Twelve plush pets. Pick one in the gallery, or let Dotdeck choose by the job.",
          (Window("pets.png", 260, 230),)),
    Slide("3-settings", "Settings that go deep",
          "Search every option: Dots, voice and microphone, AI model, notifications, privacy.",
          (Window("settings-voice.png", 380, 250),)),
    Slide("4-dark-mode", "Made for GNOME, light and dark",
          "A clean Libadwaita interface that follows your desktop, with calls one click away.",
          (Window("workspace-dark.png", 260, 230),)),
    Slide("5-setup", "Set up in seconds",
          "Guided connection to this computer, your home network, Tailscale or the cloud.",
          (Window("welcome.png", 260, 230),)),
)


def render_svg(svg: str, size: tuple[int, int]) -> Image.Image:
    """Render at the SVG's own width and height, which match `size`."""
    pixbuf = Rsvg.Handle.new_from_data(svg.encode()).get_pixbuf()
    assert (pixbuf.get_width(), pixbuf.get_height()) == size
    return Image.frombuffer("RGBA", size, pixbuf.get_pixels(), "raw", "RGBA",
                            pixbuf.get_rowstride(), 1)


def dot_grid(width: int, height: int) -> str:
    """Delicate background neural dot grid echoing OpenDots."""
    dots = []
    spacing = 48
    for x in range(24, width, spacing):
        for y in range(24, height, spacing):
            # Radial distance from top-right glow center
            dx = (x - width * 0.82)
            dy = (y - height * 0.25)
            dist = math.sqrt(dx * dx + dy * dy)
            alpha = max(0.015, min(0.08, 0.08 - (dist / (width * 1.2)) * 0.07))
            dots.append(f'<circle cx="{x}" cy="{y}" r="2" fill="#38bdf8" fill-opacity="{alpha:.3f}"/>')
    return f'<g>{"".join(dots)}</g>'


def waves(cx: float, cy: float, radii: tuple[int, ...], start: float, end: float) -> str:
    """Faint voice-wave arcs, echoing the headset audio call feature."""
    paths = []
    for r in radii:
        a, b = math.radians(start), math.radians(end)
        x1, y1 = cx + r * math.cos(a), cy + r * math.sin(a)
        x2, y2 = cx + r * math.cos(b), cy + r * math.sin(b)
        paths.append(f'<path d="M{x1:.1f} {y1:.1f}A{r} {r} 0 0 1 {x2:.1f} {y2:.1f}"/>')
    return ('<g fill="none" stroke="#38bdf8" stroke-opacity="0.08" stroke-width="26" '
            f'stroke-linecap="round">{"".join(paths)}</g>')


def backdrop(size: tuple[int, int], content: str) -> str:
    width, height = size
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
  <defs>
    <linearGradient id="bd-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#090d16"/>
      <stop offset="0.45" stop-color="#0f172a"/>
      <stop offset="1" stop-color="#1e1b4b"/>
    </linearGradient>
    <radialGradient id="bd-glow-blue" cx="0.85" cy="0.18" r="0.75">
      <stop offset="0" stop-color="#3b82f6" stop-opacity="0.35"/>
      <stop offset="1" stop-color="#3b82f6" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="bd-glow-cyan" cx="0.12" cy="0.88" r="0.6">
      <stop offset="0" stop-color="#06b6d4" stop-opacity="0.22"/>
      <stop offset="1" stop-color="#06b6d4" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="{width}" height="{height}" fill="url(#bd-bg)"/>
  <rect width="{width}" height="{height}" fill="url(#bd-glow-blue)"/>
  <rect width="{width}" height="{height}" fill="url(#bd-glow-cyan)"/>
  {dot_grid(width, height)}
  {waves(-40, height * 0.72, (240, 380, 520), -60, 60)}
  {waves(width + 40, height * 0.72, (240, 380, 520), 120, 240)}
  {content}
</svg>"""


def text(x: float, y: float, value: str, size: int, *, weight: str = "Regular",
         fill: str = "#ffffff", anchor: str = "middle", opacity: float = 1.0) -> str:
    family = "Inter SemiBold" if weight == "SemiBold" else "Inter"
    spacing = -0.02 * size if size >= 48 else 0
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'letter-spacing="{spacing:.1f}" fill="{fill}" fill-opacity="{opacity}" '
            f'text-anchor="{anchor}">{escape(value)}</text>')


def rounded(size: tuple[int, int], radius: float, outline: int = 0) -> Image.Image:
    """Antialiased rounded-rectangle mask (filled, or just the outline)."""
    k = 4
    big = Image.new("L", (size[0] * k, size[1] * k), 0)
    box = (0, 0, big.width - 1, big.height - 1)
    if outline:
        ImageDraw.Draw(big).rounded_rectangle(box, radius * k, outline=255, width=outline * k)
    else:
        ImageDraw.Draw(big).rounded_rectangle(box, radius * k, fill=255)
    return big.resize(size, Image.LANCZOS)


def layer(canvas: Image.Image, image: Image.Image, pos: tuple[int, int]) -> Image.Image:
    top = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    top.paste(image, pos)
    return Image.alpha_composite(canvas, top)


def place(canvas: Image.Image, window: Window) -> Image.Image:
    shot = Image.open(SHOTS / window.file).convert("RGBA")
    if window.width:
        shot = shot.crop((0, 0, window.width, shot.height))
    if window.scale != 1:
        shot = shot.resize((round(shot.width * window.scale), round(shot.height * window.scale)),
                           Image.LANCZOS)
    radius = RADIUS * window.scale
    mask = rounded(shot.size, radius)

    pad = 90
    blur = Image.new("L", (shot.width + 2 * pad, shot.height + 2 * pad), 0)
    blur.paste(mask, (pad, pad))
    blur = blur.filter(ImageFilter.GaussianBlur(32)).point(lambda v: int(v * 0.65))
    shadow = Image.new("RGBA", blur.size, (4, 8, 20, 0))
    shadow.putalpha(blur)
    canvas = layer(canvas, shadow, (window.x - pad, window.y - pad + 24))

    shot.putalpha(mask)
    canvas = layer(canvas, shot, (window.x, window.y))
    edge = Image.new("RGBA", shot.size, (255, 255, 255, 0))
    edge.putalpha(rounded(shot.size, radius, outline=1).point(lambda v: int(v * 0.16)))
    return layer(canvas, edge, (window.x, window.y))


def save(image: Image.Image, path: Path) -> None:
    image.convert("RGB").save(path, optimize=True)
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"{path.name} has {size} bytes, over the store limit of {MAX_BYTES}")
    print(f"{path.relative_to(ROOT)}  {image.width}×{image.height}  {size // 1024} KB")


def build_slide(slide: Slide, out: Path) -> None:
    cx = SLIDE_SIZE[0] / 2
    header = (text(cx, 110, slide.title, 56, weight="SemiBold")
              + text(cx, 168, slide.subtitle, 28, fill="#cbd5e1"))
    canvas = render_svg(backdrop(SLIDE_SIZE, header), SLIDE_SIZE)
    for window in slide.windows:
        canvas = place(canvas, window)
    save(canvas, out / f"screenshot-{slide.name}.png")


def build_banner(out: Path) -> None:
    # 1800x600 banner
    badge_bg = """<g>
      <rect x="396" y="400" width="168" height="40" rx="20" fill="#3b82f6" fill-opacity="0.18"/>
      <rect x="396" y="400" width="168" height="40" rx="20" fill="none" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.5"/>
      <rect x="580" y="400" width="144" height="40" rx="20" fill="#3b82f6" fill-opacity="0.18"/>
      <rect x="580" y="400" width="144" height="40" rx="20" fill="none" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.5"/>
      <rect x="740" y="400" width="210" height="40" rx="20" fill="#3b82f6" fill-opacity="0.18"/>
      <rect x="740" y="400" width="210" height="40" rx="20" fill="none" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="1.5"/>
    </g>"""

    content = (
        text(396, 285, "Dotdeck", 124, weight="SemiBold", anchor="start")
        + text(400, 355, "Your OpenDots AI coworkers on the desktop", 36, anchor="start", fill="#f8fafc")
        + badge_bg
        + text(480, 426, "Native GNOME", 18, weight="SemiBold", fill="#e2e8f0", anchor="middle")
        + text(652, 426, "Voice Calls", 18, weight="SemiBold", fill="#e2e8f0", anchor="middle")
        + text(845, 426, "Free & Open Source", 18, weight="SemiBold", fill="#e2e8f0", anchor="middle")
    )
    canvas = render_svg(backdrop(BANNER_SIZE, content), BANNER_SIZE)

    # Paste the icon on the left
    if ICON_PNG.exists():
        icon_img = Image.open(ICON_PNG).convert("RGBA").resize((240, 240), Image.LANCZOS)
        # Icon shadow
        icon_shadow = Image.new("RGBA", (280, 280), (0, 0, 0, 0))
        icon_mask = icon_img.split()[3].filter(ImageFilter.GaussianBlur(18)).point(lambda v: int(v * 0.7))
        shadow_layer = Image.new("RGBA", (240, 240), (2, 6, 18, 0))
        shadow_layer.putalpha(icon_mask)
        canvas.paste(shadow_layer, (110, 190), shadow_layer)
        canvas.paste(icon_img, (110, 180), icon_img)

    # Place preview window on the right
    canvas = place(canvas, Window("workspace.png", 1000, 100, scale=0.62))
    save(canvas, out / "banner.png")


def build_icon(out: Path) -> None:
    if ICON_PNG.exists():
        img = Image.open(ICON_PNG)
        q = img.quantize(colors=256, method=Image.Quantize.LIBIMAGEQUANT)
        out_icon = out / "icon.png"
        q.save(out_icon, optimize=True)
        print(f"{out_icon.relative_to(ROOT)}  {q.width}×{q.height}  {out_icon.stat().st_size // 1024} KB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "store")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    build_icon(args.out)
    for slide in SLIDES:
        build_slide(slide, args.out)
    build_banner(args.out)


if __name__ == "__main__":
    main()
