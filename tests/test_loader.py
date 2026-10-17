"""
Filename classification, layer loading, and the cut-orientation verdict.

These three are what stand between a plain Gerber export and a correctly
oriented burn, and getting them wrong is expensive: an unrecognised outline
file etches a board with no edge, and a wrong mirror verdict etches the whole
board reversed.
"""

import pytest

from gerber2laser import (
    classify_filename,
    load_layers,
    orientation_advice,
    primary_copper_side,
    required_mirror_h,
    LayerCfg,
)


# --------------------------------------------------------------------------- #
#  classify_filename — first match wins, so ordering regressions matter
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,key", [
    # KiCad's own export names
    ("MyBoard-Edge_Cuts.gbr", "outline"),
    ("MyBoard-F_Cu.gbr", "top_copper"),
    ("MyBoard-B_Cu.gbr", "bottom_copper"),
    ("MyBoard-In1_Cu.gbr", "inner_copper"),
    ("MyBoard-F_Silkscreen.gbr", "f_silk"),
    ("MyBoard-B_Silkscreen.gbr", "b_silk"),
    ("MyBoard-F_Mask.gbr", "f_mask"),
    ("MyBoard-B_Mask.gbr", "b_mask"),
    ("MyBoard-PTH.drl", "drill"),
    ("MyBoard-NPTH.drl", "drill"),
    # Protel / legacy extensions
    ("board.gtl", "top_copper"),
    ("board.gbl", "bottom_copper"),
    ("board.gko", "outline"),
    ("board.gm1", "outline"),
    ("board.gto", "f_silk"),
    ("board.gbo", "b_silk"),
    ("board.gts", "f_mask"),
    ("board.gbs", "b_mask"),
    ("board.drl", "drill"),
    ("board.xln", "drill"),
])
def test_known_names_classify(name, key):
    got = classify_filename(name)
    assert got is not None, f"{name} was not recognised at all"
    assert got[0] == key


def test_back_copper_is_not_mistaken_for_front():
    """`f[_-. ]?cu` must not match inside a B.Cu name — the whole front/back
    flip decision hangs off this, and getting it backwards etches a mirrored
    board."""
    assert classify_filename("MyBoard-B_Cu.gbr")[0] == "bottom_copper"
    assert classify_filename("board.gbl")[0] == "bottom_copper"


@pytest.mark.parametrize("name", ["Profile.gbr", "board-profile.gbr",
                                  "board_Profile.gbr", "PROFILE.GBR"])
def test_bare_profile_is_an_outline(name):
    """Altium and friends export the outline as a bare `Profile.gbr`. It used
    to need a leading hyphen, so those boards loaded with no outline."""
    assert classify_filename(name)[0] == "outline"


@pytest.mark.parametrize("name", ["myprofile.gbr", "profiles.gbr",
                                  "profiler.txt"])
def test_profile_stays_a_whole_word(name):
    """Widening `-profile` must not let it swallow unrelated filenames."""
    got = classify_filename(name)
    assert got is None or got[0] != "outline"


def test_unrecognised_names_return_none():
    assert classify_filename("notes.pdf") is None
    assert classify_filename("board.kicad_pcb") is None


def test_classification_carries_sane_defaults():
    """Copper and outline are on by default; silk/mask/drill are opt-in."""
    assert classify_filename("b-Edge_Cuts.gbr")[3] is True    # enabled
    assert classify_filename("b-F_Cu.gbr")[3] is True
    assert classify_filename("b-B_Cu.gbr")[3] is False
    assert classify_filename("b-F_Mask.gbr")[3] is False
    assert classify_filename("b-PTH.drl")[4] is True          # is_excellon
    assert classify_filename("b-F_Cu.gbr")[4] is False


# --------------------------------------------------------------------------- #
#  load_layers
# --------------------------------------------------------------------------- #

def test_load_layers_finds_the_board(board_dir):
    layers = load_layers(board_dir)
    keys = [L.key for L in layers]
    assert "outline" in keys
    assert "top_copper" in keys
    assert any(k.startswith("drill") for k in keys)
    assert all(L.obj is not None for L in layers)


def test_drill_files_are_numbered_not_collided(tmp_path):
    """Every drill file gets its own key, so a board with PTH + NPTH does not
    lose one to a key clash. CLAUDE.md documents these as drill1, drill2, ..."""
    from tests.conftest import GBR_EDGE_CUTS, DRL_PTH
    d = tmp_path / "two_drills"
    d.mkdir()
    (d / "b-Edge_Cuts.gbr").write_text(GBR_EDGE_CUTS, newline="\n")
    (d / "b-PTH.drl").write_text(DRL_PTH, newline="\n")
    (d / "b-NPTH.drl").write_text(DRL_PTH, newline="\n")
    drills = [L.key for L in load_layers(d) if L.key.startswith("drill")]
    assert sorted(drills) == ["drill1", "drill2"]


