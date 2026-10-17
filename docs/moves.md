# Moves log

Every file or folder that moved or was renamed in this repo, newest first. It is for anything outside
this repo that names one of its paths: other repos, servers, synced folders, memory files and other
LLM sessions. To fix your side: find the old path in the table, replace it with the new one, and
check the "What else changes" column.

Rules (`CLAUDE.md`, Repo layout): a move gets its rows here in the same commit, with every mention
inside this repo already updated. Old paths in `docs/archive/` stay as written: that is history.

## 2026-10-16: repo organised

| Old path | New path | What else changes |
|---|---|---|
| `tools/make_icons.py` | `tools/icons/make_icons.py` | Run it as `python tools/icons/make_icons.py`; nothing outside the repo named it |
| README.md, how-it-works / orientation / registration / preview / drill / scale-bar sections | `docs/user-guide.md` | README anchors to those sections are gone |
| README.md, Kerf + Calibration sections | `docs/kerf-calibration.md` | — |
| README.md, Output formats + Notes/limits sections | `docs/output-formats.md` | — |
| README.md, Headless CLI section | `docs/cli.md` | — |
| README.md, Standalone / KiCad plugin section | `docs/kicad-plugin.md` | — |
| CLAUDE.md, Suggestions & Next steps | `TODO.md` | — |
