"""PetAvatar: a Dot's pet in a soft rounded tile, with a status ring and a little life.

States: ``idle``, ``working`` (bobs, accent ring), ``listening``/``speaking``/``thinking``
(in a call: green ring; speaking pulses), ``paused`` and ``offline`` (desaturated),
``error`` (red ring). Animations follow the "pet-animations" setting and are skipped
entirely when off, so idle rows cost nothing.
"""

from __future__ import annotations

import math
import weakref

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

from ..core import pets  # noqa: E402

ANIMATED = {"working", "speaking", "thinking"}
CALL_STATES = {"listening", "speaking", "thinking", "call"}
GREYED = {"paused", "offline"}

_textures: dict[tuple[str, int], Gdk.Texture] = {}
_animations = {"value": "full"}
_avatars: weakref.WeakSet = weakref.WeakSet()


def set_animations(value: str) -> None:
    """"full", "subtle" or "off" (the Appearance setting), for every avatar, at once."""
    _animations["value"] = value
    for avatar in list(_avatars):
        avatar._sync_animation()
        avatar.queue_draw()


def texture(pet_id: str, pixels: int) -> Gdk.Texture | None:
    """The pet picture at ``pixels`` px, downscaled once with a good filter and cached."""
    pixels = max(8, min(256, int(pixels)))
    key = (pet_id, pixels)
    cached = _textures.get(key)
    if cached is not None:
        return cached
    path = pets.image_path(pet_id)
    try:
        source = cairo.ImageSurface.create_from_png(str(path))
    except (cairo.Error, OSError, MemoryError):
        return None
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixels, pixels)
    ctx = cairo.Context(surface)
    scale = pixels / source.get_width()
    # Halve with box filtering first, then one last good scale: sharp and not jaggy.
    while scale < 0.5:
        half = cairo.ImageSurface(cairo.FORMAT_ARGB32, source.get_width() // 2,
                                  source.get_height() // 2)
        hctx = cairo.Context(half)
        hctx.scale(0.5, 0.5)
        pattern = cairo.SurfacePattern(source)
        pattern.set_filter(cairo.FILTER_BILINEAR)
        hctx.set_source(pattern)
        hctx.paint()
        source = half
        scale = pixels / source.get_width()
    ctx.scale(scale, scale)
    pattern = cairo.SurfacePattern(source)
    pattern.set_filter(cairo.FILTER_BEST)
    ctx.set_source(pattern)
    ctx.paint()
    surface.flush()
    data = GLib.Bytes.new(bytes(surface.get_data()))
    tex = Gdk.MemoryTexture.new(pixels, pixels, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data,
                                surface.get_stride())
    _textures[key] = tex
    return tex


def tile_colour(pet: pets.Pet) -> tuple[float, float, float]:
    """The tint behind a pet: its fur colour, or a cool blue for near-white fur."""
    r, g, b = pet.fur
    if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.85:
        return (0.42, 0.56, 0.78)
    return pet.fur


def _rgba(r, g, b, a=1.0) -> Gdk.RGBA:
    colour = Gdk.RGBA()
    colour.red, colour.green, colour.blue, colour.alpha = r, g, b, a
    return colour


def _stop(offset: float, colour: Gdk.RGBA) -> Gsk.ColorStop:
    stop = Gsk.ColorStop()
    stop.offset = offset
    stop.color = colour
    return stop


class PetAvatar(Gtk.Widget):
    __gtype_name__ = "DotdeckPetAvatar"

    def __init__(self, pet_id: str | None = None, size: int = 36, state: str = "idle",
                 tile: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._pet = pet_id or pets.DEFAULT.id
        self._size = size
        self._state = state
        self._tile = tile
        self._tick = 0
        self._start = 0
        self.set_overflow(Gtk.Overflow.VISIBLE)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.update_property([Gtk.AccessibleProperty.LABEL], [self._label()])
        self.set_accessible_role(Gtk.AccessibleRole.IMG) if hasattr(
            self, "set_accessible_role") else None
        self._style_handler = 0
        _avatars.add(self)
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    # -- properties ------------------------------------------------------------------------

    @GObject.Property(type=str)
    def pet(self) -> str:
        return self._pet

    @pet.setter
    def pet(self, value: str) -> None:
        value = value if pets.get(value) else pets.DEFAULT.id
        if value != self._pet:
            self._pet = value
            self.update_property([Gtk.AccessibleProperty.LABEL], [self._label()])
            self.queue_draw()

    @GObject.Property(type=str, default="idle")
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        if value != self._state:
            self._state = value or "idle"
            self._sync_animation()
            self.queue_draw()

    @GObject.Property(type=int, default=36)
    def size(self) -> int:
        return self._size

    @size.setter
    def size(self, value: int) -> None:
        if value != self._size:
            self._size = value
            self.queue_resize()

    def _label(self) -> str:
        from ..i18n import _
        pet = pets.get(self._pet) or pets.DEFAULT
        return _(pet.name)

    def _on_map(self, *_args) -> None:
        # Only mapped avatars follow the dark style (a connection would keep them alive).
        if not self._style_handler:
            self._style_handler = Adw.StyleManager.get_default().connect(
                "notify::dark", lambda *_: self.queue_draw())
        self._sync_animation()

    def _on_unmap(self, *_args) -> None:
        if self._style_handler:
            Adw.StyleManager.get_default().disconnect(self._style_handler)
            self._style_handler = 0
        self._stop_animation()

    # -- animation -------------------------------------------------------------------------

    def _wants_animation(self) -> bool:
        if _animations["value"] == "off" or self._state not in ANIMATED:
            return False
        settings = Gtk.Settings.get_default()
        if settings is not None and settings.find_property("gtk-interface-reduced-motion") \
                and settings.get_property("gtk-interface-reduced-motion") == 1:
            return False
        return self.get_mapped()

    def _sync_animation(self) -> None:
        if self._wants_animation():
            if not self._tick:
                self._start = 0
                self._tick = self.add_tick_callback(self._on_tick)
        else:
            self._stop_animation()

    def _stop_animation(self) -> None:
        if self._tick:
            self.remove_tick_callback(self._tick)
            self._tick = 0
            self.queue_draw()

    def _on_tick(self, _widget, clock: Gdk.FrameClock) -> bool:
        if not self._wants_animation():
            self._tick = 0
            self.queue_draw()
            return GLib.SOURCE_REMOVE
        now = clock.get_frame_time()
        if not self._start:
            self._start = now
        last = getattr(self, "_last_frame", 0)
        if now - last >= 33_000:  # ~30 fps is plenty for a breathing pet
            self._last_frame = now
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _phase(self) -> float:
        if not self._tick or not self._start:
            return 0.0
        clock = self.get_frame_clock()
        if clock is None:
            return 0.0
        return (clock.get_frame_time() - self._start) / 1_000_000

    # -- drawing ---------------------------------------------------------------------------

    def do_measure(self, orientation, for_size):
        return self._size, self._size, -1, -1

    def _ring_colour(self) -> Gdk.RGBA | None:
        dark = Adw.StyleManager.get_default().get_dark()
        if self._state in CALL_STATES:
            return _rgba(*((0.47, 0.91, 0.67) if dark else (0.11, 0.52, 0.33)))
        if self._state == "error":
            return _rgba(*((1.0, 0.58, 0.55) if dark else (0.75, 0.11, 0.16)))
        if self._state in ("working", "needs-input"):
            accent = Adw.StyleManager.get_default().get_accent_color_rgba()
            return accent
        return None

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        size = min(width, height, self._size)
        x, y = (width - size) / 2, (height - size) / 2
        pet = pets.get(self._pet) or pets.DEFAULT
        dark = Adw.StyleManager.get_default().get_dark()
        t = self._phase()
        subtle = _animations["value"] == "subtle"
        bob = scale = 0.0
        if self._tick:
            if self._state == "working":
                bob = math.sin(t * 2 * math.pi / 1.2) * size * (0.02 if subtle else 0.045)
            elif self._state == "thinking":
                bob = math.sin(t * 2 * math.pi / 1.8) * size * 0.03
            elif self._state == "speaking":
                scale = (math.sin(t * 2 * math.pi / 0.7) * 0.5 + 0.5) * (0.03 if subtle else 0.06)

        ring = self._ring_colour()
        inset = max(2.0, size * 0.07) if ring else 0.0
        tile = Graphene.Rect().init(x + inset, y + inset, size - 2 * inset, size - 2 * inset)
        radius = (size - 2 * inset) * 0.32
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(tile, radius)

        if ring is not None:
            outer = Gsk.RoundedRect()
            outer.init_from_rect(Graphene.Rect().init(x, y, size, size), radius + inset)
            width_px = max(1.5, size * 0.045)
            pulse = 1.0
            if self._tick and self._state == "working":
                pulse = 0.55 + 0.45 * (math.sin(t * 2 * math.pi / 1.2) * 0.5 + 0.5)
            colour = _rgba(ring.red, ring.green, ring.blue, ring.alpha * pulse)
            snapshot.append_border(outer, [width_px] * 4, [colour] * 4)

        snapshot.push_rounded_clip(rounded)
        if self._tile:
            r, g, b = tile_colour(pet)
            alpha = 0.3 if dark else 0.2
            snapshot.append_color(_rgba(r, g, b, alpha), tile)
            # a soft light from the top so the tile does not look flat
            snapshot.append_linear_gradient(
                tile, Graphene.Point().init(x, y + inset),
                Graphene.Point().init(x, y + size * 0.75),
                [_stop(0.0, _rgba(1, 1, 1, 0.08 if dark else 0.35)),
                 _stop(1.0, _rgba(1, 1, 1, 0))])

        device_scale = self.get_scale_factor() or 1
        zoom = 1.16 + scale
        drawn = (size - 2 * inset) * zoom
        tex = texture(pet.id, int(drawn * device_scale))
        if tex is not None:
            greyed = self._state in GREYED
            if greyed:
                matrix = Graphene.Matrix().init_from_float([
                    0.33, 0.33, 0.33, 0, 0.59, 0.59, 0.59, 0, 0.11, 0.11, 0.11, 0, 0, 0, 0, 0.75])
                snapshot.push_color_matrix(matrix, Graphene.Vec4().init(0, 0, 0, 0))
            dx = x + (size - drawn) / 2
            dy = y + (size - drawn) / 2 + (size - 2 * inset) * 0.07 + bob
            snapshot.append_texture(tex, Graphene.Rect().init(dx, dy, drawn, drawn))
            if greyed:
                snapshot.pop()
        snapshot.pop()
