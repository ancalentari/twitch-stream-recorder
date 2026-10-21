#!/bin/bash
# Unified dashboard. Use --once for a plain text snapshot or --json for automation.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/dashboard.py" "$@"
