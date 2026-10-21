#!/bin/bash

# Batch process all recordings using Docker
# Usage: ./process_recordings.sh

# Load shared configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

RECORDINGS_DIR="$TWITCH_RECORDER_RECORDING"
DOCKER_IMAGE="twitch-recorder:latest"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Starting batch FFmpeg processing with Docker${NC}"
echo "Recordings directory: $RECORDINGS_DIR"
echo ""

# Counter
TOTAL=0
PROCESSED=0
FAILED=0
temp_output=""
trap 'if [ -n "$temp_output" ]; then rm -f -- "$temp_output"; fi' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

valid_output() {
    local host_file="$1"
    local container_file="$2"
    local duration
    [ -s "$host_file" ] || return 1
    duration=$(docker run --rm -v "$RECORDINGS_DIR:/recordings" "$DOCKER_IMAGE" \
        ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "$container_file") || return 1
    awk -v duration="$duration" 'BEGIN {exit !(duration ~ /^[0-9]+([.][0-9]+)?$/ && duration + 0 > 0)}'
}

# Process each recorded file
for dir in "$RECORDINGS_DIR/recorded"/*; do
    if [ -d "$dir" ]; then
        username=$(basename "$dir")
        echo -e "${YELLOW}Processing $username...${NC}"
        
        for file in "$dir"/*.mp4; do
            if [ -f "$file" ]; then
                TOTAL=$((TOTAL + 1))
                filename=$(basename "$file")
                output_dir="$RECORDINGS_DIR/processed/$username"
                output_file="$output_dir/${filename}"
                if ! mkdir -p "$output_dir"; then
                    FAILED=$((FAILED + 1))
                    continue
                fi
                
                # Skip if already processed
                if valid_output "$output_file" "/recordings/processed/$username/$filename"; then
                    echo -e "${GREEN}✓${NC} Already processed: $filename"
                    PROCESSED=$((PROCESSED + 1))
                    continue
                fi
                
                echo "  Processing: $filename"
                temp_output=$(mktemp "$output_dir/.processing.XXXXXX") || {
                    FAILED=$((FAILED + 1))
                    continue
                }
                container_output="/recordings/processed/$username/$(basename "$temp_output")"
                
                # Run FFmpeg inside Docker
                docker run --rm \
                    -v "$RECORDINGS_DIR:/recordings" \
                    "$DOCKER_IMAGE" \
                    ffmpeg \
                    -err_detect ignore_err \
                    -i "/recordings/recorded/$username/$filename" \
                    -c:v libx264 \
                    -crf 28 \
                    -preset medium \
                    -c:a aac \
                    -b:a 128k \
                    -movflags +faststart \
                    -f mp4 \
                    -y \
                    "$container_output" \
                    2>&1 | grep -E "frame=|Error|error"
                ffmpeg_status=${PIPESTATUS[0]}
                
                if [ "$ffmpeg_status" -eq 0 ]; then
                    if valid_output "$temp_output" "$container_output" && mv -f -- "$temp_output" "$output_file"; then
                        echo -e "${GREEN}✓ Success${NC}: $filename"
                        PROCESSED=$((PROCESSED + 1))
                    else
                        echo -e "${RED}✗ Failed${NC}: Output file invalid for $filename"
                        FAILED=$((FAILED + 1))
                    fi
                else
                    echo -e "${RED}✗ Failed${NC}: FFmpeg error for $filename"
                    FAILED=$((FAILED + 1))
                fi
                rm -f -- "$temp_output"
                temp_output=""
            fi
        done
    fi
done

echo ""
echo -e "${YELLOW}Processing complete!${NC}"
echo "Total files: $TOTAL"
echo -e "Processed: ${GREEN}$PROCESSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
[ "$FAILED" -eq 0 ]
