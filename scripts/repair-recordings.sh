#!/bin/bash

# Repair corrupted MP4 recordings
# Usage: ./repair-recordings.sh [channel_name]

# Load shared configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

RECORDING_DIR="$TWITCH_RECORDER_RECORDED"
BACKUP_DIR="$TWITCH_RECORDER_BACKUP"
LOG_FILE="$TWITCH_RECORDER_REPAIR_LOG"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Ensure directories exist
mkdir -p "$BACKUP_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

# Log function
log_message() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] $1" | tee -a "$LOG_FILE"
}

echo -e "${BLUE}=== MP4 Repair Tool ===${NC}\n"
log_message "=== Repair Started ==="

TOTAL_FILES=0
REPAIRED=0
FAILED=0
ALREADY_OK=0

# Function to repair a single file
repair_file() {
    local input_file="$1"
    local channel="$2"
    local filename=$(basename "$input_file")
    
    TOTAL_FILES=$((TOTAL_FILES + 1))
    
    # First check if file is already valid
    if ffprobe -v error -select_streams v:0 -show_entries stream=codec_type "$input_file" &>/dev/null; then
        echo -e "  ${GREEN}✓ OK${NC} $filename (no repair needed)"
        ALREADY_OK=$((ALREADY_OK + 1))
        return 0
    fi
    
    echo -e "${YELLOW}Attempting to repair:${NC} $filename"
    log_message "Repairing: $filename"
    
    # Create backup
    local backup_file="$BACKUP_DIR/${channel}_$(basename "$input_file")"
    echo "  Creating backup..."
    if ! cp "$input_file" "$backup_file"; then
        echo -e "  ${RED}✗ Failed to create backup${NC}"
        log_message "FAILED: Could not create backup for $filename"
        FAILED=$((FAILED + 1))
        return 1
    fi
    
    # Create temp file
    local temp_file="${input_file}.repaired.mp4"
    
    echo "  Attempting repair with ffmpeg..."
    
    # Try method 1: Copy streams and fix container
    if ffmpeg -hide_banner -loglevel error -err_detect ignore_err \
        -i "$input_file" \
        -c copy \
        -map 0 \
        -ignore_unknown \
        -y "$temp_file" 2>&1 | tee -a "$LOG_FILE"; then
        
        # Verify repaired file
        if ffprobe -v error -select_streams v:0 -show_entries stream=codec_type "$temp_file" &>/dev/null && \
           [ -s "$temp_file" ] && [ $(stat -c%s "$temp_file" 2>/dev/null || stat -f%z "$temp_file" 2>/dev/null) -gt 1000000 ]; then
            
            echo -e "  ${GREEN}✓ Repaired successfully${NC}"
            
            # Replace original with repaired version
            mv "$temp_file" "$input_file"
            
            # Show file sizes
            orig_size=$(stat -c%s "$backup_file" 2>/dev/null || stat -f%z "$backup_file" 2>/dev/null)
            new_size=$(stat -c%s "$input_file" 2>/dev/null || stat -f%z "$input_file" 2>/dev/null)
            orig_human=$(awk "BEGIN {printf \"%.1fM\", $orig_size/1048576}")
            new_human=$(awk "BEGIN {printf \"%.1fM\", $new_size/1048576}")
            
            echo -e "  Original: ${orig_human} → Repaired: ${new_human}"
            log_message "SUCCESS: Repaired $filename (${orig_human} → ${new_human})"
            REPAIRED=$((REPAIRED + 1))
            return 0
        else
            echo -e "  ${YELLOW}Method 1 failed, trying re-encode...${NC}"
            rm -f "$temp_file"
            
            # Try method 2: Re-encode video (slower but more thorough)
            if ffmpeg -hide_banner -loglevel error -err_detect ignore_err \
                -i "$input_file" \
                -c:v libx264 -crf 28 -preset fast \
                -c:a aac -b:a 128k \
                -movflags +faststart \
                -y "$temp_file" 2>&1 | tee -a "$LOG_FILE"; then
                
                if [ -s "$temp_file" ] && [ $(stat -c%s "$temp_file" 2>/dev/null || stat -f%z "$temp_file" 2>/dev/null) -gt 1000000 ]; then
                    echo -e "  ${GREEN}✓ Repaired via re-encode${NC}"
                    mv "$temp_file" "$input_file"
                    log_message "SUCCESS: Re-encoded $filename"
                    REPAIRED=$((REPAIRED + 1))
                    return 0
                fi
            fi
        fi
    fi
    
    # All methods failed
    echo -e "  ${RED}✗ Repair failed${NC}"
    log_message "FAILED: Could not repair $filename"
    rm -f "$temp_file"
    
    # Restore from backup
    echo "  Restoring from backup..."
    cp "$backup_file" "$input_file"
    FAILED=$((FAILED + 1))
    return 1
}

# Main processing loop
CHANNEL_FILTER="$1"

if [ -n "$CHANNEL_FILTER" ]; then
    echo -e "${CYAN}Processing channel:${NC} $CHANNEL_FILTER\n"
    DIRS=("$RECORDING_DIR/$CHANNEL_FILTER")
else
    echo -e "${CYAN}Processing all channels...${NC}\n"
    DIRS=("$RECORDING_DIR"/*)
fi

for channel_dir in "${DIRS[@]}"; do
    if [ ! -d "$channel_dir" ]; then
        if [ -n "$CHANNEL_FILTER" ]; then
            echo -e "${RED}Error: Channel directory not found: $channel_dir${NC}"
            exit 1
        fi
        continue
    fi
    
    channel=$(basename "$channel_dir")
    echo -e "${BLUE}Channel: $channel${NC}"
    
    # Find all mp4 files
    shopt -s nullglob
    files=("$channel_dir"/*.mp4)
    
    if [ ${#files[@]} -eq 0 ]; then
        echo "  No MP4 files found"
        continue
    fi
    
    for file in "${files[@]}"; do
        repair_file "$file" "$channel"
    done
    
    echo ""
done

# Summary
echo -e "${BLUE}=== Repair Summary ===${NC}"
echo "Total files checked: $TOTAL_FILES"
echo -e "${GREEN}Already valid: $ALREADY_OK${NC}"
echo -e "${GREEN}Repaired: $REPAIRED${NC}"
echo -e "${RED}Failed: $FAILED${NC}"
echo ""
echo -e "${CYAN}Backups saved to:${NC} $BACKUP_DIR"
echo -e "${CYAN}Log file:${NC} $LOG_FILE"

log_message "=== Repair Complete: $REPAIRED repaired, $FAILED failed, $ALREADY_OK already valid ==="

# Exit with error if any failures
[ $FAILED -gt 0 ] && exit 1 || exit 0
