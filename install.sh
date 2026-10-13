#!/usr/bin/env bash
set -e

INSTALL_DIR="$HOME/.local/bin"
VENV_DIR="$HOME/.local/share/xsync/venv"
SCRIPT_NAME="xsync"

# Ensure install dir exists and is on PATH
mkdir -p "$INSTALL_DIR"

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$INSTALL_DIR"; then
    SHELL_RC="$HOME/.zshrc"
    [[ "$SHELL" == *bash* ]] && SHELL_RC="$HOME/.bashrc"
    echo "export PATH=\"$INSTALL_DIR:\$PATH\"" >> "$SHELL_RC"
    echo "Added $INSTALL_DIR to PATH in $SHELL_RC"
    echo "Restart your shell or run: source $SHELL_RC"
fi

# Find Python 3.11+
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON=$(command -v "$candidate")
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    _py=$(command -v python3 || true)
    if [[ -n "$_py" ]]; then
        _minor=$("$_py" -c 'import sys; print(sys.version_info.minor)')
        _major=$("$_py" -c 'import sys; print(sys.version_info.major)')
        if [[ "$_major" -eq 3 && "$_minor" -ge 11 ]]; then
            PYTHON="$_py"
        fi
    fi
fi

if [[ -z "$PYTHON" ]]; then
    echo "error: Python 3.11+ not found. Install it (e.g. brew install python@3.13) and try again."
    exit 1
fi

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Using $PYTHON ($PY_VERSION)"

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# A dedicated venv, so xsync and watchdog never touch the system Python
mkdir -p "$(dirname "$VENV_DIR")"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating venv at $VENV_DIR..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python3"

# watchdog is what watch mode uses to notice file changes
if "$VENV_PYTHON" -c "import watchdog" 2>/dev/null; then
    echo "watchdog already installed."
else
    echo "Installing watchdog..."
    "$VENV_PYTHON" -m pip install --quiet "watchdog>=3"
    echo "watchdog installed."
fi

# Install xsync itself from this checkout; rerun this script after pulling changes
echo "Installing xsync from $REPO_DIR..."
"$VENV_PYTHON" -m pip install --quiet --force-reinstall --no-deps "$REPO_DIR"

# Put the command on PATH. An older install left a script here; replace it with a link.
rm -f "$INSTALL_DIR/$SCRIPT_NAME"
ln -s "$VENV_DIR/bin/$SCRIPT_NAME" "$INSTALL_DIR/$SCRIPT_NAME"

echo "Installed xsync $("$INSTALL_DIR/$SCRIPT_NAME" --version | cut -d' ' -f2) to $INSTALL_DIR/$SCRIPT_NAME"
echo "   Run 'xsync' in a terminal. Press a to add a project."
