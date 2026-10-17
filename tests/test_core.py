"""
Core helpers with no Qt and no gerbonara in them:
settings_tags / export_filename, kerf_from_measurement, union_bounds,
_is_dark, _rdp, _thin_poly.
"""

import math

import pytest

from gerber2laser import (
    MAX_EXPORT_NAME,
    LayerCfg,
    RenderCfg,
    _is_dark,
    _rdp,
    _thin_poly,
    export_filename,
    kerf_from_measurement,
    settings_tags,
    union_bounds,
)


STAMP = "20200101-0000"


def cfg(**kw):
    layers = kw.pop("layers", [])
    return RenderCfg(layers=list(layers), **kw)


def L(key, enabled=True, obj=True):
    return LayerCfg(key=key, name=key, color="black", enabled=enabled,
                    obj=object() if obj else None)


# --------------------------------------------------------------------------- #
#  settings_tags
# --------------------------------------------------------------------------- #

def test_tags_start_with_the_side_and_end_with_the_least_important():
    tags = settings_tags(cfg(invert=True, mirror_h=True, rotate=90,
                             reg_marks=True), side="back", dpi=600, kerf=-0.05)
    assert tags == ["back", "inv", "mirh", "rot90", "k-0.05", "600dpi",
                    "m2", "reg", "sb10"]


def test_a_default_config_tags_only_what_is_on():
    assert settings_tags(cfg(), side="front") == ["front", "m2", "sb10"]


@pytest.mark.parametrize("keys,expect", [
    (["top_copper"], "front"),
    (["bottom_copper"], "back"),
    (["top_copper", "bottom_copper"], "both"),
    (["outline"], "nocu"),
])
def test_the_side_tag_is_derived_from_the_enabled_copper(keys, expect):
    tags = settings_tags(cfg(layers=[L(k) for k in keys]))
    assert tags[0] == expect


def test_a_disabled_copper_layer_does_not_count_towards_the_side():
    tags = settings_tags(cfg(layers=[L("top_copper"),
                                     L("bottom_copper", enabled=False)]))
    assert tags[0] == "front"


def test_a_copper_layer_with_no_file_does_not_count_towards_the_side():
    tags = settings_tags(cfg(layers=[L("top_copper"),
                                     L("bottom_copper", obj=False)]))
    assert tags[0] == "front"


def test_drill_sizing_is_tagged_only_when_it_changes_the_geometry():
    assert "h0.5" in settings_tags(cfg(drill_size_mm=0.5), side="front")
    assert "hx1.5" in settings_tags(cfg(drill_scale=1.5), side="front")
    plain = settings_tags(cfg(drill_size_mm=0.0, drill_scale=1.0), side="front")
    assert not [t for t in plain if t.startswith(("h0", "hx"))]


def test_zero_kerf_and_zero_rotation_are_not_tagged():
    tags = settings_tags(cfg(rotate=0), side="front", kerf=0.0)
    assert not [t for t in tags if t.startswith(("k", "rot"))]


def test_the_scale_bar_tag_disappears_when_the_bar_is_off():
    assert "sb10" not in settings_tags(cfg(scale_bar=False), side="front")


# --------------------------------------------------------------------------- #
#  export_filename
# --------------------------------------------------------------------------- #

def test_a_short_name_keeps_every_tag():
    name = export_filename("board", ["front", "m2", "sb10"], "png", stamp=STAMP)
    assert name == "board_front_m2_sb10_20200101-0000.png"


def test_the_stamp_defaults_to_now_and_is_always_present():
    name = export_filename("board", [], "svg")
    assert name.startswith("board_") and name.endswith(".svg")
    assert len(name.split("_")[-1]) == len(STAMP) + len(".svg")


def test_unsafe_characters_are_sanitised():
    assert export_filename("my board/v2:x", [], "png", stamp=STAMP) == \
        "my-board-v2-x_20200101-0000.png"


def test_leading_and_trailing_separators_are_stripped():
    assert export_filename("..board..", [], "png", stamp=STAMP) == \
        "board_20200101-0000.png"


def test_an_empty_or_unusable_stem_becomes_board():
    for stem in ("", "   ", "///", "..."):
        assert export_filename(stem, [], "png", stamp=STAMP) == \
            "board_20200101-0000.png"


