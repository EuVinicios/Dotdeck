#!/usr/bin/env python3
"""Render the Dotdeck pets: a family of plush characters, one per Dot.

Every pet is built from the same strand-by-strand fur renderer as the app icon
(tools/render_icon.py): a soft body, little arms and feet, a face, and one or two
details that make it recognizable at 32 px (ears, leaves, a hat, glasses...).
Colours and silhouettes are picked so that no two pets look alike, even in the
sidebar.

Deterministic (one fixed seed per pet), pure Python + pycairo. Usage:

    python3 tools/render_pets.py src/pets             # all pets, 256 px PNGs
    python3 tools/render_pets.py src/pets sky ember   # only some
    python3 tools/render_pets.py --sheet build/pets.png src/pets   # contact sheet

The roster (ids, names, colours) lives in src/core/pets.py so the app and the
renderer never disagree.
"""

from __future__ import annotations

import math
import sys
import zlib
from pathlib import Path

import cairo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import render_icon as plush  # noqa: E402
from render_icon import Blob, S, clamp, draw_fur, lerp, soft_ellipse, tube  # noqa: E402
from src.core.pets import PETS, Pet  # noqa: E402

OUT_SIZE = 256


# -- geometry helpers ---------------------------------------------------------------------

class PetBlob(Blob):
    """A fur volume whose rim light takes the pet's own colour (the icon's rim is blue)."""

    def shade(self, x, y, lift=0.0):
        nx, ny, nz, h = self.normal(x, y)
        L = plush.LIGHT
        ndl = nx * L[0] + ny * L[1] + nz * L[2]
        diffuse = clamp((ndl + 0.45) / 1.45)
        occlusion = (0.7 + 0.3 * h ** 0.5) * (1 - 0.2 * plush.smoothstep(0.4, 1.0, (y - self.cy) / self.b))
        rim = (1 - nz) ** 2 * clamp(nx * plush.RIM[0] + ny * plush.RIM[1] + 0.35) * 0.55
        sheen = clamp(ndl) ** 8 * 0.16
        k = (0.4 + 0.82 * diffuse) * occlusion * self.tone * (1 + lift)
        tint = lighter(self.colour, 0.55)
        return tuple(clamp(c * k + rim * t + sheen) for c, t in zip(self.colour, tint))


class Frame:
    """Maps the app icon's coordinates (body centred at 512,572 with a=316, b=352) onto
    the current pet's body, so face and accessories follow its size."""

    def __init__(self, body: Blob):
        self.body = body
        self.sx = body.a / 316
        self.sy = body.b / 352

    def x(self, x: float) -> float:
        return self.body.cx + (x - 512) * self.sx

    def y(self, y: float) -> float:
        return self.body.cy + (y - 572) * self.sy

    def top(self) -> float:
        return self.body.cy - self.body.b

    def apply(self, ctx) -> None:
        ctx.translate(self.body.cx, self.body.cy)
        ctx.scale(self.sx, self.sy)
        ctx.translate(-512, -572)


def darker(colour, k):
    return tuple(clamp(c * k) for c in colour)


def lighter(colour, k):
    return tuple(clamp(c + (1 - c) * k) for c in colour)


def glossy(ctx, x0, y0, x1, y1, base, highlight=0.55, shadow=0.55):
    gradient = cairo.LinearGradient(x0, y0, x1, y1)
    gradient.add_color_stop_rgb(0, *lighter(base, highlight))
    gradient.add_color_stop_rgb(0.45, *base)
    gradient.add_color_stop_rgb(1, *darker(base, shadow))
    return gradient


# -- body parts ---------------------------------------------------------------------------

BODIES = {
    # name: (cx, cy, a, b, p, flare)
    "round": (512, 600, 300, 318, 2.55, 0.08),
    "pear": (512, 610, 296, 314, 2.25, 0.2),
    "bean": (512, 596, 270, 330, 2.6, 0.04),
    "wide": (512, 618, 318, 296, 2.7, 0.1),
}


