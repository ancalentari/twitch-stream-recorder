#!/bin/bash

# Clean up old Twitch Recorder logs
# Usage: ./cleanup-logs.sh [days] (default: 30)

# Load shared configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

RECORDER_DIR="$TWITCH_RECORDER_DIR"
LOGS_DIR="$TWITCH_RECORDER_LOGS"
DAYS="${1:-30}"

# Colors
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== Twitch Recorder Log Cleanup ===${NC}"
echo "Removing logs older than ${DAYS} days..."
echo ""

# Count files before
BEFORE=$(find "$LOGS_DIR" -name "*.log" -type f | wc -l)
SIZE_BEFORE=$(du -sh "$LOGS_DIR" 2>/dev/null | cut -f1)

# Remove old logs
DELETED=$(find "$LOGS_DIR" -name "twitch-recorder-*.log" -type f -mtime +${DAYS} -delete -print | wc -l)

# Count files after
AFTER=$(find "$LOGS_DIR" -name "*.log" -type f | wc -l)
SIZE_AFTER=$(du -sh "$LOGS_DIR" 2>/dev/null | cut -f1)

echo -e "${GREEN}Cleanup complete!${NC}"
echo "  Files before: $BEFORE"
echo "  Files deleted: $DELETED"
echo "  Files remaining: $AFTER"
echo "  Space before: $SIZE_BEFORE"
echo "  Space after: $SIZE_AFTER"
echo ""

# Show remaining logs
echo -e "${YELLOW}Recent logs:${NC}"
ls -lht "$LOGS_DIR"/*.log 2>/dev/null | head -10 | awk '{print "  " $9 " (" $5 ")"}'

exit 0
