#!/usr/bin/env bash
# Install wizard for gerber2laser on Linux.
#
# Creates a private virtualenv with PySide6 + gerbonara, a launcher on your
# PATH, a desktop entry with icon, and (optionally) registers the pcbnew
# action plugin. KiCad itself is never installed or modified.
#
#   ./install/linux/install.sh            interactive
#   ./install/linux/install.sh --yes      take every default
#
# Flags: --yes  --prefix DIR  --no-shortcuts  --no-kicad-plugin
set -euo pipefail

APP=gerber2laser
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
BINDIR="$HOME/.local/bin"
APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONROOT="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
ASSUME_YES=0
DO_SHORTCUTS=1
DO_PLUGIN=1

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)          ASSUME_YES=1 ;;
    --prefix)          PREFIX="$2"; shift ;;
    --no-shortcuts)    DO_SHORTCUTS=0 ;;
    --no-kicad-plugin) DO_PLUGIN=0 ;;
    -h|--help)         sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step()  { printf '\n\033[36m[%s] %s\033[0m\n' "$1" "$2"; }
note()  { printf '    \033[90m%s\033[0m\n' "$1"; }
ask()   { # ask "question" "default"
  if [ "$ASSUME_YES" = 1 ]; then printf '%s' "$2"; return; fi
  read -r -p "    $1 [$2] " a </dev/tty || a=""
  printf '%s' "${a:-$2}"
}
askyn() { # askyn "question" y|n
  if [ "$ASSUME_YES" = 1 ]; then [ "$2" = y ]; return; fi
  local d="y/N"; [ "$2" = y ] && d="Y/n"
  read -r -p "    $1 [$d] " a </dev/tty || a=""
  a="${a:-$2}"
  case "$a" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

echo
echo "  gerber2laser installer"
echo "  Turns KiCad Gerbers into laser-ready art for CO2 resist etching."
echo "  This installs Python dependencies only. KiCad is not installed or changed."

# ------------------------------------------------------------------- python
step 1 "Looking for Python 3.9 or newer"
PY=""
for c in python3 python; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "    No Python 3.9+ found." >&2
  note "Debian/Ubuntu: sudo apt install python3 python3-venv"
  note "Fedora:        sudo dnf install python3"
  note "Arch:          sudo pacman -S python"
  exit 1
fi
note "using $($PY --version) ($(command -v $PY))"
if ! "$PY" -c 'import venv' 2>/dev/null; then
  echo "    Python is missing the venv module." >&2
  note "Debian/Ubuntu: sudo apt install python3-venv"
  exit 1
fi

# ----------------------------------------------------------------- location
step 2 "Choosing where to install"
PREFIX="$(ask 'Install folder' "$PREFIX")"
note "$PREFIX"
mkdir -p "$PREFIX"

# -------------------------------------------------------------------- files
step 3 "Copying application files"
install -m 644 "$SOURCE/gerber2laser.py" "$PREFIX/gerber2laser.py"
mkdir -p "$PREFIX/assets"
cp -f "$SOURCE"/assets/* "$PREFIX/assets/"
for extra in README.md requirements.txt LICENSE; do
  [ -f "$SOURCE/$extra" ] && cp -f "$SOURCE/$extra" "$PREFIX/"
done
note "gerber2laser.py + assets"

# --------------------------------------------------------------------- venv
step 4 "Creating the virtualenv and installing dependencies"
[ -x "$PREFIX/venv/bin/python" ] || "$PY" -m venv "$PREFIX/venv"
VENV_PY="$PREFIX/venv/bin/python"
note "installing pyside6 + gerbonara (this pulls ~100 MB and takes a minute)"
"$VENV_PY" -m pip install --upgrade pip --quiet
if [ -f "$SOURCE/requirements.txt" ]; then
  "$VENV_PY" -m pip install -r "$SOURCE/requirements.txt" --quiet
else
  "$VENV_PY" -m pip install pyside6 gerbonara --quiet
fi
"$VENV_PY" -c 'import PySide6, gerbonara'
note "dependencies OK"

# ----------------------------------------------------------------- launcher
step 5 "Writing the launcher"
mkdir -p "$BINDIR"
cat > "$BINDIR/$APP" <<EOF
#!/usr/bin/env bash
# gerber2laser — runs from its own venv, not the system Python
exec "$PREFIX/venv/bin/python" "$PREFIX/gerber2laser.py" "\$@"
EOF
chmod +x "$BINDIR/$APP"
note "$BINDIR/$APP"
case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) note "note: $BINDIR is not on your PATH — add it to ~/.profile" ;;
esac

# ---------------------------------------------------------------- shortcuts
if [ "$DO_SHORTCUTS" = 1 ]; then
  step 6 "Creating the desktop entry"
  for s in 16 24 32 48 64 128 256 512; do
    src="$SOURCE/assets/gerber2laser_$s.png"
    [ -f "$src" ] || continue
    mkdir -p "$ICONROOT/${s}x${s}/apps"
    cp -f "$src" "$ICONROOT/${s}x${s}/apps/$APP.png"
  done
  mkdir -p "$APPDIR"
  DESKTOP_FILE="$APPDIR/$APP.desktop"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=gerber2laser
GenericName=PCB laser art
Comment=Turn KiCad Gerbers into laser-ready art for CO2 resist etching
Exec=$BINDIR/$APP %f
Icon=$APP
Terminal=false
Categories=Graphics;Engineering;Electronics;
MimeType=application/zip;inode/directory;
Keywords=pcb;gerber;laser;kicad;etch;
EOF
  chmod +x "$DESKTOP_FILE"
  note "$DESKTOP_FILE"
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPDIR" 2>/dev/null || true
  command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -q "$ICONROOT" 2>/dev/null || true

  DESKDIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  if [ -d "$DESKDIR" ] && askyn "Put a launcher on the Desktop too?" y; then
    cp -f "$DESKTOP_FILE" "$DESKDIR/$APP.desktop"
    chmod +x "$DESKDIR/$APP.desktop"
    # GNOME 42+ will not run a desktop file it does not trust
    command -v gio >/dev/null 2>&1 && \
      gio set "$DESKDIR/$APP.desktop" metadata::trusted true 2>/dev/null || true
    note "$DESKDIR/$APP.desktop"
  fi
fi

# ------------------------------------------------------------- kicad plugin
if [ "$DO_PLUGIN" = 1 ]; then
  step 7 "Registering the KiCad plugin (optional)"
  PLUGDIR=""
  for base in "${XDG_DATA_HOME:-$HOME/.local/share}/kicad" "$HOME/.config/kicad"; do
    [ -d "$base" ] || continue
    for v in $(ls -1r "$base" 2>/dev/null); do
      [ -d "$base/$v" ] || continue
      PLUGDIR="$base/$v/scripting/plugins"; break
    done
    [ -n "$PLUGDIR" ] && break
  done
  if [ -z "$PLUGDIR" ]; then
    note "no KiCad user folder found — skipping (run this again after KiCad has started once)"
  elif askyn "Install the pcbnew plugin into $PLUGDIR ?" y; then
    mkdir -p "$PLUGDIR"
    cp -f "$PREFIX/gerber2laser.py" "$PLUGDIR/"
    cp -f "$PREFIX/assets/gerber2laser_24.png" "$PLUGDIR/"
    # KiCad's bundled Python has no PySide6, and a plugin cannot set KiCad's
    # environment, so pin the venv interpreter in a file the plugin reads.
    printf '%s\n' "$PREFIX/venv/bin/python" > "$PLUGDIR/.g2l_python"
    note "installed — restart pcbnew, then Tools > External Plugins > Gerber to Laser"
  fi
fi

# ---------------------------------------------------------------- uninstall
step 8 "Writing the uninstaller"
cat > "$PREFIX/uninstall.sh" <<EOF
#!/usr/bin/env bash
# Removes gerber2laser. Does not touch KiCad or Python.
set -u
rm -rf "$PREFIX"
rm -f "$BINDIR/$APP" "$APPDIR/$APP.desktop"
rm -f "\${HOME}/Desktop/$APP.desktop"
for s in 16 24 32 48 64 128 256 512; do
  rm -f "$ICONROOT/\${s}x\${s}/apps/$APP.png"
done
rm -f "${PLUGDIR:-/nonexistent}/gerber2laser.py" "${PLUGDIR:-/nonexistent}/.g2l_python" \
      "${PLUGDIR:-/nonexistent}/gerber2laser_24.png"
echo "gerber2laser removed."
EOF
chmod +x "$PREFIX/uninstall.sh"
note "$PREFIX/uninstall.sh"

echo
printf '\033[32m  Done.\033[0m\n'
echo "  Launch it from your application menu, or run: $APP"
echo