def ear_blobs(pet: Pet, body: Blob, f: Frame) -> list[tuple[Blob, tuple | None]]:
    """Ears are fur volumes drawn behind the body; each may have a lighter inner ear."""
    kind, colour = pet.ears, pet.fur
    inner = pet.inner or lighter(colour, 0.45)
    ears = []
    if kind == "bear":
        for side in (-1, 1):
            ears.append((PetBlob(f.x(512 + side * 236), f.y(262), 92 * f.sx, 86 * f.sy, p=2.0,
                              flare=0.0, colour=colour, tone=0.96), inner))
    elif kind in ("cat", "fox"):
        tall = 150 if kind == "fox" else 118
        wide = 96 if kind == "fox" else 84
        for side in (-1, 1):
            ears.append((PetBlob(f.x(512 + side * 196), f.y(250 - (tall - 118) * 0.4),
                              wide * f.sx, tall * f.sy, p=1.45, flare=0.0,
                              angle=math.radians(side * 22), colour=colour, tone=0.95), inner))
    elif kind == "bunny":
        for side in (-1, 1):
            ears.append((PetBlob(f.x(512 + side * 112), f.y(120), 62 * f.sx, 196 * f.sy, p=2.0,
                              flare=0.0, angle=math.radians(side * 11), colour=colour,
                              tone=0.97), inner))
    elif kind == "leaves":
        leaf = pet.detail or (0.42, 0.78, 0.55)
        for side in (-1, 1):
            ears.append((PetBlob(f.x(512 + side * 118), f.y(170), 132 * f.sx, 70 * f.sy, p=1.7,
                              flare=0.0, angle=math.radians(side * -28), colour=leaf,
                              tone=1.0), None))
    return ears


def draw_inner_ear(ctx, blob: Blob, colour) -> None:
    """A soft, slightly velvety inner ear, following the ear's shape and tilt."""
    ctx.save()
    ctx.translate(blob.cx, blob.cy)
    ctx.rotate(blob.angle)
    ctx.translate(0, blob.b * 0.12)
    ctx.scale(blob.a * 0.5, blob.b * 0.62)
    gradient = cairo.RadialGradient(0, -0.2, 0.05, 0, 0, 1)
    gradient.add_color_stop_rgba(0, *lighter(colour, 0.25), 0.95)
    gradient.add_color_stop_rgba(0.7, *colour, 0.85)
    gradient.add_color_stop_rgba(1, *colour, 0)
    ctx.set_source(gradient)
    if blob.p < 1.8:  # pointy ears: a rounded triangle
        ctx.move_to(0, -1)
        ctx.curve_to(0.5, -0.2, 0.9, 0.7, 0, 0.9)
        ctx.curve_to(-0.9, 0.7, -0.5, -0.2, 0, -1)
    else:
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
    ctx.fill()
    ctx.restore()


def limbs(pet: Pet, body: Blob, f: Frame) -> list[Blob]:
    tone = 0.92
    colour = pet.fur
    feet = [PetBlob(f.x(512 + side * 128), body.cy + body.b * 0.93, 98 * f.sx, 56 * f.sy, p=2.2,
                 flare=0.0, colour=colour, tone=0.94) for side in (-1, 1)]
    reach = body.a * 0.98
    arms = [PetBlob(body.cx + side * reach, body.cy + body.b * 0.36, 56 * f.sx, 94 * f.sy,
                 p=2.2, flare=0.0, angle=math.radians(-side * 18), colour=colour, tone=tone)
            for side in (-1, 1)]
    return feet + arms


# -- face ---------------------------------------------------------------------------------

