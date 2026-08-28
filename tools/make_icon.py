"""Draws the add-on store icon.

    python3 tools/make_icon.py [blue|slate] monitorha/icon.png

The icon is drawn rather than hand-pixelled so it can be regenerated at any
size: the store shows it at 128px, but Home Assistant's own list renders it far
smaller, so every element is checked at 32px before it earns its place.

Needs Pillow, which is a development-time dependency only — the add-on image
still ships nothing but aiohttp.
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw, ImageFilter

# The design is laid out in a 1000x1000 space and scaled to whatever size is
# asked for, so the proportions hold at every resolution.
GRID = 1000
SS = 2  # supersampling factor on top of the requested size

HA_BLUE = (3, 169, 244)
HA_BLUE_DEEP = (2, 119, 189)
GREEN = (88, 200, 119)
SLATE_TOP = (35, 40, 48)
SLATE_BOTTOM = (18, 20, 24)
WHITE = (255, 255, 255)


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """A vertical gradient, drawn as a 1px column and stretched."""
    column = Image.new("RGB", (1, size))
    px = column.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return column.resize((size, size), Image.BILINEAR)


def _rounded_mask(size: int, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    return mask


def _glow(size: int, draw_fn, blur: float) -> Image.Image:
    """Renders shapes onto a transparent layer and blurs them into a glow."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def render(variant: str, size: int) -> Image.Image:
    """Draws one icon variant at `size` pixels square."""
    s = size * SS
    k = s / GRID  # grid units -> pixels

    def u(v: float) -> float:
        return v * k

    # --- background tile -------------------------------------------------
    if variant == "blue":
        bg = _vertical_gradient(s, HA_BLUE, HA_BLUE_DEEP)
        slab_fill = WHITE
        slab_edge = (222, 240, 250)
        vent = (150, 196, 226)
        leds = [GREEN, GREEN, GREEN]
    elif variant == "slate":
        bg = _vertical_gradient(s, SLATE_TOP, SLATE_BOTTOM)
        slab_fill = (47, 53, 64)
        slab_edge = (66, 74, 88)
        vent = (84, 93, 110)
        leds = [HA_BLUE, GREEN, GREEN]
    else:
        raise SystemExit(f"unknown variant: {variant}")

    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    icon.paste(bg, (0, 0), _rounded_mask(s, u(220)))

    # --- three stacked rack units ---------------------------------------
    # Three, because the add-on speaks to three kinds of box; stacked, because
    # that is the shape of the rack they live in.
    slab_x0, slab_x1 = u(190), u(810)
    slab_h, gap = u(150), u(55)
    tops = [u(220) + i * (slab_h + gap) for i in range(3)]
    led_x, led_r = u(272), u(33)

    # The lit LEDs glow through the tile, which is what a rack actually looks
    # like in a dark room and keeps the dots from reading as flat stickers.
    def draw_glows(d: ImageDraw.ImageDraw) -> None:
        for top, colour in zip(tops, leds):
            cy = top + slab_h / 2
            d.ellipse(
                (led_x - led_r * 2, cy - led_r * 2, led_x + led_r * 2, cy + led_r * 2),
                fill=(*colour, 150),
            )

    glow = _glow(s, draw_glows, blur=u(26))

    draw = ImageDraw.Draw(icon)
    for top, colour in zip(tops, leds):
        draw.rounded_rectangle(
            (slab_x0, top, slab_x1, top + slab_h), radius=u(32), fill=slab_fill
        )
        # A lighter cap along the top edge gives the slab a front face.
        draw.rounded_rectangle(
            (slab_x0, top, slab_x1, top + u(18)), radius=u(9), fill=slab_edge
        )
        cy = top + slab_h / 2
        # One vent bar per unit: at 32px anything finer collapses into mud.
        draw.rounded_rectangle(
            (u(400), cy - u(10), u(740), cy + u(10)), radius=u(10), fill=vent
        )

    icon.alpha_composite(glow.crop((0, 0, s, s)))
    for top, colour in zip(tops, leds):
        cy = top + slab_h / 2
        draw = ImageDraw.Draw(icon)
        draw.ellipse((led_x - led_r, cy - led_r, led_x + led_r, cy + led_r), fill=colour)

    return icon.resize((size, size), Image.LANCZOS)


def main() -> None:
    variant = sys.argv[1] if len(sys.argv) > 1 else "blue"
    out = sys.argv[2] if len(sys.argv) > 2 else "icon.png"
    render(variant, 256).save(out)


if __name__ == "__main__":
    main()
