#!/bin/bash
# Legacy watch entry point: use the same TUI with an optional refresh interval.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    interval="$1"
    shift
    exec python3 "$SCRIPT_DIR/dashboard.py" --refresh "$interval" "$@"
fi
exec python3 "$SCRIPT_DIR/dashboard.py" "$@"