def draw_face(ctx, pet: Pet, f: Frame) -> None:
    shade = darker(pet.fur, 0.25)
    eye_y = 528
    for ex in (402, 622):
        soft_ellipse(ctx, f.x(ex), f.y(eye_y + 10), 74 * f.sx, 88 * f.sy, (*shade, 0.32))
    for ex in (402, 622):
        if pet.eyes == "happy":
            ctx.save()
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_line_width(17 * f.sx)
            ctx.set_source_rgb(0.05, 0.05, 0.08)
            ctx.move_to(f.x(ex - 40), f.y(eye_y + 12))
            ctx.curve_to(f.x(ex - 22), f.y(eye_y - 30), f.x(ex + 22), f.y(eye_y - 30),
                         f.x(ex + 40), f.y(eye_y + 12))
            ctx.stroke()
            ctx.restore()
            continue
        rx, ry = 48 * f.sx, 60 * f.sy
        ctx.save()
        ctx.translate(f.x(ex), f.y(eye_y))
        ctx.scale(rx, ry)
        gradient = cairo.RadialGradient(-0.25, -0.3, 0.05, 0, 0, 1)
        gradient.add_color_stop_rgb(0, 0.26, 0.27, 0.36)
        gradient.add_color_stop_rgb(0.55, 0.05, 0.06, 0.09)
        gradient.add_color_stop_rgb(1, 0.01, 0.01, 0.02)
        ctx.set_source(gradient)
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
        ctx.fill()
        ctx.arc(0, 0, 0.86, math.radians(30), math.radians(150))
        ctx.restore()
        ctx.set_line_width(5 * f.sx)
        ctx.set_source_rgba(*lighter(pet.fur, 0.3), 0.4)
        ctx.stroke()
        soft_ellipse(ctx, f.x(ex + 16), f.y(eye_y - 22), 19 * f.sx, 16 * f.sy, (1, 1, 1, 0.98),
                     inner=0.55)
        soft_ellipse(ctx, f.x(ex - 16), f.y(eye_y + 28), 8 * f.sx, 7 * f.sy, (1, 1, 1, 0.7),
                     inner=0.4)
    for cx in (318, 706):
        soft_ellipse(ctx, f.x(cx), f.y(618), 62 * f.sx, 36 * f.sy, (*pet.blush, 0.55))

    ctx.save()
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ink = (0.06, 0.05, 0.08)
    if pet.mouth == "open":
        ctx.move_to(f.x(470), f.y(612))
        ctx.curve_to(f.x(480), f.y(668), f.x(544), f.y(668), f.x(554), f.y(612))
        ctx.close_path()
        ctx.set_source_rgb(0.18, 0.04, 0.06)
        ctx.fill_preserve()
        ctx.save()
        ctx.clip()
        soft_ellipse(ctx, f.x(512), f.y(660), 40 * f.sx, 26 * f.sy, (0.98, 0.45, 0.52, 1),
                     inner=0.6)
        ctx.restore()
        ctx.set_line_width(7 * f.sx)
        ctx.set_source_rgb(*ink)
        ctx.stroke()
    elif pet.mouth == "cat":
        ctx.move_to(f.x(462), f.y(616))
        ctx.curve_to(f.x(476), f.y(648), f.x(506), f.y(646), f.x(512), f.y(620))
        ctx.curve_to(f.x(518), f.y(646), f.x(548), f.y(648), f.x(562), f.y(616))
        ctx.set_line_width(15 * f.sx)
        ctx.set_source_rgb(*ink)
        ctx.stroke()
        # tiny nose
        ctx.move_to(f.x(496), f.y(596))
        ctx.line_to(f.x(528), f.y(596))
        ctx.line_to(f.x(512), f.y(614))
        ctx.close_path()
        ctx.set_source_rgb(*darker(pet.blush, 0.8))
        ctx.fill()
    else:
        ctx.move_to(f.x(464), f.y(622))
        ctx.curve_to(f.x(488), f.y(658), f.x(536), f.y(658), f.x(560), f.y(622))
        ctx.set_line_width(18 * f.sx)
        ctx.set_source_rgb(*ink)
        ctx.stroke()
    ctx.restore()


# -- accessories --------------------------------------------------------------------------

def headset(ctx, f: Frame) -> None:
    """The app icon's headset, fitted to this pet."""
    ctx.save()
    f.apply(ctx)
    edge, mid, top = (0.62, 0.53, 0.38, 1), (0.88, 0.81, 0.67, 1), (0.99, 0.97, 0.91, 1)
    widths = [96, 88, 78, 66, 54, 40, 26, 12]
    colours = [lerp(edge, mid, i / 4) if i < 4 else lerp(mid, top, (i - 4) / 3) for i in range(8)]
    tube(ctx, plush.band_path, widths, colours)
    for x, inner_x in ((60, 192), (796, 784)):
        plush.cup_path(ctx, x, 384, 168, 290, 84)
        gradient = cairo.LinearGradient(x, 384, x + 168, 674)
        gradient.add_color_stop_rgb(0, 0.99, 0.97, 0.91)
        gradient.add_color_stop_rgb(0.55, 0.9, 0.84, 0.71)
        gradient.add_color_stop_rgb(1, 0.66, 0.57, 0.42)
        ctx.set_source(gradient)
        ctx.fill()
        plush.cup_path(ctx, inner_x, 420, 48, 218, 24)
        gradient = cairo.LinearGradient(inner_x, 420, inner_x + 48, 638)
        gradient.add_color_stop_rgb(0, 0.86, 0.79, 0.64)
        gradient.add_color_stop_rgb(1, 0.6, 0.51, 0.36)
        ctx.set_source(gradient)
        ctx.fill()
    tube(ctx, plush.boom_path, [34, 28, 20, 11], [lerp(edge, top, t) for t in (0, 0.4, 0.75, 1)],
         offset=(0.6, -1.4))
    ctx.translate(330, 774)
    gradient = cairo.RadialGradient(-12, -14, 4, 0, 0, 44)
    gradient.add_color_stop_rgb(0, 0.36, 0.37, 0.42)
    gradient.add_color_stop_rgb(1, 0.06, 0.06, 0.08)
    ctx.set_source(gradient)
    ctx.arc(0, 0, 42, 0, 2 * math.pi)
    ctx.fill()
    ctx.restore()


