# Recording Validation & Monitoring

## Watch Mode - Real-time Dashboard

Monitor recordings in real-time with auto-refreshing dashboard:

```bash
# Refresh every 10 seconds (default)
dashboard-watch

# Or customize refresh interval
dashboard-watch 5   # 5 seconds
dashboard-watch 20  # 20 seconds
```

Press `Ctrl+C` to exit watch mode.

---

## Recording Validation - Check File Integrity

Validate all recordings for corruption or incomplete files:

```bash
# Run validation
./validate-recordings.sh
```

### What it checks:
- ✓ File exists and is readable
- ✓ File size is reasonable (>10MB minimum)
- ✓ MP4 structure is valid (uses ffprobe)
- ⚠️ Flags suspicious small files
- ✗ Detects corrupted/incomplete recordings

### Output:
```
Channel: pyka
  ✓ pyka - 2026-01-30 02h27m57s - basssssssss for a bit.mp4 (724.0M)
  ✓ pyka - 2026-01-23 01h43m36s - Deep Dark .mp4 (12.0G)

Channel: sheensayerdj
  ✓ sheensayerdj - 2026-01-26 03h03m41s - divine... (14.0G)

═════════════════════════════════════
Total Files: 3
Valid: 3
Suspicious (small): 0
Corrupted: 0
═════════════════════════════════════
```

---

## Automated Validation via Cron

Run validation daily to catch corrupted files early:

### Setup:

```bash
# Edit crontab
crontab -e

# Add this line to run validation daily at 4 AM
0 4 * * * /home/pi/twitch-recoder/twitch-stream-recorder/validate-recordings.sh >> /home/pi/twitch-recoder/twitch-stream-recorder/logs/validation.log 2>&1
```

### Cron Frequency Options:

```bash
# Daily at 4 AM
0 4 * * * validate-recordings.sh

# Every 6 hours
0 */6 * * * validate-recordings.sh

# Twice daily (2 AM and 2 PM)
0 2,14 * * * validate-recordings.sh

# Weekly on Sunday at 3 AM
0 3 * * 0 validate-recordings.sh
```

---

## Monitoring the Validation Log

View validation results:

```bash
# Last 20 lines
tail -20 ~/twitch-recoder/twitch-stream-recorder/logs/validation.log

# Watch log in real-time
tail -f ~/twitch-recoder/twitch-stream-recorder/logs/validation.log
```

---

## Installation

Make scripts executable:

```bash
chmod +x ~/twitch-recoder/twitch-stream-recorder/validate-recordings.sh
chmod +x ~/twitch-recoder/twitch-stream-recorder/dashboard-watch.sh
```

Add aliases for easy access:

```bash
# Edit ~/.bashrc
nano ~/.bashrc

# Add these lines:
alias validate='~/twitch-recoder/twitch-stream-recorder/validate-recordings.sh'
alias dwatch='~/twitch-recoder/twitch-stream-recorder/dashboard-watch.sh'

# Reload
source ~/.bashrc
```

Then use:
```bash
validate    # Run validation
dwatch      # Watch dashboard (10 sec refresh)
dwatch 5    # Watch dashboard (5 sec refresh)
```

---

## Requirements

### For validation script:
- `ffprobe` (optional, for integrity checking)
  ```bash
  sudo apt-get install ffmpeg
  ```

### For watch mode:
- `watch` command (usually pre-installed)
  ```bash
  sudo apt-get install procps
  ```

---

## Troubleshooting

### "ffprobe not found"
The validation script will still work - it just checks file sizes instead of full integrity. Install ffmpeg to get full checking:
```bash
sudo apt-get install ffmpeg
```

### "watch command not found"
Install procps:
```bash
sudo apt-get install procps
```

### Validation finds issues
Check the detailed log:
```bash
cat ~/twitch-recoder/twitch-stream-recorder/logs/validation.log
```

Files marked as "SUSPICIOUS" are too small and might be incomplete. Check if the stream was still recording or if it crashed.
