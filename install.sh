#!/usr/bin/env bash
#
# docjuice installer.
#
# Creates a private virtual environment, installs the Python dependencies into
# it, and puts a `docjuice` launcher on your PATH so you can run the tool from
# any directory without activating anything.
#
#   ./install.sh              install (or repair) docjuice
#   ./install.sh --uninstall  remove the launcher and the virtual environment
#
set -euo pipefail

ORANGE='\033[38;5;208m'
LIGHT='\033[38;5;215m'
DEEP='\033[38;5;166m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
DIM='\033[2m'
OFF='\033[0m'

# Colour only when attached to a terminal.
if [ ! -t 1 ]; then
    ORANGE=''; LIGHT=''; DEEP=''; GREEN=''; YELLOW=''; RED=''; DIM=''; OFF=''
fi

APP_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${DOCJUICE_VENV:-$HOME/.local/share/docjuice/venv}"
BIN_DIR="${DOCJUICE_BIN:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/docjuice"
REQUIRED_FILES=(docjuice validate.py clean.py requirements.txt)

banner() {
    printf "\n"
    # Full-colour logo when possible: logo.py needs only the standard library,
    # so it works before the virtual environment exists. Falls back to the
    # compact wordmark if it is missing, the terminal is too narrow, or
    # output is not a terminal.
    # logo.py itself checks the terminal width and exits non-zero if the
    # logo will not fit, so no width is hard-coded here.
    if [ -t 1 ] && [ -f "$APP_DIR/logo.py" ] \
        && command -v python3 >/dev/null 2>&1 \
        && python3 "$APP_DIR/logo.py" 2>/dev/null; then
        printf "\n"
    fi
    printf "${DEEP}████▄   ▄▄▄   ▄▄▄▄     ▄▄ ▄▄ ▄▄ ▄▄  ▄▄▄▄ ▄▄▄▄▄ ${OFF}\n"
    printf "${ORANGE}██  ██ ██▀██ ██▀▀▀     ██ ██ ██ ██ ██▀▀▀ ██▄▄  ${OFF}\n"
    printf "${LIGHT}████▀  ▀███▀ ▀████   ▄▄█▀ ▀███▀ ██ ▀████ ██▄▄▄ ${OFF}\n"
    printf "${LIGHT}  installer${OFF}\n\n"
}

say()  { printf "${ORANGE}==>${OFF} %s\n" "$1"; }
ok()   { printf "    ${GREEN}%s${OFF}\n" "$1"; }
warn() { printf "    ${YELLOW}%s${OFF}\n" "$1"; }
die()  { printf "\n${RED}ERROR:${OFF} %s\n" "$1" >&2; exit 1; }
note() { printf "    ${DIM}%s${OFF}\n" "$1"; }

# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
    banner
    say "Removing docjuice"
    [ -e "$LAUNCHER" ] && rm -f "$LAUNCHER" && ok "removed $LAUNCHER" || note "no launcher at $LAUNCHER"
    if [ -d "$VENV_DIR" ]; then
        rm -rf "$VENV_DIR"
        ok "removed $VENV_DIR"
    else
        note "no virtual environment at $VENV_DIR"
    fi
    printf "\n${GREEN}Done.${OFF} The docjuice source files in %s were left alone.\n\n" "$APP_DIR"
    exit 0
fi

banner

# --------------------------------------------------------------------------
# 1. Check the source files are all here
# --------------------------------------------------------------------------
say "Checking source files in $APP_DIR"
for f in "${REQUIRED_FILES[@]}"; do
    [ -f "$APP_DIR/$f" ] || die "missing '$f' in $APP_DIR
  All of these must sit in the same folder: ${REQUIRED_FILES[*]}"
done
ok "all ${#REQUIRED_FILES[@]} files present"

# --------------------------------------------------------------------------
# 2. Check Python
# --------------------------------------------------------------------------
say "Checking Python"
command -v python3 >/dev/null 2>&1 || die "python3 was not found.
  Fix: sudo apt install python3"

