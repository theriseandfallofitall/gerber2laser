#!/usr/bin/env bash
# Install wizard for gerber2laser on macOS.
#
# Double-click this file in Finder, or run it from Terminal. It creates a
# private virtualenv with PySide6 + gerbonara, builds a gerber2laser.app you
# can launch from Launchpad or the Dock, puts an alias on the Desktop, and
# optionally registers the pcbnew action plugin. KiCad itself is never
# installed or modified.
#
#   ./install/macos/install.command --yes    take every default
#
# Flags: --yes  --prefix DIR  --no-shortcuts  --no-kicad-plugin
set -euo pipefail

APP=gerber2laser
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFIX="$HOME/Library/Application Support/$APP"
APPBUNDLE="$HOME/Applications/$APP.app"
ASSUME_YES=0
DO_SHORTCUTS=1
DO_PLUGIN=1

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)          ASSUME_YES=1 ;;
    --prefix)          PREFIX="$2"; shift ;;
    --no-shortcuts)    DO_SHORTCUTS=0 ;;
    --no-kicad-plugin) DO_PLUGIN=0 ;;
    -h|--help)         sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step()  { printf '\n\033[36m[%s] %s\033[0m\n' "$1" "$2"; }
note()  { printf '    \033[90m%s\033[0m\n' "$1"; }
ask()   {
  if [ "$ASSUME_YES" = 1 ]; then printf '%s' "$2"; return; fi
  read -r -p "    $1 [$2] " a </dev/tty || a=""
  printf '%s' "${a:-$2}"
}
askyn() {
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
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "    No Python 3.9+ found." >&2
  note "Install it with:  brew install python"
  note "or download it from https://www.python.org/downloads/macos/"
  exit 1
fi
note "using $($PY --version 2>&1) ($(command -v $PY))"
if [ "$(uname -m)" = "arm64" ] && ! "$PY" -c 'import platform,sys; sys.exit(0 if platform.machine()=="arm64" else 1)'; then
  note "warning: that Python is Intel (x86_64) on an Apple Silicon Mac — PySide6 will run under Rosetta"
fi

# ----------------------------------------------------------------- location
step 2 "Choosing where to install"
PREFIX="$(ask 'Support folder' "$PREFIX")"
note "$PREFIX"
mkdir -p "$PREFIX"

# -------------------------------------------------------------------- files
step 3 "Copying application files"
cp -f "$SOURCE/gerber2laser.py" "$PREFIX/gerber2laser.py"
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

# ------------------------------------------------------------- app bundle
if [ "$DO_SHORTCUTS" = 1 ]; then
  step 5 "Building $APP.app"
  APPBUNDLE="$(ask 'App bundle path' "$APPBUNDLE")"
  rm -rf "$APPBUNDLE"
  mkdir -p "$APPBUNDLE/Contents/MacOS" "$APPBUNDLE/Contents/Resources"
  cat > "$APPBUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>gerber2laser</string>
  <key>CFBundleDisplayName</key>       <string>gerber2laser</string>
  <key>CFBundleIdentifier</key>        <string>dev.gerber2laser.app</string>
  <key>CFBundleVersion</key>           <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleExecutable</key>        <string>gerber2laser</string>
  <key>CFBundleIconFile</key>          <string>gerber2laser</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>LSMinimumSystemVersion</key>    <string>10.15</string>
</dict>
</plist>
EOF
  cat > "$APPBUNDLE/Contents/MacOS/$APP" <<EOF
#!/usr/bin/env bash
# gerber2laser — runs from its own venv, not the system Python
exec "$PREFIX/venv/bin/python" "$PREFIX/gerber2laser.py" "\$@"
EOF
  chmod +x "$APPBUNDLE/Contents/MacOS/$APP"
  cp -f "$SOURCE/assets/gerber2laser.icns" \
        "$APPBUNDLE/Contents/Resources/gerber2laser.icns"
  # make Finder pick up the new icon straight away
  touch "$APPBUNDLE"
  note "$APPBUNDLE"

  if askyn "Put an alias on the Desktop?" y; then
    ln -sfn "$APPBUNDLE" "$HOME/Desktop/$APP.app"
    note "$HOME/Desktop/$APP.app"
  fi
fi

# ------------------------------------------------------------- kicad plugin
if [ "$DO_PLUGIN" = 1 ]; then
  step 6 "Registering the KiCad plugin (optional)"
  PLUGDIR=""
  for base in "$HOME/Documents/KiCad" "$HOME/Library/Preferences/kicad"; do
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
step 7 "Writing the uninstaller"
cat > "$PREFIX/uninstall.command" <<EOF
#!/usr/bin/env bash
# Removes gerber2laser. Does not touch KiCad or Python.
set -u
rm -rf "$PREFIX" "$APPBUNDLE" "$HOME/Desktop/$APP.app"
rm -f "${PLUGDIR:-/nonexistent}/gerber2laser.py" "${PLUGDIR:-/nonexistent}/.g2l_python" \
      "${PLUGDIR:-/nonexistent}/gerber2laser_24.png"
echo "gerber2laser removed."
EOF
chmod +x "$PREFIX/uninstall.command"
note "$PREFIX/uninstall.command"

echo
printf '\033[32m  Done.\033[0m\n'
echo "  Open it from Launchpad, the Desktop alias, or: open '$APPBUNDLE'"
echo "  First launch: right-click the app and choose Open, so Gatekeeper lets an"
echo "  unsigned local app through."
echo
