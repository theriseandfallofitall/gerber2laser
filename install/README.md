# Installing gerber2laser

Each installer is a wizard: it asks where to install, whether you want a
desktop item, and whether to register the KiCad plugin. Answer nothing and the
defaults apply. Add `--yes` (or `-Yes` on Windows) to accept every default
without being asked.

All three do the same five things:

1. find a Python 3.9+ (and tell you how to get one if there isn't one),
2. create a private virtualenv and install PySide6 + gerbonara into it — your
   system Python is left alone,
3. copy the app and its icons into place,
4. make a launcher, a menu entry and a desktop item,
5. optionally drop the pcbnew action plugin into your KiCad user folder,
   pinned to the virtualenv it just built.

**KiCad is never installed or modified**, and you do not need KiCad to use
gerber2laser — it reads Gerber and Excellon files from any tool. The plugin
step just adds a toolbar button to pcbnew if you happen to have it.

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File install\windows\install.ps1
```

Installs to `%LOCALAPPDATA%\Programs\gerber2laser`, with shortcuts on the
Desktop and in the Start Menu. Uninstall with the `uninstall.ps1` written into
the install folder.

## macOS

```bash
./install/macos/install.command          # or double-click it in Finder
```

Builds `~/Applications/gerber2laser.app` (Launchpad, Dock, Spotlight) and an
alias on the Desktop. The app is unsigned, so on first launch right-click it
and choose **Open** once. Uninstall with `uninstall.command` in
`~/Library/Application Support/gerber2laser`.

## Linux

```bash
./install/linux/install.sh
```

Installs to `~/.local/share/gerber2laser`, puts `gerber2laser` in
`~/.local/bin`, and adds a `.desktop` entry plus hicolor icons — so it shows up
in your application menu, with an optional copy on the Desktop. Uninstall with
`uninstall.sh` in the install folder.

Needs `python3-venv` on Debian/Ubuntu (`sudo apt install python3-venv`).

## The KiCad plugin

KiCad's bundled Python has neither PySide6 nor gerbonara, so the plugin plots
the open board's Gerbers and hands off to the virtualenv the installer built.
It finds that interpreter through a `.g2l_python` file the installer writes
next to the plugin; the `G2L_PYTHON` environment variable overrides it.

After installing, restart pcbnew — the action appears under
**Tools → External Plugins → Gerber → Laser**, and on the toolbar.