PY_OK=$(python3 -c 'import sys; print(1 if (3,10) <= sys.version_info < (3,15) else 0)')
PY_VER=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
if [ "$PY_OK" != "1" ]; then
    die "Python $PY_VER is outside the range markitdown supports (3.10 - 3.14).
  Install a supported version and re-run, e.g.:
      sudo apt install python3.12 python3.12-venv
      DOCJUICE_PYTHON=python3.12 ./install.sh"
fi
ok "python $PY_VER"

python3 -c 'import venv' 2>/dev/null || die "the venv module is missing.
  Fix: sudo apt install python3-venv"

# --------------------------------------------------------------------------
# 3. Build the virtual environment
# --------------------------------------------------------------------------
say "Setting up the virtual environment"
note "$VENV_DIR"
if [ -x "$VENV_DIR/bin/python3" ]; then
    ok "already exists, reusing it"
else
    mkdir -p "$(dirname "$VENV_DIR")"
    if ! python3 -m venv "$VENV_DIR"; then
        rm -rf "$VENV_DIR"
        die "could not create the virtual environment (see the output above)."
    fi
    ok "created"
fi

say "Installing Python dependencies"
note "this pulls in markitdown and can take a couple of minutes"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip --quiet 2>/dev/null || true
if ! "$VENV_DIR/bin/python3" -m pip install -r "$APP_DIR/requirements.txt"; then
    die "dependency installation failed (see the pip output above).
  If this is a network problem, fix it and re-run ./install.sh"
fi
ok "dependencies installed"

# Record which requirements this venv was built from, so the launcher can
# notice later edits and re-sync without a full reinstall.
sha256sum "$APP_DIR/requirements.txt" | awk '{print $1}' > "$VENV_DIR/.requirements-hash"

# --------------------------------------------------------------------------
# 4. Write the launcher
# --------------------------------------------------------------------------
say "Installing the launcher"
mkdir -p "$BIN_DIR"

cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/usr/bin/env bash
#
# docjuice launcher -- generated by install.sh on $(date -Iseconds)
#
# Runs docjuice inside its own virtual environment. There is nothing to
# activate and nothing to close: calling the environment's python directly
# is exactly what 'activate' would arrange, and the process exits normally
# when the run finishes.
#
set -euo pipefail

APP_DIR="$APP_DIR"
VENV_DIR="\${DOCJUICE_VENV:-$VENV_DIR}"
PY="\$VENV_DIR/bin/python3"
REQ="\$APP_DIR/requirements.txt"
STAMP="\$VENV_DIR/.requirements-hash"

die() { printf "\033[31mdocjuice:\033[0m %s\n" "\$1" >&2; exit 2; }

[ -f "\$APP_DIR/docjuice" ] || die "the docjuice source is missing from \$APP_DIR.
  If you moved the folder, re-run install.sh from its new location."

# Rebuild the environment if it is missing or broken.
if [ ! -x "\$PY" ]; then
    printf "\033[38;5;208m==>\033[0m first run: building the docjuice environment...\n"
    rm -rf "\$VENV_DIR"
    mkdir -p "\$(dirname "\$VENV_DIR")"
    python3 -m venv "\$VENV_DIR" || die "could not create the virtual environment.
  Fix: sudo apt install python3-venv"
    "\$PY" -m pip install --upgrade pip --quiet 2>/dev/null || true
    "\$PY" -m pip install -r "\$REQ" || die "could not install dependencies."
    sha256sum "\$REQ" | awk '{print \$1}' > "\$STAMP"
fi

