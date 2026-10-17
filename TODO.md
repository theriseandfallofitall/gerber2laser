# TODO

Open work. Tick and remove items as they land.

- [ ] A **burn-coloured `--invert-margin`** still falls back to the paint-over form (with a `RuntimeWarning`), so that combination is not LightBurn-ready. Folding it in means making the whole viewBox the outer contour and the registration marks holes in it — but the merged path is anchored to the board frame, and the margin lives in the output frame past the mirror/rotate matrix, so it needs the marks mapped back through that transform first.
- [ ] The kerf progress dialog has no Cancel button, deliberately (a half-built dilation disc is silently the wrong kerf). If a cancel is ever added it must discard the whole image, never save a partial one.
- [ ] Consider a `laser-export` skill if the CLI export sequence (front + back + coupon at calibrated kerf) becomes a repeated ritual. Not warranted yet — one `run_cli` invocation per artefact is still shorter than a skill.