def test_tags_are_dropped_from_the_end_first():
    tags = ["front", "inv", "m2", "reg", "sb10"]
    full = export_filename("board", tags, "png", stamp=STAMP)
    assert full == "board_front_inv_m2_reg_sb10_20200101-0000.png"
    # one char over -> exactly one tag goes, and it is the last one
    tight = export_filename("board", tags, "png", stamp=STAMP,
                            max_len=len(full) - 1)
    assert tight == "board_front_inv_m2_reg_20200101-0000.png"


def test_tags_keep_dropping_until_the_name_fits():
    tags = ["front", "inv", "m2", "reg", "sb10"]
    name = export_filename("board", tags, "png", stamp=STAMP, max_len=30)
    assert name == "board_front_20200101-0000.png"
    assert len(name) <= 30


def test_the_board_name_survives_when_the_tags_do_not():
    name = export_filename("myboard", ["front", "inv", "reg"], "png",
                           stamp=STAMP, max_len=40)
    assert name.startswith("myboard_")
    assert len(name) <= 40


def test_the_stem_is_truncated_only_as_a_last_resort():
    # no tags left to drop, so the board name has to give
    name = export_filename("board", [], "png", stamp=STAMP, max_len=20)
    assert name == "bo_20200101-0000.png"
    assert len(name) == 20


def test_the_stem_never_vanishes_entirely():
    name = export_filename("board", [], "png", stamp=STAMP, max_len=5)
    assert name.split("_")[0]


def test_real_settings_fit_inside_the_default_cap():
    tags = settings_tags(cfg(invert=True, mirror_h=True, rotate=180,
                             reg_marks=True, drill_size_mm=0.5,
                             drill_scale=1.25),
                         side="back", dpi=1200, kerf=-0.075)
    name = export_filename("MyBoard-rev-C", tags, "png", stamp=STAMP)
    assert len(name) <= MAX_EXPORT_NAME
    assert name.startswith("MyBoard-rev-C_back_inv_mirh_rot180_")


# --------------------------------------------------------------------------- #
#  kerf_from_measurement
# --------------------------------------------------------------------------- #

def test_an_overburned_black_feature_asks_for_negative_kerf():
    assert kerf_from_measurement(0.30, 0.40, True) == -0.05


def test_an_underburned_black_feature_asks_for_positive_kerf():
    assert kerf_from_measurement(0.30, 0.20, True) == 0.05


def test_a_widened_gap_asks_for_positive_kerf():
    assert kerf_from_measurement(0.30, 0.40, False) == 0.05


def test_a_narrowed_gap_asks_for_negative_kerf():
    assert kerf_from_measurement(0.30, 0.20, False) == -0.05


def test_an_on_size_measurement_needs_no_compensation():
    assert kerf_from_measurement(0.30, 0.30, True) == 0.0
    assert kerf_from_measurement(0.30, 0.30, False) == 0.0


@pytest.mark.parametrize("d", [0.02, 0.05, 0.1, -0.04, -0.12])
def test_a_black_feature_round_trips(d):
    """Measure nominal+d on a black feature -> the answer is exactly -d/2."""
    nominal = 0.30
    assert kerf_from_measurement(nominal, nominal + d, True) == \
        pytest.approx(round(-d / 2.0, 3))


@pytest.mark.parametrize("d", [0.02, 0.05, 0.1, -0.04, -0.12])
def test_the_two_senses_are_exact_opposites(d):
    nominal = 0.30
    black = kerf_from_measurement(nominal, nominal + d, True)
    gap = kerf_from_measurement(nominal, nominal + d, False)
    assert black == pytest.approx(-gap)


def test_the_result_is_rounded_to_micron_scale():
    k = kerf_from_measurement(0.30, 0.3123456, True)
    assert k == round(k, 3)


# --------------------------------------------------------------------------- #
#  union_bounds
# --------------------------------------------------------------------------- #

class FakeCam:
    def __init__(self, bb):
        self._bb = bb

    def bounding_box(self):
        if self._bb == "boom":
            raise RuntimeError("unparseable")
        return self._bb


def test_union_bounds_covers_every_object():
    got = union_bounds([FakeCam(((0, 0), (10, 6))),
                        FakeCam(((-1, 2), (4, 12)))])
    assert got == ((-1.0, 0.0), (10.0, 12.0))


def test_union_bounds_of_one_object_is_that_object():
    assert union_bounds([FakeCam(((1.5, 2.5), (3.5, 4.5)))]) == \
        ((1.5, 2.5), (3.5, 4.5))


def test_union_bounds_ignores_empty_and_broken_objects():
    got = union_bounds([FakeCam(((0, 0), (10, 6))), FakeCam(None),
                        FakeCam("boom")])
    assert got == ((0.0, 0.0), (10.0, 6.0))


