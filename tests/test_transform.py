"""
build_transform: the mirror/rotate frame every layer shares.

The spec (CLAUDE.md, module docstring): rotate then mirror, both about the
BOARD CENTRE, so front and back are expressed in one reference frame. Rotation
is CCW in degrees; SVG y points down, so a CCW-on-screen rotation by theta maps
(x, y) -> (x cos + y sin, -x sin + y cos).
"""

import math

import pytest

from gerber2laser import build_transform, _apply, _mat_mul


IDENTITY = (1, 0, 0, 1, 0, 0)
BOX = ((0.0, 0.0), (10.0, 6.0))          # centre (5, 3)
OFFSET_BOX = ((5.0, 3.0), (15.0, 9.0))   # centre (10, 6), not on the origin


def _ref(bounds, mirror_h, mirror_v, rotate, x, y):
    """Independent reference: rotate then mirror, both about the centre."""
    (x0, y0), (x1, y1) = bounds
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    dx, dy = x - cx, y - cy
    th = math.radians(rotate)
    c, s = math.cos(th), math.sin(th)
    rx, ry = dx * c + dy * s, -dx * s + dy * c
    if mirror_h:
        rx = -rx
    if mirror_v:
        ry = -ry
    return cx + rx, cy + ry


def _corners(bounds):
    (x0, y0), (x1, y1) = bounds
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _approx_box(box):
    return (pytest.approx(box[0], abs=1e-9), pytest.approx(box[1], abs=1e-9))


def test_identity_is_identity():
    m, out = build_transform(BOX, False, False, 0)
    assert m == pytest.approx(IDENTITY, abs=1e-12)
    assert out == _approx_box(BOX)


def test_rotate_360_is_identity():
    m, out = build_transform(BOX, False, False, 360)
    assert m == pytest.approx(IDENTITY, abs=1e-12)
    assert out == _approx_box(BOX)


@pytest.mark.parametrize("bounds", [BOX, OFFSET_BOX])
@pytest.mark.parametrize("mirror_h,mirror_v", [(True, False), (False, True),
                                               (True, True)])
def test_mirror_is_about_the_centre_so_the_aabb_does_not_move(
        bounds, mirror_h, mirror_v):
    """A mirror about the board centre leaves the box exactly where it was."""
    _, out = build_transform(bounds, mirror_h, mirror_v, 0)
    assert out == _approx_box(bounds)


def test_mirror_h_swaps_left_and_right_edges():
    m, _ = build_transform(BOX, True, False, 0)
    assert _apply(m, 0.0, 0.0) == pytest.approx((10.0, 0.0), abs=1e-9)
    assert _apply(m, 10.0, 6.0) == pytest.approx((0.0, 6.0), abs=1e-9)
    # a point on the vertical centre line is a fixed point
    assert _apply(m, 5.0, 2.0) == pytest.approx((5.0, 2.0), abs=1e-9)


def test_mirror_h_is_an_involution():
    m, _ = build_transform(BOX, True, False, 0)
    for x, y in _corners(BOX) + [(1.234, 5.678)]:
        assert _apply(m, *_apply(m, x, y)) == pytest.approx((x, y), abs=1e-9)


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_rotate_matches_the_reference_rotation(rotate):
    m, _ = build_transform(OFFSET_BOX, False, False, rotate)
    for x, y in _corners(OFFSET_BOX) + [(7.5, 4.25)]:
        assert _apply(m, x, y) == pytest.approx(
            _ref(OFFSET_BOX, False, False, rotate, x, y), abs=1e-9)


def test_rotate_90_gives_a_transposed_box_on_the_same_centre():
    _, out = build_transform(BOX, False, False, 90)
    # 10 x 6 about centre (5, 3) becomes 6 x 10 about the same centre
    assert out == _approx_box(((2.0, -2.0), (8.0, 8.0)))


def test_rotate_180_leaves_the_aabb_alone():
    _, out = build_transform(BOX, False, False, 180)
    assert out == _approx_box(BOX)


def test_rotate_90_composed_twice_equals_rotate_180():
    m90, _ = build_transform(BOX, False, False, 90)
    m180, _ = build_transform(BOX, False, False, 180)
    assert _mat_mul(m90, m90) == pytest.approx(m180, abs=1e-9)


def test_rotate_90_then_270_is_the_identity():
    m90, _ = build_transform(BOX, False, False, 90)
    m270, _ = build_transform(BOX, False, False, 270)
    assert _mat_mul(m90, m270) == pytest.approx(IDENTITY, abs=1e-9)


def test_rotate_180_equals_mirroring_both_axes():
    m180, _ = build_transform(BOX, False, False, 180)
    mboth, _ = build_transform(BOX, True, True, 0)
    assert m180 == pytest.approx(mboth, abs=1e-9)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror_h,mirror_v", [(False, False), (True, False),
                                               (False, True), (True, True)])
def test_mirror_and_rotate_compose_as_rotate_then_mirror(
        rotate, mirror_h, mirror_v):
    m, _ = build_transform(OFFSET_BOX, mirror_h, mirror_v, rotate)
    for x, y in _corners(OFFSET_BOX) + [(11.0, 7.0)]:
        assert _apply(m, x, y) == pytest.approx(
            _ref(OFFSET_BOX, mirror_h, mirror_v, rotate, x, y), abs=1e-9)


def test_mirror_h_with_rotate_90_maps_a_known_corner():
    """Worked by hand: (0,0) -> rel(-5,-3) -> rot90(-3,5) -> mirror(3,5) -> (8,8)."""
    m, _ = build_transform(BOX, True, False, 90)
    assert _apply(m, 0.0, 0.0) == pytest.approx((8.0, 8.0), abs=1e-9)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror_h", [False, True])
def test_out_bounds_is_the_transformed_aabb(rotate, mirror_h):
    m, out = build_transform(OFFSET_BOX, mirror_h, False, rotate)
    pts = [_apply(m, x, y) for x, y in _corners(OFFSET_BOX)]
    expect = ((min(p[0] for p in pts), min(p[1] for p in pts)),
              (max(p[0] for p in pts), max(p[1] for p in pts)))
    assert out == _approx_box(expect)


def test_transform_preserves_area_and_lengths():
    """No transform here is allowed to scale: |det| == 1."""
    for rotate in (0, 90, 180, 270):
        for mh in (False, True):
            m, _ = build_transform(OFFSET_BOX, mh, False, rotate)
            det = m[0] * m[3] - m[1] * m[2]
            assert abs(det) == pytest.approx(1.0, abs=1e-12)


def test_mat_mul_applies_the_right_hand_matrix_first():
    translate = (1, 0, 0, 1, 10, 0)
    scale = (2, 0, 0, 2, 0, 0)
    # scale after translate: (1,1) -> (11,1) -> (22,2)
    assert _apply(_mat_mul(scale, translate), 1, 1) == pytest.approx((22, 2))
    # translate after scale: (1,1) -> (2,2) -> (12,2)
    assert _apply(_mat_mul(translate, scale), 1, 1) == pytest.approx((12, 2))
