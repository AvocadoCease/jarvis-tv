"""Generate the Jarvis theme's assets.

  python3 tools/make_jarvis_assets.py                      a plain dark-cyan backdrop (what ships)
  python3 tools/make_jarvis_assets.py /path/to/artwork.jpg  your own 16:9 picture as the backdrop

No artwork ships with this project: use a picture you have the rights to. If it has glowing
cyan eyes (an Iron Man helmet, say), set EYES_BOX to where they are and they'll pulse while
JARVIS talks; otherwise the eyes layer stays empty.
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.join(os.path.dirname(__file__), "..", "widget", "img")
CYAN = (24, 224, 255)
S = 4


def background(src=None):
    if src:
        im = Image.open(src).convert("RGB").resize((1280, 720), Image.LANCZOS)
    else:  # a dark radial wash with a faint grid
        im = Image.new("RGB", (1280, 720), (2, 10, 14))
        d = ImageDraw.Draw(im)
        for r in range(520, 0, -8):
            k = 1 - r / 520
            d.ellipse((640 - r * 1.4, 330 - r, 640 + r * 1.4, 330 + r), fill=(2, int(10 + 30 * k), int(14 + 38 * k)))
        for x in range(0, 1280, 40):
            d.line((x, 0, x, 720), fill=(6, 30, 38))
        for y in range(0, 720, 40):
            d.line((0, y, 1280, y), fill=(6, 30, 38))
        im = im.filter(ImageFilter.GaussianBlur(0.6))
    im.save(os.path.join(ROOT, "jarvis-bg.jpg"), quality=88, optimize=True, progressive=False)


def glow():
    size = 220
    img = Image.new("RGBA", (size, size), CYAN + (0,))
    px = img.load()
    c = size / 2
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c) / c
            a = max(0.0, 1 - d) ** 2.2
            px[x, y] = CYAN + (int(a * 200),)
    img.save(os.path.join(ROOT, "jarvis-glow.png"), optimize=True)


def ring():
    size = 170
    big = Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    c = size * S / 2
    # outer dashed arc segments
    r = 80 * S
    for i in range(24):
        if i % 6 == 5:
            continue
        a0 = i * 15 + 2
        d.arc((c - r, c - r, c + r, c + r), a0, a0 + 10, fill=CYAN + (210,), width=2 * S)
    # inner thin ring with ticks
    r2 = 66 * S
    d.ellipse((c - r2, c - r2, c + r2, c + r2), outline=CYAN + (120,), width=1 * S)
    for i in range(60):
        ang = math.radians(i * 6)
        inner = r2 + (3 if i % 5 else 0) * S
        outer = r2 + 7 * S
        d.line((c + inner * math.cos(ang), c + inner * math.sin(ang),
                c + outer * math.cos(ang), c + outer * math.sin(ang)),
               fill=CYAN + (170 if i % 5 == 0 else 90,), width=S)
    # two bright markers
    for a0 in (40, 220):
        d.arc((c - r, c - r, c + r, c + r), a0, a0 + 22, fill=(255, 255, 255, 230), width=3 * S)
    big.resize((size, size), Image.LANCZOS).save(os.path.join(ROOT, "jarvis-ring.png"), optimize=True)


def speaking_frame():
    """Glow around the screen edge plus HUD corner brackets, shown while JARVIS talks."""
    w, h = 1280, 720
    alpha = Image.new("L", (w, h), 0)
    px = alpha.load()
    for y in range(h):
        for x in range(w):
            d = min(x, y, w - 1 - x, h - 1 - y)
            px[x, y] = int(max(0.0, 1 - d / 60.0) ** 2 * 200)
    img = Image.new("RGBA", (w, h), CYAN + (0,))
    img.putalpha(alpha)
    d = ImageDraw.Draw(img)
    L, m, t = 70, 16, 3
    for (x, y, dx, dy) in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
        d.line((x, y, x + dx * L, y), fill=(232, 253, 255, 255), width=t)
        d.line((x, y, x, y + dy * L), fill=(232, 253, 255, 255), width=t)
    img.save(os.path.join(ROOT, "jarvis-frame.png"), optimize=True)


EYES_BOX = (560, 125, 700, 180)  # x0, y0, x1, y1 in the 1280x720 artwork


def eyes():
    """The suit's own eye shapes, brightened and haloed; laid over the artwork while JARVIS talks."""
    bg = Image.open(os.path.join(ROOT, "jarvis-bg.jpg")).convert("RGB").crop(EYES_BOX)
    w, h = bg.size
    mask = Image.new("L", (w, h), 0)
    src, m = bg.load(), mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b = src[x, y]
            if g > 120 and b > 140:  # the cyan eye slits
                m[x, y] = min(255, int((g + b) / 2))
    core = Image.new("RGBA", (w, h), (235, 255, 255, 0))
    core.putalpha(mask)
    halo = Image.new("RGBA", (w, h), CYAN + (0,))
    halo.putalpha(mask.filter(ImageFilter.GaussianBlur(6)).point(lambda v: min(255, v * 2)))
    out = Image.alpha_composite(halo, core)
    out.save(os.path.join(ROOT, "jarvis-eyes.png"), optimize=True)


def scanlines():
    img = Image.new("RGBA", (4, 3), (0, 0, 0, 0))
    for x in range(4):
        img.putpixel((x, 2), (0, 0, 0, 70))
    img.save(os.path.join(ROOT, "jarvis-scan.png"))


def cyan_icons():
    for name in ("tv", "hub", "frame", "settings"):
        src = Image.open(os.path.join(ROOT, f"icon-{name}.png")).convert("RGBA")
        alpha = src.getchannel("A")
        out = Image.new("RGBA", src.size, CYAN + (0,))
        out.putalpha(alpha)
        out = out.resize((24, 24), Image.LANCZOS)
        out.save(os.path.join(ROOT, f"icon-{name}-cyan.png"))
        dark = Image.new("RGBA", src.size, (0, 16, 20, 0))
        dark.putalpha(alpha)
        dark.resize((24, 24), Image.LANCZOS).save(os.path.join(ROOT, f"icon-{name}-ink.png"))


if __name__ == "__main__":
    background(sys.argv[1] if len(sys.argv) > 1 else None)
    eyes()
    glow()
    ring()
    scanlines()
    cyan_icons()
    print("jarvis assets written")
