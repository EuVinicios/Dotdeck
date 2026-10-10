#!/usr/bin/env python3
"""Build the Snap Store listing media and the site's social card from the screenshots.

The store page (https://snapcraft.io/dotdeck/listing) takes up to five
screenshots and a 3:1 banner, separately from the AppStream metainfo. This
frames the Brazilian Portuguese screenshots (data/screenshots/pt_BR, from
``make screenshots``) on the brand gradient with short headlines:

    python3 tools/store_assets.py             # writes data/store/*.png

It also draws data/store/social-card.png (1200×630), the picture that link
previews show for the website. Needs Pillow and librsvg (gir1.2-rsvg-2.0);
text uses the Inter font.
"""

from __future__ import annotations

import argparse
import functools
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "screenshots" / "pt_BR"
ICON_PNG = ROOT / "data" / "icons" / "hicolor" / "512x512" / "apps" / "io.github.euvinicios.Dotdeck.png"

SLIDE_SIZE = (1760, 1100)  # 16:10
BANNER_SIZE = (1800, 600)  # 3:1, as the store asks for the featured banner
CARD_SIZE = (1200, 630)    # Open Graph / Twitter large card
MAX_BYTES = 2 * 1024 * 1024  # store limit per image (2 MB)
RADIUS = 14  # GNOME window corners at 1×


@dataclass(frozen=True)
class Window:
    file: str
    x: int
    y: int
    scale: float = 1.0


@dataclass(frozen=True)
class Slide:
    name: str
    title: str
    subtitle: str
    windows: tuple[Window, ...]  # back to front


# The main window is 1342×842 and the Settings window 982×682 (tools/screenshots.py).
SLIDES = (
    Slide("1-dots", "Todos os seus Dots a um clique",
          "Uma barra lateral nativa com o pet, o nome e a função de cada Dot.",
          (Window("workspace.png", 276, 238, 0.9),)),
    Slide("2-pets", "Um pet para cada Dot",
          "Doze pets de pelúcia. Escolha na galeria ou deixe o Dotdeck escolher pela função.",
          (Window("pets.png", 276, 238, 0.9),)),
    Slide("3-modelos-de-ia", "Modelos de IA, com o custo à vista",
          "Saiba qual modelo os seus Dots usam, quanto custa e teste com um clique.",
          (Window("workspace.png", 150, 330, 0.7), Window("settings-model.png", 628, 290))),
    Slide("4-chamadas", "Ligue para os seus Dots",
          "Chamadas de voz com cancelamento de eco, no tema claro ou no escuro.",
          (Window("workspace-dark.png", 150, 330, 0.7),
           Window("settings-voice-dark.png", 628, 290))),
    Slide("5-configuracao", "Pronto em poucos segundos",
          "Conecte-se ao seu OpenDots: neste computador, na rede de casa ou na nuvem.",
          (Window("welcome.png", 130, 280, 0.6), Window("connected.png", 825, 420, 0.6))),
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


@functools.cache
def _font(family: str, size: int):
    """The font librsvg will use for ``family`` (asked to fontconfig), for measuring."""
    try:
        path = subprocess.run(["fc-match", "-f", "%{file}", family], check=True,
                              capture_output=True, text=True).stdout
        return ImageFont.truetype(path, size)
    except (OSError, subprocess.CalledProcessError):
        return None


def text_width(value: str, size: int, weight: str = "Regular") -> float:
    font = _font("Inter SemiBold" if weight == "SemiBold" else "Inter", size)
    return font.getlength(value) if font else len(value) * size * 0.56


def pills(x: float, y: float, labels: tuple[str, ...], size: int = 18,
          gap: int = 16) -> str:
    """Rounded tags on one line, starting at ``x`` (each sized to its label)."""
    parts = []
    height = round(size * 2.2)
    for label in labels:
        width = round(text_width(label, size, "SemiBold") + size * 2.2)
        parts.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{height / 2}" '
            'fill="#3b82f6" fill-opacity="0.18" stroke="#38bdf8" stroke-opacity="0.35" '
            'stroke-width="1.5"/>'
            + text(x + width / 2, y + height * 0.66, label, size, weight="SemiBold",
                   fill="#e2e8f0"))
        x += width + gap
    return "".join(parts)


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
    if window.scale != 1:
        shot = shot.resize((round(shot.width * window.scale), round(shot.height * window.scale)),
                           Image.LANCZOS)
    radius = RADIUS * max(window.scale, 0.75)
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


