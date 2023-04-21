import datetime
import enum
import getopt
import logging
import os
import subprocess
import sys
import shutil
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
        # Global configuration
        self.ffmpeg_path = "ffmpeg"
        self.disable_ffmpeg = False
        self.refresh = max(15, config.refresh_interval)
        self.root_path = config.root_path

        # User configuration
        self.username = config.username
        self.quality = "best"

        # Twitch configuration
        self.client_id = config.client_id
        self.client_secret = config.client_secret
        self.token_url = f"https://id.twitch.tv/oauth2/token?client_id={self.client_id}&client_secret={self.client_secret}&grant_type=client_credentials"
        self.url = "https://api.twitch.tv/helix/streams"
        self.access_token = self.fetch_access_token()

    def fetch_access_token(self):
        token_response = requests.post(self.token_url, timeout=15)
        token_response.raise_for_status()
        token = token_response.json()
        return token["access_token"]

    def run(self):
        recorded_path, processed_path = self.create_directories()
        self.process_previous_recordings(recorded_path, processed_path)

        logging.info(
            f"checking for {self.username} every {self.refresh} seconds, recording with {self.quality} quality")
        self.loop_check(recorded_path, processed_path)

    def create_directories(self):
        recorded_path = os.path.join(self.root_path, "recorded", self.username)
        processed_path = os.path.join(
            self.root_path, "processed", self.username)

        os.makedirs(recorded_path, exist_ok=True)
        os.makedirs(processed_path, exist_ok=True)

        return recorded_path, processed_path

    def process_previous_recordings(self, recorded_path, processed_path):
        video_list = [f for f in os.listdir(
            recorded_path) if os.path.isfile(os.path.join(recorded_path, f))]
        if video_list:
            logging.info("processing previously recorded files")
        for f in video_list:
            recorded_filename = os.path.join(recorded_path, f)
            processed_filename = os.path.join(processed_path, f)
            self.process_recorded_file(recorded_filename, processed_filename)

    def process_recorded_file(self, recorded_filename, processed_filename):
        if self.disable_ffmpeg:
            logging.info(f"moving: {recorded_filename}")
            shutil.move(recorded_filename, processed_filename)
        else:
            logging.info(f"fixing {recorded_filename}")
            self.ffmpeg_copy_and_fix_errors(
                recorded_filename, processed_filename)

    def ffmpeg_copy_and_fix_errors(self, recorded_filename, processed_filename):
        try:
            subprocess.call([self.ffmpeg_path, "-err_detect", "ignore_err",
                            "-i", recorded_filename, "-c", "copy", processed_filename])
            os.remove(recorded_filename)
        except Exception as e:
            logging.error(e)

    def check_user(self):
        headers = {"Client-ID": self.client_id,
                   "Authorization": f"Bearer {self.access_token}"}
        try:
            r = requests.get(
                f"{self.url}?user_login={self.username}", headers=headers, timeout=15)
            r.raise_for_status()
            info = r.json()
            return TwitchResponseStatus.ONLINE if info["data"] else TwitchResponseStatus.OFFLINE, info
        except requests.exceptions.RequestException as e:
            if e.response:
                if e.response.status_code == 401:
                    return TwitchResponseStatus.UNAUTHORIZED, None
                if e.response.status_code == 404:
                    return TwitchResponseStatus.NOT_FOUND, None
        return TwitchResponseStatus.ERROR, None

    def loop_check(self, recorded_path, processed_path):
        while True:
            status, info = self.check_user()
            if status == TwitchResponseStatus.NOT_FOUND:
                logging.error("username not found, invalid username or typo")
            elif status == TwitchResponseStatus.ERROR:
                logging.error(
                    f"{datetime.datetime.now().strftime('%Hh%Mm%Ss')} unexpected error. will try again in 5 minutes")
                time.sleep(300)
            elif status == TwitchResponseStatus.OFFLINE:
                logging.info(
                    f"{self.username} currently offline, checking again in {self.refresh} seconds")
            elif status == TwitchResponseStatus.UNAUTHORIZED:
                logging.info(
                    "unauthorized, will attempt to log back in immediately")
                self.access_token = self.fetch_access_token()
            elif status == TwitchResponseStatus.ONLINE:
                logging.info(
                    f"{self.username} online, stream recording in session")

                channel = info["data"][0]
                filename = f"{self.username} - {datetime.datetime.now().strftime('%Y-%m-%d %Hh%Mm%Ss')} - {channel.get('title')}.mp4"

                # Clean filename from unnecessary characters
                filename = "".join(x for x in filename if x.isalnum() or x in [
                                   " ", "-", "_", "."])

                recorded_filename = os.path.join(recorded_path, filename)
                processed_filename = os.path.join(processed_path, filename)

                # Start streamlink process
                subprocess.call(["streamlink", "--twitch-disable-ads",
                                f"twitch.tv/{self.username}", self.quality, "-o", recorded_filename])

                logging.info("recording stream is done, processing video file")
                if os.path.exists(recorded_filename):
                    self.process_recorded_file(
                        recorded_filename, processed_filename)
                else:
                    logging.info("skip fixing, file not found")

                logging.info("processing is done, going back to checking...")
            time.sleep(self.refresh)


def main(argv):
    twitch_recorder = TwitchRecorder()
    usage_message = "twitch-recorder.py -u <username> -q <quality>"
    logging.basicConfig(filename="twitch-recorder.log", level=logging.INFO)
    logging.getLogger().addHandler(logging.StreamHandler())

    try:
        opts, args = getopt.getopt(argv, "hu:q:l:", [
                                   "username=", "quality=", "log=", "logging=", "disable-ffmpeg"])
    except getopt.GetoptError:
        print(usage_message)
        sys.exit(2)
    for opt, arg in opts:
        if opt == "-h":
            print(usage_message)
            sys.exit()
        elif opt in ("-u", "--username"):
            twitch_recorder.username = arg
        elif opt in ("-q", "--quality"):
            twitch_recorder.quality = arg
        elif opt in ("-l", "--log", "--logging"):
            logging_level = getattr(logging, arg.upper(), None)
            if not isinstance(logging_level, int):
                raise ValueError(f"invalid log level: {arg.upper()}")
            logging.basicConfig(level=logging_level)
            logging.info(f"logging configured to {arg.upper()}")
        elif opt == "--disable-ffmpeg":
            twitch_recorder.disable_ffmpeg = True
            logging.info("ffmpeg disabled")

    twitch_recorder.run()


if __name__ == "__main__":
    main(sys.argv[1:])