def glasses(ctx, f: Frame, colour=(0.12, 0.1, 0.1)) -> None:
    ctx.save()
    f.apply(ctx)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    for ex in (402, 622):
        # lens tint and glare
        ctx.arc(ex, 532, 86, 0, 2 * math.pi)
        ctx.set_source_rgba(0.85, 0.92, 1.0, 0.16)
        ctx.fill()
        ctx.save()
        ctx.arc(ex, 532, 80, 0, 2 * math.pi)
        ctx.clip()
        ctx.set_source_rgba(1, 1, 1, 0.28)
        ctx.set_line_width(14)
        ctx.move_to(ex - 60, 500)
        ctx.line_to(ex - 12, 452)
        ctx.stroke()
        ctx.set_line_width(7)
        ctx.move_to(ex - 40, 530)
        ctx.line_to(ex + 6, 484)
        ctx.stroke()
        ctx.restore()
        for width, shade in ((20, 0.0), (12, 0.35), (4, 0.7)):
            ctx.arc(ex, 532, 86, 0, 2 * math.pi)
            ctx.set_line_width(width)
            ctx.set_source_rgb(*lighter(colour, shade))
            ctx.stroke()
    ctx.move_to(488, 520)
    ctx.curve_to(500, 500, 524, 500, 536, 520)
    ctx.set_line_width(16)
    ctx.set_source_rgb(*colour)
    ctx.stroke()
    ctx.restore()


def crown(ctx, f: Frame) -> None:
    gold = (0.98, 0.76, 0.22)
    top = f.top()
    cx = f.body.cx
    w, h = 300 * f.sx, 210 * f.sy
    base = top + 64 * f.sy
    ctx.save()
    ctx.translate(cx, base)
    ctx.rotate(math.radians(-7))
    soft_ellipse(ctx, 0, 6, w * 0.62, 26 * f.sy, (0, 0, 0, 0.25))
    ctx.move_to(-w / 2, 0)
    points = [(-w / 2, -h * 0.82), (-w / 4, -h * 0.42), (0, -h), (w / 4, -h * 0.42),
              (w / 2, -h * 0.82), (w / 2, 0)]
    for x, y in points:
        ctx.line_to(x, y)
    ctx.close_path()
    ctx.set_source(glossy(ctx, -w / 2, -h, w / 2, 0, gold, 0.5, 0.62))
    ctx.fill_preserve()
    ctx.set_line_width(5)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.set_source_rgba(*darker(gold, 0.55), 0.9)
    ctx.stroke()
    ctx.rectangle(-w / 2, -h * 0.2, w, h * 0.2)
    ctx.set_source(glossy(ctx, 0, -h * 0.2, 0, 0, darker(gold, 0.9), 0.3, 0.55))
    ctx.fill()
    for x, y, gem in ((-w / 2, -h * 0.82, (0.3, 0.6, 1.0)), (0, -h, (0.95, 0.25, 0.35)),
                      (w / 2, -h * 0.82, (0.3, 0.85, 0.55)), (0, -h * 0.1, (0.95, 0.25, 0.35))):
        r = 22 * f.sx
        gradient = cairo.RadialGradient(x - r * 0.3, y - r * 0.3, r * 0.1, x, y, r)
        gradient.add_color_stop_rgb(0, *lighter(gem, 0.7))
        gradient.add_color_stop_rgb(1, *darker(gem, 0.6))
        ctx.set_source(gradient)
        ctx.arc(x, y, r, 0, 2 * math.pi)
        ctx.fill()
    ctx.restore()


