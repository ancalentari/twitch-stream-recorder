#!/usr/bin/env bash
set -euo pipefail

list_dir="${1:-combined-videos/concat-lists}"
output_dir="${2:-combined-videos}"

mkdir -p "$output_dir"

shopt -s nullglob
for list_file in "$list_dir"/*.txt; do
  base="$(basename "$list_file" .txt)"
  output_file="$output_dir/$base.mp4"

  if [[ -f "$output_file" ]]; then
    echo "SKIP $output_file"
    continue
  fi

  echo "START $output_file"
  ffmpeg \
    -hide_banner \
    -y \
    -f concat \
    -safe 0 \
    -i "$list_file" \
    -c copy \
    -movflags +faststart \
    "$output_file"
done
