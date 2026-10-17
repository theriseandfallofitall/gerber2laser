"""
SVG composition and the QtSvg path-size workaround.

The structural assertions here are the ones LightBurn depends on: one flat root
<svg>, features FILLED rather than stroked, Edge.Cuts kept as a centreline, and
the scale bar isolated in its own group. Breaking any of them produces a file
that still opens fine in a browser and imports as nothing usable.
"""

import math
import re

import pytest

import gerber2laser as g2l
from gerber2laser import (
    QT_PATH_VERT_LIMIT,
    RenderCfg,
    build_svg,
    build_calibration_svg,
    load_layers,
    svg_for_qt,
)


# --------------------------------------------------------------------------- #
#  svg_for_qt — thinning oversized paths so QtSvg does not drop them
# --------------------------------------------------------------------------- #

def _poly_path(n, closed=False, scale=0.01):
    """An M/L path with n vertices, wandering enough that RDP cannot collapse
    it to a straight line."""
    pts = []
    for i in range(n):
        x = i * scale
        y = math.sin(i * 0.7) * 5.0
        pts.append(f"{x:.4f} {y:.4f}")
    d = "M " + " L ".join(pts) + (" Z" if closed else "")
    return f'<path d="{d}" fill="black"/>'


def _count_verts(svg):
    d = re.search(r'\bd="([^"]+)"', svg).group(1)
    return len(re.findall(r"[ML]", d))


def test_oversized_path_is_thinned_below_the_cap():
    n = QT_PATH_VERT_LIMIT + 5000
    out = svg_for_qt(_poly_path(n))
    assert _count_verts(out) <= QT_PATH_VERT_LIMIT


def test_small_path_is_returned_byte_identical():
    """Thinning must not touch geometry that Qt can already render."""
    src = _poly_path(50)
    assert svg_for_qt(src) == src


def test_curved_paths_are_left_alone():
    """The parser only understands M/L/Z; anything with arcs or cubics must be
    passed through untouched rather than mangled."""
    d = "M 0 0 C 1 1 2 2 3 3 A 1 1 0 0 1 4 4 " + \
        " ".join(f"L {i} {i}" for i in range(QT_PATH_VERT_LIMIT + 2000))
    src = f'<path d="{d}" fill="black"/>'
    assert svg_for_qt(src) == src


def test_closed_flag_survives_thinning():
    out = svg_for_qt(_poly_path(QT_PATH_VERT_LIMIT + 5000, closed=True))
    assert re.search(r'\bd="[^"]*Z\s*"', out)


def test_multiple_subpaths_survive_thinning():
    """A pour arrives as one path holding an outline plus its cut-outs; losing
    a subpath would silently fill in a void.

    Driven through the `limit` parameter rather than a 30k-vertex path: the
    thinning logic is identical at any cap, and this keeps the test instant.
    """
    d = " ".join(
        re.search(r'\bd="([^"]+)"', _poly_path(400, closed=True)).group(1)
        for _ in range(2))
    out = svg_for_qt(f'<path d="{d}" fill="black"/>', limit=100)
    assert out.count("M") == 2
    assert out.count("Z") == 2


def test_thinning_warns_when_it_cannot_get_under_the_cap():
    """A path that stays oversized will be dropped by QtSvg. Silence there is
    the exact failure this module exists to prevent.

    A dense zigzag is incompressible: every vertex is a local extremum, so RDP
    cannot drop any of them even at its coarsest tolerance. Kept small and
    driven through `limit` — a real 30k-vertex zigzag is RDP's O(n^2) worst
    case and takes minutes.
    """
    pts = [f"{i * 0.5:.4f} {(i % 2) * 40.0:.4f}" for i in range(400)]
    src = f'<path d="M {" L ".join(pts)}" fill="black"/>'
    with pytest.warns(RuntimeWarning, match="vertices after thinning"):
        svg_for_qt(src, limit=100)


# --------------------------------------------------------------------------- #
#  build_svg — the structural contract LightBurn relies on
# --------------------------------------------------------------------------- #

