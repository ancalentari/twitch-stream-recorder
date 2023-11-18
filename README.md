# Twitch Recorder

Twitch Recorder is a Python script for automatically recording live streams of specified Twitch users, processing the video files, and saving them to your computer. This script is an improved version of [junian's twitch-recorder](https://gist.github.com/junian/b41dd8e544bf0e3980c971b0d015f5f6), migrated to [**helix**](https://dev.twitch.tv/docs/api), the new Twitch API, and utilizes OAuth2.

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

## Installation

1. Install Python 3.8 or newer from [Python.org](https://www.python.org/downloads/).

2. Install the required Python packages:
   ```bash
   pip install requests tqdm streamlink psutil colorama
   ```

3. (Optional) Download FFmpeg from [FFmpeg.org](https://ffmpeg.org/download.html) and add the binary to your system's PATH.

## Configuration

Update the `config.json` file with your preferences:

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

## Usage

1. Ensure Python 3.8+ is installed.
2. Install required packages: `pip install -r requirements.txt`.
3. Configure `config.json`.
4. Run the script: `python twitch-recorder.py`.

Command-line arguments to override `config.json`:

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

## License

This project is under the MIT License. See [LICENSE](LICENSE) for details.
