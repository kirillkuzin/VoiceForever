"""Icons for the app, the addon and the README, made from logo.jpg.

    uv run --no-project --with pillow python docs/brand/make_icons.py
    uv run --no-project --with pillow python docs/brand/make_icons.py --web ../VoiceForeverCloud/landing

logo.jpg is the original emblem: a gold shield with a V on a dark square. The script cuts the
shield out (transparent around it) and writes:

- agent/vf_agent/gui/VoiceForever.ico: the exe, its window and the tray. Classic (BMP) images
  up to 128 px, so the tray can draw its status dot without an image library; 256 px as PNG.
- addon/VoiceForever/Icon.tga: the addon's icon in the game's addon list (## IconTexture).
- docs/images/logo.png: the README.
- with --web DIR: the website's favicon.ico, apple-touch-icon.png and img/emblem.png.

emblem.svg is the same emblem as a vector, for favicon.svg and the small inline marks (the
window title bar, the Telegram card, the site header); it is not generated here.
"""

import argparse
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOGO = HERE / "logo.jpg"

BACKGROUND_RED = 18  # red of the dark square around the shield
GOLD_RED = 230  # red of the gold
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
BOLDER = ((20, 31), (24, 21))  # up to this size, spread the gold by this filter size  # every tray size from 100% to 300% scaling
BMP_UP_TO = 128


def cut_out(logo: Image.Image) -> Image.Image:
    """The shield with a transparent background, cropped to its box."""
    rgb = logo.convert("RGB")
    span = GOLD_RED - BACKGROUND_RED
    gold = rgb.getchannel("R").point(lambda v: max(0, min(255, (v - BACKGROUND_RED) * 255 // span)))
    outside = gold.point(lambda v: 255 if v >= 115 else 0)
    ImageDraw.floodfill(outside, (0, 0), 128)  # the background around the shield, up to its gold rim
    outside = outside.point(lambda v: 255 if v == 128 else 0)
    alpha = Image.composite(gold, Image.new("L", rgb.size, 255), outside).point(lambda v: 0 if v < 8 else v)
    # Edge pixels mix the gold with the dark background: give them the gold next to them.
    rgb = Image.composite(rgb.filter(ImageFilter.MaxFilter(5)), rgb, outside)
    shield = rgb.convert("RGBA")
    shield.putalpha(alpha)
    return shield.crop(alpha.getbbox())


def bolder(shield: Image.Image, grow: int) -> Image.Image:
    """Thicker gold lines for the smallest sizes, where the rim and the V get thinner than a pixel."""
    inside = shield.getchannel("A").point(lambda v: 255 if v == 255 else 0)
    return Image.composite(shield.filter(ImageFilter.MaxFilter(grow)), shield, inside)


def resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")


type Shields = list[tuple[int, Image.Image]]


def shields(shield: Image.Image) -> Shields:
    """The shield for each size: up to 24 px with thicker lines."""
    return [(up_to, bolder(shield, grow)) for up_to, grow in BOLDER] + [(1 << 16, shield)]


def square(versions: Shields, size: int, margin: int = 0) -> Image.Image:
    """The shield centred in a transparent square, as tall as the square minus the margin."""
    shield = next(image for up_to, image in versions if size <= up_to)
    height = size - 2 * margin
    width = round(shield.width * height / shield.height)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(resize(shield, (width, height)), ((size - width) // 2, margin))
    return out


def on_background(logo: Image.Image, size: int) -> Image.Image:
    """The original square, a little tighter around the shield (app icons, avatars)."""
    margin = logo.width * 6 // 100
    return logo.convert("RGB").crop((margin, margin, logo.width - margin, logo.height - margin)).resize(
        (size, size), Image.Resampling.LANCZOS
    )


def bmp_entry(image: Image.Image) -> bytes:
    """An .ico image in the classic format: BITMAPINFOHEADER, BGRA rows bottom-up, AND mask."""
    size = image.width
    xor = b"".join(image.crop((0, y, size, y + 1)).tobytes("raw", "BGRA") for y in reversed(range(size)))
    mask = bytes(((size + 31) // 32) * 4 * size)  # all zero: the alpha channel decides
    return struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor), 0, 0, 0, 0) + xor + mask


def png_entry(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def ico(images: list[Image.Image], bmp_up_to: int) -> bytes:
    entries = [bmp_entry(im) if im.width <= bmp_up_to else png_entry(im) for im in images]
    offset = 6 + 16 * len(entries)
    directory = b""
    for im, data in zip(images, entries, strict=True):
        side = 0 if im.width >= 256 else im.width  # 0 means 256
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(entries)) + directory + b"".join(entries)


def tga(image: Image.Image) -> bytes:
    """Uncompressed 32-bit TGA, top row first: what the game loads for addon textures."""
    header = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, image.width, image.height, 32, 0x28)
    return header + image.tobytes("raw", "BGRA")


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: {len(data)} bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--web", type=Path, help="the website folder (landing) to write favicons into")
    args = parser.parse_args()

    logo = Image.open(LOGO)
    shield = shields(cut_out(logo))
    write(ROOT / "agent/vf_agent/gui/VoiceForever.ico", ico([square(shield, s) for s in ICO_SIZES], BMP_UP_TO))
    write(ROOT / "addon/VoiceForever/Icon.tga", tga(square(shield, 64, 1)))
    write(ROOT / "docs/images/logo.png", png_entry(square(shield, 256, 4)))
    if args.web:
        write(args.web / "favicon.ico", ico([square(shield, s) for s in (16, 32, 48)], 0))
        write(args.web / "apple-touch-icon.png", png_entry(on_background(logo, 180)))
        write(args.web / "img/emblem.png", png_entry(square(shield, 256)))


if __name__ == "__main__":
    main()
