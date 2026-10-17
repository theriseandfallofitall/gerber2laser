"""
The raster path: rasterize_qimage / rasterize_png and the kerf morphology.

Needs Qt but no display (conftest forces the offscreen platform) and, like the
CLI, no QApplication. The test SVG is drawn at 254 DPI, which is exactly
10 px/mm, so every width below is a whole number of pixels.
"""

import struct
import warnings

import pytest

import gerber2laser as g2l

DPI = 254                     # 10 px per mm
PX_PER_MM = DPI / g2l.MM


def bar_svg(bar_mm=2.0, size_mm=10.0):
    """A size x size mm sheet, one vertical black bar centred on it."""
    x = (size_mm - bar_mm) / 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size_mm}mm" '
            f'height="{size_mm}mm" viewBox="0 0 {size_mm} {size_mm}">'
            f'<rect x="{x}" y="0" width="{bar_mm}" height="{size_mm}" '
            f'fill="black"/></svg>')


def dark_run(img, y=None):
    """Number of dark, opaque pixels on one row (the middle one by default)."""
    from PySide6.QtGui import QColor
    y = img.height() // 2 if y is None else y
    n = 0
    for x in range(img.width()):
        c = QColor(img.pixelColor(x, y))
        if c.alpha() > 127 and c.lightness() < 128:
            n += 1
    return n


# --------------------------------------------------------------------------- #
#  Size and DPI
# --------------------------------------------------------------------------- #

def test_image_size_follows_dpi():
    img = g2l.rasterize_qimage(bar_svg(), DPI, flatten_bg="white")
    assert (img.width(), img.height()) == (100, 100)


def test_bar_width_without_kerf():
    img = g2l.rasterize_qimage(bar_svg(2.0), DPI, flatten_bg="white")
    assert dark_run(img) == 20


def test_dpi_is_stamped_on_the_image():
    img = g2l.rasterize_qimage(bar_svg(), DPI, flatten_bg="white")
    dpm = round(DPI * 1000 / g2l.MM)
    assert img.dotsPerMeterX() == dpm
    assert img.dotsPerMeterY() == dpm


def test_png_carries_phys_chunk(tmp_path):
    """LightBurn places the PNG at true size only if pHYs is in the file."""
    out = tmp_path / "bar.png"
    w, h = g2l.rasterize_png(bar_svg(), DPI, str(out))
    assert (w, h) == (100, 100)
    data = out.read_bytes()
    i = data.find(b"pHYs")
    assert i > 0, "PNG has no pHYs chunk"
    x_ppm, y_ppm, unit = struct.unpack(">IIB", data[i + 4:i + 13])
    assert unit == 1                              # 1 = metre
    assert x_ppm == y_ppm == round(DPI * 1000 / g2l.MM)


# --------------------------------------------------------------------------- #
#  Kerf: per-edge, raster only
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kerf, grow_black, width", [
    (+0.1, True, 22),     # 0.1 mm = 1 px per edge, so 20 px -> 22 px
    (-0.1, True, 18),
    (+0.1, False, 18),    # "expand grows black" off: positive kerf shrinks black
    (-0.1, False, 22),
    (+0.3, True, 26),
])
def test_kerf_moves_each_edge(kerf, grow_black, width):
    """A kerf of k moves every black boundary by k, so the bar changes by 2k."""
    img = g2l.rasterize_qimage(bar_svg(2.0), DPI, kerf_mm=kerf,
                               kerf_grow_black=grow_black, flatten_bg="white")
    assert dark_run(img) == width


def test_kerf_below_half_a_pixel_is_a_no_op():
    img = g2l.rasterize_qimage(bar_svg(2.0), DPI, kerf_mm=0.04,
                               flatten_bg="white")
    assert dark_run(img) == 20


# --------------------------------------------------------------------------- #
#  _morph: the fast disc dilation must be the exact disc
# --------------------------------------------------------------------------- #

def _dot_image(size, cx, cy):
    from PySide6.QtGui import QImage, QColor
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    img.setPixelColor(cx, cy, QColor("black"))
    return img


@pytest.mark.parametrize("r", [1, 3, 7])
def test_morph_dilates_by_an_exact_disc(r):
    from PySide6.QtGui import QColor
    size, c = 2 * r + 9, r + 4
    out = g2l._morph(_dot_image(size, c, c), r, grow_black=True)
    got = {(x, y) for y in range(size) for x in range(size)
           if QColor(out.pixelColor(x, y)).lightness() < 128}
    want = {(x, y) for y in range(size) for x in range(size)
            if (x - c) ** 2 + (y - c) ** 2 <= r * r}
    assert got == want


def test_morph_shrink_is_the_dual():
    """grow_black=False dilates white, so an isolated black dot vanishes."""
    from PySide6.QtGui import QColor
    out = g2l._morph(_dot_image(11, 5, 5), 2, grow_black=False)
    assert QColor(out.pixelColor(5, 5)).lightness() > 127


def test_morph_reports_progress_once_per_row():
    calls = []
    g2l._morph(_dot_image(15, 7, 7), 4, True,
               progress=lambda done, total: calls.append((done, total)))
    assert calls == [(i, 5) for i in range(1, 6)]


def test_morph_zero_radius_returns_input():
    img = _dot_image(5, 2, 2)
    assert g2l._morph(img, 0, True) is img


# --------------------------------------------------------------------------- #
#  Background: "none" and typos are transparent, never black
# --------------------------------------------------------------------------- #

def _corner_alpha(img):
    from PySide6.QtGui import QColor
    return QColor(img.pixelColor(0, 0)).alpha()


def test_white_background_is_opaque():
    img = g2l.rasterize_qimage(bar_svg(), DPI, flatten_bg="white")
    assert _corner_alpha(img) == 255


def test_bg_none_is_transparent_and_silent():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        img = g2l.rasterize_qimage(bar_svg(), DPI, flatten_bg="none")
    assert _corner_alpha(img) == 0


def test_unparseable_bg_warns_and_falls_back_to_transparent():
    """An invalid QColor fills BLACK, which here means 'burn the whole sheet'."""
    with pytest.warns(RuntimeWarning, match="unrecognised background"):
        img = g2l.rasterize_qimage(bar_svg(), DPI, flatten_bg="whiet")
    assert _corner_alpha(img) == 0
    assert dark_run(img) == 20
