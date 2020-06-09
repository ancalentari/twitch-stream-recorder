# Ancalentari Twitch Stream Recorder
This script allows you to record twitch streams live to .mp4 files.  
It is an improved version of [junian's twitch-recorder](https://gist.github.com/junian/b41dd8e544bf0e3980c971b0d015f5f6), migrated to [**helix**](https://dev.twitch.tv/docs/api) - the new twitch API. It uses OAuth2.
## Requirements
1. [python3.8](https://www.python.org/downloads/release/python-380/) or higher  
2. [streamlink](https://streamlink.github.io/)  
3. [ffmpeg](https://ffmpeg.org/)

## Setting up
1. Install [streamlink](https://streamlink.github.io/)
2. Create `config.py` file in the same directory as `twitch-recorder.py` with:
```properties
root_path = "/home/abathur/Videos/twitch"
username = "forsen"
client_id = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
client_secret = "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
```
`root_path` - path to a folder where you want your VODs to be saved to  
`username` - name of the streamer you want to record by default  
`client_id` - you can grab this from [here](https://dev.twitch.tv/console/apps) once you register your application  
`client_secret` - you generate this [here](https://dev.twitch.tv/console/apps) as well, for your registered application

## Running script
The script will be logging to a console and to a file `twitch-recorder.log`
### On linux
Run the script
```shell script
python3.8 twitch-recorder.py
```
To record a specific streamer use `-u` or `--username`
```shell script
python3.8 twitch-recorder.py --username forsen
```
To specify quality use `-q` or `--quality`
```shell script
python3.8 twitch-recorder.py --quality 720p
```
To change default logging use `-l`, `--log` or `--logging`
```shell script
python3.8 twitch-recorder.py --log warn
```
To disable ffmpeg processing (fixing errors in recorded file) use `--disable-ffmpeg`
```shell script
python3.8 twitch-recorder.py --disable-ffmpeg
```
If you want to run the script as a job in the background and be able to close the terminal:
```shell script
nohup python3.8 twitch-recorder.py >/dev/null 2>&1 &
```
In order to kill the job, you first list them all:
```shell script
jobs
```
The output should show something like this:
```shell script
[1]+  Running                 nohup python3.8 twitch-recorder > /dev/null 2>&1 &
```
And now you can just kill the job:
```shell script
kill %1
```