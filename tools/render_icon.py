#!/usr/bin/env python3
"""Render the Dotdeck app icon: a plush Dot wearing a headset.

The look of a soft toy comes from drawing the fur strand by strand: about a hundred
thousand short curved strokes, each lit from the 3D normal of the body underneath
(key light, wrap-around diffuse, occlusion, rim light and a little sheen), over a
shaded undercoat. The headset gets a tube-shaded band, padded cups and a woven texture.

Deterministic (fixed seed), pure Python + pycairo. Usage:

    python3 tools/render_icon.py data/icons/hicolor

writes ``<size>x<size>/apps/io.github.euvinicios.Dotdeck.png`` for 64, 128, 256 and 512.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import cairo

APP_ID = "io.github.euvinicios.Dotdeck"
S = 1024                      # working canvas; the 512 px icon is downscaled from it
SIZES = (512, 256, 128, 64)
rnd = random.Random(20261009)

LIGHT = (-0.48, -0.62, 0.62)
_l = math.sqrt(sum(c * c for c in LIGHT))
LIGHT = tuple(c / _l for c in LIGHT)
RIM = (0.75, -0.25, 0.0)
FUR_BLUE = (0.2, 0.53, 1.0)


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def smoothstep(a, b, x):
    t = clamp((x - a) / (b - a))
    return t * t * (3 - 2 * t)


class Blob:
    """A soft superellipse volume: inside test, 3D normal and surface shading."""

    def __init__(self, cx, cy, a, b, p=2.6, flare=0.07, angle=0.0, colour=FUR_BLUE,
                 tone=1.0, flat=1.0):
        self.cx, self.cy, self.a, self.b, self.p = cx, cy, a, b, p
        self.flare, self.angle, self.colour, self.tone, self.flat = flare, angle, colour, tone, flat
        self.cos, self.sin = math.cos(angle), math.sin(angle)

    def local(self, x, y):
        dx, dy = x - self.cx, y - self.cy
        u = dx * self.cos + dy * self.sin
        v = -dx * self.sin + dy * self.cos
        a = self.a * (1 - self.flare + self.flare * clamp(v / self.b, -1, 1))
        return u / a, v / self.b, a

    def value(self, x, y):
        u, v, _ = self.local(x, y)
        return abs(u) ** self.p + abs(v) ** self.p

    def normal(self, x, y):
        u, v, a = self.local(x, y)
        f = abs(u) ** self.p + abs(v) ** self.p
        h = math.sqrt(max(1e-4, 1 - min(f, 1.0)))
        gu = self.p * abs(u) ** (self.p - 1) * math.copysign(1, u) / a
        gv = self.p * abs(v) ** (self.p - 1) * math.copysign(1, v) / self.b
        k = 70.0 * self.flat / (2 * h)
        nu, nv = gu * k, gv * k
        nx = nu * self.cos - nv * self.sin
        ny = nu * self.sin + nv * self.cos
        length = math.sqrt(nx * nx + ny * ny + 1)
        return nx / length, ny / length, 1 / length, h

    def shade(self, x, y, lift=0.0):
        nx, ny, nz, h = self.normal(x, y)
        ndl = nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]
        diffuse = clamp((ndl + 0.45) / 1.45)
        occlusion = (0.7 + 0.3 * h ** 0.5) * (1 - 0.2 * smoothstep(0.4, 1.0, (y - self.cy) / self.b))
        rim = (1 - nz) ** 2 * clamp(nx * RIM[0] + ny * RIM[1] + 0.35) * 0.8
        sheen = clamp(ndl) ** 8 * 0.16
        k = (0.4 + 0.82 * diffuse) * occlusion * self.tone * (1 + lift)
        r, g, b = self.colour
        return (clamp(r * k + rim * 0.35 + sheen), clamp(g * k + rim * 0.5 + sheen),
                clamp(b * k + rim * 0.6 + sheen * 0.8))

    def bounds(self):
        reach = max(self.a, self.b) * (1 + self.flare) + 40
        return self.cx - reach, self.cy - reach, self.cx + reach, self.cy + reach

    def path(self, ctx, grow=0.0, steps=240):
        for i in range(steps + 1):
            t = 2 * math.pi * i / steps
            ct, st = math.cos(t), math.sin(t)
            u = math.copysign(abs(ct) ** (2 / self.p), ct)
            v = math.copysign(abs(st) ** (2 / self.p), st)
            a = self.a * (1 - self.flare + self.flare * v) + grow
            lu, lv = u * a, v * (self.b + grow)
            x = self.cx + lu * self.cos - lv * self.sin
            y = self.cy + lu * self.sin + lv * self.cos
            (ctx.move_to if i == 0 else ctx.line_to)(x, y)
        ctx.close_path()


def undercoat(blob: Blob) -> cairo.ImageSurface:
    """Smoothly shaded, slightly darker body under the strands (computed at 1/4 size)."""
    q = 4
    n = S // q
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, n, n)
    stride = surface.get_stride()
    data = surface.get_data()
    x0, y0, x1, y1 = blob.bounds()
    for j in range(max(0, int(y0 / q)), min(n, int(y1 / q) + 1)):
        for i in range(max(0, int(x0 / q)), min(n, int(x1 / q) + 1)):
            x, y = (i + 0.5) * q, (j + 0.5) * q
            if blob.value(x, y) > 1.0:
                continue
            r, g, b = blob.shade(x, y, -0.12)
            o = j * stride + i * 4
            data[o:o + 4] = bytes((int(b * 255), int(g * 255), int(r * 255), 255))
    surface.mark_dirty()
    return surface


def draw_fur(ctx, blob: Blob, strands: int, length=(15, 27), width=(1.7, 2.7)):
    ctx.save()
    blob.path(ctx)
    ctx.clip()
    ctx.save()
    ctx.scale(4, 4)
    pattern = cairo.SurfacePattern(undercoat(blob))
    pattern.set_filter(cairo.FILTER_GOOD)
    ctx.set_source(pattern)
    ctx.paint()
    ctx.restore()
    ctx.restore()

    x0, y0, x1, y1 = blob.bounds()
    roots = []
    while len(roots) < strands:
        x, y = rnd.uniform(x0, x1), rnd.uniform(y0, y1)
        if blob.value(x, y) <= 1.0:
            roots.append((x, y))
    roots.sort(key=lambda p: p[1])

    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    for x, y in roots:
        nx, ny, nz, _h = blob.normal(x, y)
        spread = math.sqrt(nx * nx + ny * ny)
        dx, dy = nx, ny + 0.45                       # outward, with a little gravity
        angle = math.atan2(dy, dx) + rnd.gauss(0, 0.38)
        span = rnd.uniform(*length) * (0.35 + 0.85 * spread)
        ex, ey = x + math.cos(angle) * span, y + math.sin(angle) * span
        bend = rnd.uniform(-0.35, 0.35) * span
        mx, my = (x + ex) / 2 - math.sin(angle) * bend, (y + ey) / 2 + math.cos(angle) * bend
        r, g, b = blob.shade(ex, ey)
        jitter = rnd.uniform(0.94, 1.06)
        ctx.set_line_width(rnd.uniform(*width))
        ctx.set_source_rgba(r * jitter * 0.95, g * jitter * 0.95, b * jitter * 0.96, 0.88)
        ctx.move_to(x, y)
        ctx.curve_to(mx, my, mx, my, ex, ey)
        ctx.stroke()
        # brighter tip catches the light
        tx, ty = (mx + ex) / 2, (my + ey) / 2
        ctx.set_line_width(rnd.uniform(0.9, 1.5))
        ctx.set_source_rgba(clamp(r * jitter * 1.1), clamp(g * jitter * 1.08),
                            clamp(b * jitter * 1.03), 0.6)
        ctx.move_to(tx, ty)
        ctx.line_to(ex, ey)
        ctx.stroke()


def woven(alpha_surface: cairo.ImageSurface, dark=(0.42, 0.35, 0.24)) -> None:
    """Paint a linen-like weave inside the mask surface's opaque area."""
    ctx = cairo.Context(alpha_surface)
    ctx.set_operator(cairo.OPERATOR_ATOP)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    for _ in range(70000):
        x, y = rnd.uniform(0, S), rnd.uniform(0, S)
        horizontal = rnd.random() < 0.5
        length = rnd.uniform(2.5, 6)
        ctx.set_line_width(rnd.uniform(0.8, 1.4))
        if rnd.random() < 0.5:
            ctx.set_source_rgba(1, 1, 1, rnd.uniform(0.06, 0.16))
        else:
            ctx.set_source_rgba(*dark, rnd.uniform(0.05, 0.13))
        ctx.move_to(x, y)
        ctx.line_to(x + (length if horizontal else 0), y + (0 if horizontal else length))
        ctx.stroke()


