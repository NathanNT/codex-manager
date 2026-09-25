"""Create small, dependency-free Windows tray icons for the supervision status."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZE = 32


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def make_icon(color: tuple[int, int, int]) -> bytes:
    pixels = [[(0, 0, 0, 0) for _ in range(SIZE)] for _ in range(SIZE)]

    def rect(x1: int, y1: int, x2: int, y2: int, fill: tuple[int, int, int, int]):
        for y in range(y1, y2):
            for x in range(x1, x2):
                pixels[y][x] = fill

    # A quiet, high-contrast mark that stays readable at the usual 16 px tray size.
    rect(1, 1, 31, 31, (255, 255, 255, 255))
    dark = (40, 69, 59, 255)
    for x, y in ((5, 5), (17, 5), (5, 17)):
        rect(x, y, x + 9, y + 2, dark)
        rect(x, y + 7, x + 9, y + 9, dark)
        rect(x, y, x + 2, y + 9, dark)
        rect(x + 7, y, x + 9, y + 9, dark)
    rect(18, 18, 29, 29, (255, 255, 255, 255))
    for y in range(19, 29):
        for x in range(19, 29):
            if (x - 24) ** 2 + (y - 24) ** 2 <= 22:
                pixels[y][x] = (*color, 255)

    raw = b"".join(b"\x00" + b"".join(bytes(pixel) for pixel in row) for row in pixels)
    png = (b"\x89PNG\r\n\x1a\n"
           + png_chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
           + png_chunk(b"IDAT", zlib.compress(raw, 9))
           + png_chunk(b"IEND", b""))
    return (struct.pack("<HHH", 0, 1, 1)
            + struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(png), 22)
            + png)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    for name, color in {
        "online": (37, 104, 86),
        "starting": (143, 92, 23),
        "offline": (172, 61, 54),
    }.items():
        (ASSETS / f"tray-{name}.ico").write_bytes(make_icon(color))


if __name__ == "__main__":
    main()