def bow(ctx, f: Frame, colour=(0.96, 0.32, 0.45)) -> None:
    cx, cy = f.x(676), f.top() + 64 * f.sy
    s = f.sx * 1.45
    ctx.save()
    ctx.translate(cx, cy)
    ctx.rotate(math.radians(16))
    soft_ellipse(ctx, 0, 30 * s, 150 * s, 40 * s, (0, 0, 0, 0.22))
    for side in (-1, 1):
        ctx.save()
        ctx.scale(side, 1)
        ctx.move_to(0, 0)
        ctx.curve_to(60 * s, -110 * s, 170 * s, -80 * s, 150 * s, 0)
        ctx.curve_to(170 * s, 80 * s, 60 * s, 110 * s, 0, 0)
        ctx.set_source(glossy(ctx, 0, -100 * s, 0, 100 * s, colour, 0.45, 0.6))
        ctx.fill_preserve()
        ctx.set_source_rgba(*darker(colour, 0.5), 0.5)
        ctx.set_line_width(4 * s)
        ctx.stroke()
        # satin folds
        ctx.move_to(20 * s, -10 * s)
        ctx.curve_to(70 * s, -50 * s, 110 * s, -40 * s, 130 * s, -10 * s)
        ctx.set_source_rgba(1, 1, 1, 0.3)
        ctx.set_line_width(9 * s)
        ctx.stroke()
        # ribbon tails
        ctx.move_to(10 * s, 20 * s)
        ctx.line_to(60 * s, 130 * s)
        ctx.line_to(90 * s, 110 * s)
        ctx.line_to(30 * s, 14 * s)
        ctx.close_path()
        ctx.set_source(glossy(ctx, 0, 0, 0, 130 * s, darker(colour, 0.92), 0.3, 0.6))
        ctx.fill()
        ctx.restore()
    ctx.arc(0, 0, 34 * s, 0, 2 * math.pi)
    ctx.set_source(glossy(ctx, 0, -34 * s, 0, 34 * s, colour, 0.5, 0.55))
    ctx.fill()
    ctx.restore()


def beanie(ctx, f: Frame, body: Blob, colour=(0.85, 0.24, 0.24), pom=(0.97, 0.96, 0.93)) -> None:
    rim = f.top() + 150 * f.sy
    layer = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
    lctx = cairo.Context(layer)
    body.path(lctx, grow=14)
    lctx.clip()
    lctx.rectangle(0, 0, S, rim)
    gradient = cairo.LinearGradient(body.cx - body.a, 0, body.cx + body.a, 0)
    gradient.add_color_stop_rgb(0, *darker(colour, 0.62))
    gradient.add_color_stop_rgb(0.4, *lighter(colour, 0.12))
    gradient.add_color_stop_rgb(1, *darker(colour, 0.5))
    lctx.set_source(gradient)
    lctx.fill()
    # knitted ribs
    lctx.set_line_cap(cairo.LINE_CAP_ROUND)
    x = body.cx - body.a
    while x < body.cx + body.a:
        for y in range(int(f.top()) - 20, int(rim), 22):
            lctx.move_to(x - 7, y)
            lctx.line_to(x, y + 14)
            lctx.line_to(x + 7, y)
            lctx.set_line_width(5)
            lctx.set_source_rgba(1, 1, 1, 0.1)
            lctx.stroke()
        x += 26
    # shading on the dome
    shade = cairo.RadialGradient(body.cx - body.a * 0.3, f.top() + 30, 10, body.cx, rim, body.a * 1.1)
    shade.add_color_stop_rgba(0, 1, 1, 1, 0.18)
    shade.add_color_stop_rgba(1, 0, 0, 0, 0.3)
    lctx.rectangle(0, 0, S, rim)
    lctx.set_source(shade)
    lctx.fill()
    ctx.set_source_surface(layer)
    ctx.paint()
    # folded cuff
    ctx.save()
    body.path(ctx, grow=22)
    ctx.clip()
    ctx.rectangle(0, rim - 70 * f.sy, S, 74 * f.sy)
    gradient = cairo.LinearGradient(0, rim - 70 * f.sy, 0, rim + 4)
    gradient.add_color_stop_rgb(0, *lighter(colour, 0.1))
    gradient.add_color_stop_rgb(1, *darker(colour, 0.6))
    ctx.set_source(gradient)
    ctx.fill()
    x = body.cx - body.a - 20
    ctx.set_line_width(9)
    while x < body.cx + body.a + 20:
        ctx.move_to(x, rim - 64 * f.sy)
        ctx.line_to(x, rim - 4)
        ctx.set_source_rgba(0, 0, 0, 0.16)
        ctx.stroke()
        x += 22
    ctx.restore()
    soft_ellipse(ctx, body.cx, rim + 10, body.a * 0.95, 26, (0, 0, 0, 0.25))
    ball = PetBlob(body.cx + 30 * f.sx, f.top() - 26 * f.sy, 70 * f.sx, 66 * f.sy, p=2.0, flare=0.0,
                colour=pom, tone=1.0)
    draw_fur(ctx, ball, 7000, length=(9, 16), width=(1.4, 2.2))