def test_load_layers_orders_outline_and_copper_first(board_dir):
    order = [L.key for L in load_layers(board_dir)]
    assert order[0] == "outline"
    assert order.index("top_copper") < min(
        i for i, k in enumerate(order) if k.startswith("drill"))


def test_load_layers_reads_a_zip(board_dir, tmp_path):
    """A fab zip loads without being unpacked first, and the temp extraction
    directory is cleaned up — the objects are fully parsed into memory."""
    import zipfile
    z = tmp_path / "fab.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in board_dir.iterdir():
            zf.write(f, f.name)
    keys = {L.key for L in load_layers(z)}
    assert "top_copper" in keys and "outline" in keys


def test_load_layers_rejects_a_folder_with_nothing_in_it(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        load_layers(empty)


def test_load_layers_skips_files_that_only_look_like_drills(tmp_path):
    """`.txt` is in the drill pattern because some tools use it, so a README
    must be skipped rather than crashing the load."""
    from tests.conftest import GBR_EDGE_CUTS, GBR_F_CU
    d = tmp_path / "with_readme"
    d.mkdir()
    (d / "b-Edge_Cuts.gbr").write_text(GBR_EDGE_CUTS, newline="\n")
    (d / "b-F_Cu.gbr").write_text(GBR_F_CU, newline="\n")
    (d / "readme.txt").write_text("not a drill file\n", newline="\n")
    keys = {L.key for L in load_layers(d)}
    assert keys == {"outline", "top_copper"}


# --------------------------------------------------------------------------- #
#  Orientation — which side needs the mirror
# --------------------------------------------------------------------------- #

def _cu(key, name=None, enabled=True):
    return LayerCfg(key, name or key, "black", enabled=enabled, obj=object())


def test_required_mirror_h_only_for_bottom():
    assert required_mirror_h("bottom_copper") is True
    assert required_mirror_h("top_copper") is False
    assert required_mirror_h("inner_copper") is False


def test_primary_copper_side_prefers_top_when_both_exist(board_dir,
                                                         back_board_dir):
    assert primary_copper_side(load_layers(board_dir)) == "top"
    assert primary_copper_side(load_layers(back_board_dir)) == "bottom"


def test_primary_copper_side_is_none_without_copper():
    assert primary_copper_side([_cu("outline")]) is None


def test_top_copper_is_correct_unmirrored():
    ok, need, msg = orientation_advice([_cu("top_copper", "F.Cu")], False)
    assert ok is True and need is False
    assert "F.Cu" in msg


def test_top_copper_mirrored_is_flagged():
    ok, need, _ = orientation_advice([_cu("top_copper", "F.Cu")], True)
    assert ok is False and need is False


def test_bottom_copper_needs_the_flip():
    ok, need, _ = orientation_advice([_cu("bottom_copper", "B.Cu")], False)
    assert ok is False and need is True
    ok, need, _ = orientation_advice([_cu("bottom_copper", "B.Cu")], True)
    assert ok is True and need is True


def test_both_sides_enabled_is_refused():
    """Each side is a separate burn; rendering both into one file is never
    what the user wants."""
    ok, need, msg = orientation_advice(
        [_cu("top_copper", "F.Cu"), _cu("bottom_copper", "B.Cu")], False)
    assert ok is False
    assert need is None          # no single mirror can fix it
    assert "SEPARATE" in msg.upper()


def test_inner_copper_only_reports_no_verifiable_face():
    """There is no outer face to lay on the bed, so the tool must not claim an
    F.Cu verdict it cannot back up."""
    ok, need, msg = orientation_advice([_cu("inner_copper", "Inner copper")],
                                       False)
    assert need is None
    assert "F.Cu" not in msg
    assert "inner" in msg.lower()


def test_no_copper_enabled_is_not_an_error():
    ok, need, msg = orientation_advice(
        [_cu("outline", "Edge.Cuts"), _cu("drill1", "Drills")], False)
    assert ok is True and need is None


def test_disabled_copper_is_ignored():
    ok, need, _ = orientation_advice(
        [_cu("bottom_copper", "B.Cu", enabled=False),
         _cu("top_copper", "F.Cu")], False)
    assert ok is True and need is False