@pytest.fixture
def cfg(board_dir):
    return RenderCfg(layers=load_layers(board_dir))


def test_output_is_one_flat_root_svg(cfg):
    """Nested <svg> is all Qt's SVG Tiny 1.2 rejects, and LightBurn dislikes it
    too — layers must be plain <g> groups."""
    out = build_svg(cfg)
    assert out.count("<svg") == 1
    assert out.startswith("<svg") and out.rstrip().endswith("</svg>")
    assert "<g" in out


def test_declares_physical_millimetres(cfg):
    out = build_svg(cfg)
    assert re.search(r'width="[\d.]+mm"', out)
    assert re.search(r'height="[\d.]+mm"', out)
    assert "viewBox=" in out


def test_copper_is_filled_and_edge_cuts_is_stroked(cfg):
    """LightBurn ignores stroke-width, so a stroked trace imports as a hairline
    and fills to nothing. Edge.Cuts is the deliberate exception: it stays a
    centreline because that is the path the laser should cut along."""
    out = build_svg(cfg)
    assert 'stroke="red"' in out          # Edge.Cuts centreline survives
    assert re.search(r'fill="black"', out)   # copper is real filled geometry


def test_no_svg_text_elements_anywhere(cfg):
    """QtSvg silently drops <text>, so every label is a stroke-font path."""
    out = build_svg(cfg)
    assert "<text" not in out
    assert "<tspan" not in out


def test_scale_bar_is_its_own_group(cfg):
    out = build_svg(cfg)
    assert '<g id="scale-bar">' in out
    assert cfg.scale_bar_color in out


def test_scale_bar_can_be_turned_off(cfg):
    from dataclasses import replace
    assert '<g id="scale-bar">' not in build_svg(replace(cfg, scale_bar=False))


def test_scale_bar_is_outside_the_transform_group(cfg):
    """It must never be mirrored — a reversed ruler still measures, but a
    mirrored *label* is how you discover the flip too late."""
    out = build_svg(cfg)
    sb = out.index('<g id="scale-bar">')
    assert sb > out.rindex("</g>", 0, sb)


def test_registration_marks_grow_the_frame(cfg):
    from dataclasses import replace
    def vb(c):
        m = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+)"',
                      build_svg(c))
        return tuple(float(x) for x in m.groups())
    plain = vb(cfg)
    marked = vb(replace(cfg, reg_marks=True))
    assert marked[2] > plain[2] and marked[3] > plain[3]


def test_invert_paints_a_burn_field_over_the_board_only(cfg):
    """In invert mode the margin keeps its own colour, so you burn the board
    rather than the whole sheet of paint. This is the raster/preview form
    (merge_invert=False) — the painter composition Qt can actually draw."""
    from dataclasses import replace
    out = build_svg(replace(cfg, invert=True, invert_margin="white"),
                    merge_invert=False)
    rects = re.findall(
        r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" '
        r'height="([\d.]+)" fill="(\w+)"/>', out)
    black = [r for r in rects if r[4] == "black"]
    white = [r for r in rects if r[4] == "white"]
    assert black and white, "expected both a burn rect and a margin rect"
    # the burn field must be strictly smaller than the margin it sits in
    assert float(black[0][2]) < float(white[0][2])


# --------------------------------------------------------------------------- #
#  Invert, export form: one even-odd path, because LightBurn has no painter
#  model — it maps colour -> layer and fills each layer on its own, so a
#  white-on-black knock-out arrives as two unrelated layers and never
#  subtracts.
# --------------------------------------------------------------------------- #

def _inv(cfg, **kw):
    from dataclasses import replace
    return build_svg(replace(cfg, invert=True, **kw))


def test_invert_export_is_one_even_odd_path(cfg):
    out = _inv(cfg)
    paths = re.findall(r'<path fill-rule="evenodd" fill="(\w+)" d="([^"]+)"',
                       out)
    assert len(paths) == 1, "the burn field must be a single shape"
    color, d = paths[0]
    assert color == "black"                 # the burn colour
    assert d.count("M ") > 1                # board rect + feature sub-paths


