"""The VoiceForever emblem (gold ring with a microphone) drawn in pure Python.

The tray needs it in five colours at the system icon size, and the build needs an .ico for
the exe: `python -m vf_agent.gui.icons VoiceForever.ico`. No image library is involved.
"""

import struct
import sys
from pathlib import Path

type RGB = tuple[int, int, int]

GOLD: RGB = (0xDC, 0xB5, 0x63)
DARK: RGB = (0x12, 0x0D, 0x09)
EDGE: RGB = (0x0B, 0x09, 0x07)

# Status dot colours, the same as the window uses.
DOTS: frozendict[str, RGB] = frozendict({
    "connected": (0x5E, 0xD4, 0x74),
    "connecting": (0xF2, 0xAD, 0x42),
    "disconnected": (0x8A, 0x81, 0x72),
    "error": (0xFF, 0x75, 0x63),
    "recording": (0xE0, 0x33, 0x1B),
})

EXE_SIZES = (16, 24, 32, 48, 64, 128, 256)
SAMPLES = 4  # supersampling per axis


def _paint(x: float, y: float, dot: RGB | None) -> RGB | None:
    """Colour at a point of the 16x16 design, or None where the icon is transparent."""
    if dot is not None:
        d = (x - 12.4) ** 2 + (y - 12.4) ** 2
        if d <= 3.2**2:
            return dot
        if d <= 4.0**2:
            return EDGE
    r = ((x - 7.5) ** 2 + (y - 7.5) ** 2) ** 0.5
    if r > 7.1:
        return None
    if r >= 5.9:
        return GOLD
    # microphone capsule: x 6..9, y 3.3..8.9, round ends
    if 6 <= x <= 9 and 4.8 <= y <= 7.4:
        return GOLD
    for cy in (4.8, 7.4):
        if (x - 7.5) ** 2 + (y - cy) ** 2 <= 1.5**2:
            return GOLD
    # holder: lower half ring around the capsule
    if y >= 7.4 and 2.6 <= ((x - 7.5) ** 2 + (y - 7.4) ** 2) ** 0.5 <= 3.5:
        return GOLD
    # stem
    if 7.0 <= x <= 8.0 and 10.4 <= y <= 11.6:
        return GOLD
    return DARK


def render(size: int, dot: RGB | None = None) -> bytes:
    """Straight-alpha BGRA pixels, top row first."""
    scale = 16 / size
    step = 1 / SAMPLES
    out = bytearray()
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    colour = _paint((px + (sx + 0.5) * step) * scale, (py + (sy + 0.5) * step) * scale, dot)
                    if colour is not None:
                        r += colour[0]
                        g += colour[1]
                        b += colour[2]
                        a += 1
            if a:
                out += bytes((b // a, g // a, r // a, round(255 * a / SAMPLES**2)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def icon_image(size: int, dot: RGB | None = None) -> bytes:
    """One icon image in the classic format: BITMAPINFOHEADER, BGRA rows bottom-up, AND mask.

    This is what CreateIconFromResourceEx takes and what an .ico file stores per size.
    """
    pixels = render(size, dot)
    row = size * 4
    xor = b"".join(pixels[y * row : (y + 1) * row] for y in reversed(range(size)))
    mask_row = ((size + 31) // 32) * 4  # all zero: the alpha channel decides
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor), 0, 0, 0, 0)
    return header + xor + bytes(mask_row * size)


def ico_file(sizes: tuple[int, ...] = EXE_SIZES, dot: RGB | None = None) -> bytes:
    images = [icon_image(s, dot) for s in sizes]
    offset = 6 + 16 * len(images)
    entries = b""
    for size, image in zip(sizes, images, strict=True):
        side = 0 if size >= 256 else size  # 0 means 256 in an icon directory
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(images)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m vf_agent.gui.icons OUTPUT.ico")
        return 2
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ico_file())
    return 0


if __name__ == "__main__":
    sys.exit(main())
