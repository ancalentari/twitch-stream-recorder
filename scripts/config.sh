#!/bin/bash

# Twitch Stream Recorder - Shared Configuration
# This file is sourced by all scripts to provide consistent paths and settings

# Base directory - auto-detect or use environment variable
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TWITCH_RECORDER_BASE="${TWITCH_RECORDER_BASE:-$(dirname "$SCRIPT_DIR")}"

# Core directories
export TWITCH_RECORDER_DIR="$TWITCH_RECORDER_BASE"
export TWITCH_RECORDER_SCRIPTS="$TWITCH_RECORDER_BASE/scripts"
export TWITCH_RECORDER_LOGS="$TWITCH_RECORDER_BASE/logs"

# Configuration file
export TWITCH_RECORDER_CONFIG="${TWITCH_RECORDER_CONFIG:-$TWITCH_RECORDER_BASE/config.json}"
[ ! -f "$TWITCH_RECORDER_CONFIG" ] && TWITCH_RECORDER_CONFIG="$TWITCH_RECORDER_BASE/config/config.json"

# Prefer root_path from config so scripts and Python use identical recording locations.
CONFIG_ROOT_PATH=""
if [ -f "$TWITCH_RECORDER_CONFIG" ]; then
	if command -v jq >/dev/null 2>&1; then
		CONFIG_ROOT_PATH="$(jq -r '.root_path // empty' "$TWITCH_RECORDER_CONFIG" 2>/dev/null)"
	fi

	if [ -z "$CONFIG_ROOT_PATH" ] && command -v python3 >/dev/null 2>&1; then
		CONFIG_ROOT_PATH="$(python3 - "$TWITCH_RECORDER_CONFIG" <<'PY'
import json
import sys

try:
	with open(sys.argv[1], 'r', encoding='utf-8') as f:
		data = json.load(f)
	value = data.get('root_path')
	if isinstance(value, str) and value.strip():
		print(value.strip())
except Exception:
	pass
PY
)"
	fi
fi

export TWITCH_RECORDER_RECORDING="${TWITCH_RECORDER_RECORDING:-${CONFIG_ROOT_PATH:-$TWITCH_RECORDER_BASE/recording}}"
export TWITCH_RECORDER_RECORDED="$TWITCH_RECORDER_RECORDING/recorded"
export TWITCH_RECORDER_PROCESSED="$TWITCH_RECORDER_RECORDING/processed"
export TWITCH_RECORDER_BACKUP="$TWITCH_RECORDER_RECORDING/backup"

# Log files
export TWITCH_RECORDER_MAIN_LOG="$TWITCH_RECORDER_BASE/twitch-recorder.log"
export TWITCH_RECORDER_VALIDATION_LOG="$TWITCH_RECORDER_LOGS/validation.log"
export TWITCH_RECORDER_REPAIR_LOG="$TWITCH_RECORDER_LOGS/repair.log"
export TWITCH_RECORDER_CLEANUP_LOG="$TWITCH_RECORDER_LOGS/cleanup.log"

# Ensure log directory exists
mkdir -p "$TWITCH_RECORDER_LOGS"
