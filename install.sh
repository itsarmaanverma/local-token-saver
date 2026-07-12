#!/usr/bin/env bash
# One-shot installer for Local Token Saver.
# Installs the package + all pipeline dependencies (pypdf), verifies FTS5,
# and leaves `token-saver` / `token-saver-mcp` on PATH.
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "ERROR: python3 not found"; exit 1; }

echo "Installing local-token-saver (+ pypdf dependency)..."
"$PY" -m pip install --quiet .

echo "Verifying pipeline dependencies..."
token-saver setup

echo
echo "Done. Quick start:"
echo "  cd <your-folder>"
echo "  token-saver select .                      # init + index (PDF->md->vectors automatic)"
echo "  token-saver mcp install . --claude --codex --protocol"
echo "  token-saver mcp install . --with-proxy       # preview only; changes nothing"
