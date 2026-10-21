# Twitch Recorder

Twitch Recorder is a Python script for automatically recording live streams of specified Twitch users, processing the video files, and saving them to your computer. This script is an improved version of [junian's twitch-recorder](https://gist.github.com/junian/b41dd8e544bf0e3980c971b0d015f5f6), migrated to [**helix**](https://dev.twitch.tv/docs/api), the new Twitch API, and utilizes OAuth2.

## Project Structure

```
twitch-stream-recorder/
├── twitch-recorder.py          # Main application
├── requirements.txt
├── config/
│   ├── config.json             # Your configuration (gitignored)
│   └── config.example.json     # Example configuration template
├── scripts/
│   ├── dashboard-live.sh       # Compatibility launcher for the unified TUI
│   ├── dashboard.sh            # Unified recorder TUI
│   ├── dashboard-watch.sh      # Compatibility launcher with refresh option
│   ├── validate-recordings.sh  # MP4 integrity checker
│   └── process-recordings.sh   # Batch processing script
├── systemd/
│   ├── twitch-recorder.service # Systemd service file
│   └── install.sh              # Service installation script
├── docker/
│   ├── dockerfile
│   └── compose-dev.yaml
└── docs/
    ├── MONITORING.md           # Dashboard documentation
    ├── SYSTEMD_SETUP.md        # Service setup guide
    └── VALIDATION_AND_WATCH.md # Validation tools guide
```

## Requirements

1. [Python 3.8](https://www.python.org/downloads/release/python-380/) or higher
2. [Streamlink](https://streamlink.github.io/)
3. [FFmpeg](https://ffmpeg.org/)

## Features

- Automatically record Twitch streams when a streamer goes online.
- Save recordings to your local machine.
- Prune old files after a specified number of days.
- Optional feature to upload recorded streams to a network drive.
- Enhanced resource monitoring to prevent system overloads.
- **Real-time monitoring dashboard** with live stats.
- **Systemd service** for auto-start and crash recovery.

## Installation

1. Install Python 3.8 or newer from [Python.org](https://www.python.org/downloads/).

2. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

3. For video processing, install FFmpeg and FFprobe from [FFmpeg.org](https://ffmpeg.org/download.html) and add both binaries to your system's PATH.

## Configuration

Copy the example config and update with your preferences:

```bash
cp config/config.example.json config/config.json
```

Edit `config/config.json`:

- `root_path`: Directory for recorded and processed files.
- `username`: Twitch username.
- `client_id`: Your Twitch client ID.
- `client_secret`: Your Twitch client secret.
- `ffmpeg_path`: Path to FFmpeg executable (if not in PATH).
- `disable_ffmpeg`: Disable FFmpeg processing (true/false).
- `refresh_interval`: Interval in seconds for online checks.
- `stream_quality`: Desired quality of recorded streams.
- `prune_after_days`: Days after which to delete old files.
- `upload_to_network_drive`: Enable uploading to network drive (true/false).
- `network_drive_path`: Path for network drive uploads.

See [the configuration guide](docs/CONFIGURATION.md#recording-and-background-processing)
for stall recovery, periodic retention, processing limits, and upload retries.

## Usage

1. Ensure Python 3.8+ is installed.
2. Install required packages: `pip install -r requirements.txt`.
3. Configure `config/config.json`.
4. Run the script: `python twitch-recorder.py`.

Command-line arguments to override `config/config.json`:

```bash
python twitch_recorder.py -u <username> -q <quality> [--disable-ffmpeg]
```

- `-u` or `--username`: Twitch username to monitor.
- `-q` or `--quality`: Stream quality (e.g., "best", "1080p60").
- `--disable-ffmpeg`: Disable FFmpeg processing.

## Logging

Logs events to `twitch-recorder.log`. Change log level with `-l` or `--log`:

```bash
python twitch_recorder.py -l DEBUG
```

## Monitoring Dashboard

Use the consolidated TUI for broadcast history, live processing, queue, logs and system health:

```bash
./scripts/dashboard.sh
# On the Pi after SSH login:
dashboard
# One-time status without an interactive terminal:
./scripts/dashboard.sh --once
```

Switch views with **1–6** or **Tab**. Search files with **/**, filter by channel
with **c** or stage with **g**, and open details with **Enter**. Press **?** for
shortcuts and **q** to exit. The former live and watch scripts launch the same TUI.
Use **b** for broadcasts/files, **R** to queue a quarantined-file retry, and **D**
to export diagnostics. Recorder heartbeats, measured FFmpeg progress and persistent
history improve status accuracy. It makes no Twitch API calls and caches file scans
to limit Pi disk activity.

See [docs/MONITORING.md](docs/MONITORING.md) for details.

## Running as a Service

To run automatically on boot with crash recovery:

```bash
cd systemd
sudo bash install.sh
```

See [docs/SYSTEMD_SETUP.md](docs/SYSTEMD_SETUP.md) for details.

## Tests

Install the development requirements and run the regression suite:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

The FFmpeg integration test uses a generated video and runs when FFmpeg and
FFprobe are installed. Tests do not access Twitch or start Docker containers.

## License

This project is under the MIT License. See [LICENSE](LICENSE) for details.

### Compression benchmarks

Run `./scripts/benchmark.sh` on the Pi to compare presets on short recording
samples and save recommendations with preview clips. See the
[benchmark guide](docs/COMPRESSION-BENCHMARK.md) for limits, recorder coordination,
and how to interpret the estimates.
