from PIL import Image

from canvas import EMPTY, HEIGHT, WIDTH, PALETTE, png_bytes, snapshot_payload


def test_snapshot_payload_round_trips_compact_pixels():
    pixels = bytes([EMPTY] * (WIDTH * HEIGHT))
    payload = snapshot_payload(7, pixels)
    assert payload["width"] == 64
    assert payload["height"] == 64
    assert payload["revision"] == 7
    assert len(payload["pixels"]) == 5464
    assert payload["palette"] == list(PALETTE)


def test_png_is_512_square_and_uses_fixed_palette():
    data = png_bytes(bytes([0] + [EMPTY] * (WIDTH * HEIGHT - 1)))
    image = Image.open(__import__("io").BytesIO(data))
    assert image.size == (512, 512)
    assert image.mode == "P"
    assert image.getpixel((0, 0)) == 0
    assert image.getpixel((511, 511)) == EMPTY
    assert image.getpalette()[:3] == [0, 0, 0]
