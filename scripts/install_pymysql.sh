#!/usr/bin/env bash
set -euo pipefail
PYTHON_BIN="${1:-python}"
"$PYTHON_BIN" -m pip install "PyMySQL>=1.1.1,<2" "tomlkit>=0.12,<1"
