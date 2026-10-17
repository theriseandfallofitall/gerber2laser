# Command line

Everything the GUI does, scriptable and without a display:

```bash
# front side, engrave PNG at 1000 DPI
python gerber2laser.py --input MyBoard/ --side front --png front.png

# back side: auto-mirrored, with registration marks + LightBurn colour SVG
python gerber2laser.py --input MyBoard.zip --side back --reg --svg back.svg

# pick exact layers, add drills, apply calibrated kerf
python gerber2laser.py --input MyBoard/ --side back \
    --layers bottom_copper,outline,drill --reg --kerf -0.05 --png back.png

# force one layer's colour — used verbatim, even under --invert
python gerber2laser.py --input MyBoard/ --side front \
    --invert --layer-color drill1=black --svg front.svg

# generate a kerf calibration coupon
python gerber2laser.py --coupon --png kerf_cal.png
```

- With no arguments, it launches the GUI. With `--input` alone (no
  `--png`/`--svg`), it launches the GUI with that folder or zip already loaded.
  `python gerber2laser.py --help` lists every option.
- `--side front` enables top copper and the outline. `--side back` enables
  bottom copper and the outline, and mirrors.
- `--layers` overrides that selection. The keys are `top_copper`,
  `bottom_copper`, `inner_copper`, `outline`, `f_silk`, `b_silk`, `f_mask`,
  `b_mask`, and `drill1`, `drill2`, … (one per drill file). It also matches on
  substrings, so `--layers drill` catches every drill file.
- `--layer-color KEY=COLOUR` can be repeated. It wins over both Invert and the
  derived drill colour.
- `--bg none` gives a **transparent** PNG. A background colour Qt cannot parse
  (a typo) warns and falls back to transparent, not to black, because black
  would mean "fire the laser over the whole sheet".

`--side` only selects layers; it does not run the GUI's auto-detection. Asking
for a side the export has no copper for still produces a file with only the
outline in it, so the CLI warns on stderr and names the side that *is* present:

```
warning: --side back enables bottom_copper, which is not in MyBoard/ —
this export has F.Cu (--side front). The output will contain no copper.
```

On a machine with no display, set `QT_QPA_PLATFORM=offscreen` (PowerShell:
`$env:QT_QPA_PLATFORM='offscreen'`). Only the Qt window itself needs a real
display.