def soft_ellipse(ctx, cx, cy, rx, ry, rgba, inner=0.0):
    ctx.save()
    ctx.translate(cx, cy)
    ctx.scale(rx, ry)
    gradient = cairo.RadialGradient(0, 0, 0, 0, 0, 1)
    r, g, b, a = rgba
    gradient.add_color_stop_rgba(inner, r, g, b, a)
    gradient.add_color_stop_rgba(1, r, g, b, 0)
    ctx.set_source(gradient)
    ctx.arc(0, 0, 1, 0, 2 * math.pi)
    ctx.fill()
    ctx.restore()


def tube(ctx, build_path, widths, colours, offset=(0, -1.6)):
    """A cylinder-like stroke: wide dark edge to narrow bright core, nudged towards the light."""
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    steps = len(widths)
    for i, (w, colour) in enumerate(zip(widths, colours)):
        ctx.save()
        ctx.translate(offset[0] * i, offset[1] * i)
        build_path(ctx)
        ctx.set_line_width(w)
        ctx.set_source_rgba(*colour)
        ctx.stroke()
        ctx.restore()
    return steps


def lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def band_path(ctx):
    ctx.move_to(168, 520)
    ctx.curve_to(168, 262, 322, 100, 512, 100)
    ctx.curve_to(702, 100, 856, 262, 856, 520)


