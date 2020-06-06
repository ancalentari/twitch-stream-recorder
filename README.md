# Twitch Stream Recorder
## Requirements
[python3.8](https://www.python.org/downloads/release/python-380/) or higher  
[streamlink](https://streamlink.github.io/)

## Setting up
This script allows you to record twitch streams live to a .mp4 files. But first:
1. Install [streamlink](https://streamlink.github.io/)
1. Create config.py file in the same directory as `twitch-recorder.py` with:
```properties
root_path = "/home/abathur/Videos/twitch"
username = "forsen"
client_id = "client id"
client_secret = "client secret"
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