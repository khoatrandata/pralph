#!/usr/bin/env bash
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$PROJ_DIR/.venv"

# --- Find a suitable Python (>=3.10) ---
PYTHON=""
for p in python3.13 python3.12 python3.11 python3; do
    if command -v "$p" &>/dev/null; then
        PYTHON="$p"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "Error: No suitable python3 found on PATH." >&2
    exit 1
fi

echo "Using $($PYTHON --version) ($PYTHON)"

# --- Create venv ---
if [[ -d "$VENV" ]]; then
    echo "Removing existing venv..."
    rm -rf "$VENV"
fi

echo "Creating venv at $VENV ..."
"$PYTHON" -m venv "$VENV"

# --- Install requirements + package ---
echo "Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJ_DIR/requirements.txt"
"$VENV/bin/pip" install --quiet -e "$PROJ_DIR"

echo "Installation complete."

# --- Add bin/ to shell PATH ---
BIN_DIR="$PROJ_DIR/bin"
PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""

# Detect shell rc file
if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
    RC_FILE="$HOME/.zshrc"
else
    RC_FILE="$HOME/.bashrc"
fi

if grep -qF "$BIN_DIR" "$RC_FILE" 2>/dev/null; then
    echo "PATH already configured in $RC_FILE"
else
    echo "" >> "$RC_FILE"
    echo "# pralph" >> "$RC_FILE"
    echo "$PATH_LINE" >> "$RC_FILE"
    echo "Added $BIN_DIR to PATH in $RC_FILE"
    echo "Run: source $RC_FILE  (or open a new terminal)"
fi