def test_union_bounds_falls_back_to_a_default_box():
    assert union_bounds([]) == ((0.0, 0.0), (10.0, 10.0))
    assert union_bounds([FakeCam(None)]) == ((0.0, 0.0), (10.0, 10.0))


def test_union_bounds_returns_floats():
    (x0, y0), (x1, y1) = union_bounds([FakeCam(((0, 0), (10, 6)))])
    assert all(isinstance(v, float) for v in (x0, y0, x1, y1))


# --------------------------------------------------------------------------- #
#  _is_dark
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("color", ["black", "#000", "#000000", "BLACK",
                                   "  black  ", "#222222", "#003300"])
def test_dark_colours_read_as_dark(color):
    assert _is_dark(color) is True


@pytest.mark.parametrize("color", ["white", "#ffffff", "#cccccc", "#dddddd",
                                   "#999999"])
def test_light_colours_read_as_light(color):
    assert _is_dark(color) is False


def test_exact_mid_grey_is_a_documented_coin_flip():
    """#808080 sits exactly on the threshold: its weighted luminance is 128,
    and the float sum lands at 127.99999999999999, so `< 128` calls it dark.

    Either answer is defensible for a colour this ambiguous, and the only
    consequence is which way a drill mark contrasts against a mid-grey copper
    layer — visible either way. Pinned so the behaviour cannot drift silently,
    NOT because dark is the semantically right answer here.
    """
    assert _is_dark("#808080") is True
    assert _is_dark("#818181") is False   # one step lighter is unambiguous
    assert _is_dark("#7f7f7f") is True    # one step darker is unambiguous


@pytest.mark.parametrize("color", [None, "", "none", "#abc", "#gggggg",
                                   "rebeccapurple"])
def test_unparseable_colours_default_to_not_dark(color):
    """Only 'black' and #rrggbb are understood; anything else must not be
    treated as a burn field by accident."""
    assert _is_dark(color) is False


# --------------------------------------------------------------------------- #
#  _rdp / _thin_poly
# --------------------------------------------------------------------------- #

def test_rdp_keeps_short_polylines_intact():
    assert _rdp([], 0.1) == []
    assert _rdp([(0, 0)], 0.1) == [(0, 0)]
    assert _rdp([(0, 0), (1, 1)], 0.1) == [(0, 0), (1, 1)]


def test_rdp_collapses_a_straight_run_to_its_endpoints():
    pts = [(x / 10.0, 0.0) for x in range(101)]
    assert _rdp(pts, 0.002) == [(0.0, 0.0), (10.0, 0.0)]


def test_rdp_keeps_a_corner_that_matters():
    pts = [(0.0, 0.0), (5.0, 1.0), (10.0, 0.0)]
    assert _rdp(pts, 0.1) == pts


def test_rdp_drops_a_deviation_below_tolerance():
    pts = [(0.0, 0.0), (5.0, 0.0005), (10.0, 0.0)]
    assert _rdp(pts, 0.002) == [(0.0, 0.0), (10.0, 0.0)]


def test_rdp_always_keeps_the_endpoints():
    pts = [(0.0, 0.0), (1.0, 0.0001), (2.0, 0.0)]
    out = _rdp(pts, 1.0)
    assert out[0] == pts[0] and out[-1] == pts[-1]


def test_thin_poly_drops_consecutive_duplicates():
    pts = [(0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (1.0, 1.0), (1.0, 1.0),
           (2.0, 0.0)]
    assert _thin_poly(pts, 100) == [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]


def test_thin_poly_leaves_a_small_polyline_alone():
    pts = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
    assert _thin_poly(pts, 100) == pts


def test_thin_poly_gets_a_dense_circle_under_the_limit():
    n = 4000
    r = 20.0
    pts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n))
           for i in range(n)]
    out = _thin_poly(pts, 500)
    assert len(out) <= 500
    # still recognisably the same circle: every kept vertex is on it
    for x, y in out:
        assert math.hypot(x, y) == pytest.approx(r, abs=1e-9)


def test_thin_poly_warns_when_it_cannot_reach_the_limit():
    """A 1 mm zigzag has nothing to remove at 0.05 mm tolerance; the module
    must say so rather than let QtSvg drop the path in silence."""
    pts = [(i * 0.5, 1.0 if i % 2 else 0.0) for i in range(200)]
    with pytest.warns(RuntimeWarning, match="after thinning"):
        out = _thin_poly(pts, 10)
    assert len(out) > 10
