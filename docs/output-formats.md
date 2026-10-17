# Output formats

## PNG or SVG

- **PNG**: a flat raster at your DPI. Drop it into LightBurn as an **Engrave**
  image. The export **carries its DPI** (PNG `pHYs`), so LightBurn places it at
  true board size on import. Don't rescale it there. This is the only format
  that includes kerf ([kerf-calibration.md](kerf-calibration.md)).
- **SVG**: vector, with **one colour per feature class**. LightBurn maps each
  *colour to a cut layer*, so in one file you can set copper to **Fill/engrave**
  and `Edge.Cuts` to a **red vector Cut**. That is what the colour buttons are
  for.

Every feature is emitted as a **filled outline**, not a stroked centreline.
LightBurn ignores SVG stroke width, so a trace exported the way the Gerber
describes it (a zero-width path with `stroke-width="0.5"`) would import as a
hairline and fill to nothing. `Edge.Cuts` is the exception: it stays a
centreline, because that is the path LightBurn should cut along.

1000 DPI is a good starting raster resolution for fine traces. Go higher for
0.2 mm pitch.

## File names

Exports are saved next to the source folder or zip and named after the
settings that made them, for example:

```
MyBoard-rev-b_front_inv_mirh_k-0.05_1200dpi_h0.5_m0_reg_sb10_20260813-0800.png
```

so in a folder of attempts you can tell them apart without opening any. Names
are capped at 100 characters because of Windows' 260-character path limit. The
least important tags are dropped first; the board name is shortened only after
that.

## Notes and limits

- The output SVG puts all layers into one root `<svg>` (no nested `<svg>`), so it
  renders the same in Qt, Inkscape, browsers and LightBurn.
- QtSvg silently drops paths with more than about 41,000 vertices, and KiCad zone
  pours exceed that. Before Qt sees them, the paths for the preview and the PNG
  export are thinned (Ramer–Douglas–Peucker, starting at a 2 µm tolerance and
  doubling up to 50 µm only if a path is still over the cap). The exported SVG
  keeps the full-detail geometry.