def test_invert_export_knocks_nothing_out_in_paper_colour(cfg):
    """The bug this replaces: features painted white over a black rect. White
    is not in LightBurn's palette, so all of it snapped onto one unrelated
    layer and the burn field never got its holes."""
    out = _inv(cfg, invert_margin="white")
    # the only white left is the margin rect, drawn before the transform group
    body = out[out.index("<g transform=\"matrix"):]
    assert 'fill="white"' not in body


def test_invert_export_emits_no_circle_or_rect_primitives(cfg):
    """Merged sub-paths only — a <circle> or a rotated <rect> cannot be part of
    an even-odd path, so every primitive goes through to_arc_poly()."""
    for L in cfg.layers:
        L.enabled = True
    out = _inv(cfg, scale_bar=False)     # the bar is built from plain <rect>s
    body = out[out.index("<g transform=\"matrix"):]
    assert "<circle" not in body
    assert "<rect" not in body


def test_invert_export_leaves_feature_class_colours_alone(cfg):
    """Edge.Cuts red is its own LightBurn layer and must not be folded into the
    burn field — it is the path the laser cuts along."""
    out = _inv(cfg)
    assert 'stroke="red"' in out


def test_invert_export_keeps_a_hand_picked_colour_out_of_the_merge(cfg):
    """color_overridden is the documented escape hatch (black drills etching in
    invert mode); an overridden layer keeps its own colour and its own shapes."""
    for L in cfg.layers:
        L.enabled = True
        if L.key.startswith("drill"):
            L.color, L.color_overridden = "#00ccff", True
    out = _inv(cfg)
    assert '#00ccff' in out


def test_invert_export_drills_are_holes_in_the_same_shape(cfg):
    """Even-odd nests all the way down: board burns, trace holds, a drill
    inside that trace burns again. So the drill has to be a sub-path of the
    merged field, not a shape of its own."""
    for L in cfg.layers:
        L.enabled = True
    out = _inv(cfg)
    d = re.search(r'<path fill-rule="evenodd" fill="\w+" d="([^"]+)"',
                  out).group(1)
    with_drills = d.count("M ")
    for L in cfg.layers:
        if L.key.startswith("drill"):
            L.enabled = False
    d2 = re.search(r'<path fill-rule="evenodd" fill="\w+" d="([^"]+)"',
                   _inv(cfg)).group(1)
    assert with_drills > d2.count("M ")


def test_drill_resize_still_reaches_the_merged_path(cfg):
    """Drill sizing is geometry, so it must survive the merge too."""
    from dataclasses import replace
    for L in cfg.layers:
        L.enabled = True

    def merged_d(c):
        return re.search(r'<path fill-rule="evenodd" fill="\w+" d="([^"]+)"',
                         build_svg(replace(c, invert=True))).group(1)

    assert merged_d(cfg) != merged_d(replace(cfg, drill_scale=2.0))


def test_invert_export_drops_the_paper_margin_rect(cfg):
    """One stray white rectangle is one stray LightBurn layer."""
    out = _inv(cfg, invert_margin="white")
    assert 'fill="white"' not in out


def test_burn_coloured_invert_margin_warns_and_keeps_the_paint_over_form(cfg):
    """The margin lives in the output frame, not the board frame the merged
    path is anchored to, so it cannot be folded in. Falling back is fine —
    doing it silently is not."""
    with pytest.warns(RuntimeWarning, match="invert-margin"):
        out = _inv(cfg, invert_margin="black")
    assert 'fill-rule="evenodd"' not in out
    assert 'fill="white"' in out          # features repainted in paper colour


def test_merge_invert_is_a_no_op_without_invert(cfg):
    assert build_svg(cfg) == build_svg(cfg, merge_invert=False)


# --------------------------------------------------------------------------- #
#  The burn field is a real polygon boolean, not a fill rule
# --------------------------------------------------------------------------- #

