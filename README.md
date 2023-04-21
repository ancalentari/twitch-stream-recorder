# Twitch Recorder

Twitch Recorder is a Python script that automatically records live streams of a specified Twitch user, processes the video files, and saves them to your computer.
Improved version of Ancalentari Twitch Stream Recorder
```
This script allows you to record twitch streams live to .mp4 files.  
It is an improved version of [junian's twitch-recorder](https://gist.github.com/junian/b41dd8e544bf0e3980c971b0d015f5f6), migrated to [**helix**](https://dev.twitch.tv/docs/api) - the new twitch API. It uses OAuth2.
```

## Requirements
1. [python3.8](https://www.python.org/downloads/release/python-380/) or higher  
2. [streamlink](https://streamlink.github.io/)  
3. [ffmpeg](https://ffmpeg.org/)


## Features

- Record Twitch live streams automatically
- Process video files to fix errors
- Save recordings to specified folders
- Optional FFmpeg integration for processing

## Installation

1. Install Python 3.6 or newer from [Python.org](https://www.python.org/downloads/)

2. Install the required Python packages:

    ```
    pip install requests tqdm streamlink
    ```

3. (Optional) Download FFmpeg from [FFmpeg.org](https://ffmpeg.org/download.html) and add the binary to your system's PATH.

## Configuration

Edit the `config.json` file to provide your Twitch API credentials and desired settings:

    ```
    {
      "ffmpeg_path": "path/to/ffmpeg",
      "disable_ffmpeg": false,
      "refresh_interval": 60,
      "root_path": "path/to/recordings/folder",
      "username": "twitch_username",
      "stream_quality": "best",
      "client_id": "your_twitch_client_id",
      "client_secret": "your_twitch_client_secret"
    }
    ```

- `ffmpeg_path`: Path to your FFmpeg binary (e.g., `C:\\ffmpeg\\bin\\ffmpeg.exe` on Windows or `/usr/local/bin/ffmpeg` on macOS/Linux)
- `disable_ffmpeg`: Set to `true` to disable FFmpeg processing (default: `false`)
- `refresh_interval`: Time in seconds between checks for stream status (default: `60`)
- `root_path`: Directory where recorded and processed videos will be stored
- `username`: Twitch username to monitor and record
- `stream_quality`: Desired stream quality (`best`, `1080p60`, `720p60`, etc.)
- `client_id`: Your Twitch API client ID
- `client_secret`: Your Twitch API client secret

## Usage

Run the Twitch Recorder script:

    ```
    python twitch_recorder.py
    ```

You can also pass command-line arguments to override settings from the `config.json` file:

    ```
    python twitch_recorder.py -u <username> -q <quality> [--disable-ffmpeg]
    ```

- `-u` or `--username`: Twitch username to monitor and record
- `-q` or `--quality`: Desired stream quality (`best`, `1080p60`, `720p60`, etc.)
- `--disable-ffmpeg`: Disable FFmpeg processing

## Logging

By default, the script logs events to `twitch-recorder.log`. You can change the logging level by passing the `-l` or `--log` option followed by the desired level (e.g., `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`):

    ```
    python twitch_recorder.py -l DEBUG
    ```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
