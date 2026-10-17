#!/usr/bin/env python3
"""
gerber2laser  —  turn KiCad Gerbers into laser-ready art for a CO2 laser cutter.

Workflow this targets (laser-resist etching, the realistic CO2+copper method):
  1. Spray the copper-clad board with matte black paint.
  2. Laser-ablate the paint OFF wherever this image is BLACK.
  3. Chemically etch: bare copper (paint removed) dissolves, painted copper stays.

So "black = laser fires = paint removed". Whether that leaves your TRACES or the
GAPS as copper depends on the Invert toggle — flip it to match your chemistry.

Front vs back:
  - You always put the side you are lasering FACE UP.
  - The back copper must be mirrored so its features line up with the front
    (vias / board edge) once you physically flip the board over.
  - Default: back side = horizontal mirror ON. Rotation is also provided.

Output:
  - PNG  : flat raster, ready to drop into LightBurn as an Engrave image. The
           export DPI is written into the file, so LightBurn places it at the
           true board size on import — do not rescale it there.
           Kerf compensation is applied HERE (raster morphology) and only here.
  - SVG  : vector, one COLOUR per feature-class. LightBurn maps colour -> cut
           layer, so copper can Fill/engrave while Edge.Cuts is a red vector Cut.
           NEVER contains kerf — exact geometry; use LightBurn's kerf offset.

Invert mode burns only the board area; the margin/offset ring around it keeps
its own colour (GUI "Margin area (invert)" / CLI --invert-margin, default white).
Invert swaps the black and white layers only — a feature-class colour (Edge.Cuts
red, mask green, ...) rides through untouched, and any colour you set by hand
(GUI colour button / CLI --layer-color drill1=black) is used exactly as given.
Drills are never a colour of their own: they always come out the opposite of
the copper, so a hole reads as a hole whichever way Invert is set.

Deps:  pip install pyside6 gerbonara
Run:   python gerber2laser.py                     (GUI)
       python gerber2laser.py --input Gerbers/    (GUI, preloaded)
       python gerber2laser.py --input ... --png/--svg ...   (headless CLI)

KiCad: copy/symlink this file into KiCad's scripting/plugins folder — a
"Gerber → Laser" action appears in pcbnew. It plots the open board's Gerbers +
drills and opens them here via an external Python (set G2L_PYTHON if the one
on PATH lacks pyside6/gerbonara).
"""

import re
import os
import sys
import math
import glob
import shutil
import zipfile
import tempfile
import argparse
import warnings
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, replace as dc_replace

try:
    from gerbonara import GerberFile, ExcellonFile
    from gerbonara.utils import MM as GB_MM
    import gerbonara.graphic_primitives as gb_gp
except ImportError:
    # KiCad's bundled Python has no gerbonara — the ActionPlugin part of this
    # file still works there (it hands off to an external Python for the GUI).
    GerberFile = ExcellonFile = None
    GB_MM = gb_gp = None

MM = 25.4  # mm per inch


# --------------------------------------------------------------------------- #
#  Pure core (no Qt) — everything here is unit-testable headless.
# --------------------------------------------------------------------------- #

_BLACKS = {"black", "#000", "#000000"}
_WHITES = {"white", "#fff", "#ffffff"}
_COPPER_KEYS = ("top_copper", "bottom_copper", "inner_copper")


@dataclass
class LayerCfg:
    key: str            # gerbonara access key, e.g. "top copper"
    name: str           # friendly UI name
    color: str          # svg colour for this feature class
    enabled: bool = True
    obj: object = None   # the gerbonara file object
    # True once the user has picked this layer's colour by hand — that choice
    # is then honoured verbatim and Invert leaves it alone (see layer_color)
    color_overridden: bool = False


@dataclass
class RenderCfg:
    layers: list                # list[LayerCfg], draw order = list order
    bg: str = "white"           # background fill ("none" = transparent)
    mirror_h: bool = False
    mirror_v: bool = False
    rotate: int = 0             # degrees CCW: 0/90/180/270
    invert: bool = False        # swap copper<->gaps (fg<->bg per layer)
    invert_margin: str = "white"  # margin/offset area colour when inverted
    margin_mm: float = 2.0
    reg_marks: bool = False     # corner registration crosshairs (front/back align)
    reg_size_mm: float = 3.0
    reg_offset_mm: float = 2.0
    reg_color: str = "black"
    scale_bar: bool = True      # true-length ruler below the board (size check)
    scale_bar_mm: float = 10.0
    scale_bar_color: str = "#ff00ff"  # own colour -> own LightBurn layer
    # drill mark sizing: 0 = as drilled, else force every hole to this diameter
    # (mm); drill_scale multiplies whichever of the two applies
    drill_size_mm: float = 0.0
    drill_scale: float = 1.0


# Common drill-mark diameters offered in the GUI, mm. First entry = as drilled.
DRILL_SIZES = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0]


# A whole path is capped at 260 chars on Windows unless long paths are turned
# on, and one name is capped at 255 bytes on ext4/APFS/NTFS. 100 chars for the
# file name clears all three and still leaves room for the folder it lands in.
MAX_EXPORT_NAME = 100

_FN_SAFE = re.compile(r"[^A-Za-z0-9.+-]+")


def settings_tags(cfg, side=None, dpi=None, kerf=None):
    """
    Short tags describing what produced a file, most important first, so a
    folder of exports says which is which without opening any of them.
    Ordered for truncation: `export_filename` drops from the end.
    """
    tags = []
    if side:
        tags.append(side)
    else:
        cu = {L.key for L in cfg.layers if L.enabled and L.obj is not None}
        top, bot = "top_copper" in cu, "bottom_copper" in cu
        tags.append("front" if top and not bot else
                    "back" if bot and not top else
                    "both" if top and bot else "nocu")
    if cfg.invert:
        tags.append("inv")
    if cfg.mirror_h:
        tags.append("mirh")
    if cfg.mirror_v:
        tags.append("mirv")
    if cfg.rotate:
        tags.append(f"rot{cfg.rotate}")
    if kerf:
        tags.append(f"k{kerf:+g}")
    if dpi:
        tags.append(f"{dpi}dpi")
    if cfg.drill_size_mm > 0:
        tags.append(f"h{cfg.drill_size_mm:g}")
    if not math.isclose(cfg.drill_scale, 1.0):
        tags.append(f"hx{cfg.drill_scale:g}")
    tags.append(f"m{cfg.margin_mm:g}")
    if cfg.reg_marks:
        tags.append("reg")
    if cfg.scale_bar:
        tags.append(f"sb{cfg.scale_bar_mm:g}")
    return tags


def export_filename(stem, tags, ext, stamp=None, max_len=MAX_EXPORT_NAME):
    """
    "<board>_<tags…>_<stamp>.<ext>", short enough for any filesystem.

    Over the cap, the least important tags go first (they are ordered for it),
    and only if that is still not enough does the board name get truncated —
    losing which settings made the file beats losing which board it is.
    """
    stem = _FN_SAFE.sub("-", str(stem)).strip("-.") or "board"
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M")
    tags = list(tags)
    while True:
        name = "_".join([stem] + tags + [stamp]) + "." + ext
        if len(name) <= max_len or not tags:
            break
        tags.pop()
    if len(name) > max_len:
        over = len(name) - max_len
        stem = stem[:max(1, len(stem) - over)]
        name = f"{stem}_{stamp}.{ext}"
    return name


def union_bounds(objs):
    """AABB covering every gerbonara object, in mm: ((x0,y0),(x1,y1))."""
    boxes = []
    for o in objs:
        # gerbonara bounding_box() defaults to mm and returns ((x0,y0),(x1,y1))
        try:
            bb = o.bounding_box()
        except Exception:
            bb = None
        if bb:
            boxes.append(bb)
    if not boxes:
        return ((0.0, 0.0), (10.0, 10.0))
    x0 = min(b[0][0] for b in boxes)
    y0 = min(b[0][1] for b in boxes)
    x1 = max(b[1][0] for b in boxes)
    y1 = max(b[1][1] for b in boxes)
    return ((float(x0), float(y0)), (float(x1), float(y1)))


def _mat_mul(a, b):
    """Compose two SVG affine matrices [a b c d e f] (a∘b: apply b then a)."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (
        a0 * b0 + a2 * b1,
        a1 * b0 + a3 * b1,
        a0 * b2 + a2 * b3,
        a1 * b2 + a3 * b3,
        a0 * b4 + a2 * b5 + a4,
        a1 * b4 + a3 * b5 + a5,
    )


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def build_transform(bounds, mirror_h, mirror_v, rotate):
    """
    Return (matrix, out_bounds) where matrix is an SVG affine that maps the
    original mm coords into a new axis-aligned box, applying rotate then mirror
    about the content centre. out_bounds is the transformed AABB (mm).
    """
    (x0, y0), (x1, y1) = bounds
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

    m = (1, 0, 0, 1, 0, 0)  # identity
    # translate centre -> origin
    m = _mat_mul((1, 0, 0, 1, -cx, -cy), m)
    # rotate CCW by `rotate` degrees (SVG y is down, so negate for CCW-on-board)
    th = math.radians(-rotate % 360)
    c, s = math.cos(th), math.sin(th)
    m = _mat_mul((c, s, -s, c, 0, 0), m)
    # mirror
    sx = -1 if mirror_h else 1
    sy = -1 if mirror_v else 1
    m = _mat_mul((sx, 0, 0, sy, 0, 0), m)
    # translate origin -> centre
    m = _mat_mul((1, 0, 0, 1, cx, cy), m)

    # transformed AABB
    corners = [_apply(m, x, y) for x, y in
               ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    nx0 = min(p[0] for p in corners)
    ny0 = min(p[1] for p in corners)
    nx1 = max(p[0] for p in corners)
    ny1 = max(p[1] for p in corners)
    return m, ((nx0, ny0), (nx1, ny1))


def _is_dark(color):
    """Rough luminance test for an SVG colour name / #rrggbb hex."""
    c = (color or "").strip().lower()
    if c in ("black", "#000", "#000000"):
        return True
    if c.startswith("#") and len(c) == 7:
        try:
            r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
            return (0.299 * r + 0.587 * g + 0.114 * b) < 128
        except ValueError:
            pass
    return False


def _reg_marks(bounds, size, offset, color, t=0.15):
    """Corner crosshairs pushed diagonally outward from the board bbox.
    Returns (svg_fragment, bbox_of_marks)."""
    (x0, y0), (x1, y1) = bounds
    half = size / 2.0
    # push each corner outward along its diagonal
    pts = [(x0 - offset, y0 - offset), (x1 + offset, y0 - offset),
           (x1 + offset, y1 + offset), (x0 - offset, y1 + offset)]
    rects = []
    for px, py in pts:
        rects.append(f'<rect x="{px-half:.4f}" y="{py-t/2:.4f}" '
                     f'width="{size:.4f}" height="{t:.4f}" fill="{color}"/>')
        rects.append(f'<rect x="{px-t/2:.4f}" y="{py-half:.4f}" '
                     f'width="{t:.4f}" height="{size:.4f}" fill="{color}"/>')
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bb = ((min(xs) - half, min(ys) - half), (max(xs) + half, max(ys) + half))
    return "".join(rects), bb


# How far the scale bar's ink reaches from the bar's top edge: the tall ticks
# stand 1.0 mm above it, and below it sit the 0.6 mm bar plus the label, whose
# glyphs end 1.2 mm down (+ half a stroke).
_SB_TICK_UP = 1.0
_SB_INK_DOWN = 1.3


def _scale_bar(x, y, length, color):
    """Horizontal scale bar: solid bar + 1 mm ticks (tall tick every 5) + label.
    (x, y) is the bar's top-left, bar is 0.6 mm tall. Emitted as its own
    <g id="scale-bar"> so it can be deleted as one object, in its own colour so
    LightBurn can map it to a no-output layer."""
    out = ['<g id="scale-bar">']
    out.append(f'<rect x="{x:.3f}" y="{y:.3f}" width="{length:.3f}" '
               f'height="0.6" fill="{color}"/>')
    for t in range(int(round(length)) + 1):
        tx = x + t
        th = 1.6 if t % 5 == 0 else 1.0
        out.append(f'<rect x="{tx:.3f}" y="{y-th+0.6:.3f}" width="0.12" '
                   f'height="{th:.3f}" fill="{color}"/>')
    out.append(_text(x + length + 1.0, y + 1.2, f"{length:g} mm",
                     size=1.2, fill=color))
    out.append("</g>")
    return "".join(out)


def layer_color(L, invert, paper, burn):
    """
    The colour a layer is actually drawn in.

    Invert swaps the two laser-meaningful colours — black (fire) and white
    (hold) — so a black layer is knocked out of the burn field and a white one
    fires. Any other colour is a deliberate feature-class colour (Edge.Cuts
    red, mask green, ...) that maps to its own LightBurn layer, so it rides
    through Invert untouched. A colour the user picked by hand is honoured
    verbatim: that is the override for keeping, say, black drills etching in
    invert mode.
    """
    if not invert or L.color_overridden:
        return L.color
    c = (L.color or "").strip().lower()
    if c in _BLACKS:
        return paper
    if c in _WHITES:
        return burn
    return L.color


def _is_drill(L):
    return L.key.startswith("drill")


def active_layers(cfg):
    return [L for L in cfg.layers if L.enabled and L.obj is not None]


def resolve_colors(cfg, active=None):
    """
    Work out what colour each enabled layer is drawn in, and in what order.

    Order: copper, then the other feature classes, then drills. Drills come
    last because a drill mark has to punch through whatever it lands on.

    Drill colour is not a colour of its own — it is always the opposite of the
    field under it (the copper, or the board background when no copper layer is
    on), so every hole reads as a hole no matter which way Invert is set. Pick
    a colour for the drill layer by hand to opt out of that.

    Returns [(LayerCfg, colour)] in paint order.
    """
    if active is None:
        active = active_layers(cfg)
    burn = "black"
    paper = cfg.bg if cfg.bg and cfg.bg != "none" else "white"

    def rank(L):
        if L.key in _COPPER_KEYS:
            return 0
        return 2 if _is_drill(L) else 1

    cu = [L for L in active if L.key in _COPPER_KEYS]
    field = (layer_color(cu[0], cfg.invert, paper, burn) if cu
             else (burn if cfg.invert else paper))
    hole = paper if _is_dark(field) else burn

    out = []
    for L in sorted(active, key=rank):     # stable: keeps loader order within
        if _is_drill(L) and not L.color_overridden:
            out.append((L, hole))
        else:
            out.append((L, layer_color(L, cfg.invert, paper, burn)))
    return out


