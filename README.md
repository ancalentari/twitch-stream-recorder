# Twitch Recorder

Twitch Recorder is a Python script that automatically records live streams of a specified Twitch user, processes the video files, and saves them to your computer.
Improved version of Ancalentari Twitch Stream Recorder

```md
This script allows you to record twitch streams live to .mp4 files.  
It is an improved version of [junian's twitch-recorder](https://gist.github.com/junian/b41dd8e544bf0e3980c971b0d015f5f6), migrated to [**helix**](https://dev.twitch.tv/docs/api) - the new twitch API. It uses OAuth2.

```

## Requirements
1. [python3.8](https://www.python.org/downloads/release/python-380/) or higher  
2. [streamlink](https://streamlink.github.io/)  
3. [ffmpeg](https://ffmpeg.org/)


## Features

- Record Twitch streams automatically when a streamer goes online
- Save recordings to your local machine
- Prune old files after a specified number of days
- Optional feature to upload recorded streams to a network drive

## Installation

1. Install Python 3.6 or newer from [Python.org](https://www.python.org/downloads/)

2. Install the required Python packages:

``` bash
    pip install requests tqdm streamlink
```

3. (Optional) Download FFmpeg from [FFmpeg.org](https://ffmpeg.org/download.html) and add the binary to your system's PATH.

## Configuration

To configure the script, update the `config.json` file with the following information:

- `root_path`: The root directory where recorded and processed files will be stored
- `username`: Twitch username
- `client_id`: Your Twitch client ID
- `client_secret`: Your Twitch client secret
- `ffmpeg_path`: Path to your FFmpeg executable (only needed if FFmpeg is not in your system PATH)
- `disable_ffmpeg`: Set to `true` to disable FFmpeg processing of recorded files
- `refresh_interval`: The interval (in seconds) at which the script checks if the streamer is online
- `stream_quality`: The desired quality of the recorded stream (e.g., "best", "720p", etc.)
- `prune_after_days`: The number of days after which old files will be deleted
- `upload_to_network_drive`: Set to `true` to enable uploading files to a network drive
- `network_drive_path`: The path to the network drive where files will be uploaded (if `upload_to_network_drive` is set to `true`)


## Usage

1. Make sure you have Python 3.6 or newer installed on your system.
2. Install the required Python packages: `pip install -r requirements.txt`
3. Configure the `config.json` file with your desired settings.
4. Run the script: `python twitch-recorder.py`

You can also pass command-line arguments to override settings from the `config.json` file:

```bash
    python twitch_recorder.py -u <username> -q <quality> [--disable-ffmpeg]
```

- `-u` or `--username`: Twitch username to monitor and record
- `-q` or `--quality`: Desired stream quality (`best`, `1080p60`, `720p60`, etc.)
- `--disable-ffmpeg`: Disable FFmpeg processing

## Logging

By default, the script logs events to `twitch-recorder.log`. You can change the logging level by passing the `-l` or `--log` option followed by the desired level (e.g., `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`):

```bash
   python twitch_recorder.py -l DEBUG
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
