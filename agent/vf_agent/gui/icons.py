"""The VoiceForever emblem (gold shield with a V) for the tray, with a status dot.

VoiceForever.ico is made from the logo by docs/brand/make_icons.py. The exe, its window and
the tray use it; the images the tray takes are in the classic format (BGRA rows), so the dot
is drawn on them here without an image library.
"""

import functools
import struct
from pathlib import Path

type RGB = tuple[int, int, int]

ICO = Path(__file__).with_name("VoiceForever.ico")

EDGE: RGB = (0x0B, 0x09, 0x07)

# Status dot colours, the same as the window uses.
DOTS: frozendict[str, RGB] = frozendict({
    "connected": (0x5E, 0xD4, 0x74),
    "connecting": (0xF2, 0xAD, 0x42),
    "disconnected": (0x8A, 0x81, 0x72),
    "error": (0xFF, 0x75, 0x63),
    "recording": (0xE0, 0x33, 0x1B),
})

HEADER = struct.Struct("<IiiHHIIiiII")  # BITMAPINFOHEADER
SAMPLES = 4  # supersampling per axis for the dot's edge


def ico_images(data: bytes) -> dict[int, bytes]:
    """The classic-format images of an .ico file by size; PNG-compressed ones are left out."""
    _, kind, count = struct.unpack_from("<HHH", data)
    if kind != 1:
        raise ValueError("not an icon file")
    images = {}
    for i in range(count):
        side, _, _, _, _, _, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        image = data[offset : offset + length]
        if image[:4] == struct.pack("<I", HEADER.size):
            images[side or 256] = image
    return images


@functools.cache
def emblem_images() -> dict[int, bytes]:
    return ico_images(ICO.read_bytes())


def _dot_colour(x: float, y: float, dot: RGB) -> RGB | None:
    """The dot in the bottom right corner of a 16x16 design, with a dark edge around it."""
    d = (x - 12.4) ** 2 + (y - 12.4) ** 2
    if d <= 3.2**2:
        return dot
    if d <= 4.0**2:
        return EDGE
    return None


def with_dot(image: bytes, dot: RGB) -> bytes:
    """A copy of a classic-format icon image with the status dot drawn over it."""
    size = HEADER.unpack_from(image)[1]
    pixels = bytearray(image)
    scale = 16 / size
    step = 1 / SAMPLES
    first = int(8.4 / scale)  # the dot lies right of and below 8.4 of 16
    for py in range(first, size):
        for px in range(first, size):
            at = HEADER.size + ((size - 1 - py) * size + px) * 4  # rows are bottom-up
            b, g, r, a = pixels[at : at + 4]
            sum_r = sum_g = sum_b = sum_a = 0.0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    colour = _dot_colour((px + (sx + 0.5) * step) * scale, (py + (sy + 0.5) * step) * scale, dot)
                    if colour is None:
                        weight = a / 255
                        sum_r += r * weight
                        sum_g += g * weight
                        sum_b += b * weight
                        sum_a += weight
                    else:
                        sum_r += colour[0]
                        sum_g += colour[1]
                        sum_b += colour[2]
                        sum_a += 1
            if sum_a:
                pixels[at : at + 4] = bytes((
                    round(sum_b / sum_a),
                    round(sum_g / sum_a),
                    round(sum_r / sum_a),
                    round(255 * sum_a / SAMPLES**2),
                ))
    return bytes(pixels)


def icon_image(size: int, dot: RGB | None = None) -> bytes:
    """The emblem for CreateIconFromResourceEx: the smallest image of at least `size` pixels
    (Windows scales it to the size asked for), with the status dot if one is given."""
    images = emblem_images()
    side = min((s for s in images if s >= size), default=max(images))
    return with_dot(images[side], dot) if dot else images[side]
