#!/bin/bash

# Twitch Recorder Validation Script
# Checks for corrupted or incomplete MP4 files
# Run: ./validate-recordings.sh or add to cron

# Load shared configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

RECORDING_DIR="$TWITCH_RECORDER_RECORDED"
LOG_FILE="$TWITCH_RECORDER_VALIDATION_LOG"
MIN_FILE_SIZE=10485760  # 10MB - suspicious if smaller

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

# Log function
log_message() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] $1" >> "$LOG_FILE"
}

echo -e "${BLUE}Validating Twitch Recordings...${NC}\n"
log_message "=== Validation Started ==="

TOTAL_FILES=0
VALID_FILES=0
CORRUPTED_FILES=0
SUSPICIOUS_FILES=0
ISSUES=()

# Check if ffprobe is available
if ! command -v ffprobe &> /dev/null; then
    echo -e "${YELLOW}Warning: ffprobe not found. Install with: sudo apt-get install ffmpeg${NC}"
    echo "Will only check file sizes, not integrity."
    USE_FFPROBE=false
else
    USE_FFPROBE=true
fi

echo -e "${BLUE}Scanning: $RECORDING_DIR${NC}\n"

# Iterate through channels
for channel_dir in "$RECORDING_DIR"/*; do
    if [ ! -d "$channel_dir" ]; then
        continue
    fi
    
    channel=$(basename "$channel_dir")
    echo -e "${BLUE}Channel: $channel${NC}"
    
    # Find all mp4 files
    for file in "$channel_dir"/*.mp4; do
        if [ ! -f "$file" ]; then
            continue
        fi
        
        TOTAL_FILES=$((TOTAL_FILES + 1))
        filename=$(basename "$file")
        filesize=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)
        
        # Check file size
        if [ "$filesize" -lt "$MIN_FILE_SIZE" ]; then
            echo -e "  ${RED}✗ SUSPICIOUS${NC} $filename (${filesize} bytes)"
            SUSPICIOUS_FILES=$((SUSPICIOUS_FILES + 1))
            ISSUES+=("SUSPICIOUS: $channel/$filename - Only ${filesize} bytes")
            log_message "SUSPICIOUS: $filename - Only ${filesize} bytes"
            continue
        fi
        
        # Check file integrity with ffprobe if available
        if [ "$USE_FFPROBE" = true ]; then
            if ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of default=noprint_wrappers=1:nokey=1 "$file" &>/dev/null; then
                echo -e "  ${GREEN}✓${NC} $filename ($(numfmt --to=iec-i --suffix=B $filesize 2>/dev/null || echo $filesize))"
                VALID_FILES=$((VALID_FILES + 1))
            else
                echo -e "  ${RED}✗ CORRUPTED${NC} $filename"
                CORRUPTED_FILES=$((CORRUPTED_FILES + 1))
                ISSUES+=("CORRUPTED: $channel/$filename")
                log_message "CORRUPTED: $filename"
            fi
        else
            # Just check size if ffprobe unavailable
            echo -e "  ${GREEN}✓${NC} $filename (Size OK)"
            VALID_FILES=$((VALID_FILES + 1))
        fi
    done
done

# Summary
echo ""
echo -e "${BLUE}═════════════════════════════════════${NC}"
echo -e "Total Files: $TOTAL_FILES"
echo -e "${GREEN}Valid: $VALID_FILES${NC}"
echo -e "${YELLOW}Suspicious (small): $SUSPICIOUS_FILES${NC}"
echo -e "${RED}Corrupted: $CORRUPTED_FILES${NC}"
echo -e "${BLUE}═════════════════════════════════════${NC}\n"

# Report issues
if [ ${#ISSUES[@]} -gt 0 ]; then
    echo -e "${RED}Issues Found:${NC}"
    for issue in "${ISSUES[@]}"; do
        echo "  • $issue"
    done
    log_message "Found ${#ISSUES[@]} issues"
else
    echo -e "${GREEN}No issues detected!${NC}"
    log_message "Validation complete - No issues"
fi

log_message "=== Validation Ended ==="

# Exit with error code if issues found
if [ ${#ISSUES[@]} -gt 0 ]; then
    exit 1
else
    exit 0
fi
