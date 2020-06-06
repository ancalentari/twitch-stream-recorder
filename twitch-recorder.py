import datetime
import enum
import getopt
import os
import subprocess
import sys
import time

import requests

import config


class TwitchResponseStatus(enum.Enum):
    ONLINE = 0
    OFFLINE = 1
    NOT_FOUND = 2
    UNAUTHORIZED = 3
    ERROR = 4


class TwitchRecorder:
    def __init__(self):
        # global configuration
        self.ffmpeg_path = 'ffmpeg'
        self.refresh = 15.0
        self.root_path = config.root_path

        # user configuration
        self.username = config.username
        self.quality = "best"

        # twitch configuration
        self.client_id = config.client_id
        self.client_secret = config.client_secret
        self.token_url = 'https://id.twitch.tv/oauth2/token?client_id=' + self.client_id + '&client_secret=' \
                         + self.client_secret + '&grant_type=client_credentials'
        self.url = 'https://api.twitch.tv/helix/streams'
        self.access_token = self.fetch_access_token()

    def fetch_access_token(self):
        token_response = requests.post(self.token_url, timeout=15)
        token_response.raise_for_status()
        token = token_response.json()
        return token['access_token']

    def run(self):
        # path to recorded stream
        recorded_path = os.path.join(self.root_path, "recorded", self.username)
        # path to finished video, errors removed
        processed_path = os.path.join(self.root_path, "processed", self.username)

        # create directory for recordedPath and processedPath if not exist
        if os.path.isdir(recorded_path) is False:
            os.makedirs(recorded_path)
        if os.path.isdir(processed_path) is False:
            os.makedirs(processed_path)

        # make sure the interval to check user availability is not less than 15 seconds
        if self.refresh < 15:
            print("Check interval should not be lower than 15 seconds.")
            self.refresh = 15
            print("System set check interval to 15 seconds.")

        # fix videos from previous recording session
        try:
            video_list = [f for f in os.listdir(recorded_path) if
                          os.path.isfile(os.path.join(recorded_path, f))]
            if len(video_list) > 0:
                print('Fixing previously recorded files.')
            for f in video_list:
                recorded_filename = os.path.join(recorded_path, f)
                print('Fixing ' + recorded_filename + '.')
                try:
                    subprocess.call(
                        [self.ffmpeg_path, '-err_detect', 'ignore_err', '-i', recorded_filename, '-c', 'copy',
                         os.path.join(processed_path, f)])
                    os.remove(recorded_filename)
                except Exception as e:
                    print(e)
        except Exception as e:
            print(e)

        print("Checking for", self.username, "every", self.refresh, "seconds. Record with", self.quality, "quality.")
        self.loop_check(recorded_path, processed_path)

    def check_user(self):
        info = None
        status = TwitchResponseStatus.ERROR
        try:
            headers = {"Client-ID": self.client_id, "Authorization": 'Bearer ' + self.access_token}
            r = requests.get(self.url + '?user_login=' + self.username, headers=headers, timeout=15)
            r.raise_for_status()
            info = r.json()
            if info is None or not info['data']:
                status = TwitchResponseStatus.OFFLINE
            else:
                status = TwitchResponseStatus.ONLINE
        except requests.exceptions.RequestException as e:
            if e.response:
                if e.response.status_code == 401:
                    status = TwitchResponseStatus.UNAUTHORIZED
                if e.response.status_code == 404:
                    status = TwitchResponseStatus.NOT_FOUND

        return status, info

    def loop_check(self, recorded_path, processed_path):
        while True:
            status, info = self.check_user()
            if status == TwitchResponseStatus.NOT_FOUND:
                print("Username not found. Invalid username or typo.")
                time.sleep(self.refresh)
            elif status == TwitchResponseStatus.ERROR:
                print(datetime.datetime.now().strftime("%Hh%Mm%Ss"), " ",
                      "unexpected error. will try again in 5 minutes.")
                time.sleep(300)
            elif status == TwitchResponseStatus.OFFLINE:
                print(self.username, "currently offline, checking again in", self.refresh, "seconds.")
                time.sleep(self.refresh)
            elif status == TwitchResponseStatus.UNAUTHORIZED:
                print(self.username, "unauthorized, will attempt to log back in immediately")
                self.access_token = self.fetch_access_token()
            elif status == TwitchResponseStatus.ONLINE:
                print(self.username, "online. Stream recording in session.")

                channels = info['data']
                channel = next(iter(channels), None)
                filename = self.username + " - " + datetime.datetime.now() \
                    .strftime("%Y-%m-%d %Hh%Mm%Ss") + " - " + channel.get("title") + ".mp4"

                # clean filename from unnecessary characters
                filename = "".join(x for x in filename if x.isalnum() or x in [" ", "-", "_", "."])

                recorded_filename = os.path.join(recorded_path, filename)

                # start streamlink process
                subprocess.call(
                    ["streamlink", "--twitch-oauth-token", self.access_token, "twitch.tv/" + self.username,
                     self.quality, "-o", recorded_filename])

                print("Recording stream is done. Fixing video file.")
                if os.path.exists(recorded_filename) is True:
                    try:
                        subprocess.call(
                            [self.ffmpeg_path, '-err_detect', 'ignore_err', '-i', recorded_filename, '-c', 'copy',
                             os.path.join(processed_path, filename)])
                        os.remove(recorded_filename)
                    except Exception as e:
                        print(e)
                else:
                    print("Skip fixing. File not found.")

                print("Fixing is done. Going back to checking..")
                time.sleep(self.refresh)


def main(argv):
    twitch_recorder = TwitchRecorder()
    usage_message = 'twitch-recorder.py -u <username> -q <quality>'

    try:
        opts, args = getopt.getopt(argv, "hu:q:", ["username=", "quality="])
    except getopt.GetoptError:
        print(usage_message)
        sys.exit(2)
    for opt, arg in opts:
        if opt == '-h':
            print(usage_message)
            sys.exit()
        elif opt in ("-u", "--username"):
            twitch_recorder.username = arg
        elif opt in ("-q", "--quality"):
            twitch_recorder.quality = arg

    twitch_recorder.run()


if __name__ == "__main__":
    main(sys.argv[1:])
