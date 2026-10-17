"""
resolve_colors / layer_color — the single source of truth for what each layer
is drawn in and in what order (CLAUDE.md "Domain invariants").

  * paint order is copper, then the other feature classes, then drills LAST
  * Invert swaps black <-> white only; a feature-class colour rides through
  * a hand-picked colour (color_overridden) is used verbatim in both modes
  * a drill is never a colour of its own: it is the opposite of the copper
    field, or of the background when no copper layer is on
"""

import pytest

from gerber2laser import LayerCfg, RenderCfg, layer_color, resolve_colors


def L(key, color="black", **kw):
    """A LayerCfg that counts as active (obj is not None)."""
    return LayerCfg(key=key, name=key, color=color, obj=object(), **kw)


def cfg(*layers, **kw):
    return RenderCfg(layers=list(layers), **kw)


def by_key(resolved):
    return {lay.key: col for lay, col in resolved}


def order(resolved):
    return [lay.key for lay, _ in resolved]


# --------------------------------------------------------------------------- #
#  layer_color
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("color", ["black", "#000", "#000000", "BLACK", " #000 "])
def test_invert_turns_a_black_layer_into_the_paper_colour(color):
    assert layer_color(L("top_copper", color), True, "white", "black") == "white"


@pytest.mark.parametrize("color", ["white", "#fff", "#ffffff", "WHITE"])
def test_invert_turns_a_white_layer_into_the_burn_colour(color):
    assert layer_color(L("top_copper", color), True, "white", "black") == "black"


@pytest.mark.parametrize("color", ["red", "#227722", "#888888", "#ff00ff"])
def test_invert_leaves_a_feature_class_colour_untouched(color):
    assert layer_color(L("outline", color), True, "white", "black") == color


@pytest.mark.parametrize("color", ["black", "white", "red"])
def test_without_invert_the_colour_is_used_as_is(color):
    assert layer_color(L("x", color), False, "white", "black") == color


@pytest.mark.parametrize("invert", [False, True])
@pytest.mark.parametrize("color", ["black", "white", "#123456"])
def test_a_hand_picked_colour_is_honoured_verbatim(invert, color):
    lay = L("top_copper", color, color_overridden=True)
    assert layer_color(lay, invert, "white", "black") == color


# --------------------------------------------------------------------------- #
#  paint order
# --------------------------------------------------------------------------- #

def test_paint_order_is_copper_then_others_then_drills():
    c = cfg(L("drill1"), L("f_mask", "#227722"), L("outline", "red"),
            L("top_copper"))
    assert order(resolve_colors(c)) == ["top_copper", "f_mask", "outline",
                                        "drill1"]


def test_every_drill_layer_lands_after_every_other_layer():
    c = cfg(L("drill1"), L("drill2"), L("outline", "red"), L("bottom_copper"))
    got = order(resolve_colors(c))
    assert got[0] == "bottom_copper"
    assert set(got[-2:]) == {"drill1", "drill2"}


def test_order_within_a_rank_keeps_the_loader_order():
    c = cfg(L("f_mask", "#227722"), L("outline", "red"), L("b_silk", "#555555"))
    assert order(resolve_colors(c)) == ["f_mask", "outline", "b_silk"]


def test_disabled_and_objectless_layers_are_dropped():
    c = cfg(L("top_copper"),
            LayerCfg("outline", "outline", "red", enabled=False, obj=object()),
            LayerCfg("drill1", "drill1", "black", enabled=True, obj=None))
    assert order(resolve_colors(c)) == ["top_copper"]


# --------------------------------------------------------------------------- #
#  drills: always the opposite of the field under them
# --------------------------------------------------------------------------- #

def test_drill_is_white_over_black_copper():
    c = cfg(L("top_copper", "black"), L("drill1", "black"))
    assert by_key(resolve_colors(c))["drill1"] == "white"


def test_drill_is_black_over_inverted_copper():
    """Invert makes the copper white, so the hole has to fire to read."""
    c = cfg(L("top_copper", "black"), L("drill1", "black"), invert=True)
    resolved = by_key(resolve_colors(c))
    assert resolved["top_copper"] == "white"
    assert resolved["drill1"] == "black"


@pytest.mark.parametrize("invert", [False, True])
def test_drill_is_always_the_opposite_of_the_copper(invert):
    c = cfg(L("top_copper", "black"), L("drill1"), invert=invert)
    resolved = by_key(resolve_colors(c))
    assert resolved["drill1"] != resolved["top_copper"]
    assert {resolved["drill1"], resolved["top_copper"]} == {"black", "white"}


def test_drill_falls_back_to_the_opposite_of_the_background():
    """No copper on: the field is the board background, white by default."""
    c = cfg(L("outline", "red"), L("drill1"))
    assert by_key(resolve_colors(c))["drill1"] == "black"


def test_drill_falls_back_to_the_opposite_of_the_inverted_background():
    c = cfg(L("outline", "red"), L("drill1"), invert=True)
    assert by_key(resolve_colors(c))["drill1"] == "white"


def test_a_transparent_background_still_counts_as_paper():
    c = cfg(L("outline", "red"), L("drill1"), bg="none")
    assert by_key(resolve_colors(c))["drill1"] == "black"


@pytest.mark.parametrize("invert", [False, True])
def test_a_hand_picked_drill_colour_opts_out_of_all_of_that(invert):
    c = cfg(L("top_copper", "black"),
            L("drill1", "black", color_overridden=True), invert=invert)
    assert by_key(resolve_colors(c))["drill1"] == "black"


def test_the_first_copper_layer_decides_the_field():
    """Inner copper is grey and reads as light, so the hole over it fires."""
    c = cfg(L("inner_copper", "#888888"), L("drill1"))
    resolved = by_key(resolve_colors(c))
    assert resolved["inner_copper"] == "#888888"
    assert resolved["drill1"] == "black"


# --------------------------------------------------------------------------- #
#  invert applied through resolve_colors
# --------------------------------------------------------------------------- #

def test_invert_swaps_only_the_laser_colours():
    c = cfg(L("top_copper", "black"), L("outline", "red"),
            L("f_mask", "#227722"), invert=True)
    resolved = by_key(resolve_colors(c))
    assert resolved["top_copper"] == "white"
    assert resolved["outline"] == "red"
    assert resolved["f_mask"] == "#227722"


def test_invert_uses_the_configured_background_as_the_paper_colour():
    c = cfg(L("top_copper", "black"), invert=True, bg="#ffffff")
    assert by_key(resolve_colors(c))["top_copper"] == "#ffffff"
