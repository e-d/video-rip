"""Generate the extension's PNG icons (red rounded square, white down-arrow over a play triangle)."""
import struct, zlib, sys
from pathlib import Path

def png(size, pixels):
    raw = b"".join(b"\x00" + bytes(pixels[y*size*4:(y+1)*size*4]) for y in range(size))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")

def inside(x, y):
    """Shape test in unit coords (0..1). Returns 0 = transparent, 1 = red, 2 = white."""
    r = 0.22
    cx = min(max(x, r), 1 - r); cy = min(max(y, r), 1 - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r: return 0
    # play triangle
    tx0, ty0, ty1, tx1 = 0.36, 0.22, 0.62, 0.70
    if tx0 <= x <= tx1:
        half = (ty1 - ty0) / 2 * (1 - (x - tx0) / (tx1 - tx0))
        if abs(y - (ty0 + ty1) / 2) <= half: return 2
    # bottom bar (download tray)
    if 0.26 <= x <= 0.74 and 0.72 <= y <= 0.80: return 2
    return 1

def render(size, ss=4):
    px = []
    for py in range(size):
        for qx in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    v = inside((qx + (sx + .5) / ss) / size, (py + (sy + .5) / ss) / size)
                    if v == 1: c = (229, 72, 77, 255)
                    elif v == 2: c = (255, 255, 255, 255)
                    else: c = (0, 0, 0, 0)
                    for i in range(4): acc[i] += c[i]
            n = ss * ss
            a = acc[3] / n
            px += [int(acc[i] / n * 255 / a) if a else 0 for i in range(3)] + [int(a)]
    return px

out = Path(sys.argv[1])
for s in (16, 32, 48, 128):
    (out / f"icon{s}.png").write_bytes(png(s, render(s)))
print("ok")
