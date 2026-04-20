#!/usr/bin/env bash
set -e

INSTALL_DIR="$HOME/.local/bin"
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

# Check Python version (need 3.11+ for tomllib)
PYTHON=$(command -v python3 || true)
if [[ -z "$PYTHON" ]]; then
    echo "❌ python3 not found. Install Python 3.11+ and try again."
    exit 1
fi

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MINOR=$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')
PY_MAJOR=$("$PYTHON" -c 'import sys; print(sys.version_info.major)')

if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ) ]]; then
    echo "❌ Python 3.11+ required (found $PY_VERSION). tomllib is only in stdlib from 3.11."
    exit 1
fi

# Copy and make executable
cp "$(dirname "$0")/$SCRIPT_NAME" "$INSTALL_DIR/$SCRIPT_NAME"
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"

echo "✅ Installed xsync to $INSTALL_DIR/$SCRIPT_NAME"
echo "   Run 'xsync --help' or 'xsync --init' in a project directory to get started."