def cap(ctx, f: Frame) -> None:
    """A graduation cap with a golden tassel."""
    top = f.top()
    cx = f.body.cx
    ink = (0.13, 0.14, 0.2)
    s = f.sx
    ctx.save()
    ctx.translate(cx, top + 70 * f.sy)
    ctx.rotate(math.radians(-6))
    # skull cap
    ctx.move_to(-150 * s, 10 * s)
    ctx.curve_to(-150 * s, -60 * s, 150 * s, -60 * s, 150 * s, 10 * s)
    ctx.curve_to(80 * s, 40 * s, -80 * s, 40 * s, -150 * s, 10 * s)
    ctx.set_source(glossy(ctx, 0, -60 * s, 0, 40 * s, ink, 0.25, 0.5))
    ctx.fill()
    # board in perspective
    ctx.move_to(0, -150 * s)
    ctx.line_to(250 * s, -70 * s)
    ctx.line_to(0, 6 * s)
    ctx.line_to(-250 * s, -70 * s)
    ctx.close_path()
    ctx.set_source(glossy(ctx, -250 * s, -150 * s, 250 * s, 6 * s, ink, 0.3, 0.45))
    ctx.fill_preserve()
    ctx.set_source_rgba(1, 1, 1, 0.12)
    ctx.set_line_width(5 * s)
    ctx.stroke()
    ctx.move_to(-250 * s, -70 * s)
    ctx.line_to(0, 6 * s)
    ctx.line_to(250 * s, -70 * s)
    ctx.line_to(250 * s, -56 * s)
    ctx.line_to(0, 22 * s)
    ctx.line_to(-250 * s, -56 * s)
    ctx.close_path()
    ctx.set_source_rgb(*darker(ink, 0.6))
    ctx.fill()
    # tassel
    gold = (0.97, 0.76, 0.25)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.move_to(0, -74 * s)
    ctx.curve_to(90 * s, -70 * s, 170 * s, -50 * s, 186 * s, -40 * s)
    ctx.line_to(190 * s, 60 * s)
    ctx.set_line_width(9 * s)
    ctx.set_source_rgb(*gold)
    ctx.stroke()
    ctx.arc(0, -74 * s, 16 * s, 0, 2 * math.pi)
    ctx.set_source(glossy(ctx, 0, -90 * s, 0, -58 * s, gold, 0.5, 0.6))
    ctx.fill()
    for i in range(14):
        x = 178 * s + i * 1.4 * s
        ctx.move_to(x, 60 * s)
        ctx.line_to(x + (i - 7) * 2.2 * s, 130 * s)
        ctx.set_line_width(4 * s)
        ctx.set_source_rgb(*(lighter(gold, 0.2) if i % 2 else darker(gold, 0.8)))
        ctx.stroke()
    ctx.restore()


def hardhat(ctx, f: Frame, colour=(1.0, 0.78, 0.12)) -> None:
    top = f.top()
    cx = f.body.cx
    s = f.sx
    ctx.save()
    ctx.translate(cx, top + 116 * f.sy)
    soft_ellipse(ctx, 0, 18 * s, 300 * s, 40 * s, (0, 0, 0, 0.28))
    # brim
    ctx.save()
    ctx.scale(1, 0.24)
    ctx.arc(0, 0, 300 * s, 0, 2 * math.pi)
    ctx.restore()
    ctx.set_source(glossy(ctx, 0, -40 * s, 0, 40 * s, darker(colour, 0.9), 0.35, 0.55))
    ctx.fill()
    # dome
    ctx.move_to(-218 * s, 0)
    ctx.curve_to(-218 * s, -230 * s, 218 * s, -230 * s, 218 * s, 0)
    ctx.close_path()
    gradient = cairo.RadialGradient(-80 * s, -150 * s, 10 * s, 0, -60 * s, 260 * s)
    gradient.add_color_stop_rgb(0, *lighter(colour, 0.75))
    gradient.add_color_stop_rgb(0.35, *colour)
    gradient.add_color_stop_rgb(1, *darker(colour, 0.6))
    ctx.set_source(gradient)
    ctx.fill()
    # ridge
    ctx.move_to(-30 * s, -168 * s)
    ctx.curve_to(-30 * s, -175 * s, 30 * s, -175 * s, 30 * s, -168 * s)
    ctx.line_to(30 * s, -4 * s)
    ctx.line_to(-30 * s, -4 * s)
    ctx.close_path()
    ctx.set_source_rgba(*darker(colour, 0.75), 0.9)
    ctx.fill()
    ctx.move_to(-140 * s, -120 * s)
    ctx.curve_to(-110 * s, -150 * s, -80 * s, -162 * s, -50 * s, -166 * s)
    ctx.set_source_rgba(1, 1, 1, 0.55)
    ctx.set_line_width(14 * s)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.stroke()
    ctx.restore()