def _resize_hole(prim, size_mm, scale):
    """
    Resize one drill primitive: a round hole (Circle) or a slot (Line).

    `size_mm` > 0 forces the diameter, so a board full of assorted drills comes
    out as one uniform mark you can dial to the smallest dot your laser makes;
    0 keeps the drilled size. `scale` then multiplies whichever applies.
    """
    if isinstance(prim, gb_gp.Circle):
        r = (size_mm / 2.0 if size_mm > 0 else prim.r) * scale
        return dc_replace(prim, r=max(r, 1e-4))
    if isinstance(prim, (gb_gp.Line, gb_gp.Arc)):     # slotted / routed holes
        w = (size_mm if size_mm > 0 else prim.width) * scale
        return dc_replace(prim, width=max(w, 1e-4))
    return prim


def _layer_fragment(camfile, bounds, fg, outline_only=False,
                    hole_size=0.0, hole_scale=1.0):
    """
    Render one gerbonara file as an SVG `<g>` fragment in mm board coords.

    gerbonara's own to_svg() draws traces the way a Gerber describes them: a
    zero-width centreline `<path>` with `stroke-width` = the trace width.
    LightBurn ignores stroke width entirely — every trace imports as a bare
    hairline, so a Fill produces nothing and only pads (already real filled
    shapes) survive. So every stroked primitive is converted to its filled
    outline (`to_arc_poly()`, exact stadium/annulus geometry, no approximation)
    before it is emitted.

    `outline_only=True` keeps the centreline stroke — that is what Edge.Cuts
    wants, since LightBurn cuts along the imported path.

    The wrapping transform matches gerbonara's (flip y within `bounds`) so all
    layers share one frame; `bounds` must be the same box for every layer.
    """
    (x0, y0), (x1, y1) = bounds
    parts = [f'<g transform="translate({x0:.6f} {y1:.6f}) scale(1 -1) '
             f'translate({-x0:.6f} {-y0:.6f})">']
    resize = (hole_size > 0) or not math.isclose(hole_scale, 1.0)
    for obj in camfile.objects:
        for prim in obj.to_primitives(unit=GB_MM):
            if resize:
                prim = _resize_hole(prim, hole_size, hole_scale)
            if not outline_only and isinstance(prim, (gb_gp.Line, gb_gp.Arc)):
                if math.isclose(prim.width, 0):
                    # a 0-width aperture has no outline to fill; keep gerbonara's
                    # hairline so the feature is still visible
                    parts.append(str(prim.to_svg(fg, "none")))
                    continue
                prim = prim.to_arc_poly()
            parts.append(str(prim.to_svg(fg, "none")))
    parts.append("</g>")
    return "".join(parts)


# How finely arcs are flattened when geometry goes through the boolean below.
# 1 µm — twenty times under a 0.02 mm kerf, and two orders under anything a CO2
# spot can resolve, so the approximation is invisible on the etched board.
MERGE_ARC_TOLERANCE_MM = 1e-3


def _signed_area(pts):
    """Twice the signed area of a closed polygon; > 0 is counter-clockwise."""
    s = 0.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        s += ax * by - bx * ay
    return s


def _merged_outlines(camfile, hole_size=0.0, hole_scale=1.0):
    """
    Yield each primitive of one layer as a closed polygon (list of points),
    every one of them wound counter-clockwise.

    Same geometry `_layer_fragment()` emits — `to_arc_poly()`, exact
    stadium/annulus outlines — then flattened to `MERGE_ARC_TOLERANCE_MM`,
    because the boolean it feeds works on polygons. Every gerbonara primitive
    has `to_arc_poly()`, so circles and rotated rectangles arrive as real
    outlines rather than <circle>/<rect>.

    The winding normalisation is not cosmetic. gerbonara does not promise an
    orientation — a Rectangle comes out in a fixed order, an ArcPoly keeps
    whatever direction the Gerber region contour had — and the union is
    winding-filled, so a clockwise pad overlapping a counter-clockwise region
    cancels to zero and the copper under it turns into burn. An ArcPoly holds
    exactly one contour and copper holes arrive as clear polarity (skipped
    below), so no primitive here needs a hole of its own and flipping every
    contour to one direction is safe.

    Skipped: clear-polarity primitives (they draw nothing today either) and
    zero-width strokes, which have no area to knock out of anything.
    """
    resize = (hole_size > 0) or not math.isclose(hole_scale, 1.0)
    for obj in camfile.objects:
        for prim in obj.to_primitives(unit=GB_MM):
            if resize:
                prim = _resize_hole(prim, hole_size, hole_scale)
            if not getattr(prim, "polarity_dark", True):
                continue
            if (isinstance(prim, (gb_gp.Line, gb_gp.Arc))
                    and math.isclose(prim.width, 0)):
                continue
            poly = prim.to_arc_poly()
            if not poly.outline:
                continue
            pts = poly.approximate_arcs(
                max_error=MERGE_ARC_TOLERANCE_MM).outline
            if len(pts) < 3:
                continue
            yield pts if _signed_area(pts) >= 0 else pts[::-1]


def _have_qt_paths():
    try:
        from PySide6.QtGui import QPainterPath        # noqa: F401
        return True
    except ImportError:
        return False


def _burn_field_d(bounds, hold, fire):
    """
    The Invert burn field as one SVG path `d`: the board rect minus every
    feature that must hold its paint, plus every feature that must fire.

    This has to be a real polygon boolean, not a fill rule. Copper overlaps
    itself constantly — a polyline's segments share their round end caps, pads
    sit on traces, traces run through a pour — and both SVG fill rules treat an
    overlap as a change of depth. Even-odd punches the overlaps back out and
    nonzero cannot express a difference-against-a-union at all, so either one
    turns every trace junction and the whole inside of a zone pour into burn.
    Only a union of the copper, subtracted from the board, is right.

    `hold` and `fire` are lists of point-list polygons, in the same frame as
    `bounds`. Order is subtract-then-add so a drill inside a trace comes back
    as a burn island: board − copper + drills.

    Qt does the boolean (`QPainterPath.simplified()` unions a winding-filled
    path in one pass). The import is function-local like every other PySide6
    import in this file; QPainterPath is geometry only, so it needs no display
    and no QApplication.
    """
    from PySide6.QtGui import QPainterPath
    from PySide6.QtCore import Qt, QRectF

    def union(polys):
        p = QPainterPath()
        p.setFillRule(Qt.WindingFill)
        for pts in polys:
            p.moveTo(*pts[0])
            for xy in pts[1:]:
                p.lineTo(*xy)
            p.closeSubpath()
        return p.simplified()

    (x0, y0), (x1, y1) = bounds
    res = QPainterPath()
    res.addRect(QRectF(x0, y0, x1 - x0, y1 - y0))
    if hold:
        res = res.subtracted(union(hold))
    if fire:
        res = res.united(union(fire))

    # The result is a set of non-overlapping, properly nested contours, so
    # even-odd and nonzero agree on it; even-odd is emitted because it does not
    # depend on Qt's choice of contour orientation.
    out, i, n = [], 0, res.elementCount()
    while i < n:
        e = res.elementAt(i)
        if e.isMoveTo():
            out.append(f"M {e.x:.4f} {e.y:.4f}")
        elif e.isLineTo():
            out.append(f"L {e.x:.4f} {e.y:.4f}")
        elif e.isCurveTo():                      # cubic: this + 2 data points
            c2, ep = res.elementAt(i + 1), res.elementAt(i + 2)
            out.append(f"C {e.x:.4f} {e.y:.4f} {c2.x:.4f} {c2.y:.4f} "
                       f"{ep.x:.4f} {ep.y:.4f}")
            i += 2
        i += 1
    return " ".join(out) + " Z"


def build_svg(cfg: RenderCfg, merge_invert: bool = True) -> str:
    """
    Compose all enabled layers into one self-contained SVG string.

    `merge_invert` picks how Invert is expressed, and the two are NOT
    interchangeable:

    * ``True`` (export) — one `<path>` in the burn colour holding the board
      minus the copper plus the drills, computed by `_burn_field_d()`. This is
      the only form LightBurn can read: LightBurn maps colour → layer and fills
      each layer on its own, with no painter's model, so a white-on-black
      knock-out arrives as two unrelated layers and nothing ever subtracts.
      The paper-coloured margin rect is dropped with it — a full-page white
      rectangle is one more stray LightBurn layer, and a dangerous one if it
      lands on a cut layer.
    * ``False`` (raster/preview) — the painter composition: opaque burn rect,
      features repainted in the paper colour on top. Correct for a raster, and
      required for Qt, whose path-size workaround (`svg_for_qt`) can only thin
      plain M/L polylines — one merged path of that size would be dropped
      silently, taking the whole board with it.
    """
    active = active_layers(cfg)
    if not active:
        raise ValueError("No layers enabled.")

    bounds = union_bounds([L.obj for L in active])
    # registration crosshairs sit just outside the board corners, in the same
    # (svg) coordinate frame gerbonara maps content into, so they ride through
    # the mirror/rotate with the board and land on the SAME physical spot on
    # both sides once you flip it.
    burn = "black"                     # laser-fire colour
    paper = cfg.bg if cfg.bg and cfg.bg != "none" else "white"
    # the margin/offset area around the board: its own colour when inverted
    # (default white = don't burn the whole margin), page bg otherwise
    margin_fill = (cfg.invert_margin or "white") if cfg.invert else cfg.bg

    reg_svg = ""
    draw_bounds = bounds
    if cfg.reg_marks:
        # marks sit in the margin area — pick whichever contrasts with it
        rc = "white" if _is_dark(margin_fill) else "black"
        reg_svg, reg_bb = _reg_marks(bounds, cfg.reg_size_mm,
                                     cfg.reg_offset_mm, rc)
        (bx0, by0), (bx1, by1) = bounds
        (rx0, ry0), (rx1, ry1) = reg_bb
        draw_bounds = ((min(bx0, rx0), min(by0, ry0)),
                       (max(bx1, rx1), max(by1, ry1)))

    matrix, (nb) = build_transform(draw_bounds, cfg.mirror_h, cfg.mirror_v,
                                   cfg.rotate)
    (ox0, oy0), (ox1, oy1) = nb
    mg = cfg.margin_mm
    vb_x, vb_y = ox0 - mg, oy0 - mg
    vb_w, vb_h = (ox1 - ox0) + 2 * mg, (oy1 - oy0) + 2 * mg

    # the board itself in output coords (the drawing also holds the reg marks)
    (bx0, by0), (bx1, by1) = bounds
    _bc = [_apply(matrix, x, y) for x, y in
           ((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1))]
    board_x0, board_y0 = min(c[0] for c in _bc), min(c[1] for c in _bc)
    board_x1, board_y1 = max(c[0] for c in _bc), max(c[1] for c in _bc)

    # The scale bar is drawn AFTER the mirror/rotate transform, so it always
    # reads left-to-right at true length no matter how the board is flipped.
    # Its length is exact mm — measure it on the burned/etched piece to verify
    # the machine's absolute scaling.
    #
    # Registration marks push the drawing out past the board on every side, and
    # that strip below the board is empty. The bar goes THERE, level with the
    # bottom crosshairs and starting clear of the left one, so it costs no
    # extra height. Without the marks (or if that strip is too shallow) it
    # falls back to its own band added below the frame.
    sb_svg = ""
    if cfg.scale_bar:
        sb_len = max(1.0, cfg.scale_bar_mm)
        # The bar keeps its own clearance whatever the margin is — at margin 0
        # it would otherwise sit hard against the frame with its ticks touching
        # the board, and the end of the label would fall outside the viewBox.
        pad = max(mg, 1.5)
        label_w = 1.0 + 0.9 * len(f"{sb_len:g} mm")   # gap + stroke-font advance
        ink_h = _SB_TICK_UP + _SB_INK_DOWN
        sb_x = vb_x + pad
        strip_top, strip_bot = board_y1, oy1 + mg     # below the board
        if cfg.reg_marks and (strip_bot - strip_top) >= ink_h + 1.0:
            sb_x = max(sb_x, ox0 + cfg.reg_size_mm + 1.0)
            sb_y = (strip_top + strip_bot) / 2 - ink_h / 2 + _SB_TICK_UP
        else:
            sb_y = oy1 + mg + pad + _SB_TICK_UP
            vb_h += pad + ink_h + pad                 # a band of its own
        sb_svg = _scale_bar(sb_x, sb_y, sb_len, cfg.scale_bar_color)
        vb_w = max(vb_w, (sb_x - vb_x) + sb_len + label_w + pad)

    parts = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{vb_w:.4f}mm" height="{vb_h:.4f}mm" '
        f'viewBox="{vb_x:.4f} {vb_y:.4f} {vb_w:.4f} {vb_h:.4f}">'
    )
    # Invert = the BOARD area is the "burn" colour and the copper features are
    # knocked back out to the paper colour (traces stay, gaps fire). One opaque
    # burn rect over the board bbox only — the margin/offset area keeps its own
    # colour (cfg.invert_margin) so you don't burn the whole sheet of paint.
    # Features draw in paper colour with a transparent per-layer bg, so layers
    # never occlude each other.
    colored = resolve_colors(cfg, active)
    merged = []
    if cfg.invert and merge_invert:
        if margin_fill and margin_fill != "none" and _is_dark(margin_fill):
            # A burn-coloured margin turns the whole page into one burn region
            # with the reg marks as holes in it. That region is the output
            # frame, not the board frame the merged path is anchored to, so it
            # cannot be folded in — keep the paint-over form and say so, rather
            # than shipping a file LightBurn silently reads wrong.
            warnings.warn(
                "--invert-margin is a burn colour, so the SVG keeps the "
                "paint-over form that LightBurn cannot compose. Use a light "
                "invert margin for a LightBurn-ready SVG.", RuntimeWarning)
        elif not _have_qt_paths():
            # SVG export is otherwise Qt-free; the boolean is not.
            warnings.warn(
                "PySide6 is missing, so the SVG keeps the paint-over form that "
                "LightBurn cannot compose. Install PySide6 for a "
                "LightBurn-ready inverted SVG.", RuntimeWarning)
        else:
            # Layers that are pure laser geometry — no hand-picked colour, no
            # feature-class colour of their own (Edge.Cuts red, mask green,
            # ... keep their own LightBurn layer), and not the outline, which
            # stays a centreline stroke. Only these can be folded in.
            merged = [L for L, fg in colored
                      if L.key != "outline" and not L.color_overridden
                      and fg in (paper, burn)]
    merged_ids = {id(L) for L in merged}

    # The margin rect is paper — pure raster paint. Merged output drops it:
    # one stray white rectangle is one stray LightBurn layer.
    if margin_fill and margin_fill != "none" and not merged:
        parts.append(
            f'<rect x="{vb_x:.4f}" y="{vb_y:.4f}" width="{vb_w:.4f}" '
            f'height="{vb_h:.4f}" fill="{margin_fill}"/>'
        )

    if cfg.invert and not merged:
        # painter composition: burn field = the BOARD only, not the reg-mark /
        # margin area
        parts.append(
            f'<rect x="{board_x0:.4f}" y="{board_y0:.4f}" '
            f'width="{board_x1-board_x0:.4f}" '
            f'height="{board_y1-board_y0:.4f}" fill="{burn}"/>'
        )

    mstr = "matrix(%s)" % " ".join(f"{v:.8f}" for v in matrix)
    parts.append(f'<g transform="{mstr}">')

    if merged:
        # The burn field lives inside the same y-flip `_layer_fragment` uses,
        # so it shares the frame the feature outlines are already in — and that
        # flip maps the bounds box onto itself, so the board rect that seeds
        # the boolean is just `bounds` verbatim.
        (fx0, fy0), (fx1, fy1) = bounds
        hold, fire = [], []
        for L, fg in colored:
            if id(L) not in merged_ids:
                continue
            drill = _is_drill(L)
            (fire if fg == burn else hold).extend(_merged_outlines(
                L.obj,
                hole_size=cfg.drill_size_mm if drill else 0.0,
                hole_scale=cfg.drill_scale if drill else 1.0))
        parts.append(
            f'<g transform="translate({fx0:.6f} {fy1:.6f}) scale(1 -1) '
            f'translate({-fx0:.6f} {-fy0:.6f})">'
            f'<path fill-rule="evenodd" fill="{burn}" '
            f'd="{_burn_field_d(bounds, hold, fire)}"/>'
            f'</g>')

    for L, fg in colored:
        if id(L) in merged_ids:
            continue
        # shared `bounds` keeps every layer in the SAME frame so they align;
        # a bare <g> (not a nested <svg>) is also all Qt's SVG Tiny 1.2 allows
        drill = _is_drill(L)
        parts.append(_layer_fragment(
            L.obj, bounds, fg,
            outline_only=(L.key == "outline"),
            hole_size=cfg.drill_size_mm if drill else 0.0,
            hole_scale=cfg.drill_scale if drill else 1.0))
    if reg_svg:
        parts.append(reg_svg)
    parts.append("</g>")
    if sb_svg:
        parts.append(sb_svg)   # outside the transform group: never mirrored
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
#  Qt path-size workaround
#
#  QtSvg (Qt 6) silently DROPS any <path> whose data exceeds an internal
#  element cap (~41k vertices observed on PySide6 6.11 — no error, the path
#  just doesn't paint; sometimes "qt.svg: Invalid path data; path truncated"
#  is logged). KiCad zone pours arrive from gerbonara as one giant stitched
#  outline and blow straight through the cap, so the pour vanished from the
#  Qt preview AND the exported PNG while the exported SVG (browsers,
#  LightBurn) rendered it correctly. Before handing an SVG to Qt we thin any
#  oversized M/L polyline below the cap. Tolerance starts at 2 µm — far
#  below laser kerf, so the change is cosmetic. Export SVG is untouched.
# --------------------------------------------------------------------------- #

