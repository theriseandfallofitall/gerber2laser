#!/usr/bin/env python3
"""
Capture the README screenshots from the real GUI.

    python tools/screenshots/make_screenshots.py <gerber folder or zip>

The images in docs/images/ come from KiCad's own `ecc83` demo board
(share/kicad/demos/ecc83/ecc83-pp.kicad_pcb), plotted with:

    kicad-cli pcb export gerbers -l F.Cu,B.Cu,Edge.Cuts,F.SilkS,F.Mask -o gerbers/ ecc83-pp.kicad_pcb
    kicad-cli pcb export drill -o gerbers/ ecc83-pp.kicad_pcb

Opens the GUI on that board, sets the controls for each shot, grabs the
window, then quits. Also writes a calibration coupon through the CLI.
Needs a display (the window is shown for a few seconds).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))

import gerber2laser  # noqa: E402


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    board = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True)

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    def scripted_exec(app):
        win = next(w for w in app.topLevelWidgets()
                   if w.isVisible() and hasattr(w, "pmode"))
        win.resize(1280, 820)

        # The label shows the absolute path it was loaded from; keep the
        # capturing machine's folder names out of the published image.
        win.path_lbl.setText("ecc83-pp/  (KiCad demo board)")

        def back_side():
            # ecc83 is single-sided through-hole: its traces are on B.Cu
            win.side.setCurrentIndex(1)
            for L in win.layers:
                if L.key in ("top_copper", "bottom_copper") or L.key.startswith("drill"):
                    L.enabled = L.key != "top_copper"
            win.rebuild_layer_ui()
            win.refresh()

        # (file name, controls to set) — applied in order, each on top of the last
        shots = [
            ("gui-back.png", back_side),
            ("gui-invert-raster.png", lambda: (win.cb_invert.setChecked(True),
                                               win.cb_reg.setChecked(True),
                                               win.pmode.setCurrentIndex(1))),
        ]

        def step(i=0):
            if i:
                name = shots[i - 1][0]
                win.grab().save(str(OUT / name))
                print("wrote", OUT / name)
            if i == len(shots):
                app.quit()
                return
            shots[i][1]()
            QTimer.singleShot(2500, lambda: step(i + 1))

        QTimer.singleShot(2500, step)
        return base_exec()

    base_exec = QApplication.exec
    QApplication.exec = scripted_exec
    try:
        gerber2laser.run_gui(preload=board)
    except SystemExit:
        pass

    subprocess.run([sys.executable, str(ROOT / "gerber2laser.py"), "--coupon",
                    "--dpi", "200", "--png", str(OUT / "coupon.png")], check=True)


if __name__ == "__main__":
    main()