def antenna(ctx, f: Frame, glow=(0.62, 0.95, 1.0)) -> None:
    top = f.top() + 24 * f.sy
    cx = f.body.cx + 20 * f.sx
    s = f.sx

    def stalk(c):
        c.move_to(cx, top + 30 * s)
        c.curve_to(cx + 10 * s, top - 60 * s, cx + 60 * s, top - 100 * s, cx + 70 * s, top - 150 * s)

    tube(ctx, stalk, [34 * s, 26 * s, 17 * s, 8 * s],
         [(0.25, 0.25, 0.3, 1), (0.42, 0.42, 0.48, 1), (0.6, 0.6, 0.66, 1), (0.8, 0.8, 0.85, 1)],
         offset=(-0.6, -0.4))
    bx, by = cx + 74 * s, top - 182 * s
    soft_ellipse(ctx, bx, by, 150 * s, 150 * s, (*glow, 0.5))
    gradient = cairo.RadialGradient(bx - 20 * s, by - 22 * s, 6 * s, bx, by, 68 * s)
    gradient.add_color_stop_rgb(0, 1, 1, 1)
    gradient.add_color_stop_rgb(0.4, *lighter(glow, 0.4))
    gradient.add_color_stop_rgb(1, *darker(glow, 0.6))
    ctx.set_source(gradient)
    ctx.arc(bx, by, 68 * s, 0, 2 * math.pi)
    ctx.fill()


def scarf(ctx, f: Frame, body: Blob, colour=(0.2, 0.55, 0.85)) -> None:
    y = body.cy + body.b * 0.38
    ctx.save()
    body.path(ctx, grow=24)
    ctx.clip()
    ctx.move_to(0, y - 46 * f.sy)
    ctx.curve_to(S * 0.3, y - 10 * f.sy, S * 0.7, y - 10 * f.sy, S, y - 46 * f.sy)
    ctx.line_to(S, y + 40 * f.sy)
    ctx.curve_to(S * 0.7, y + 76 * f.sy, S * 0.3, y + 76 * f.sy, 0, y + 40 * f.sy)
    ctx.close_path()
    ctx.set_source(glossy(ctx, 0, y - 40 * f.sy, 0, y + 70 * f.sy, colour, 0.35, 0.55))
    ctx.fill()
    for i in range(-12, 13):
        x = body.cx + i * 26 * f.sx
        ctx.move_to(x, y - 30 * f.sy)
        ctx.line_to(x + 6, y + 60 * f.sy)
        ctx.set_source_rgba(1, 1, 1, 0.12 if i % 2 else 0.0)
        ctx.set_line_width(10)
        ctx.stroke()
    ctx.restore()
    # hanging end
    x0 = body.cx + body.a * 0.35
    ctx.move_to(x0, y + 30 * f.sy)
    ctx.line_to(x0 + 70 * f.sx, y + 30 * f.sy)
    ctx.line_to(x0 + 84 * f.sx, y + 220 * f.sy)
    ctx.line_to(x0 + 6 * f.sx, y + 214 * f.sy)
    ctx.close_path()
    ctx.set_source(glossy(ctx, x0, 0, x0 + 84 * f.sx, 0, darker(colour, 0.95), 0.3, 0.55))
    ctx.fill()
    for i in range(5):
        ctx.move_to(x0 + (14 + i * 15) * f.sx, y + 212 * f.sy)
        ctx.line_to(x0 + (16 + i * 15) * f.sx, y + 250 * f.sy)
        ctx.set_line_width(8 * f.sx)
        ctx.set_source_rgb(*darker(colour, 0.85))
        ctx.stroke()


# -- composition --------------------------------------------------------------------------

