"""Generate Lounge's PNG assets: tile icons, Smart Hub icons and the shade overlay.

The 2012 TV browser is slow at CSS gradients and has no SVG guarantees, so
everything visual that isn't a photo is rendered here ahead of time.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.join(os.path.dirname(__file__), "..", "widget", "img")
S = 4  # supersample factor
ICON = 48


def canvas(size=ICON):
    return Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))


def finish(img, size=ICON):
    return img.resize((size, size), Image.LANCZOS)


def icon_tv(d, c):
    w = 3 * S
    d.rounded_rectangle((5 * S, 9 * S, 43 * S, 35 * S), radius=4 * S, outline=c, width=w)
    d.line((17 * S, 41 * S, 31 * S, 41 * S), fill=c, width=w)
    d.line((24 * S, 35 * S, 24 * S, 41 * S), fill=c, width=w)


def icon_hub(d, c):
    for x, y in ((7, 7), (26, 7), (7, 26), (26, 26)):
        d.rounded_rectangle((x * S, y * S, (x + 15) * S, (y + 15) * S), radius=4 * S, fill=c)


def icon_frame(d, c):
    w = 3 * S
    d.rounded_rectangle((5 * S, 8 * S, 43 * S, 40 * S), radius=3 * S, outline=c, width=w)
    d.polygon([(10 * S, 35 * S), (20 * S, 21 * S), (27 * S, 30 * S), (31 * S, 25 * S), (38 * S, 35 * S)], fill=c)
    d.ellipse((30 * S, 13 * S, 36 * S, 19 * S), fill=c)


def icon_settings(d, c):
    w = 3 * S
    for y, knob in ((13, 30), (24, 16), (35, 26)):
        d.line((7 * S, y * S, 41 * S, y * S), fill=c, width=w)
        r = 5 * S
        d.ellipse((knob * S - r, y * S - r, knob * S + r, y * S + r), fill=c)


ICONS = {"tv": icon_tv, "hub": icon_hub, "frame": icon_frame, "settings": icon_settings}


def make_tile_icons():
    for name, draw in ICONS.items():
        for suffix, colour in (("", (255, 255, 255, 255)), ("-dark", (20, 22, 26, 255))):
            img = canvas()
            draw(ImageDraw.Draw(img), colour)
            finish(img).save(os.path.join(ROOT, f"icon-{name}{suffix}.png"))


def make_app_icons():
    """Smart Hub needs four icon sizes for a 2012 widget."""
    for w, h in ((106, 87), (115, 95), (85, 70), (95, 78)):
        wall = Image.open(os.path.join(ROOT, "wall1.jpg")).convert("RGBA")
        scale = max(w * S / wall.width, h * S / wall.height)
        wall = wall.resize((math.ceil(wall.width * scale), math.ceil(wall.height * scale)), Image.LANCZOS)
        left, top = (wall.width - w * S) // 2, (wall.height - h * S) // 2
        bg = wall.crop((left, top, left + w * S, top + h * S))
        bg = Image.alpha_composite(bg, Image.new("RGBA", bg.size, (0, 0, 0, 90)))
        d = ImageDraw.Draw(bg)
        # a small sofa: the "lounge" mark
        cx, cy, u = w * S / 2, h * S / 2, min(w, h) * S / 60
        white = (255, 255, 255, 255)
        d.rounded_rectangle((cx - 16 * u, cy - 10 * u, cx + 16 * u, cy + 4 * u), radius=4 * u, fill=white)
        d.rounded_rectangle((cx - 22 * u, cy - 4 * u, cx - 12 * u, cy + 10 * u), radius=3 * u, fill=white)
        d.rounded_rectangle((cx + 12 * u, cy - 4 * u, cx + 22 * u, cy + 10 * u), radius=3 * u, fill=white)
        d.rounded_rectangle((cx - 16 * u, cy + 2 * u, cx + 16 * u, cy + 10 * u), radius=2 * u, fill=white)
        d.line((cx - 18 * u, cy + 10 * u, cx - 18 * u, cy + 14 * u), fill=white, width=int(2 * u))
        d.line((cx + 18 * u, cy + 10 * u, cx + 18 * u, cy + 14 * u), fill=white, width=int(2 * u))
        bg.resize((w, h), Image.LANCZOS).convert("RGB").save(os.path.join(ROOT, f"app-{w}x{h}.png"))


def make_shade():
    """Darken the top-left (clock) and bottom (dock) so text reads on any photo."""
    w, h = 1280, 720
    shade = Image.new("L", (w, h), 0)
    px = shade.load()
    for y in range(h):
        for x in range(w):
            corner = max(0.0, 1 - math.hypot(x / 900, y / 520)) * 170
            bottom = max(0.0, (y - 400) / 320) ** 1.6 * 200
            px[x, y] = int(min(215, corner + bottom))
    shade = shade.filter(ImageFilter.GaussianBlur(8))
    out = Image.new("RGBA", (w, h), (8, 10, 14, 0))
    out.putalpha(shade)
    out.save(os.path.join(ROOT, "shade.png"), optimize=True)


if __name__ == "__main__":
    make_tile_icons()
    make_app_icons()
    make_shade()
    print("assets written to", os.path.abspath(ROOT))
