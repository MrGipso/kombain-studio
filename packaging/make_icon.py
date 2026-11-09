"""Render packaging/kombain-studio.ico from the app logo (frontend/public/favicon.svg).

The logo is simple (a rounded square with a red gradient and a white "K"
polygon), so it is redrawn with Pillow instead of needing an SVG renderer.
Run after changing the logo:  uv run python packaging/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZE = 1024  # drawn large, downscaled for every icon size
SCALE = SIZE / 48
TOP, BOTTOM = (0xEF, 0x44, 0x44), (0xB9, 0x1C, 0x1C)
K = [(13, 11), (19.3, 11), (19.3, 20.2), (27.4, 11), (35.2, 11), (25, 22.1),
     (35.8, 37), (27.9, 37), (19.3, 26.3), (19.3, 37), (13, 37)]


def render() -> Image.Image:
    gradient = Image.new("RGBA", (SIZE, SIZE))
    pixels = gradient.load()
    for y in range(SIZE):
        for x in range(SIZE):
            # The SVG gradient runs from (8,6) to (40,42): roughly the diagonal.
            t = min(1.0, max(0.0, ((x + y) / SCALE - 14) / 68))
            pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=round(14 * SCALE), fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(gradient, mask=mask)
    ImageDraw.Draw(icon).polygon([(x * SCALE, y * SCALE) for x, y in K], fill=(255, 255, 255, 255))
    return icon


if __name__ == "__main__":
    image = render().resize((256, 256), Image.LANCZOS)
    image.save(HERE / "kombain-studio.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("written", HERE / "kombain-studio.ico")