def render(pet: Pet) -> cairo.ImageSurface:
    plush.rnd.seed(zlib.crc32(pet.id.encode()))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
    ctx = cairo.Context(surface)
    cx, cy, a, b, p, flare = BODIES[pet.body]
    body = PetBlob(cx, cy, a, b, p=p, flare=flare, colour=pet.fur)
    f = Frame(body)

    soft_ellipse(ctx, cx, cy + b + 52, a * 1.05, 44, (0, 0, 0, 0.3))
    soft_ellipse(ctx, cx, cy + b + 48, a * 0.76, 26, (0, 0, 0, 0.24))

    ears = ear_blobs(pet, body, f)
    for ear, inner in ears:
        draw_fur(ctx, ear, 9000 if pet.ears != "bunny" else 12000, length=(9, 16),
                 width=(1.4, 2.2))
        if inner:
            draw_inner_ear(ctx, ear, inner)
    if pet.ears == "leaves":  # stem and leaf veins
        ctx.save()
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        for ear, _ in ears:
            ctx.move_to(cx, f.top() + 40)
            ctx.curve_to(cx, f.top() + 10, (cx + ear.cx) / 2, ear.cy + 20, ear.cx, ear.cy)
            ctx.set_line_width(10 * f.sx)
            ctx.set_source_rgba(*darker(ear.colour, 0.6), 0.65)
            ctx.stroke()
        ctx.restore()

    for blob in limbs(pet, body, f):
        draw_fur(ctx, blob, 9000, length=(9, 15), width=(1.4, 2.2))
    soft_ellipse(ctx, cx, cy + b * 0.88, a * 1.04, 70, (*darker(pet.fur, 0.15), 0.22))
    draw_fur(ctx, body, int(130000 * (a * b) / (316 * 352)), length=(13, 24), width=(1.5, 2.4))
    if pet.belly:
        ctx.save()
        body.path(ctx)
        ctx.clip()
        soft_ellipse(ctx, cx, cy + b * 0.42, a * 0.55, b * 0.42, (*pet.belly, 0.55), inner=0.45)
        ctx.restore()

    if pet.top == "headset":
        soft_ellipse(ctx, f.x(250), f.y(560), 110 * f.sx, 190 * f.sy, (*darker(pet.fur, 0.1), 0.42))
        soft_ellipse(ctx, f.x(790), f.y(560), 110 * f.sx, 190 * f.sy, (*darker(pet.fur, 0.1), 0.42))
    draw_face(ctx, pet, f)
    if pet.face == "glasses":
        glasses(ctx, f)
    if pet.extra == "scarf":
        scarf(ctx, f, body, pet.detail or (0.2, 0.55, 0.85))

    top = {
        "headset": lambda: headset(ctx, f),
        "crown": lambda: crown(ctx, f),
        "bow": lambda: bow(ctx, f, pet.detail or (0.96, 0.32, 0.45)),
        "beanie": lambda: beanie(ctx, f, body, pet.detail or (0.85, 0.24, 0.24)),
        "cap": lambda: cap(ctx, f),
        "hardhat": lambda: hardhat(ctx, f),
        "antenna": lambda: antenna(ctx, f, pet.detail or (0.62, 0.95, 1.0)),
    }.get(pet.top)
    if top:
        top()
    return surface


def scaled(surface: cairo.ImageSurface, size: int) -> cairo.ImageSurface:
    while surface.get_width() >= size * 2:
        surface = plush.halve(surface)
    if surface.get_width() != size:
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        ctx = cairo.Context(out)
        k = size / surface.get_width()
        ctx.scale(k, k)
        pattern = cairo.SurfacePattern(surface)
        pattern.set_filter(cairo.FILTER_GOOD)
        ctx.set_source(pattern)
        ctx.paint()
        surface = out
    return surface


def contact_sheet(paths: list[Path], target: Path, columns=6) -> None:
    cell = 180
    rows = (len(paths) + columns - 1) // columns
    sheet = cairo.ImageSurface(cairo.FORMAT_ARGB32, columns * cell, rows * (cell + 26))
    ctx = cairo.Context(sheet)
    ctx.set_source_rgb(0.96, 0.96, 0.97)
    ctx.paint()
    for i, path in enumerate(paths):
        img = cairo.ImageSurface.create_from_png(str(path))
        x, y = (i % columns) * cell, (i // columns) * (cell + 26)
        ctx.save()
        ctx.translate(x + 10, y + 6)
        k = (cell - 20) / img.get_width()
        ctx.scale(k, k)
        ctx.set_source_surface(img)
        ctx.get_source().set_filter(cairo.FILTER_GOOD)
        ctx.paint()
        ctx.restore()
        ctx.set_source_rgb(0.2, 0.2, 0.24)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(15)
        name = path.stem
        ext = ctx.text_extents(name)
        ctx.move_to(x + (cell - ext.width) / 2, y + cell + 14)
        ctx.show_text(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_to_png(str(target))
    print(target)


def main(argv: list[str]) -> None:
    sheet = None
    if argv[:1] == ["--sheet"]:
        sheet, argv = Path(argv[1]), argv[2:]
    out = Path(argv[0] if argv else ROOT / "src" / "pets")
    wanted = set(argv[1:])
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for pet in PETS:
        if wanted and pet.id not in wanted:
            continue
        target = out / f"{pet.id}.png"
        scaled(render(pet), OUT_SIZE).write_to_png(str(target))
        written.append(target)
        print(target, flush=True)
    if sheet:
        contact_sheet(sorted(out.glob("*.png"), key=lambda p: [x.id for x in PETS].index(p.stem)
                             if p.stem in [x.id for x in PETS] else 99), sheet)


if __name__ == "__main__":
    main(sys.argv[1:])