def cup_path(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def boom_path(ctx):
    ctx.move_to(134, 640)
    ctx.curve_to(150, 728, 218, 774, 306, 774)


def render() -> cairo.ImageSurface:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
    ctx = cairo.Context(surface)

    # contact shadow
    soft_ellipse(ctx, 512, 952, 330, 44, (0, 0, 0, 0.32))
    soft_ellipse(ctx, 512, 948, 240, 26, (0, 0, 0, 0.25))

    body = Blob(512, 572, 316, 352, p=2.55, flare=0.08)
    feet = [Blob(380, 900, 104, 60, p=2.2, flare=0.0, tone=0.94),
            Blob(644, 900, 104, 60, p=2.2, flare=0.0, tone=0.94)]
    arms = [Blob(206, 712, 58, 98, p=2.2, flare=0.0, angle=math.radians(18), tone=0.92),
            Blob(818, 712, 58, 98, p=2.2, flare=0.0, angle=math.radians(-18), tone=0.92)]
    for blob in feet + arms:
        draw_fur(ctx, blob, 9000, length=(9, 15), width=(1.4, 2.2))
    # the body shades the limbs where they meet
    soft_ellipse(ctx, 512, 880, 330, 70, (0.02, 0.06, 0.2, 0.22))
    draw_fur(ctx, body, 130000, length=(13, 24), width=(1.5, 2.4))

    # shadow of the headset on the fur
    soft_ellipse(ctx, 250, 560, 110, 190, (0.02, 0.05, 0.18, 0.42))
    soft_ellipse(ctx, 790, 560, 110, 190, (0.02, 0.05, 0.18, 0.42))
    soft_ellipse(ctx, 512, 205, 300, 70, (0.02, 0.05, 0.18, 0.28))

    # face: eye sockets, eyes, cheeks, smile
    for ex in (402, 622):
        soft_ellipse(ctx, ex, 538, 78, 92, (0.01, 0.04, 0.16, 0.45))
    for ex in (402, 622):
        ctx.save()
        ctx.translate(ex, 528)
        ctx.scale(50, 62)
        gradient = cairo.RadialGradient(-0.25, -0.3, 0.05, 0, 0, 1)
        gradient.add_color_stop_rgb(0, 0.24, 0.26, 0.34)
        gradient.add_color_stop_rgb(0.55, 0.05, 0.06, 0.09)
        gradient.add_color_stop_rgb(1, 0.01, 0.01, 0.02)
        ctx.set_source(gradient)
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
        ctx.fill()
        ctx.restore()
        # reflection of the fur along the bottom of the eye
        ctx.save()
        ctx.translate(ex, 528)
        ctx.scale(50, 62)
        ctx.arc(0, 0, 0.86, math.radians(30), math.radians(150))
        ctx.restore()
        ctx.set_line_width(5)
        ctx.set_source_rgba(0.35, 0.6, 1.0, 0.35)
        ctx.stroke()
        soft_ellipse(ctx, ex + 16, 506, 19, 16, (1, 1, 1, 0.98), inner=0.55)
        soft_ellipse(ctx, ex - 16, 556, 8, 7, (1, 1, 1, 0.7), inner=0.4)
    for cx in (318, 706):
        soft_ellipse(ctx, cx, 618, 64, 38, (1.0, 0.5, 0.66, 0.5))
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.move_to(462, 622)
    ctx.curve_to(486, 660, 538, 660, 562, 622)
    ctx.set_line_width(19)
    ctx.set_source_rgb(0.05, 0.05, 0.08)
    ctx.stroke()

    # headset band: tube shading, then the weave
    edge, mid, top = (0.62, 0.53, 0.38, 1), (0.88, 0.81, 0.67, 1), (0.99, 0.97, 0.91, 1)
    widths = [96, 88, 78, 66, 54, 40, 26, 12]
    colours = [lerp(edge, mid, i / 4) if i < 4 else lerp(mid, top, (i - 4) / 3) for i in range(8)]
    band = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
    bctx = cairo.Context(band)
    tube(bctx, band_path, widths, colours)
    woven(band)
    ctx.set_source_surface(band)
    ctx.paint()

    # ear cups with padding, their weave and a bevel
    cups = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
    cctx = cairo.Context(cups)
    for x, inner_x in ((60, 192), (796, 784)):
        cup_path(cctx, x, 384, 168, 290, 84)
        gradient = cairo.LinearGradient(x, 384, x + 168, 674)
        gradient.add_color_stop_rgb(0, 0.99, 0.97, 0.91)
        gradient.add_color_stop_rgb(0.55, 0.9, 0.84, 0.71)
        gradient.add_color_stop_rgb(1, 0.66, 0.57, 0.42)
        cctx.set_source(gradient)
        cctx.fill()
        # cushion where the cup meets the head
        cup_path(cctx, inner_x, 420, 48, 218, 24)
        gradient = cairo.LinearGradient(inner_x, 420, inner_x + 48, 638)
        gradient.add_color_stop_rgb(0, 0.86, 0.79, 0.64)
        gradient.add_color_stop_rgb(1, 0.6, 0.51, 0.36)
        cctx.set_source(gradient)
        cctx.fill()
        # bevel highlight on the upper edge
        cctx.save()
        cup_path(cctx, x + 10, 394, 148, 270, 74)
        cctx.clip()
        cctx.set_line_width(14)
        cctx.set_source_rgba(1, 1, 1, 0.35)
        cctx.arc(x + 84, 478, 74, math.radians(200), math.radians(300))
        cctx.stroke()
        cctx.restore()
    woven(cups)
    ctx.set_source_surface(cups)
    ctx.paint()

    # microphone boom and its foam tip
    tube(ctx, boom_path, [34, 28, 20, 11], [lerp(edge, top, t) for t in (0, 0.4, 0.75, 1)],
         offset=(0.6, -1.4))
    ctx.save()
    ctx.translate(330, 774)
    gradient = cairo.RadialGradient(-12, -14, 4, 0, 0, 44)
    gradient.add_color_stop_rgb(0, 0.36, 0.37, 0.42)
    gradient.add_color_stop_rgb(1, 0.06, 0.06, 0.08)
    ctx.set_source(gradient)
    ctx.arc(0, 0, 42, 0, 2 * math.pi)
    ctx.fill_preserve()
    ctx.clip()
    for _ in range(1600):
        x, y = rnd.uniform(-42, 42), rnd.uniform(-42, 42)
        ctx.set_source_rgba(1, 1, 1, rnd.uniform(0.02, 0.08)) if rnd.random() < 0.5 else \
            ctx.set_source_rgba(0, 0, 0, rnd.uniform(0.05, 0.15))
        ctx.arc(x, y, rnd.uniform(0.8, 2.2), 0, 2 * math.pi)
        ctx.fill()
    ctx.restore()
    return surface


def halve(surface: cairo.ImageSurface) -> cairo.ImageSurface:
    """Downscale by two: bilinear sampling at half scale averages each 2x2 block."""
    size = surface.get_width() // 2
    smaller = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(smaller)
    ctx.scale(0.5, 0.5)
    pattern = cairo.SurfacePattern(surface)
    pattern.set_filter(cairo.FILTER_BILINEAR)
    ctx.set_source(pattern)
    ctx.paint()
    return smaller


def save_sizes(surface: cairo.ImageSurface, root: Path) -> None:
    while surface.get_width() > max(SIZES):
        surface = halve(surface)
    while True:
        size = surface.get_width()
        if size in SIZES:
            target = root / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            surface.write_to_png(str(target))
            print(target)
        if size <= min(SIZES):
            break
        surface = halve(surface)


if __name__ == "__main__":
    save_sizes(render(), Path(sys.argv[1] if len(sys.argv) > 1 else "data/icons/hicolor"))