def place_icon(canvas: Image.Image, size: int, pos: tuple[int, int]) -> Image.Image:
    """The plush icon with a soft shadow under it."""
    if not ICON_PNG.exists():
        return canvas
    icon = Image.open(ICON_PNG).convert("RGBA").resize((size, size), Image.LANCZOS)
    pad = size // 4
    alpha = Image.new("L", (size + 2 * pad, size + 2 * pad), 0)
    alpha.paste(icon.split()[3], (pad, pad))
    alpha = alpha.filter(ImageFilter.GaussianBlur(size / 14)).point(lambda v: int(v * 0.7))
    shadow = Image.new("RGBA", alpha.size, (2, 6, 18, 0))
    shadow.putalpha(alpha)
    canvas = layer(canvas, shadow, (pos[0] - pad, pos[1] - pad + size // 20))
    return layer(canvas, icon, pos)


def save(image: Image.Image, path: Path) -> None:
    image.convert("RGB").save(path, optimize=True)
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"{path.name} has {size} bytes, over the store limit of {MAX_BYTES}")
    print(f"{path.relative_to(ROOT)}  {image.width}×{image.height}  {size // 1024} KB")


def build_slide(slide: Slide, out: Path) -> None:
    cx = SLIDE_SIZE[0] / 2
    header = (text(cx, 112, slide.title, 56, weight="SemiBold")
              + text(cx, 170, slide.subtitle, 28, fill="#cbd5e1"))
    canvas = render_svg(backdrop(SLIDE_SIZE, header), SLIDE_SIZE)
    for window in slide.windows:
        canvas = place(canvas, window)
    save(canvas, out / f"screenshot-{slide.name}.png")


def build_banner(out: Path) -> None:
    """1800×600: the icon, the name and the promise on the left, the app on the right."""
    content = (
        text(396, 282, "Dotdeck", 120, weight="SemiBold", anchor="start")
        + text(400, 350, "Seus Dots na área de trabalho", 38, anchor="start", fill="#f8fafc")
        + pills(400, 396, ("Feito para o GNOME", "Chamadas de voz", "Livre e gratuito"))
    )
    canvas = render_svg(backdrop(BANNER_SIZE, content), BANNER_SIZE)
    canvas = place_icon(canvas, 236, (124, 182))
    canvas = place(canvas, Window("workspace.png", 1075, 88, 0.5))
    save(canvas, out / "banner.png")


def build_social_card(out: Path) -> None:
    """1200×630 for link previews of the website (Open Graph and Twitter)."""
    content = (
        text(80, 312, "Dotdeck", 92, weight="SemiBold", anchor="start")
        + text(84, 370, "Seus Dots na área de trabalho", 32, anchor="start", fill="#f8fafc")
        + text(84, 414, "App nativo para Linux do OpenDots", 24, anchor="start",
               fill="#cbd5e1")
        + pills(84, 454, ("GNOME", "Chamadas de voz", "Código aberto"), size=17, gap=12)
    )
    canvas = render_svg(backdrop(CARD_SIZE, content), CARD_SIZE)
    canvas = place_icon(canvas, 132, (80, 106))
    canvas = place(canvas, Window("workspace.png", 650, 172, 0.56))
    path = out / "social-card.png"
    canvas.convert("RGB").save(path, optimize=True)
    print(f"{path.relative_to(ROOT)}  {canvas.width}×{canvas.height}  "
          f"{path.stat().st_size // 1024} KB")


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
    build_social_card(args.out)


if __name__ == "__main__":
    main()