# Re-sync if requirements.txt changed since the environment was built.
# Set DOCJUICE_SKIP_CHECK=1 to skip this (it costs a few milliseconds).
if [ "\${DOCJUICE_SKIP_CHECK:-0}" != "1" ]; then
    want="\$(sha256sum "\$REQ" | awk '{print \$1}')"
    have="\$(cat "\$STAMP" 2>/dev/null || echo none)"
    if [ "\$want" != "\$have" ]; then
        printf "\033[38;5;208m==>\033[0m requirements changed, updating the environment...\n"
        "\$PY" -m pip install -r "\$REQ" || die "could not update dependencies."
        printf "%s\n" "\$want" > "\$STAMP"
    fi
fi

# exec replaces this shell, so the exit code, signals (Ctrl+C) and the
# terminal all pass straight through to docjuice.
exec "\$PY" "\$APP_DIR/docjuice" "\$@"
LAUNCHER_EOF

chmod +x "$LAUNCHER"
ok "installed $LAUNCHER"

# --------------------------------------------------------------------------
# 5. PATH check
# --------------------------------------------------------------------------
say "Checking your PATH"
if printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
    ok "$BIN_DIR is on your PATH"
    PATH_OK=1
else
    PATH_OK=0
    warn "$BIN_DIR is NOT on your PATH"
    SHELL_RC="$HOME/.bashrc"
    [ -n "${ZSH_VERSION:-}" ] && SHELL_RC="$HOME/.zshrc"
    case "${SHELL:-}" in */zsh) SHELL_RC="$HOME/.zshrc" ;; esac
    note "add it with:"
    printf "        ${ORANGE}echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> %s${OFF}\n" "$SHELL_RC"
    printf "        ${ORANGE}source %s${OFF}\n" "$SHELL_RC"
fi

# --------------------------------------------------------------------------
# 6. Optional system tools
# --------------------------------------------------------------------------
say "Checking optional system tools"
MISSING_APT=()
command -v ocrmypdf  >/dev/null 2>&1 || MISSING_APT+=("ocrmypdf")
command -v tesseract >/dev/null 2>&1 || MISSING_APT+=("tesseract-ocr")
command -v 7z        >/dev/null 2>&1 || MISSING_APT+=("p7zip-full")
[ -f /usr/share/dict/words ]         || MISSING_APT+=("wamerican")

if [ ${#MISSING_APT[@]} -eq 0 ]; then
    ok "ocrmypdf, tesseract, 7z and a system dictionary are all present"
else
    warn "not installed: ${MISSING_APT[*]}"
    note "docjuice works without these, but they improve it:"
    note "  ocrmypdf + tesseract-ocr : read scanned PDFs (otherwise they convert to nothing)"
    note "  p7zip-full               : widest archive format support"
    note "  wamerican                : better gibberish detection"
    printf "        ${ORANGE}sudo apt install %s${OFF}\n" "${MISSING_APT[*]}"
fi

# --------------------------------------------------------------------------
# 7. Verify
# --------------------------------------------------------------------------
say "Verifying the installation"
if VER_OUT=$("$VENV_DIR/bin/python3" "$APP_DIR/docjuice" --version --no-banner 2>&1); then
    printf "%s\n" "$VER_OUT" | sed 's/^/    /'
    ok "docjuice runs"
else
    die "docjuice did not start. Output:
$VER_OUT"
fi

printf "\n${GREEN}Installed.${OFF}\n\n"
if [ "$PATH_OK" = "1" ]; then
    printf "  Run it from anywhere:\n"
    printf "      ${ORANGE}docjuice /path/to/folder${OFF}\n"
    printf "      ${ORANGE}docjuice /path/to/archive.zip${OFF}\n"
    printf "      ${ORANGE}docjuice --doctor${OFF}\n"
else
    printf "  After fixing your PATH (above), run it from anywhere:\n"
    printf "      ${ORANGE}docjuice /path/to/folder${OFF}\n"
    printf "  Until then, the full path works:\n"
    printf "      ${ORANGE}%s /path/to/folder${OFF}\n" "$LAUNCHER"
fi
printf "\n  ${DIM}Uninstall with: ./install.sh --uninstall${OFF}\n\n"