QT_PATH_VERT_LIMIT = 30000

_PATH_D_RE = re.compile(r'(<path\b[^>]*?\bd=")([^"]+)(")')
_ML_ONLY_RE = re.compile(r'^[MLZ\s0-9eE+.\-]+$')


def _rdp(pts, tol):
    """Iterative Ramer–Douglas–Peucker polyline simplification."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    t2 = tol * tol
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        x0, y0 = pts[i0]
        x1, y1 = pts[i1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        dmax, imax = -1.0, i0
        for i in range(i0 + 1, i1):
            px, py = pts[i]
            if seg2 <= 1e-30:
                d2 = (px - x0) ** 2 + (py - y0) ** 2
            else:
                cross = dx * (py - y0) - dy * (px - x0)
                d2 = cross * cross / seg2
            if d2 > dmax:
                dmax, imax = d2, i
        if dmax > t2:
            keep[imax] = True
            stack.append((i0, imax))
            stack.append((imax, i1))
    return [p for p, k in zip(pts, keep) if k]


def _thin_poly(pts, limit):
    """Drop consecutive duplicates, then RDP with escalating tolerance until
    the vertex count is under `limit`."""
    out = [pts[0]]
    for p in pts[1:]:
        if p != out[-1]:
            out.append(p)
    tol = 0.002
    while len(out) > limit and tol <= 0.05:
        out = _rdp(out, tol)
        tol *= 2
    if len(out) > limit:
        # Thinning stops at 0.05 mm because past that the shape is no longer
        # the board. A path that is still over the cap is the exact failure
        # this whole module exists to prevent, so say so instead of letting
        # QtSvg drop it in silence.
        warnings.warn(
            f"a path still has {len(out)} vertices after thinning to 0.05 mm "
            f"(QtSvg drops paths over ~{limit}); it may be missing from the "
            f"preview and the PNG. The exported SVG is unaffected.",
            RuntimeWarning, stacklevel=3)
    return out


def svg_for_qt(svg_str, limit=QT_PATH_VERT_LIMIT):
    """Return svg_str with any oversized M/L polyline path thinned so QtSvg
    renders it instead of silently dropping it. Non-M/L paths untouched."""
    def _one(m):
        d = m.group(2)
        # ~18 chars per vertex; skip anything that cannot be near the cap
        if len(d) < limit * 12 or not _ML_ONLY_RE.match(d):
            return m.group(0)
        toks = d.split()
        subpaths = []          # list of (pts, closed)
        pts, closed = [], False
        i, ok = 0, True
        while i < len(toks):
            t = toks[i]
            if t in ("M", "L"):
                if t == "M" and pts:
                    subpaths.append((pts, closed))
                    pts, closed = [], False
                try:
                    pts.append((float(toks[i + 1]), float(toks[i + 2])))
                except (IndexError, ValueError):
                    ok = False
                    break
                i += 3
            elif t == "Z":
                closed = True
                i += 1
            else:
                ok = False
                break
        if not ok:
            return m.group(0)
        if pts:
            subpaths.append((pts, closed))
        parts = []
        for pts, closed in subpaths:
            pts = _thin_poly(pts, limit)
            seg = "M " + " L ".join(f"{x:.4f} {y:.4f}" for x, y in pts)
            if closed:
                seg += " Z"
            parts.append(seg)
        return m.group(1) + " ".join(parts) + m.group(3)

    return _PATH_D_RE.sub(_one, svg_str)


# --------------------------------------------------------------------------- #
#  Calibration coupon
# --------------------------------------------------------------------------- #

def _comb(x, y, w, h, pitch, barwidth, fill="black"):
    """Vertical black bars filling a box, bar width `barwidth`, spacing `pitch`."""
    out = []
    bw = max(0.0, min(barwidth, pitch))
    n = max(1, int(w // pitch))
    for i in range(n):
        bx = x + i * pitch
        out.append(f'<rect x="{bx:.4f}" y="{y:.4f}" width="{bw:.4f}" '
                   f'height="{h:.4f}" fill="{fill}"/>')
    return "".join(out)


# Line-segment stroke font, one glyph = list of polylines on a 2x4 unit grid
# (x 0..2, y 0..4 top->baseline). Labels are emitted as stroked <path>s, NOT
# SVG <text>: QtSvg silently drops <text> (labels vanished from PNG export and
# raster preview) and LightBurn ignores it too. Lowercase renders as
# uppercase; unknown characters advance without drawing.
_FONT = {
    "0": [[(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)]],
    "1": [[(1, 0), (1, 4)], [(0.4, 4), (1.6, 4)], [(0.4, 0.8), (1, 0)]],
    "2": [[(0, 0), (2, 0), (2, 2), (0, 2), (0, 4), (2, 4)]],
    "3": [[(0, 0), (2, 0), (2, 4), (0, 4)], [(0.6, 2), (2, 2)]],
    "4": [[(0, 0), (0, 2), (2, 2)], [(2, 0), (2, 4)]],
    "5": [[(2, 0), (0, 0), (0, 2), (2, 2), (2, 4), (0, 4)]],
    "6": [[(2, 0), (0, 0), (0, 4), (2, 4), (2, 2), (0, 2)]],
    "7": [[(0, 0), (2, 0), (1, 4)]],
    "8": [[(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)], [(0, 2), (2, 2)]],
    "9": [[(2, 2), (0, 2), (0, 0), (2, 0), (2, 4), (0, 4)]],
    "A": [[(0, 4), (0, 1.2), (1, 0), (2, 1.2), (2, 4)], [(0, 2.4), (2, 2.4)]],
    "B": [[(0, 0), (0, 4)],
          [(0, 0), (1.6, 0), (2, 0.5), (2, 1.5), (1.6, 2), (0, 2)],
          [(1.6, 2), (2, 2.5), (2, 3.5), (1.6, 4), (0, 4)]],
    "C": [[(2, 0.4), (1.6, 0), (0.4, 0), (0, 0.4), (0, 3.6), (0.4, 4),
           (1.6, 4), (2, 3.6)]],
    "D": [[(0, 0), (0, 4)],
          [(0, 0), (1.4, 0), (2, 0.8), (2, 3.2), (1.4, 4), (0, 4)]],
    "E": [[(2, 0), (0, 0), (0, 4), (2, 4)], [(0, 2), (1.6, 2)]],
    "F": [[(2, 0), (0, 0), (0, 4)], [(0, 2), (1.6, 2)]],
    "G": [[(2, 0.4), (1.6, 0), (0.4, 0), (0, 0.4), (0, 3.6), (0.4, 4),
           (2, 4), (2, 2.2), (1.1, 2.2)]],
    "H": [[(0, 0), (0, 4)], [(2, 0), (2, 4)], [(0, 2), (2, 2)]],
    "I": [[(1, 0), (1, 4)], [(0.4, 0), (1.6, 0)], [(0.4, 4), (1.6, 4)]],
    "J": [[(2, 0), (2, 3.4), (1.4, 4), (0.6, 4), (0, 3.4)]],
    "K": [[(0, 0), (0, 4)], [(2, 0), (0, 2.2), (2, 4)]],
    "L": [[(0, 0), (0, 4), (2, 4)]],
    "M": [[(0, 4), (0, 0), (1, 1.8), (2, 0), (2, 4)]],
    "N": [[(0, 4), (0, 0), (2, 4), (2, 0)]],
    "O": [[(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)]],
    "P": [[(0, 4), (0, 0), (2, 0), (2, 2), (0, 2)]],
    "Q": [[(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)], [(1.2, 3), (2, 4.2)]],
    "R": [[(0, 4), (0, 0), (2, 0), (2, 2), (0, 2)], [(0.8, 2), (2, 4)]],
    "S": [[(2, 0), (0, 0), (0, 2), (2, 2), (2, 4), (0, 4)]],
    "T": [[(0, 0), (2, 0)], [(1, 0), (1, 4)]],
    "U": [[(0, 0), (0, 4), (2, 4), (2, 0)]],
    "V": [[(0, 0), (1, 4), (2, 0)]],
    "W": [[(0, 0), (0.5, 4), (1, 2.2), (1.5, 4), (2, 0)]],
    "X": [[(0, 0), (2, 4)], [(2, 0), (0, 4)]],
    "Y": [[(0, 0), (1, 1.8), (2, 0)], [(1, 1.8), (1, 4)]],
    "Z": [[(0, 0), (2, 0), (0, 4), (2, 4)]],
    ".": [[(1, 3.7), (1, 4)]],
    ",": [[(1, 3.6), (0.7, 4.5)]],
    "+": [[(1, 1), (1, 3)], [(0, 2), (2, 2)]],
    "-": [[(0, 2), (2, 2)]],
    "—": [[(0, 2), (2, 2)]],
    "=": [[(0, 1.5), (2, 1.5)], [(0, 2.5), (2, 2.5)]],
    "(": [[(1.4, 0), (0.8, 1), (0.8, 3), (1.4, 4)]],
    ")": [[(0.6, 0), (1.2, 1), (1.2, 3), (0.6, 4)]],
    "/": [[(0, 4), (2, 0)]],
}


def _text(x, y, s, size=1.2, fill="black", anchor="start"):
    """Vector stroke text. (x, y) = baseline start, size = cap height in mm."""
    sc = size / 4.0
    adv = 3.0 * sc
    if anchor == "middle":
        x -= adv * len(s) / 2.0
    elif anchor == "end":
        x -= adv * len(s)
    d, cx = [], x
    for ch in str(s):
        for line in _FONT.get(ch.upper(), ()):
            d.append("M" + "L".join(f"{cx+px*sc:.3f},{y-size+py*sc:.3f}"
                                    for px, py in line))
        cx += adv
    if not d:
        return ""
    t = max(0.1, 0.14 * size)
    return (f'<path d="{"".join(d)}" fill="none" stroke="{fill}" '
            f'stroke-width="{t:.3f}" stroke-linecap="round" '
            f'stroke-linejoin="round"/>')


def build_calibration_svg(nominal=0.30, kmin=-0.15, kmax=0.15, kstep=0.03,
                          ladder=(0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0),
                          bg="white", grow_black=True):
    """
    Self-contained kerf calibration coupon (black-on-white geometry, mm units).

    KERF SWEEP: the same line/space comb repeated, each tile with a different
    per-edge kerf baked into the geometry (bar width = nominal + 2*k). Burn +
    etch once; the tile whose bars and gaps come out EQUAL (50/50) is your
    compensation — set the main Kerf slider to that tile's labelled value.

    RESOLUTION LADDER: combs at increasing nominal width (kerf 0) so you can see
    the smallest trace/space your process actually resolves.

    SCALE BAR: a true 10 mm bar with 1 mm ticks — measure it on the etched board
    to check the laser's absolute scaling.
    """
    margin = 5.0
    label_w = 14.0          # left gutter for row labels
    tile_w = 40.0           # comb width
    row_h = 4.0
    gap_y = 2.5
    pitch = 2.0 * nominal   # nominal 50/50 duty
    sign = 1.0 if grow_black else -1.0

    ks = []
    k = kmin
    # inclusive range, float-safe
    steps = int(round((kmax - kmin) / kstep)) if kstep > 0 else 0
    for i in range(steps + 1):
        ks.append(round(kmin + i * kstep, 4))

    body = []
    y = margin + 3.0
    x0 = margin + label_w

    body.append(_text(margin, margin + 1.5,
                      "gerber2laser kerf calibration  (black = laser fires)",
                      size=1.35))
    y += 2.0

    # --- scale bar ---
    body.append(_text(margin, y + 1.2, "scale", size=1.2))
    sb_x = x0
    body.append(f'<rect x="{sb_x:.3f}" y="{y:.3f}" width="10.0" height="0.6" '
                f'fill="black"/>')
    for t in range(11):
        tx = sb_x + t
        th = 1.6 if t % 5 == 0 else 1.0
        body.append(f'<rect x="{tx:.3f}" y="{y-th+0.6:.3f}" width="0.12" '
                    f'height="{th:.3f}" fill="black"/>')
    body.append(_text(sb_x + 11, y + 1.2, "10 mm (measure me)", size=1.2))
    y += row_h + gap_y

    # --- kerf sweep ---
    body.append(_text(margin, y - 0.5,
                      f"KERF SWEEP  nominal {nominal:.2f} mm  "
                      f"(pick the 50/50 row)", size=1.3))
    y += 1.5
    for kv in ks:
        bw = nominal + 2.0 * sign * kv
        body.append(_text(margin, y + row_h * 0.7,
                          f"k={kv:+.3f}", size=1.2))
        body.append(_comb(x0, y, tile_w, row_h, pitch, bw))
        # thin frame so the tile edge is visible after etch
        body.append(f'<rect x="{x0:.3f}" y="{y:.3f}" width="{tile_w:.3f}" '
                    f'height="{row_h:.3f}" fill="none" stroke="black" '
                    f'stroke-width="0.08"/>')
        y += row_h + gap_y

    # --- resolution ladder ---
    y += 1.0
    body.append(_text(margin, y - 0.5,
                      "RESOLUTION LADDER  (kerf 0 — smallest that survives)",
                      size=1.3))
    y += 1.5
    for w in ladder:
        body.append(_text(margin, y + row_h * 0.7, f"{w:.2f} mm", size=1.2))
        body.append(_comb(x0, y, tile_w, row_h, 2.0 * w, w))
        y += row_h + gap_y

    total_w = x0 + tile_w + margin
    total_h = y + margin

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_w:.3f}mm" height="{total_h:.3f}mm" '
        f'viewBox="0 0 {total_w:.3f} {total_h:.3f}">'
    ]
    if bg and bg != "none":
        parts.append(f'<rect x="0" y="0" width="{total_w:.3f}" '
                     f'height="{total_h:.3f}" fill="{bg}"/>')
    parts.extend(body)
    parts.append("</svg>")
    return "".join(parts)


def kerf_from_measurement(nominal_mm, measured_mm, measured_is_black):
    """
    Compute the per-edge kerf slider value from a caliper reading of a nominal-
    width feature burned at kerf 0.

    If you measured the BLACK (burned) feature and it came out wider than
    nominal, the process over-burns; set a negative kerf to shrink it back.
    Per-edge kerf = -(measured - nominal)/2 for a black feature, +(...) for a
    gap. Sign is chosen so applying it returns the feature to nominal.
    """
    delta = measured_mm - nominal_mm
    return round((-delta / 2.0) if measured_is_black else (delta / 2.0), 3)


# Filename-based classifier. Order matters — first match wins. Each rule:
#   (regex, key, friendly name, colour, default_enabled, is_excellon)
# Covers KiCad (`-F_Cu.gbr`), Protel/legacy extensions (.gtl/.gbl/.gko/.drl),
# and generic word forms — so real exports from any tool just load.
_CLASSIFY = [
    # `profile` needs its own word, not a required leading hyphen: Altium and
    # friends export the outline as a bare `Profile.gbr`, which `-profile`
    # missed, so those boards loaded with no outline at all. The lookarounds
    # keep it a whole word — `board-profile`/`board_profile` match, `myprofile`
    # and `profiles` do not — so it cannot swallow an unrelated file.
    (r"(edge[_\-. ]?cuts|(?<![a-z0-9])profile(?![a-z0-9])"
     r"|\.gko$|\.gm1$|\.gml$)", "outline",
     "Edge.Cuts  (board outline)", "red", True, False),
    (r"(f[_\-. ]?cu|front.?copper|top.?copper|\.gtl$)", "top_copper",
     "F.Cu  (front copper)", "black", True, False),
    (r"(b[_\-. ]?cu|back.?copper|bottom.?copper|\.gbl$)", "bottom_copper",
     "B.Cu  (back copper)", "black", False, False),
    (r"(in\d+[_\-. ]?cu|inner.?\d)", "inner_copper",
     "Inner copper", "#888888", False, False),
    (r"(f[_\-. ]?silk|f[_\-. ]?silks|front.?silk|\.gto$)", "f_silk",
     "F.Silkscreen", "#555555", False, False),
    (r"(b[_\-. ]?silk|b[_\-. ]?silks|back.?silk|\.gbo$)", "b_silk",
     "B.Silkscreen", "#555555", False, False),
    (r"(f[_\-. ]?mask|front.?mask|\.gts$)", "f_mask",
     "F.Mask (solder mask)", "#227722", False, False),
    (r"(b[_\-. ]?mask|back.?mask|\.gbs$)", "b_mask",
     "B.Mask (solder mask)", "#227722", False, False),
    (r"(\.drl$|\.xln$|\.txt$|-pth|-npth|[_\-. ]drill)", "drill",
     "Drills", "black", False, True),
]


def classify_filename(name):
    low = name.lower()
    for rx, key, friendly, color, enabled, exc in _CLASSIFY:
        if re.search(rx, low):
            return key, friendly, color, enabled, exc
    return None


def _iter_source_files(path):
    """Yield gerber/excellon files from a folder or zip; extracts zip to temp."""
    p = Path(path)
    tmp = None
    if p.is_file() and zipfile.is_zipfile(p):
        tmp = tempfile.mkdtemp(prefix="g2l_")
        with zipfile.ZipFile(p) as z:
            z.extractall(tmp)
        root = tmp
    elif p.is_dir():
        root = str(p)
    else:
        raise ValueError(f"Not a folder or zip: {path}")
    files = []
    for f in sorted(glob.glob(os.path.join(root, "**", "*"), recursive=True)):
        if os.path.isfile(f):
            files.append(f)
    return files, tmp


def load_layers(path):
    """
    Robust loader: classify every file in a Gerber folder/zip by filename and
    open it directly (no fragile whole-stack autoguess). Returns a LayerCfg list
    ordered copper-first, with a sensible enable/colour default per layer.
    """
    if GerberFile is None:
        raise RuntimeError("gerbonara is not installed in this Python "
                           "(pip install gerbonara)")
    files, tmp = _iter_source_files(path)
    found = []
    drill_n = 0
    for f in files:
        cls = classify_filename(os.path.basename(f))
        if not cls:
            continue
        key, friendly, color, enabled, is_exc = cls
        try:
            with warnings.catch_warnings():
                # gerbonara warns about KiCad's G90-after-header drill files
                warnings.simplefilter("ignore", SyntaxWarning)
                obj = ExcellonFile.open(f) if is_exc else GerberFile.open(f)
        except Exception:
            continue
        # skip empties (e.g. a .txt that isn't a real drill file)
        try:
            if obj.bounding_box() is None:
                continue
        except Exception:
            continue
        if key == "drill":
            drill_n += 1
            key = f"drill{drill_n}"
            friendly = f"Drills #{drill_n}"
        found.append(LayerCfg(key, friendly, color, enabled=enabled, obj=obj))

    # order: outline, copper sides, everything else, drills last
    def rank(l):
        order = ["outline", "top_copper", "bottom_copper", "inner_copper"]
        return order.index(l.key) if l.key in order else 50
    found.sort(key=rank)
    # files are fully parsed into memory; the zip extraction dir can go
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    if not found:
        raise ValueError("No recognisable Gerber/drill files found in " + str(path))
    return found


# --------------------------------------------------------------------------- #
#  Cut-orientation logic
#
#  You laser the paint on the COPPER face with that face UP. Gerbers are always
#  drawn as viewed from the top of the board. So:
#    - top copper (F.Cu): the copper you laser is the top face -> cut AS-IS.
#    - bottom copper (B.Cu, and single-sided boards): you flip the board so the
#      bottom copper faces up, which is the mirror of the top-down Gerber view
#      -> cut HORIZONTALLY FLIPPED.
# --------------------------------------------------------------------------- #

def required_mirror_h(copper_key):
    """True if this copper layer must be horizontally flipped to laser it."""
    return copper_key == "bottom_copper"


def primary_copper_side(layers):
    """'top' / 'bottom' / None — which copper side actually has geometry."""
    have = set()
    for L in layers:
        if "copper" in L.key:
            try:
                if L.obj is not None and L.obj.bounding_box() is not None:
                    have.add(L.key)
            except Exception:
                pass
    if "top_copper" in have:
        return "top"      # prefer top when both exist
    if "bottom_copper" in have:
        return "bottom"
    return None


def orientation_advice(layers, mirror_h):
    """
    Inspect the enabled copper layer(s) and the current horizontal-mirror state.
    Returns (ok: bool, needed_mirror_h: bool|None, message: str).
    """
    copper = [L for L in layers if L.enabled and "copper" in L.key]
    keys = {L.key for L in copper}
    if not copper:
        return True, None, "No copper enabled — outline / drill only."
    if "top_copper" in keys and "bottom_copper" in keys:
        return (False, None,
                "⚠ Both copper sides enabled — each side is a SEPARATE burn. "
                "Enable one side at a time.")
    if not keys & {"top_copper", "bottom_copper"}:
        # inner copper only: there is no outer face to lay on the laser bed, so
        # there is no mirror this can check. Say that rather than reporting on
        # an F.Cu that isn't in play.
        names = ", ".join(sorted(L.name for L in copper))
        return (True, None,
                f"Inner copper only ({names}) — no front/back face to verify; "
                f"set the mirror to suit your process.")
    key = "bottom_copper" if "bottom_copper" in keys else "top_copper"
    need = required_mirror_h(key)
    side = "B.Cu (bottom copper)" if key == "bottom_copper" else "F.Cu (top copper)"
    if bool(mirror_h) == need:
        how = "horizontally flipped" if need else "as-is, no flip"
        return True, need, f"✓ {side}: orientation correct — laser copper up, {how}."
    fix = "a HORIZONTAL FLIP" if need else "NO flip (turn Mirror H off)"
    return (False, need,
            f"⚠ {side}: needs {fix} to cut the right way round. "
            f"Click ‘Fix orientation’.")


def _morph(img, r_px: int, grow_black: bool, progress=None):
    """
    Kerf compensation by raster morphology, Qt-only (no numpy/PIL).
    grow_black=True  -> dilate dark features (Darken/min of shifted copies).
    grow_black=False -> dilate light features i.e. shrink black (Lighten/max).

    Dilating by a disc is a union over every offset inside it, and taken
    literally that is pi*r^2 whole-image blits — ~1250 of them at r=20 px
    (0.5 mm at 1000 DPI), which stalls an export for a minute with the window
    frozen. Each row of the disc is one contiguous run, though, and the runs
    get wider monotonically as |dy| falls, so a single horizontally-dilated
    buffer can be widened one step at a time and re-used for every row that
    needs that width. Same exact disc, ~4r blits instead of pi*r^2.

    `progress`, if given, is called as progress(done, total) once per disc row
    so a caller with an event loop can show a bar and keep the window alive; a
    full-DPI export of a big board is still seconds of blitting. It stays
    optional because the CLI and the raster preview have no event loop to pump
    and must not pay for one.
    """
    if r_px <= 0:
        return img
    from PySide6.QtGui import QImage, QPainter
    mode = (QPainter.CompositionMode_Darken if grow_black
            else QPainter.CompositionMode_Lighten)

    run = QImage(img)          # img dilated horizontally by the current width
    rp = QPainter(run)
    rp.setCompositionMode(mode)
    out = QImage(img)
    op = QPainter(out)
    op.setCompositionMode(mode)

    w = 0
    rows = r_px + 1
    for dy in range(r_px, -1, -1):          # |dy| falling -> run width rising
        need = math.isqrt(r_px * r_px - dy * dy)
        while w < need:
            w += 1
            rp.drawImage(w, 0, img)
            rp.drawImage(-w, 0, img)
        op.drawImage(0, dy, run)
        if dy:
            op.drawImage(0, -dy, run)
        if progress is not None:
            progress(r_px - dy + 1, rows)
    rp.end()
    op.end()
    return out


def rasterize_qimage(svg_str: str, dpi: int, kerf_mm: float = 0.0,
                     kerf_grow_black: bool = True, flatten_bg: str = None,
                     progress=None):
    """Render SVG -> QImage at DPI, applying kerf morphology. Qt must be up.

    `progress` is handed straight to _morph(); leaving it None (the CLI and the
    preview) keeps the old behaviour exactly."""
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtCore import QByteArray, Qt

    svg_str = svg_for_qt(svg_str)   # QtSvg drops oversize paths silently
    r = QSvgRenderer(QByteArray(svg_str.encode()))
    vb = r.viewBoxF()  # width/height are in mm
    w_px = max(1, round(vb.width() / MM * dpi))
    h_px = max(1, round(vb.height() / MM * dpi))
    img = QImage(w_px, h_px, QImage.Format_ARGB32)
    # "none" is a real setting (transparent background) and a typo is a real
    # possibility; QColor parses neither, and QImage.fill() paints an invalid
    # QColor BLACK — which in this tool means "fire the laser over the whole
    # sheet". Anything Qt cannot parse falls back to transparent, loudly.
    bg = QColor(flatten_bg) if flatten_bg else None
    if bg is not None and not bg.isValid():
        if str(flatten_bg).strip().lower() != "none":
            warnings.warn(f"unrecognised background colour {flatten_bg!r} — "
                          f"using transparent", RuntimeWarning, stacklevel=2)
        bg = None
    img.fill(bg if bg is not None else Qt.transparent)
    p = QPainter(img)
    r.render(p)
    p.end()

    r_px = round(abs(kerf_mm) / MM * dpi)
    if r_px > 0:
        grow = kerf_grow_black if kerf_mm >= 0 else (not kerf_grow_black)
        img = _morph(img, r_px, grow, progress)
    # Stamp the physical resolution so the PNG carries its true size: Qt writes
    # it into the PNG pHYs chunk and LightBurn (and Inkscape, GIMP, ...) place
    # the image at exact mm on import instead of guessing 96 DPI. Without this
    # the artwork lands at the wrong scale and nothing lines up with the board.
    dpm = round(dpi * 1000.0 / MM)          # dots per metre
    img.setDotsPerMeterX(dpm)
    img.setDotsPerMeterY(dpm)
    return img


def rasterize_png(svg_str, dpi, out_path, kerf_mm=0.0, kerf_grow_black=True,
                  flatten_bg="white", progress=None):
    img = rasterize_qimage(svg_str, dpi, kerf_mm, kerf_grow_black, flatten_bg,
                           progress)
    img.save(out_path, "PNG")
    return img.width(), img.height()


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #

def run_gui(preload=None):
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QCheckBox, QLabel, QFileDialog, QComboBox, QSpinBox,
        QColorDialog, QGroupBox, QMessageBox, QScrollArea, QDoubleSpinBox,
        QDialog, QRadioButton, QFormLayout, QSlider, QFrame, QSizePolicy,
        QProgressDialog,
    )
    from PySide6.QtCore import QEvent, QTimer, QPoint
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtSvgWidgets import QSvgWidget
    from PySide6.QtGui import QColor, QPixmap
    from PySide6.QtCore import QByteArray, Qt

    class ColorButton(QPushButton):
        def __init__(self, color, on_change):
            super().__init__()
            self._c = color
            self._cb = on_change
            self._picked = False   # True once the user chose a colour by hand
            self.setFixedWidth(60)
            self._paint()
            self.clicked.connect(self._pick)

        def _paint(self):
            self.setStyleSheet(f"background:{self._c}; border:1px solid #888;")
            self.setText(self._c)

        def _pick(self):
            col = QColorDialog.getColor(QColor(self._c))
            if col.isValid():
                self._c = col.name()
                self._picked = True
                self._paint()
                self._cb()

        def color(self):
            return self._c

        def picked(self):
            return self._picked

        def show_color(self, c):
            """Display a colour the renderer worked out. Not a user choice —
            leaves `picked` alone so the layer keeps deriving its colour."""
            if c != self._c:
                self._c = c
                self._paint()

    class CalibrationDialog(QDialog):
        """Generate + export a one-burn kerf calibration coupon."""
        def __init__(self, parent):
            super().__init__(parent)
            self.par = parent
            self.setWindowTitle("Kerf calibration coupon")
            self.resize(720, 640)
            lay = QHBoxLayout(self)

            form = QFormLayout()
            self.nominal = QDoubleSpinBox(); self.nominal.setRange(0.05, 2.0)
            self.nominal.setSingleStep(0.05); self.nominal.setValue(0.30)
            self.nominal.setDecimals(2)
            self.kmin = QDoubleSpinBox(); self.kmin.setRange(-0.5, 0.0)
            self.kmin.setSingleStep(0.01); self.kmin.setValue(-0.15)
            self.kmin.setDecimals(3)
            self.kmax = QDoubleSpinBox(); self.kmax.setRange(0.0, 0.5)
            self.kmax.setSingleStep(0.01); self.kmax.setValue(0.15)
            self.kmax.setDecimals(3)
            self.kstep = QDoubleSpinBox(); self.kstep.setRange(0.005, 0.1)
            self.kstep.setSingleStep(0.005); self.kstep.setValue(0.03)
            self.kstep.setDecimals(3)
            for w in (self.nominal, self.kmin, self.kmax, self.kstep):
                w.valueChanged.connect(self.regen)
            form.addRow("Nominal line/space mm:", self.nominal)
            form.addRow("Kerf min mm:", self.kmin)
            form.addRow("Kerf max mm:", self.kmax)
            form.addRow("Kerf step mm:", self.kstep)
            form.addRow(QLabel("Burn + etch once.\nPick the row whose bars and\n"
                               "gaps are EQUAL width (50/50);\nset the main Kerf "
                               "slider to that\nrow's k value."))
            bp = QPushButton("Export PNG"); bp.clicked.connect(self.exp_png)
            bs = QPushButton("Export SVG"); bs.clicked.connect(self.exp_svg)
            form.addRow(bp); form.addRow(bs)
            fw = QWidget(); fw.setLayout(form)
            lay.addWidget(fw, 0)

            self.prev = QSvgWidget(); self.prev.setStyleSheet("background:#333;")
            sa = QScrollArea(); sa.setWidget(self.prev); sa.setWidgetResizable(True)
            lay.addWidget(sa, 1)
            self.regen()

        def _svg(self):
            return build_calibration_svg(
                nominal=self.nominal.value(), kmin=self.kmin.value(),
                kmax=self.kmax.value(), kstep=self.kstep.value(),
                bg=self.par.bg_btn.color(),
                grow_black=self.par.cb_growblack.isChecked())

        def regen(self, *_):
            svg = self._svg()
            self.prev.load(QByteArray(svg.encode()))
            vb = QSvgRenderer(QByteArray(svg.encode())).viewBoxF()
            self.prev.setFixedSize(int(vb.width() * 6), int(vb.height() * 6))

        def exp_png(self):
            fn, _ = QFileDialog.getSaveFileName(self, "Export coupon PNG",
                                                "kerf_cal.png", "PNG (*.png)")
            if fn:
                w, hpx = rasterize_png(self._svg(), self.par.dpi.value(), fn,
                                       0.0, True, self.par.bg_btn.color())
                QMessageBox.information(self, "Saved",
                                        f"{fn}\n{w}×{hpx}px @ {self.par.dpi.value()} DPI")

        def exp_svg(self):
            fn, _ = QFileDialog.getSaveFileName(self, "Export coupon SVG",
                                                "kerf_cal.svg", "SVG (*.svg)")
            if fn:
                Path(fn).write_text(self._svg(), encoding="utf-8")
                QMessageBox.information(self, "Saved", fn)

    class MeasureDialog(QDialog):
        """Compute kerf from a caliper reading and push it to the main slider."""
        def __init__(self, parent):
            super().__init__(parent)
            self.par = parent
            self.setWindowTitle("Set kerf from measurement")
            v = QVBoxLayout(self)
            v.addWidget(QLabel("Burn + etch a feature at kerf 0, measure it with\n"
                               "calipers, enter both numbers:"))
            form = QFormLayout()
            self.nominal = QDoubleSpinBox(); self.nominal.setRange(0.02, 5.0)
            self.nominal.setDecimals(3); self.nominal.setValue(0.30)
            self.measured = QDoubleSpinBox(); self.measured.setRange(0.0, 5.0)
            self.measured.setDecimals(3); self.measured.setValue(0.30)
            form.addRow("Nominal width mm:", self.nominal)
            form.addRow("Measured width mm:", self.measured)
            v.addLayout(form)
            self.rb_black = QRadioButton("I measured a BLACK (burned/copper) feature")
            self.rb_gap = QRadioButton("I measured a GAP (unburned) between features")
            self.rb_black.setChecked(True)
            v.addWidget(self.rb_black); v.addWidget(self.rb_gap)
            for w in (self.nominal, self.measured):
                w.valueChanged.connect(self.calc)
            self.rb_black.toggled.connect(self.calc)
            self.result = QLabel(""); v.addWidget(self.result)
            row = QHBoxLayout()
            ap = QPushButton("Apply to Kerf slider"); ap.clicked.connect(self.apply)
            cl = QPushButton("Cancel"); cl.clicked.connect(self.reject)
            row.addWidget(ap); row.addWidget(cl); v.addLayout(row)
            self.calc()

        def value(self):
            return kerf_from_measurement(self.nominal.value(),
                                         self.measured.value(),
                                         self.rb_black.isChecked())

        def calc(self, *_):
            self.result.setText(f"→ set Kerf = {self.value():+.3f} mm")

        def apply(self):
            self.par.cb_growblack.setChecked(True)
            self.par.kerf.setValue(self.value())
            self.accept()

    class Main(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("gerber2laser — CO2 laser PCB processor")
            self.layers = []
            self._pan = None             # (press point, h scroll, v scroll)
            self._fit_mode = True        # preview tracks the window until you zoom
            self._refit_timer = QTimer(self)
            self._refit_timer.setSingleShot(True)
            self._refit_timer.setInterval(120)
            self._refit_timer.timeout.connect(self._refit_now)

            root = QWidget()
            self.setCentralWidget(root)
            h = QHBoxLayout(root)

            # -------- left: controls --------
            # The controls column is taller than a 768px laptop screen. It lives
            # in its own scroll area so the window has a small minimum height and
            # can be resized / maximised freely.
            leftw = QWidget()
            left = QVBoxLayout(leftw)
            left.setContentsMargins(0, 0, 0, 0)
            self.left_sa = QScrollArea()
            self.left_sa.setWidget(leftw)
            self.left_sa.setWidgetResizable(True)
            self.left_sa.setFrameShape(QFrame.NoFrame)
            self.left_sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.left_sa.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            # width follows the controls; height is free to shrink
            self.left_sa.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
            h.addWidget(self.left_sa, 0)

            orow = QHBoxLayout()
            open_btn = QPushButton("Open Gerber folder…")
            open_btn.setToolTip("Load a folder of Gerber + drill files. Files "
                                "are recognised by name, so a plain KiCad "
                                "export works as-is.")
            open_btn.clicked.connect(self.open_gerbers)
            zip_btn = QPushButton("Open zip…")
            zip_btn.setToolTip("Load a Gerber zip straight from the fab export "
                               "— no need to unpack it first.")
            zip_btn.clicked.connect(self.open_zip)
            orow.addWidget(open_btn)
            orow.addWidget(zip_btn)
            left.addLayout(orow)
            self.path_lbl = QLabel("No Gerbers loaded.")
            self.path_lbl.setWordWrap(True)
            self.path_lbl.setToolTip("The folder or zip currently loaded.")
            left.addWidget(self.path_lbl)

            self.layer_box = QGroupBox("Layers  (tick = include, box = colour)")
            self.layer_box.setToolTip(
                "Which layers go into the output, and in what colour. Colour "
                "is how LightBurn tells feature classes apart: one colour "
                "becomes one cut layer.")
            self.layer_v = QVBoxLayout(self.layer_box)
            left.addWidget(self.layer_box)

            orient = QGroupBox("Side orientation")
            ov = QVBoxLayout(orient)
            self.side = QComboBox()
            self.side.addItems(["Front side (as viewed)",
                                "Back side (mirror to cut flipped)"])
            self.side.setToolTip(
                "You always laser the side facing up. The back side has to be "
                "mirrored so it lines up once you flip the board over — "
                "picking Back sets that mirror for you.")
            self.side.currentIndexChanged.connect(self.side_preset)
            ov.addWidget(self.side)
            self.cb_mh = QCheckBox("Mirror horizontal")
            self.cb_mv = QCheckBox("Mirror vertical")
            self.cb_mh.setToolTip("Flip left-right about the board centre. "
                                  "Get this wrong and the board etches "
                                  "reversed.")
            self.cb_mv.setToolTip("Flip top-bottom about the board centre. "
                                  "Rarely needed — usually you want rotation "
                                  "instead.")
            self.cb_mh.stateChanged.connect(self.refresh)
            self.cb_mv.stateChanged.connect(self.refresh)
            ov.addWidget(self.cb_mh)
            ov.addWidget(self.cb_mv)
            rrow = QHBoxLayout()
            rrow.addWidget(QLabel("Rotate:"))
            self.rot = QComboBox()
            self.rot.addItems(["0°", "90°", "180°", "270°"])
            self.rot.setToolTip("Turn the artwork to suit how the blank sits "
                                "on the laser bed.")
            self.rot.currentIndexChanged.connect(self.refresh)
            rrow.addWidget(self.rot)
            ov.addLayout(rrow)
            self.fix_btn = QPushButton("Fix orientation")
            self.fix_btn.setToolTip(
                "Set the mirror the way the enabled copper layer needs it, "
                "using the banner's verdict.")
            self.fix_btn.clicked.connect(self.fix_orientation)
            ov.addWidget(self.fix_btn)
            left.addWidget(orient)

            out = QGroupBox("Output")
            og = QVBoxLayout(out)
            self.cb_invert = QCheckBox("Invert (black = gaps instead of traces)")
            self.cb_invert.setToolTip(
                "Swaps every black layer with white against the burn field. "
                "Feature-class colours (Edge.Cuts red, mask green, ...) and "
                "any colour you picked by hand ride through unchanged.")
            self.cb_invert.stateChanged.connect(self.refresh)
            og.addWidget(self.cb_invert)
            brow = QHBoxLayout()
            brow.addWidget(QLabel("Background:"))
            self.bg_btn = ColorButton("white", self.refresh)
            self.bg_btn.setToolTip(
                "Page colour behind everything, and what the PNG is flattened "
                "onto. White = laser holds off, so it is the safe default.")
            brow.addWidget(self.bg_btn)
            og.addLayout(brow)
            mbrow = QHBoxLayout()
            mbrow.addWidget(QLabel("Margin area (invert):"))
            self.margin_bg_btn = ColorButton("white", self.refresh)
            self.margin_bg_btn.setToolTip(
                "Inverted only: the ring around the board. Keep it white so "
                "you burn the board area rather than the whole sheet of "
                "paint.")
            mbrow.addWidget(self.margin_bg_btn)
            og.addLayout(mbrow)
            hrow = QHBoxLayout()
            hrow.addWidget(QLabel("Hole marks:"))
            self.hole_size = QComboBox()
            for d in DRILL_SIZES:
                self.hole_size.addItem("As drilled" if d == 0 else f"{d:g} mm", d)
            self.hole_size.currentIndexChanged.connect(self.refresh)
            hrow.addWidget(self.hole_size, 1)
            hrow.addWidget(QLabel("×"))
            self.hole_scale = QDoubleSpinBox()
            self.hole_scale.setRange(0.05, 10.0)
            self.hole_scale.setSingleStep(0.05)
            self.hole_scale.setDecimals(2)
            self.hole_scale.setValue(1.0)
            self.hole_scale.valueChanged.connect(self.refresh)
            hrow.addWidget(self.hole_scale)
            og.addLayout(hrow)
            for w in (self.hole_size, self.hole_scale):
                w.setToolTip(
                    "Size of the drill marks on the Drills layer. \"As "
                    "drilled\" keeps each hole's real diameter; a fixed size "
                    "makes every hole the same dot. The multiplier applies on "
                    "top — shrink the marks to a centre punch, or open them up "
                    "so the laser actually clears them.")
            self.cb_reg = QCheckBox("Registration crosshairs (front/back align)")
            self.cb_reg.stateChanged.connect(self.refresh)
            og.addWidget(self.cb_reg)
            regrow = QHBoxLayout()
            regrow.addWidget(QLabel("size/offset mm:"))
            self.reg_size = QDoubleSpinBox()
            self.reg_size.setRange(0.5, 20); self.reg_size.setValue(3.0)
            self.reg_size.valueChanged.connect(self.refresh)
            self.reg_off = QDoubleSpinBox()
            self.reg_off.setRange(0.0, 20); self.reg_off.setValue(2.0)
            self.reg_off.valueChanged.connect(self.refresh)
            regrow.addWidget(self.reg_size)
            regrow.addWidget(self.reg_off)
            og.addLayout(regrow)
            for w in (self.cb_reg, self.reg_size, self.reg_off):
                w.setToolTip(
                    "Crosshairs just outside the board corners, in board "
                    "coordinates, so they land on the same physical spot on "
                    "both sides once you flip it — burn them on both sides "
                    "and use them to align a two-sided board.\nThe two boxes "
                    "are the arm length and how far out from the corner they "
                    "sit, in mm.")
            drow = QHBoxLayout()
            drow.addWidget(QLabel("PNG DPI:"))
            self.dpi = QSpinBox()
            self.dpi.setRange(100, 4000)
            self.dpi.setValue(1000)
            self.dpi.setToolTip(
                "Resolution of the PNG export and the raster preview. It is "
                "written into the PNG, so LightBurn places the image at true "
                "board size — 1000 DPI is 0.025 mm per pixel, finer than the "
                "beam.")
            drow.addWidget(self.dpi)
            og.addLayout(drow)
            krow = QHBoxLayout()
            self.kerf_lbl = QLabel("Kerf/expand mm:")
            krow.addWidget(self.kerf_lbl)
            self.kerf = QDoubleSpinBox()
            self.kerf.setRange(-1.0, 1.0)
            self.kerf.setSingleStep(0.02)
            self.kerf.setDecimals(3)
            self.kerf.setValue(0.0)
            self.kerf.valueChanged.connect(self.refresh)
            krow.addWidget(self.kerf)
            og.addLayout(krow)
            self.cb_growblack = QCheckBox("Expand grows the BLACK (burn) region")
            self.cb_growblack.setChecked(True)
            self.cb_growblack.stateChanged.connect(self.refresh)
            og.addWidget(self.cb_growblack)
            for w in (self.kerf_lbl, self.kerf, self.cb_growblack):
                w.setToolTip("Kerf is raster morphology: applied to PNG export "
                             "and the Raster preview. The vector preview and "
                             "SVG export never include it.\nIt is per edge: "
                             "0.05 here moves every black boundary 0.05 mm, so "
                             "a bar gets 0.1 mm wider.")
            prow = QHBoxLayout()
            prow.addWidget(QLabel("Preview mode:"))
            self.pmode = QComboBox()
            self.pmode.addItems(["Vector (fast)", "Raster (true DPI + kerf)"])
            self.pmode.setToolTip(
                "Vector redraws instantly and is what the SVG export looks "
                "like. Raster is slower but shows the actual pixels the PNG "
                "will have, kerf included.")
            self.pmode.currentIndexChanged.connect(self.refresh)
            prow.addWidget(self.pmode)
            og.addLayout(prow)
            mrow = QHBoxLayout()
            mrow.addWidget(QLabel("Margin mm:"))
            self.margin = QDoubleSpinBox()
            self.margin.setRange(0, 20)
            self.margin.setValue(2.0)
            self.margin.setToolTip(
                "Blank border added around the board. Useful as breathing room "
                "when positioning on the laser; 0 crops tight to the artwork.")
            self.margin.valueChanged.connect(self.refresh)
            mrow.addWidget(self.margin)
            og.addLayout(mrow)
            sbrow = QHBoxLayout()
            self.cb_scale = QCheckBox("Scale bar")
            self.cb_scale.setChecked(True)   # size check is worth having by default
            self.cb_scale.stateChanged.connect(self.refresh)
            sbrow.addWidget(self.cb_scale)
            sbrow.addWidget(QLabel("mm:"))
            self.scale_len = QDoubleSpinBox()
            self.scale_len.setRange(5, 100)
            self.scale_len.setDecimals(0)
            self.scale_len.setValue(10.0)
            self.scale_len.valueChanged.connect(self.refresh)
            sbrow.addWidget(self.scale_len)
            self.scale_color = ColorButton("#ff00ff", self.refresh)
            sbrow.addWidget(self.scale_color)
            og.addLayout(sbrow)
            for w in (self.cb_scale, self.scale_len, self.scale_color):
                w.setToolTip(
                    "True-length ruler below the board, in its own colour and "
                    "its own SVG group (id=\"scale-bar\").\nIn LightBurn map "
                    "its colour to a no-output layer (or delete the group) so "
                    "it is not cut; measure it on the exported file or a test "
                    "burn to verify absolute size.")
            left.addWidget(out)

            cal = QGroupBox("Calibration")
            cv = QVBoxLayout(cal)
            b_coupon = QPushButton("Kerf calibration coupon…")
            b_coupon.setToolTip(
                "Build a one-burn test piece: a kerf sweep, a fineness ladder "
                "and a scale bar. Burn it, measure it, and you know what this "
                "laser actually does to line widths.")
            b_coupon.clicked.connect(self.open_coupon)
            b_meas = QPushButton("Set kerf from measurement…")
            b_meas.setToolTip(
                "Type in what a feature should have been and what you "
                "measured on the burn; it works out the kerf value and sets "
                "it for you.")
            b_meas.clicked.connect(self.open_measure)
            cv.addWidget(b_coupon)
            cv.addWidget(b_meas)
            left.addWidget(cal)

            exp = QHBoxLayout()
            b1 = QPushButton("Export PNG")
            b1.setToolTip(
                "Raster export for engraving. Carries its DPI, so LightBurn "
                "places it at true board size — kerf is baked in here.")
            b1.clicked.connect(self.export_png)
            b2 = QPushButton("Export SVG")
            b2.setToolTip(
                "Vector export, exact geometry, one colour per feature class "
                "for LightBurn's colour→layer mapping. Never contains kerf: "
                "let LightBurn offset it.")
            b2.clicked.connect(self.export_svg)
            exp.addWidget(b1)
            exp.addWidget(b2)
            left.addLayout(exp)
            left.addStretch(1)

            # -------- right: preview --------
            rightw = QVBoxLayout()
            h.addLayout(rightw, 1)
            phdr = QHBoxLayout()
            phdr.addWidget(QLabel("Preview (black = laser fires):"))
            phdr.addStretch(1)
            # zoom: px per mm. None until first Fit.
            self.zoom = None
            zo = QPushButton("−"); zo.setFixedWidth(28)
            zo.setToolTip("Zoom out")
            zo.clicked.connect(lambda: self.zoom_step(1 / 1.25))
            zi = QPushButton("+"); zi.setFixedWidth(28)
            zi.setToolTip("Zoom in")
            zi.clicked.connect(lambda: self.zoom_step(1.25))
            self.zslider = QSlider(Qt.Horizontal)
            self.zslider.setRange(10, 4000)          # 0.10 .. 40 px/mm (×100)
            self.zslider.setFixedWidth(140)
            self.zslider.setToolTip("Zoom, in preview pixels per mm.")
            self.zslider.valueChanged.connect(self._zslider_changed)
            fit = QPushButton("Fit")
            fit.setToolTip("Fit the whole board in the window, and keep it "
                           "fitted as the window is resized.")
            fit.clicked.connect(self.fit_preview)
            phdr.addWidget(QLabel("Zoom"))
            phdr.addWidget(zo)
            phdr.addWidget(self.zslider)
            phdr.addWidget(zi)
            phdr.addWidget(fit)
            rightw.addLayout(phdr)

            self.banner = QLabel("Load a Gerber folder or zip to begin.")
            self.banner.setWordWrap(True)
            self.banner.setStyleSheet(
                "padding:6px; border-radius:4px; font-weight:bold; "
                "background:#444; color:#ddd;")
            rightw.addWidget(self.banner)

            self.preview = QSvgWidget()          # vector preview
            self.preview.setStyleSheet("background:#333;")
            self.raster = QLabel()               # true-DPI raster preview
            self.raster.setAlignment(Qt.AlignCenter)
            self.raster.setStyleSheet("background:#333;")
            self.raster.hide()
            holder = QWidget()
            hv = QVBoxLayout(holder)
            hv.setContentsMargins(0, 0, 0, 0)
            hv.setAlignment(Qt.AlignCenter)   # centre art smaller than the viewport
            hv.addWidget(self.preview)
            hv.addWidget(self.raster)
            self.sa = QScrollArea()
            self.sa.setWidget(holder)
            self.sa.setWidgetResizable(True)
            self.sa.viewport().installEventFilter(self)   # zoom + drag-to-pan
            self.sa.viewport().setCursor(Qt.OpenHandCursor)
            self.sa.setToolTip(
                "Drag to pan. Scroll wheel zooms about the pointer (hold Ctrl "
                "for bigger steps). Double-click zooms in, Ctrl+double-click "
                "zooms out.")
            rightw.addWidget(self.sa, 1)
            self.info = QLabel("")
            rightw.addWidget(self.info)

            # Controls column keeps its natural width; everything else is free
            # to shrink, so the window fits any screen and maximises properly.
            self._leftw = leftw
            self._fit_left_width()
            self._size_to_screen()
            self.refresh()   # sets initial kerf enabled/greyed state

        def _fit_left_width(self):
            """Size the controls column to its content. Must be re-run after
            the layer rows are (re)built — they are wider than the empty
            group box the initial hint was measured on, and the scroll area
            clips anything past its fixed width."""
            sb = self.left_sa.verticalScrollBar().sizeHint().width()
            lw = self._leftw.sizeHint().width() + sb + 4
            self.left_sa.setFixedWidth(lw)
            self.setMinimumSize(lw + 320, 420)

        def _size_to_screen(self):
            """Open at a sane size that always fits the available desktop."""
            scr = self.screen() or QApplication.primaryScreen()
            av = scr.availableGeometry()
            self.resize(min(1100, av.width() - 40), min(760, av.height() - 40))
            fg = self.frameGeometry()
            fg.moveCenter(av.center())
            self.move(max(av.left(), fg.left()), max(av.top(), fg.top()))

        # ---- data / actions ----
        def _load(self, path):
            try:
                self.layers = load_layers(path)
            except Exception as e:
                QMessageBox.critical(self, "Open failed", str(e))
                return
            self._src_path = Path(path)
            self.path_lbl.setText(str(path))
            # auto-detect which copper side actually has geometry and set the
            # correct cut orientation for it before building the UI.
            side = primary_copper_side(self.layers)
            for L in self.layers:
                if L.key == "top_copper":
                    L.enabled = (side != "bottom")
                elif L.key == "bottom_copper":
                    L.enabled = (side == "bottom")
            self.rebuild_layer_ui()
            self.side.blockSignals(True)
            self.side.setCurrentIndex(1 if side == "bottom" else 0)
            self.side.blockSignals(False)
            self.cb_mh.blockSignals(True)
            self.cb_mh.setChecked(side == "bottom")   # bottom copper -> flip
            self.cb_mh.blockSignals(False)
            self.zoom = None          # re-fit to the newly loaded board
            self._fit_mode = True
            self.refresh()

        def open_gerbers(self):
            d = QFileDialog.getExistingDirectory(self, "Select Gerber folder")
            if d:
                self._load(d)

        def open_zip(self):
            f, _ = QFileDialog.getOpenFileName(self, "Select Gerber zip", "",
                                               "Zip (*.zip);;All files (*)")
            if f:
                self._load(f)

        def rebuild_layer_ui(self):
            while self.layer_v.count():
                it = self.layer_v.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            self._rows = []
            for L in self.layers:
                row = QHBoxLayout()
                cb = QCheckBox(L.name)
                cb.setChecked(L.enabled)
                cb.stateChanged.connect(self.refresh)
                colb = ColorButton(L.color, self.refresh)
                colb.setToolTip(
                    "Drills always take the opposite colour to the copper, so "
                    "each hole reads as a hole either way up. Pick a colour "
                    "here and it is used as-is instead."
                    if _is_drill(L) else
                    "Black and white follow the Invert toggle. Pick a colour "
                    "here and it is used as-is, Invert or not.")
                row.addWidget(cb, 1)
                row.addWidget(colb)
                w = QWidget()
                w.setLayout(row)
                self.layer_v.addWidget(w)
                self._rows.append((L, cb, colb))
            self._fit_left_width()   # rows change the column's natural width

        def fix_orientation(self):
            self.gather_cfg()   # sync L.enabled from the checkboxes
            _ok, need, _msg = orientation_advice(self.layers,
                                                 self.cb_mh.isChecked())
            if need is not None:
                self.cb_mh.setChecked(need)   # triggers refresh
                self.cb_mv.setChecked(False)

        def _update_banner(self):
            ok, _need, msg = orientation_advice(self.layers,
                                                self.cb_mh.isChecked())
            if ok:
                bgc = "#1c3a1c"; fgc = "#8ef08e"    # green
            else:
                bgc = "#4a3a12"; fgc = "#ffd166"    # amber
            self.banner.setStyleSheet(
                f"padding:6px; border-radius:4px; font-weight:bold; "
                f"background:{bgc}; color:{fgc};")
            self.banner.setText(msg)

        def open_coupon(self):
            CalibrationDialog(self).exec()

        def open_measure(self):
            MeasureDialog(self).exec()

        def side_preset(self, idx):
            back = (idx == 1)
            self.cb_mh.setChecked(back)   # back side -> horizontal mirror
            self.cb_mv.setChecked(False)
            self.refresh()

        def _show_resolved_colors(self, cfg):
            """Repaint each un-picked swatch with the colour the layer is
            actually rendered in, so a drill row shows the hole colour it
            derived rather than a stored default nobody chose."""
            got = {id(L): c for L, c in resolve_colors(cfg)}
            for L, _cb, colb in getattr(self, "_rows", []):
                if not colb.picked() and id(L) in got:
                    colb.show_color(got[id(L)])

        def gather_cfg(self):
            for L, cb, colb in getattr(self, "_rows", []):
                L.enabled = cb.isChecked()
                if colb.picked():
                    L.color = colb.color()
                L.color_overridden = colb.picked()
            return RenderCfg(
                layers=self.layers,
                bg=self.bg_btn.color(),
                mirror_h=self.cb_mh.isChecked(),
                mirror_v=self.cb_mv.isChecked(),
                rotate=int(self.rot.currentText().rstrip("°")),
                invert=self.cb_invert.isChecked(),
                invert_margin=self.margin_bg_btn.color(),
                margin_mm=self.margin.value(),
                reg_marks=self.cb_reg.isChecked(),
                reg_size_mm=self.reg_size.value(),
                reg_offset_mm=self.reg_off.value(),
                scale_bar=self.cb_scale.isChecked(),
                scale_bar_mm=self.scale_len.value(),
                scale_bar_color=self.scale_color.color(),
                drill_size_mm=self.hole_size.currentData(),
                drill_scale=self.hole_scale.value(),
            )

        def current_svg(self, merge_invert=True):
            return build_svg(self.gather_cfg(), merge_invert=merge_invert)

        # ---- zoom ----
        def _fit_zoom(self, vb):
            vp = self.sa.viewport().size()
            w, h = max(50, vp.width() - 4), max(50, vp.height() - 4)
            if vb.width() <= 0 or vb.height() <= 0:
                z = 8.0
            else:
                z = min(w / vb.width(), h / vb.height())
            self.zoom = max(0.1, min(40.0, z))
            self.zslider.blockSignals(True)
            self.zslider.setValue(int(self.zoom * 100))
            self.zslider.blockSignals(False)

        def _apply_zoom(self):
            self.zslider.blockSignals(True)
            self.zslider.setValue(int(max(0.1, min(40.0, self.zoom)) * 100))
            self.zslider.blockSignals(False)
            self.refresh()

        def _zslider_changed(self, val):
            self.zoom = val / 100.0
            self._fit_mode = False        # manual zoom wins over auto-fit
            self.refresh()

        def _art_widget(self):
            return self.raster if self.pmode.currentIndex() == 1 else self.preview

        def zoom_step(self, f, anchor=None, center=False):
            """
            Zoom by factor `f`, keeping the spot under `anchor` (a point in the
            viewport) where it is — or moving it to the middle of the view with
            `center`, which is what a double-click on a feature should do.

            The work happens in artwork-relative coordinates rather than raw
            scroll offsets: the art is centred in the scroll area, so as soon
            as it is smaller than the viewport in either axis there is a
            centring gap between scroll 0 and the edge of the art, and that gap
            changes with the zoom. Ignoring it makes the view jump on every
            notch.
            """
            old = self.zoom or 8.0
            new = max(0.1, min(40.0, old * f))
            self._fit_mode = False
            if anchor is None or abs(new - old) < 1e-9:
                self.zoom = new
                self._apply_zoom()
                return

            art = self._art_widget()
            hb, vbar = self.sa.horizontalScrollBar(), self.sa.verticalScrollBar()
            o = art.mapTo(self.sa.widget(), QPoint(0, 0))
            fx = (hb.value() + anchor.x() - o.x()) / max(1, art.width())
            fy = (vbar.value() + anchor.y() - o.y()) / max(1, art.height())
            fx, fy = min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy))

            vp = self.sa.viewport()
            target = (QPoint(vp.width() // 2, vp.height() // 2) if center
                      else anchor)
            self.zoom = new
            self._apply_zoom()                     # re-renders at the new size
            self.sa.widget().layout().activate()   # sizes + centring now final
            self._place_art_point(fx, fy, target)
            # The scroll area only recomputes its range once the resize has been
            # through the event loop, so zooming out clamps the value we just
            # set against the old, larger range. Place it again a frame later.
            QTimer.singleShot(0, lambda: self._place_art_point(fx, fy, target))

        def _place_art_point(self, fx, fy, target):
            """Scroll so the artwork-relative point (fx, fy) sits at viewport
            point `target`."""
            art = self._art_widget()
            o = art.mapTo(self.sa.widget(), QPoint(0, 0))
            self.sa.horizontalScrollBar().setValue(
                round(o.x() + fx * art.width() - target.x()))
            self.sa.verticalScrollBar().setValue(
                round(o.y() + fy * art.height() - target.y()))

        def fit_preview(self):
            self._fit_mode = True
            if getattr(self, "_vb", None) is not None:
                self._fit_zoom(self._vb)
                self.refresh()

        def resizeEvent(self, ev):
            super().resizeEvent(ev)
            if self._fit_mode and getattr(self, "_vb", None) is not None:
                self._refit_timer.start()      # debounce: raster refit is costly

        def _refit_now(self):
            if self._fit_mode and getattr(self, "_vb", None) is not None:
                self._fit_zoom(self._vb)
                self.refresh()

        def eventFilter(self, obj, ev):
            # only the preview viewport is ours to take over
            if obj is not self.sa.viewport():
                return super().eventFilter(obj, ev)
            # Drag to pan: with the wheel taken over for zoom, dragging is how
            # you get around a board that is bigger than the viewport.
            if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                self._pan = (ev.position().toPoint(),
                             self.sa.horizontalScrollBar().value(),
                             self.sa.verticalScrollBar().value())
                self.sa.viewport().setCursor(Qt.ClosedHandCursor)
                return True
            if ev.type() == QEvent.MouseMove and self._pan:
                start, h0, v0 = self._pan
                d = ev.position().toPoint() - start
                self.sa.horizontalScrollBar().setValue(h0 - d.x())
                self.sa.verticalScrollBar().setValue(v0 - d.y())
                return True
            if ev.type() == QEvent.MouseButtonRelease and self._pan:
                self._pan = None
                self.sa.viewport().setCursor(Qt.OpenHandCursor)
                return True
            # The preview is a canvas, not a document: the wheel zooms rather
            # than scrolls, and Ctrl makes each notch a coarser step.
            if ev.type() == QEvent.Wheel:
                d = ev.angleDelta().y()
                if d:
                    step = 1.4 if (ev.modifiers() & Qt.ControlModifier) else 1.15
                    self.zoom_step(step if d > 0 else 1 / step,
                                   anchor=ev.position().toPoint())
                return True
            if ev.type() == QEvent.MouseButtonDblClick:
                out = bool(ev.modifiers() & Qt.ControlModifier)
                # a double-click means "look here", so bring that spot to the
                # middle of the view rather than leaving it under the pointer
                self._pan = None
                self.sa.viewport().setCursor(Qt.OpenHandCursor)
                self.zoom_step(1 / 1.6 if out else 1.6,
                               anchor=ev.position().toPoint(), center=True)
                return True
            return super().eventFilter(obj, ev)

        def refresh(self, *_):
            # kerf is raster-only — grey it out while the vector preview is up
            raster_mode = self.pmode.currentIndex() == 1
            for w in (self.kerf_lbl, self.kerf, self.cb_growblack):
                w.setEnabled(raster_mode)
            if not self.layers:
                return
            try:
                cfg = self.gather_cfg()
                # preview is Qt: painter composition, not the merged path
                svg = build_svg(cfg, merge_invert=False)
            except Exception as e:
                self.info.setText(f"⚠ {e}")
                return
            self._show_resolved_colors(cfg)
            self._update_banner()
            # viewBoxF needs a full parse, so hand QtSvg the thinned copy —
            # the raw SVG's oversize pour paths make it log
            # "qt.svg: Invalid path data; path truncated"
            #
            # _thin_poly warns when a path is STILL over the vertex cap after
            # thinning: QtSvg will drop it, so a zone pour can be missing from
            # the preview and from the PNG. That warning goes to stderr, which
            # a GUI user never sees, and a silently vanished pour is the one
            # failure this module exists to prevent — so catch it here and say
            # so on the info line. The CLI keeps its stderr warning untouched.
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                qt_svg = svg_for_qt(svg)
            n_drop = sum(1 for c in caught
                         if issubclass(c.category, RuntimeWarning))
            drop_note = ""
            if n_drop:
                drop_note = (
                    "  |  ⚠ %d path%s too complex for the preview renderer "
                    "(likely a zone pour) — may be missing here and from the "
                    "PNG; the exported SVG is complete."
                    % (n_drop, "" if n_drop == 1 else "s"))
            vb = QSvgRenderer(QByteArray(qt_svg.encode())).viewBoxF()
            self._vb = vb
            if self.zoom is None:            # first render: fit to the viewport
                self._fit_zoom(vb)
            z = self.zoom
            dims = (f"Board ~ {vb.width():.1f} × {vb.height():.1f} mm  "
                    f"| {z:.1f} px/mm")
            dw = max(1, int(vb.width() * z))
            dh = max(1, int(vb.height() * z))
            if self.pmode.currentIndex() == 1:      # raster true-DPI
                self.preview.hide()
                self.raster.show()
                dpi = self.dpi.value()
                maxpx = 1600
                wpx = vb.width() / MM * dpi
                eff = dpi * maxpx / wpx if wpx > maxpx else dpi
                img = rasterize_qimage(svg, int(eff), self.kerf.value(),
                                       self.cb_growblack.isChecked(),
                                       self.bg_btn.color())
                pix = QPixmap.fromImage(img).scaled(
                    dw, dh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.raster.setPixmap(pix)
                self.raster.setFixedSize(pix.size())
                capped = " (preview capped; export uses %d)" % dpi if eff < dpi else ""
                self.info.setText(f"{dims} | raster {img.width()}×{img.height()}px "
                                  f"@~{int(eff)} DPI{capped}{drop_note}")
            else:                                    # vector (scales crisply)
                self.raster.hide()
                self.preview.show()
                self.preview.load(QByteArray(qt_svg.encode()))
                self.preview.setFixedSize(dw, dh)
                if abs(self.kerf.value()) > 1e-9:
                    dims += ("  |  ⚠ kerf is raster-only — switch to "
                             "Raster preview to see it")
                self.info.setText(dims + drop_note)

        def _default_savename(self, ext):
            """Next to the source, named for the settings that produced it —
            side, invert, mirror, kerf, DPI, hole size and so on."""
            src = getattr(self, "_src_path", None)
            stem = src.stem if src else "board"
            folder = (src.parent if src and src.is_file() else src) \
                if src else Path.cwd()
            raster = (ext == "png")
            tags = settings_tags(
                self.gather_cfg(),
                side=("back" if self.side.currentIndex() == 1 else "front"),
                dpi=self.dpi.value() if raster else None,
                kerf=self.kerf.value() if raster else None)
            return str(folder / export_filename(stem, tags, ext))

        def export_png(self):
            if not self.layers:
                return
            fn, _ = QFileDialog.getSaveFileName(self, "Export PNG",
                                                self._default_savename("png"),
                                                "PNG (*.png)")
            if not fn:
                return
            # Kerf morphology at full export DPI is seconds of whole-image
            # blitting during which Qt paints nothing — the window greys out
            # and looks hung. A modal bar fed from the morph loop, pumping the
            # event queue once per disc row (~4 blits), keeps it alive for
            # negligible cost. Built lazily, so a kerf-free export shows no
            # dialog at all. Deliberately no Cancel button: the loop builds one
            # disc and a half-built disc is simply the wrong kerf, so there is
            # nothing safe to hand back part-way through.
            dlg = None

            def tick(done, total):
                nonlocal dlg
                if dlg is None:
                    dlg = QProgressDialog("Applying kerf compensation…", "",
                                          0, total, self)
                    dlg.setCancelButton(None)
                    dlg.setWindowTitle("Exporting PNG")
                    dlg.setWindowModality(Qt.WindowModal)
                    dlg.setMinimumDuration(0)
                dlg.setValue(done)
                QApplication.processEvents()

            try:
                dpi = self.dpi.value()
                try:
                    # raster: painter composition (see build_svg)
                    w, hpx = rasterize_png(self.current_svg(False), dpi, fn,
                                           self.kerf.value(),
                                           self.cb_growblack.isChecked(),
                                           self.bg_btn.color(), tick)
                finally:
                    if dlg is not None:
                        dlg.close()
                QMessageBox.information(
                    self, "Saved",
                    f"{fn}\n{w} × {hpx} px @ {dpi} DPI\n"
                    f"= {w / dpi * MM:.2f} × {hpx / dpi * MM:.2f} mm\n\n"
                    "The DPI is written into the file, so LightBurn places it "
                    "at exactly this size on import — don't rescale it.")
            except Exception as e:
                QMessageBox.critical(self, "Export failed", str(e))

        def export_svg(self):
            if not self.layers:
                return
            fn, _ = QFileDialog.getSaveFileName(self, "Export SVG",
                                                self._default_savename("svg"),
                                                "SVG (*.svg)")
            if not fn:
                return
            try:
                Path(fn).write_text(self.current_svg(), encoding="utf-8")
                QMessageBox.information(self, "Saved", fn)
            except Exception as e:
                QMessageBox.critical(self, "Export failed", str(e))

    app = QApplication(sys.argv)
    win = Main()
    win.show()
    if preload:
        win._load(preload)
    sys.exit(app.exec())


def _apply_side(layers, side):
    """Enable the layers appropriate for a side; return (mirror_h,)."""
    if side is None:
        return False
    want = ({"top_copper", "outline"} if side == "front"
            else {"bottom_copper", "outline"})
    for L in layers:
        L.enabled = L.key in want
    return side == "back"     # back gets mirrored


def run_cli(a):
    """Headless export — no display needed. Returns 0 on success."""
    if a.coupon:
        svg = build_calibration_svg(nominal=a.coupon_nominal, kmin=a.kmin,
                                    kmax=a.kmax, kstep=a.kstep, bg=a.bg,
                                    grow_black=not a.grow_white)
        raster_svg = svg
        board = f"coupon nominal {a.coupon_nominal}mm"
    else:
        layers = load_layers(a.input)
        mirror_h = _apply_side(layers, a.side)
        # --side is a layer selection, not the GUI's auto-detection: asking for
        # a side this export has no copper for is silently an outline-only
        # file, which is a wasted burn. Say so and name the side that is here.
        if a.side:
            want = "top_copper" if a.side == "front" else "bottom_copper"
            if not any(L.key == want and L.obj is not None for L in layers):
                have = primary_copper_side(layers)
                where = ({"top": "F.Cu (--side front)",
                          "bottom": "B.Cu (--side back)"}.get(have)
                         or "no copper layer at all")
                print(f"warning: --side {a.side} enables {want}, which is not "
                      f"in {a.input} — this export has {where}. The output "
                      f"will contain no copper.", file=sys.stderr)
        if a.layers:
            keys = {k.strip() for k in a.layers.split(",")}
            for L in layers:
                L.enabled = (L.key in keys) or any(k in L.key for k in keys)
        for spec in getattr(a, "layer_color", []) or []:
            key, _, col = spec.partition("=")
            key, col = key.strip(), col.strip()
            if not col:
                raise SystemExit(f"--layer-color needs KEY=COLOUR, got {spec!r}")
            hit = [L for L in layers if L.key == key]
            if not hit:
                raise SystemExit(f"--layer-color: no layer {key!r} "
                                 f"(have: {', '.join(L.key for L in layers)})")
            for L in hit:
                L.color = col
                L.color_overridden = True   # honoured as-is, even with --invert
        cfg = RenderCfg(
            layers=layers, bg=a.bg,
            mirror_h=mirror_h or a.mirror_h, mirror_v=a.mirror_v,
            rotate=a.rotate, invert=a.invert, invert_margin=a.invert_margin,
            margin_mm=a.margin,
            reg_marks=a.reg, reg_size_mm=a.reg_size, reg_offset_mm=a.reg_offset,
            scale_bar=a.scale_bar, scale_bar_mm=a.scale_bar_mm,
            scale_bar_color=a.scale_bar_color,
            drill_size_mm=a.drill_size, drill_scale=a.drill_scale,
        )
        # The two consumers want different forms of Invert (see build_svg):
        # the file gets the merged even-odd field LightBurn can read, the
        # rasterizer gets the painter composition Qt can draw.
        svg = build_svg(cfg)
        raster_svg = (build_svg(cfg, merge_invert=False) if a.png and cfg.invert
                      else svg)
        board = ", ".join(L.name for L in layers if L.enabled)

    if a.svg:
        Path(a.svg).write_text(svg, encoding="utf-8")
        print(f"wrote {a.svg}  [{board}]")
    if a.png:
        # PNG needs Qt for rasterization
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        w, hpx = rasterize_png(raster_svg, a.dpi, a.png, a.kerf,
                               not a.grow_white, a.bg)
        print(f"wrote {a.png}  {w}x{hpx}px @ {a.dpi} DPI = "
              f"{w / a.dpi * MM:.2f} x {hpx / a.dpi * MM:.2f} mm  [{board}]")
    if not a.svg and not a.png:
        print("Nothing to do: pass --png and/or --svg (or drop flags for GUI).")
        return 1
    return 0


# --------------------------------------------------------------------------- #
#  KiCad action plugin
#
#  Drop this file (or a symlink) into KiCad's scripting/plugins folder and a
#  "Gerber → Laser" button appears in pcbnew. It plots the open board's
#  Gerbers + drills to a temp folder, then launches this same script's GUI in
#  an EXTERNAL Python (KiCad's bundled Python has no PySide6). Point the
#  G2L_PYTHON env var at a python.exe that has pyside6+gerbonara installed;
#  otherwise the first `python` on PATH is tried.
# --------------------------------------------------------------------------- #

try:
    import pcbnew as _pcbnew
except ImportError:
    _pcbnew = None


def _external_python():
    """
    Find a Python that can run the GUI. KiCad's own interpreter has neither
    PySide6 nor gerbonara, so the plugin hands off to an outside one.

    Checked in order: the G2L_PYTHON env var, a `.g2l_python` file dropped
    next to this script (that is how the installers pin their venv, since a
    plugin cannot set KiCad's environment), then plain `python` on PATH.
    """
    env = os.environ.get("G2L_PYTHON")
    if env:
        return env
    pin = Path(__file__).resolve().parent / ".g2l_python"
    try:
        if pin.is_file():
            p = pin.read_text(encoding="utf-8").strip()
            if p and Path(p).exists():
                return p
    except OSError:
        pass
    return shutil.which("python") or shutil.which("py")


def _plugin_icon():
    """Toolbar icon for the KiCad action, if the assets came along."""
    here = Path(__file__).resolve().parent
    for cand in (here / "gerber2laser_24.png",
                 here / "assets" / "gerber2laser_24.png"):
        if cand.is_file():
            return str(cand)
    return ""


def _plot_board_gerbers(board, out_dir):
    """Plot the standard laser-relevant layers + drills via the pcbnew API."""
    pc = _pcbnew.PLOT_CONTROLLER(board)
    po = pc.GetPlotOptions()
    po.SetOutputDirectory(out_dir)
    po.SetPlotFrameRef(False)
    po.SetMirror(False)
    po.SetUseAuxOrigin(False)
    wanted = [
        ("F_Cu", "F_Cu"), ("B_Cu", "B_Cu"), ("Edge_Cuts", "Edge_Cuts"),
        ("F_Mask", "F_Mask"), ("B_Mask", "B_Mask"),
        ("F_SilkS", "F_Silkscreen"), ("B_SilkS", "B_Silkscreen"),
    ]
    for attr, suffix in wanted:
        layer_id = getattr(_pcbnew, attr, None)
        if layer_id is None:
            continue
        pc.SetLayer(layer_id)
        pc.OpenPlotfile(suffix, _pcbnew.PLOT_FORMAT_GERBER, suffix)
        pc.PlotLayer()
    pc.ClosePlot()
    ew = _pcbnew.EXCELLON_WRITER(board)
    ew.SetFormat(True)                                  # metric
    ew.CreateDrillandMapFilesSet(out_dir, True, False)  # drills, no map


if _pcbnew is not None:
    class Gerber2LaserAction(_pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "Gerber → Laser (CO2 etch art)"
            self.category = "Fabrication"
            self.description = ("Plot this board's Gerbers and open them in "
                                "gerber2laser")
            self.show_toolbar_button = True
            icon = _plugin_icon()
            if icon:
                self.icon_file_name = icon
                self.dark_icon_file_name = icon

        def Run(self):
            import subprocess
            board = _pcbnew.GetBoard()
            out = tempfile.mkdtemp(prefix="g2l_kicad_")
            try:
                _plot_board_gerbers(board, out)
                py = _external_python()
                if not py:
                    raise FileNotFoundError("no external Python found on PATH")
                # Both children are console executables, so Windows pops a
                # black cmd window: a flash for the probe, and one that sits
                # behind the GUI for its whole life. CREATE_NO_WINDOW is a
                # Windows-only flag, hence getattr — on macOS/Linux the
                # fallback 0 means "no extra creation flags", which is what
                # those platforms want anyway.
                no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                deps = subprocess.run(
                    [py, "-c", "import PySide6, gerbonara"],
                    capture_output=True, timeout=60,
                    creationflags=no_window)
                if deps.returncode != 0:
                    raise RuntimeError(
                        f"{py} lacks pyside6/gerbonara "
                        f"(pip install pyside6 gerbonara), or set G2L_PYTHON")
                subprocess.Popen([py, os.path.abspath(__file__),
                                  "--input", out],
                                 creationflags=no_window)
            except Exception as e:
                try:
                    import wx
                    wx.MessageBox(
                        f"Gerbers plotted to:\n{out}\n\n"
                        f"Could not launch the gerber2laser GUI:\n{e}\n\n"
                        f"Run it manually:\n"
                        f'python "{os.path.abspath(__file__)}" --input "{out}"',
                        "gerber2laser")
                except Exception:
                    pass

    try:
        Gerber2LaserAction().register()
    except Exception:
        pass          # imported outside a live pcbnew session


def main():
    p = argparse.ArgumentParser(
        description="KiCad Gerbers -> CO2-laser art (resist etch). "
                    "No args = GUI.")
    p.add_argument("--input", help="Gerber folder or .zip")
    p.add_argument("--side", choices=["front", "back"],
                   help="front=top copper; back=bottom copper + mirror")
    p.add_argument("--layers", help="comma keys to enable, e.g. "
                   "top_copper,outline (overrides --side selection)")
    p.add_argument("--invert", action="store_true",
                   help="black = gaps instead of traces; swaps black<->white "
                        "layers, other feature-class colours are untouched")
    p.add_argument("--layer-color", dest="layer_color", action="append",
                   metavar="KEY=COLOUR", default=[],
                   help="force one layer's colour, e.g. drill1=black — used "
                        "as-is even with --invert, and for a drill layer it "
                        "replaces the colour derived from the copper "
                        "(repeatable)")
    p.add_argument("--invert-margin", dest="invert_margin", default="white",
                   help="margin/offset area colour when inverted "
                        "(default white = don't burn the margin)")
    p.add_argument("--mirror-h", dest="mirror_h", action="store_true")
    p.add_argument("--mirror-v", dest="mirror_v", action="store_true")
    p.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    p.add_argument("--kerf", type=float, default=0.0,
                   help="per-edge kerf mm (raster/PNG only)")
    p.add_argument("--grow-white", action="store_true",
                   help="kerf grows the copper-kept region instead of the burn")
    p.add_argument("--dpi", type=int, default=1000)
    p.add_argument("--margin", type=float, default=2.0)
    p.add_argument("--bg", default="white")
    p.add_argument("--reg", action="store_true",
                   help="add corner registration crosshairs")
    p.add_argument("--reg-size", dest="reg_size", type=float, default=3.0)
    p.add_argument("--reg-offset", dest="reg_offset", type=float, default=2.0)
    p.add_argument("--scale-bar", dest="scale_bar", action="store_true",
                   default=True,
                   help="true-length scale bar below the board, in its own "
                        "colour / SVG group (id=\"scale-bar\") so it can be "
                        "mapped to a no-output layer or deleted (on by "
                        "default)")
    p.add_argument("--no-scale-bar", dest="scale_bar", action="store_false",
                   help="leave the scale bar off")
    p.add_argument("--drill-size", dest="drill_size", type=float, default=0.0,
                   help="force every drill mark to this diameter in mm "
                        "(default 0 = keep the drilled size)")
    p.add_argument("--drill-scale", dest="drill_scale", type=float, default=1.0,
                   help="multiply drill mark diameters by this (default 1.0)")
    p.add_argument("--scale-bar-mm", dest="scale_bar_mm", type=float,
                   default=10.0, help="scale bar length in mm (default 10)")
    p.add_argument("--scale-bar-color", dest="scale_bar_color",
                   default="#ff00ff",
                   help="scale bar colour (default magenta #ff00ff)")
    p.add_argument("--png", help="output PNG path")
    p.add_argument("--svg", help="output SVG path")
    p.add_argument("--coupon", action="store_true",
                   help="generate a kerf calibration coupon instead of a board")
    p.add_argument("--coupon-nominal", dest="coupon_nominal", type=float,
                   default=0.30)
    p.add_argument("--kmin", type=float, default=-0.15)
    p.add_argument("--kmax", type=float, default=0.15)
    p.add_argument("--kstep", type=float, default=0.03)
    a = p.parse_args()

    if a.coupon or (a.input and (a.png or a.svg)):
        sys.exit(run_cli(a))
    if a.png or a.svg:
        # otherwise the output flags are silently dropped and the GUI opens
        p.error("--png/--svg need something to render: pass --input "
                "(a Gerber folder or zip) or --coupon")
    # --input with no output flags = open the GUI preloaded (KiCad plugin path)
    run_gui(a.input)


if __name__ == "__main__":
    main()
