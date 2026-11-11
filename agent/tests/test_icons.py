import struct

from vf_agent.gui.icons import DOTS, EXE_SIZES, ico_file, icon_image, render


def test_render_has_transparent_corners_and_gold_ring():
    size = 32
    pixels = render(size)
    assert len(pixels) == size * size * 4
    assert pixels[3] == 0  # top-left corner: alpha 0
    top_middle = (1 * size + size // 2) * 4  # the ring crosses the top edge near the middle
    assert pixels[top_middle + 3] > 0


def test_status_dot_changes_the_corner():
    plain = render(16)
    dotted = render(16, DOTS["connected"])
    assert plain != dotted


def test_icon_image_layout():
    image = icon_image(16)
    header = struct.unpack("<IiiHHIIiiII", image[:40])
    assert header[:5] == (40, 16, 32, 1, 32)
    assert len(image) == 40 + 16 * 16 * 4 + 4 * 16  # header, BGRA, AND mask (4-byte rows)


def test_ico_file_directory():
    data = ico_file()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(EXE_SIZES))
    for i, size in enumerate(EXE_SIZES):
        width, height, _, _, planes, bits, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i : 22 + 16 * i])
        assert width == height == (0 if size == 256 else size)
        assert (planes, bits) == (1, 32)
        assert data[offset : offset + 4] == struct.pack("<I", 40)
        assert offset + length <= len(data)
