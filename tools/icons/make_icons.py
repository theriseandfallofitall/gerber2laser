#!/usr/bin/env python3
"""
Render assets/gerber2laser.svg into the icon files each platform wants.

    python tools/icons/make_icons.py

Writes, next to the SVG:
    gerber2laser_16/24/32/48/64/128/256/512/1024.png
    gerber2laser.ico    Windows shortcuts + the KiCad toolbar
    gerber2laser.icns   the macOS .app bundle

ICO and ICNS are both written by hand as containers of PNG frames, so this
needs nothing beyond PySide6 (already a dependency) — no Pillow, no
imagemagick, no iconutil, and it runs the same on all three platforms.
"""

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SVG = ROOT / "assets" / "gerber2laser.svg"
PNG_SIZES = [16, 24, 32, 48, 64, 128, 256, 512, 1024]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
# macOS icon types that take a PNG payload, and the pixel size each expects
ICNS_TYPES = [(b"icp4", 16), (b"icp5", 32), (b"ic07", 128),
              (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024)]


def render(size):
    """SVG -> PNG bytes at size x size."""
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QBuffer, Qt

    r = QSvgRenderer(str(SVG))
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p)
    p.end()

    buf = QBuffer()          # NB: QBuffer(QByteArray()) binds a temporary and
    buf.open(QBuffer.WriteOnly)   # segfaults once it is collected
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path, frames):
    """frames: {size: png_bytes}. PNG-in-ICO, understood by Windows Vista+."""
    sizes = sorted(frames)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(head) + 16 * len(sizes)
    entries, blobs = [], []
    for s in sizes:
        data = frames[s]
        entries.append(struct.pack("<BBBBHHII", s if s < 256 else 0,
                                   s if s < 256 else 0, 0, 0, 1, 32,
                                   len(data), offset))
        blobs.append(data)
        offset += len(data)
    path.write_bytes(head + b"".join(entries) + b"".join(blobs))


def write_icns(path, frames):
    """frames: {icns_type: png_bytes}."""
    body = b"".join(t + struct.pack(">I", len(d) + 8) + d
                    for t, d in frames.items())
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main():
    if not SVG.exists():
        sys.exit(f"missing {SVG}")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    out = SVG.parent
    png = {s: render(s) for s in PNG_SIZES}
    for s, data in png.items():
        (out / f"gerber2laser_{s}.png").write_bytes(data)
    # the plain name is what the .desktop entry and the KiCad plugin point at
    (out / "gerber2laser.png").write_bytes(png[256])

    write_ico(out / "gerber2laser.ico", {s: png[s] for s in ICO_SIZES})
    write_icns(out / "gerber2laser.icns",
               {t: png[s] for t, s in ICNS_TYPES})

    for f in sorted(out.iterdir()):
        print(f"{f.name:28s} {f.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
