# KiCad plugin (optional)

**You do not need KiCad to use gerber2laser.** It reads Gerber and Excellon
files from any tool, and nothing in the GUI or CLI touches `pcbnew`. The core is
standalone on purpose:

- KiCad's Python/`pcbnew` API breaks between versions, and the core does not
  depend on it.
- It works on any Gerber export from any tool.
- `gerbonara` gives clean per-layer geometry and a common bounding box, so front
  and back align to the same origin, which is essential for the flip.

The same file also works as a **KiCad action plugin**, which adds a **"Gerber →
Laser"** button to pcbnew. The button plots the open board's Gerbers and drills
to a temporary folder and opens them in the gerber2laser GUI.

## Setting it up

The installers can do this for you ([install/README.md](../install/README.md)).
To do it by hand:
1. Copy (or symlink) `gerber2laser.py` into KiCad's plugin folder. On Windows
   that is `Documents/KiCad/<version>/scripting/plugins/`; on any system,
   *Tools → External Plugins → Open Plugin Directory* opens it.
2. Restart pcbnew. The action appears under **Tools → External Plugins →
   Gerber → Laser**, and on the toolbar.

## Which Python it runs

KiCad's bundled Python does not include PySide6, so the plugin launches the GUI
with an **external** Python. It looks for one in this order:
1. the `G2L_PYTHON` environment variable,
2. a `.g2l_python` file next to the plugin (this is how the installers point it
   at the virtualenv they built, since a plugin cannot set KiCad's own
   environment),
3. plain `python` on PATH.
