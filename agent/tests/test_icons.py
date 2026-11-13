import struct

from vf_agent.gui.icons import DOTS, ICO, emblem_images, ico_images, icon_image

TRAY_SIZES = (16, 20, 24, 32, 40, 48)  # the small icon size from 100% to 300% scaling


def pixel(image: bytes, size: int, x: int, y: int) -> tuple[int, int, int, int]:
    """(b, g, r, a) at x, y counted from the top left."""
    at = 40 + ((size - 1 - y) * size + x) * 4
    return tuple(image[at : at + 4])


def test_the_icon_file_has_every_size():
    data = ICO.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sides = [data[6 + 16 * i] or 256 for i in range(count)]
    assert {*TRAY_SIZES, 256} <= set(sides)
    # The tray draws on the classic images; the 256 px one may be PNG.
    assert set(TRAY_SIZES) <= set(emblem_images())


def test_icon_image_layout():
    for size in TRAY_SIZES:
        image = icon_image(size)
        header = struct.unpack("<IiiHHIIiiII", image[:40])
        assert header[:5] == (40, size, size * 2, 1, 32)
        assert len(image) == 40 + size * size * 4 + ((size + 31) // 32) * 4 * size


def test_shield_is_gold_on_transparent():
    size = 32
    image = icon_image(size)
    assert pixel(image, size, 0, 0)[3] == 0  # outside the shield
    assert pixel(image, size, size - 1, size - 1)[3] == 0
    b, _, r, a = pixel(image, size, size // 2, 1)  # the gold rim at the top of the shield
    assert a > 128 and r > 180 and r > b + 80


def test_status_dot_changes_only_the_corner():
    size = 16
    plain = icon_image(size)
    for name, (r, g, b) in DOTS.items():
        dotted = icon_image(size, DOTS[name])
        assert len(dotted) == len(plain)
        assert pixel(dotted, size, 12, 12)[:3] == (b, g, r), name
        assert pixel(dotted, size, 12, 12)[3] == 255
        for y in range(8):
            for x in range(size):
                assert pixel(dotted, size, x, y) == pixel(plain, size, x, y)


def test_a_size_between_takes_the_next_larger_image():
    assert icon_image(18) == icon_image(20)
    assert icon_image(1000) == icon_image(max(emblem_images()))


def test_png_images_are_left_out():
    png = b"\x89PNG\r\n\x1a\n" + bytes(8)
    bmp = struct.pack("<IiiHHIIiiII", 40, 1, 2, 1, 32, 0, 4, 0, 0, 0, 0) + bytes(8)
    entries = [(0, png), (1, bmp)]
    offset = 6 + 16 * len(entries)
    directory = b""
    for side, image in entries:
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    data = struct.pack("<HHH", 0, 1, len(entries)) + directory + png + bmp
    assert ico_images(data) == {1: bmp}
