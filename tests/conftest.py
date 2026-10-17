"""
Shared setup for the gerber2laser test suite.

Everything under test lives in the single-file module at the repo root, so the
root goes on sys.path here rather than in every test file. Qt is forced to the
offscreen platform before anything can import PySide6 — the pure core does not
need a display and neither should the tests.
"""

import os
import sys
from pathlib import Path

# Must happen before any PySide6 import (rasterize_qimage imports Qt lazily).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import gerber2laser as g2l  # noqa: E402,F401  (re-exported for tests)


# --------------------------------------------------------------------------- #
#  A minimal but real board: 10 x 6 mm outline, two traces, two 1.5 mm pads,
#  two 0.8 mm plated holes. Written out as KiCad-style Gerber + Excellon so the
#  loader classifies it by filename exactly as it would a real export.
# --------------------------------------------------------------------------- #

GBR_EDGE_CUTS = """%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.100000*%
G01*
D10*
X0Y0D02*
X10000000Y0D01*
X10000000Y6000000D01*
X0Y6000000D01*
X0Y0D01*
M02*
"""

GBR_F_CU = """%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.500000*%
%ADD11C,1.500000*%
G01*
D10*
X1000000Y1000000D02*
X9000000Y1000000D01*
X9000000Y5000000D01*
D11*
X2000000Y4000000D03*
X8000000Y4000000D03*
M02*
"""

# Same geometry as F.Cu, so a "back side" board can be built from the same box.
GBR_B_CU = GBR_F_CU

DRL_PTH = """M48
METRIC,TZ
T1C0.800
%
G90
T1
X2.0Y4.0
X8.0Y4.0
T0
M30
"""


def _write_board(dirpath, name="tb", back=False):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{name}-Edge_Cuts.gbr").write_text(GBR_EDGE_CUTS, newline="\n")
    if back:
        (dirpath / f"{name}-B_Cu.gbr").write_text(GBR_B_CU, newline="\n")
    else:
        (dirpath / f"{name}-F_Cu.gbr").write_text(GBR_F_CU, newline="\n")
    (dirpath / f"{name}-PTH.drl").write_text(DRL_PTH, newline="\n")
    return dirpath


@pytest.fixture
def board_dir(tmp_path):
    """A folder holding a front-copper board (Edge.Cuts + F.Cu + PTH drill)."""
    return _write_board(tmp_path / "gb")


@pytest.fixture
def back_board_dir(tmp_path):
    """The same board exported as back copper (B.Cu)."""
    return _write_board(tmp_path / "gb_back", back=True)
