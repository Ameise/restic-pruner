#!/usr/bin/env python3
"""Generate the add-on icon and logo.

Home Assistant expects a square icon and a wider logo, both PNG. They are
generated from the shapes described here using only zlib and struct, so the
artwork stays editable in version control.

    python3 scripts/generate_icons.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "restic_pruner"

TEAL_TOP = (14, 165, 164)
TEAL_BOTTOM = (10, 94, 116)
WHITE = (255, 255, 255)

#: Supersampling factor; plenty for shapes this simple and keeps edges smooth.
SS = 4

Color = tuple[int, int, int]
Pixel = tuple[int, int, int, int]


def _lerp(a: Color, b: Color, t: float) -> Color:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _rounded_rect(x: float, y: float, w: float, h: float, r: float) -> float:
    """Signed distance to a rounded rectangle centred on (w/2, h/2)."""
    dx = abs(x - w / 2) - (w / 2 - r)
    dy = abs(y - h / 2) - (h / 2 - r)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    return outside + min(max(dx, dy), 0.0) - r


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length_sq = vx * vx + vy * vy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _broom_strokes(scale: float, ox: float, oy: float) -> list[tuple[float, ...]]:
    """Handle plus fanned bristles, described in a 128x128 design space."""
    strokes: list[tuple[float, ...]] = [(34.0, 22.0, 74.0, 68.0, 6.5)]
    for index in range(5):
        spread = (index - 2) * 11.0
        strokes.append((74.0, 68.0, 84.0 + spread * 0.85, 104.0 + abs(spread) * 0.18, 4.0))
    return [
        (
            ax * scale + ox,
            ay * scale + oy,
            bx * scale + ox,
            by * scale + oy,
            r * scale,
        )
        for ax, ay, bx, by, r in strokes
    ]


def _sample(x: float, y: float, width: float, height: float, badge: bool) -> Pixel:
    if badge:
        distance = _rounded_rect(x, y, width, height, width * 0.22)
        if distance > 0:
            return (0, 0, 0, 0)
        base = _lerp(TEAL_TOP, TEAL_BOTTOM, y / height)
        scale, ox, oy = width / 128.0, 0.0, 0.0
    else:
        base = (0, 0, 0, 0)  # type: ignore[assignment]
        scale = height / 128.0 * 0.92
        ox = (width - 128.0 * scale) / 2
        oy = (height - 128.0 * scale) / 2

    # The icon is a white mark on a teal badge; the logo is the same mark drawn
    # in teal on transparency, so it reads on both light and dark cards.
    ink = WHITE if badge else _lerp(TEAL_TOP, TEAL_BOTTOM, y / height)
    for ax, ay, bx, by, radius in _broom_strokes(scale, ox, oy):
        if _segment_distance(x, y, ax, ay, bx, by) <= radius:
            return (*ink, 255)
    return (*base, 255) if badge else (0, 0, 0, 0)


def render(width: int, height: int, badge: bool) -> bytes:
    rows: list[bytes] = []
    for py in range(height):
        row = bytearray()
        for px in range(width):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    sample = _sample(
                        px + (sx + 0.5) / SS,
                        py + (sy + 0.5) / SS,
                        float(width),
                        float(height),
                        badge,
                    )
                    r += sample[0] * sample[3]
                    g += sample[1] * sample[3]
                    b += sample[2] * sample[3]
                    a += sample[3]
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes((r // a, g // a, b // a, a // (SS * SS)))
        rows.append(bytes(row))
    return _png(width, height, rows)


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    (OUT_DIR / "icon.png").write_bytes(render(128, 128, badge=True))
    (OUT_DIR / "logo.png").write_bytes(render(250, 100, badge=False))
    for name in ("icon.png", "logo.png"):
        print(f"wrote {OUT_DIR / name}")  # noqa: T201


if __name__ == "__main__":
    main()