SQ_CCW = [(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0)]
SQ_CW = [(2.0, 2.0), (2.0, 4.0), (4.0, 4.0), (4.0, 2.0)]   # overlaps SQ_CCW
BOARD = ((0.0, 0.0), (10.0, 10.0))


def test_signed_area_orientation():
    assert g2l._signed_area(SQ_CCW) > 0
    assert g2l._signed_area(SQ_CW) < 0
    assert g2l._signed_area(SQ_CW[::-1]) > 0


def test_merged_outlines_are_all_counter_clockwise(cfg):
    """gerbonara promises no orientation. Mixed winding cancels under the
    winding fill the union uses, which turns copper into burn."""
    for L in cfg.layers:
        L.enabled = True
    seen = 0
    for L in cfg.layers:
        if L.obj is None or L.key == "outline":
            continue
        for pts in g2l._merged_outlines(L.obj):
            assert g2l._signed_area(pts) >= 0
            seen += 1
    assert seen, "fixture produced no outlines to check"


def test_overlapping_features_union_rather_than_cancel():
    """Two overlapping squares are one hole in the burn field, not two.

    Union  -> outer contour + 1 octagonal hole  = 2 sub-paths.
    Cancel -> outer contour + 2 L-shaped holes  = 3 sub-paths, and the overlap
    itself comes back as burn. That is the bug this guards.
    """
    d = g2l._burn_field_d(BOARD, [SQ_CCW, SQ_CW[::-1]], [])
    assert d.count("M ") == 2


def test_a_fire_feature_comes_back_as_an_island(cfg):
    """A drill inside a trace has to burn: board - copper + drills, in that
    order, so the drill lands back inside the hole it sits in."""
    hole = [(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0)]
    island = [(2.0, 2.0), (3.0, 2.0), (3.0, 3.0), (2.0, 3.0)]
    assert g2l._burn_field_d(BOARD, [hole], []).count("M ") == 2
    assert g2l._burn_field_d(BOARD, [hole], [island]).count("M ") == 3


def test_no_enabled_layers_is_an_error(cfg):
    from dataclasses import replace
    for L in cfg.layers:
        L.enabled = False
    with pytest.raises(ValueError):
        build_svg(cfg)


def test_drill_resize_changes_the_exported_geometry(cfg):
    """Drill sizing is geometry, not a raster effect — unlike kerf it must show
    up in the SVG."""
    from dataclasses import replace
    for L in cfg.layers:
        L.enabled = True
    def radii(c):
        return sorted(float(r) for r in re.findall(r'\br="([\d.]+)"',
                                                   build_svg(c)))
    base = radii(cfg)
    doubled = radii(replace(cfg, drill_scale=2.0))
    forced = radii(replace(cfg, drill_size_mm=1.0))
    assert base != doubled
    assert 0.5 in forced          # 1.0 mm diameter -> 0.5 mm radius


def test_mirroring_does_not_change_the_frame_size(cfg):
    """Mirror is about the board centre, so front and back share one reference
    frame and the outline stays put."""
    from dataclasses import replace
    def vb(c):
        return re.search(r'viewBox="([^"]+)"', build_svg(c)).group(1)
    assert vb(cfg) == vb(replace(cfg, mirror_h=True))


# --------------------------------------------------------------------------- #
#  Calibration coupon
# --------------------------------------------------------------------------- #

def test_coupon_is_self_contained_and_labelled():
    out = build_calibration_svg()
    assert out.count("<svg") == 1
    assert "<text" not in out          # stroke-font labels only
    assert out.count("<rect") > 20     # combs, ticks and the scale bar


def test_coupon_kerf_sweep_spans_the_requested_range():
    """Each tile bakes a different kerf into the bar width; a wider sweep must
    produce more tiles."""
    narrow = build_calibration_svg(kmin=-0.05, kmax=0.05, kstep=0.05)
    wide = build_calibration_svg(kmin=-0.15, kmax=0.15, kstep=0.05)
    assert wide.count("<rect") > narrow.count("<rect")
