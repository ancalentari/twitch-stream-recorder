import datetime
import errno
import enum
import getopt
import logging
import os
import subprocess
import sys
import shutil
import time
import requests
import psutil
import json
import threading
import signal
import tempfile
import re
import math
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from tqdm import tqdm
from pathlib import Path
from colorama import init, Fore, Style

init(autoreset=True)

class TwitchResponseStatus(enum.Enum):
    ONLINE = 0
    OFFLINE = 1
    NOT_FOUND = 2
    UNAUTHORIZED = 3
    ERROR = 4

class ProcessingInterrupted(Exception):
    """Background work yielded to recording or shutdown; this is not a failure."""

class TwitchRecorder:
    def __init__(self):
        # Load configuration with error handling
        try:
            # Try config/config.json first, fall back to config.json
            config_path = "config/config.json" if os.path.exists("config/config.json") else "config.json"
            with open(config_path, "r") as config_file:
                config_data = json.load(config_file)
        except FileNotFoundError:
            logging.error("config.json not found. Please create a configuration file.")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logging.error(f"Invalid JSON in config.json: {e}")
            sys.exit(1)
        except Exception as e:
            logging.error(f"Error loading config.json: {e}")
            sys.exit(1)

        # Global configuration with validation
        self.ffmpeg_path = config_data.get("ffmpeg_path", "ffmpeg")
        self.disable_ffmpeg = config_data.get("disable_ffmpeg", False)
        self.refresh = max(10, config_data.get("refresh_interval", 60))  # Minimum 10 seconds
        self.idle_compress_enabled = config_data.get("idle_compress_enabled", True)
        self.idle_compress_crf = config_data.get("idle_compress_crf", 28)
        self.idle_compress_preset = config_data.get("idle_compress_preset", "medium")
        self.idle_compress_audio_bitrate = config_data.get("idle_compress_audio_bitrate", "128k")
        self.idle_compress_stall_timeout = max(60, config_data.get("idle_compress_stall_timeout_seconds", 1800))
        self.processing_threads = max(1, config_data.get("processing_threads", 1))
        self.stitch_recordings_enabled = config_data.get("stitch_recordings_enabled", True)
        self.stitch_max_gap_seconds = max(0, config_data.get("stitch_max_gap_seconds", 900))
        self.eventsub_enabled = config_data.get('eventsub_enabled', False)
        self.eventsub_token_path = config_data.get('eventsub_token_path', 'config/eventsub-token.json')
        self._eventsub_listener = None
        self._eventsub_recovery_users = set()
        self._eventsub_recovery_lock = threading.Lock()
        self.recording_stall_timeout = max(30, config_data.get("recording_stall_timeout_seconds", 300))
        self.retention_check_interval = max(60, config_data.get("retention_check_interval_seconds", 3600))
        self._next_prune_at = 0
        self.root_path = config_data.get("root_path", "./recordings")
        self.max_concurrent_recordings = max(1, config_data.get("max_concurrent_recordings", 2))
        self.cpu_threshold = config_data.get("cpu_threshold", 80)
        self.memory_threshold = config_data.get("memory_threshold", 80)
        self.offline_backoff_enabled = config_data.get("offline_backoff_enabled", True)
        self.offline_backoff_base_seconds = max(30, config_data.get("offline_backoff_base_seconds", self.refresh))
        self.offline_backoff_max_seconds = max(
            self.offline_backoff_base_seconds,
            config_data.get("offline_backoff_max_seconds", 600)
        )
        
        # Thread-safe counter and locks
        self._active_recordings_lock = threading.Lock()
        self._active_recordings = 0
        self._recording_processes = {}  # Track active processes
        self._pending_recordings = set()  # Users currently launching streamlink
        self._recording_threads = {}  # Track long-running recording threads
        self._recording_stream_ids = {}  # Track active Twitch stream IDs per user
        self._recording_processes_lock = threading.Lock()  # Separate lock for processes dict
        self._recording_slots = threading.BoundedSemaphore(self.max_concurrent_recordings)
        self._idle_processing_thread = None
        self._idle_processing_lock = threading.Lock()
        self._processing_active = threading.Event()
        self._shutdown_event = threading.Event()
        self._executor = None  # Store executor reference for cleanup
        self._token_refresh_lock = threading.Lock()  # Lock for token refresh
        self._offline_backoff_lock = threading.Lock()
        self._offline_check_counts = {}
        self._next_user_check_at = {}

        # User configuration
        self.prune_after_days = config_data.get("prune_after_days", 30)
        self.upload_to_network_drive_enabled = config_data.get("upload_to_network_drive", False)
        self.network_drive_path = config_data.get("network_drive_path", "")
        self.usernames = config_data.get("usernames", [])
        self.quality = config_data.get("stream_quality", "best")
        self.max_processing_attempts = max(1, config_data.get("max_processing_attempts", 3))

        # Validate required fields
        if not self.usernames:
            logging.error("No usernames specified in config.json")
            sys.exit(1)

        # Twitch configuration
        self.client_id = config_data.get("client_id", "")
        self.client_secret = config_data.get("client_secret", "")
        if not self.client_id or not self.client_secret:
            logging.error("Missing client_id or client_secret in config.json")
            sys.exit(1)

        self.token_url = f"https://id.twitch.tv/oauth2/token?client_id={self.client_id}&client_secret={self.client_secret}&grant_type=client_credentials"
        self.url = "https://api.twitch.tv/helix/streams"
        self.access_token = None
        self.token_expires_at = 0
        self.fetch_access_token()

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # Validate dependencies
        self._validate_dependencies()

    def _validate_dependencies(self):
        """Validate required external dependencies"""
        # Check streamlink
        try:
            # Python/plugin imports can take 15 seconds on a busy Pi Zero W.
            result = subprocess.run(['streamlink', '--version'], capture_output=True, timeout=30)
            if result.returncode != 0:
                logging.warning("Streamlink not found or not working properly")
        except FileNotFoundError:
            logging.error("Streamlink is not installed. Please install: pip install streamlink")
            sys.exit(1)
        except Exception as e:
            logging.warning(f"Could not verify streamlink: {e}")
        
        # Check ffmpeg if enabled
        if not self.disable_ffmpeg:
            try:
                result = subprocess.run([self.ffmpeg_path, '-version'], capture_output=True, timeout=5)
                if result.returncode != 0:
                    logging.warning(f"FFmpeg not found at {self.ffmpeg_path}")
                    logging.warning("Processing will be disabled")
                    self.disable_ffmpeg = True
            except FileNotFoundError:
                logging.error(f"FFmpeg not found at {self.ffmpeg_path}")
                logging.warning("Processing will be disabled")
                self.disable_ffmpeg = True
            except Exception as e:
                logging.warning(f"Could not verify ffmpeg: {e}")

        if not self.disable_ffmpeg:
            try:
                subprocess.run([str(Path(self.ffmpeg_path).with_name('ffprobe')), '-version'],
                               capture_output=True, timeout=5, check=True)
            except (OSError, subprocess.SubprocessError) as e:
                logging.error(f"FFprobe is required to validate processed recordings: {e}")
                sys.exit(1)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals safely"""
        signal_name = signal.Signals(signum).name
        logging.info(f"Shutdown signal received: {signal_name}. Cleaning up...")
        self._shutdown_event.set()
        
        # Don't call cleanup here - let the main loop handle it
        # This avoids race conditions with the main thread

    def _cleanup_processes(self):
        """Clean up any running recording processes"""
        with self._recording_processes_lock:
            processes_to_cleanup = list(self._recording_processes.items())
            threads_to_join = list(self._recording_threads.items())
            
        for username, process in processes_to_cleanup:
            if process and process.poll() is None:
                logging.info(f"Terminating recording process for {username}")
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logging.warning(f"Force killing process for {username}")
                    process.kill()
                    process.wait()
                except Exception as e:
                    logging.error(f"Error terminating process for {username}: {e}")
        
        with self._recording_processes_lock:
            self._recording_processes.clear()
            self._pending_recordings.clear()
            self._recording_stream_ids.clear()

        for username, thread in threads_to_join:
            if thread.is_alive():
                logging.info(f"Waiting for recording thread for {username} to stop")
                thread.join(timeout=15)

        with self._idle_processing_lock:
            idle_thread = self._idle_processing_thread
        if idle_thread and idle_thread.is_alive():
            logging.info("Waiting for idle processing thread to stop")
            idle_thread.join(timeout=15)

    @property
    def active_recordings(self):
        with self._active_recordings_lock:
            return self._active_recordings

    def _increment_recordings(self):
        with self._active_recordings_lock:
            self._active_recordings += 1

    def _decrement_recordings(self):
        with self._active_recordings_lock:
            self._active_recordings = max(0, self._active_recordings - 1)

    def _get_stream_id(self, info):
        try:
            return info["data"][0].get("id")
        except (KeyError, IndexError, TypeError):
            return None

    def _is_user_recording(self, username):
        """Return True when a live streamlink process already exists for this user."""
        with self._recording_processes_lock:
            if username in self._pending_recordings:
                return True

            thread = self._recording_threads.get(username)
            if thread and thread.is_alive():
                return True
            if thread:
                self._recording_threads.pop(username, None)

            process = self._recording_processes.get(username)
            if not process:
                return False

            if process.poll() is None:
                return True

            # Clean up stale completed process entries.
            self._recording_processes.pop(username, None)
            return False

    def _reserve_recording(self, username, stream_id=None):
        """Atomically reserve a per-user recording and one global recording slot."""
        with self._recording_processes_lock:
            if username in self._pending_recordings:
                return False, "launch already pending"

            existing_thread = self._recording_threads.get(username)
            if existing_thread and existing_thread.is_alive():
                active_stream_id = self._recording_stream_ids.get(username)
                if stream_id and active_stream_id == stream_id:
                    return False, f"stream {stream_id} is already being recorded"
                return False, "recording thread already active"
            if existing_thread:
                self._recording_threads.pop(username, None)

            existing_process = self._recording_processes.get(username)
            if existing_process and existing_process.poll() is None:
                active_stream_id = self._recording_stream_ids.get(username)
                if stream_id and active_stream_id == stream_id:
                    return False, f"stream {stream_id} is already being recorded"
                return False, "recording process already active"
            if existing_process:
                self._recording_processes.pop(username, None)

            if not self._recording_slots.acquire(blocking=False):
                return False, "maximum concurrent recordings reached"

            self._pending_recordings.add(username)
            if stream_id:
                self._recording_stream_ids[username] = stream_id
            self._increment_recordings()
            return True, None

    def _release_recording_reservation(self, username):
        with self._recording_processes_lock:
            self._pending_recordings.discard(username)
            self._recording_processes.pop(username, None)
            self._recording_threads.pop(username, None)
            self._recording_stream_ids.pop(username, None)

        self._decrement_recordings()
        try:
            self._recording_slots.release()
        except ValueError:
            logging.error(f"Recording slot for {username} was released more than once")

    def _start_recording_thread(self, username, info, recorded_path, processed_path):
        if self._shutdown_event.is_set():
            return False
        stream_id = self._get_stream_id(info)
        reserved, reason = self._reserve_recording(username, stream_id)
        if not reserved:
            logging.info(f"{Fore.YELLOW}Cannot start recording for {username}: {reason}")
            return False

        thread = threading.Thread(
            target=self.record_stream,
            args=(username, info, recorded_path, processed_path),
            name=f"record-{username}",
        )

        start_error = None
        with self._recording_processes_lock:
            self._recording_threads[username] = thread
            try:
                thread.start()
            except Exception as e:
                start_error = e
                self._recording_threads.pop(username, None)

        if start_error:
            self._release_recording_reservation(username)
            raise start_error

        return True

    def _start_idle_processing_thread(self, paths):
        if self._shutdown_event.is_set() or self.active_recordings > 0:
            return

        with self._idle_processing_lock:
            if self._idle_processing_thread and self._idle_processing_thread.is_alive():
                return

            self._idle_processing_thread = threading.Thread(
                target=self._run_idle_processing,
                args=(paths,),
                name="idle-processing",
            )
            self._idle_processing_thread.start()

    def _run_idle_processing(self, paths):
        self._idle_processing_paths = paths
        try:
            for username in self.usernames:
                if self._shutdown_event.is_set() or self.active_recordings > 0:
                    return
                self._retry_pending_uploads(paths[username][1])
                stitched_path = Path(paths[username][1]) / 'stitched'
                if stitched_path.exists():
                    self._retry_pending_uploads(str(stitched_path))

            for username in self.usernames:
                if self._shutdown_event.is_set() or self.active_recordings > 0:
                    return
                recorded_path, processed_path = paths[username]
                self.process_previous_recordings(recorded_path, processed_path)

            if not self.disable_ffmpeg and getattr(self, 'stitch_recordings_enabled', True):
                for username in self.usernames:
                    self._check_processing_idle()
                    recorded_path, processed_path = paths[username]
                    if not list(Path(recorded_path).glob('*.mp4')):
                        self.stitch_processed_recordings(processed_path)

            if self.disable_ffmpeg or not self.idle_compress_enabled:
                return

            for username in self.usernames:
                if self._shutdown_event.is_set() or self.active_recordings > 0:
                    return
                _, processed_path = paths[username]
                self.compress_processed_file(processed_path)
        except ProcessingInterrupted:
            logging.info('Idle processing postponed for recording or shutdown')
        except Exception as e:
            logging.error(f"Idle processing error: {e}")

    def can_start_new_recording(self):
        if self.active_recordings >= self.max_concurrent_recordings:
            return False
        
        cpu_usage = psutil.cpu_percent(interval=1)
        memory_usage = psutil.virtual_memory().percent
        
        # Background FFmpeg yields as soon as a recording reserves a slot.
        if (cpu_usage > self.cpu_threshold and not self._processing_active.is_set()) or memory_usage > self.memory_threshold:
            logging.warning(f"High resource usage: CPU {cpu_usage}%, Memory {memory_usage}%")
            return False
        
        # Check available disk space (require at least 1GB free)
        try:
            disk_usage = psutil.disk_usage(self.root_path)
            free_gb = disk_usage.free / (1024**3)
            if free_gb < 1:
                logging.warning(f"Low disk space: {free_gb:.2f}GB available")
                return False
        except Exception as e:
            logging.error(f"Error checking disk space: {e}")
            return False
        
        return True

    def fetch_access_token(self, rejected_token=None):
        """Fetch or refresh access token with proper error handling and thread safety"""
        try:
            current_time = time.time()
            # Refresh token if it expires in the next 5 minutes
            if self.access_token and self.access_token != rejected_token and current_time < (self.token_expires_at - 300):
                return self.access_token

            # Use lock to prevent multiple threads from refreshing simultaneously
            with self._token_refresh_lock:
                # Double-check after acquiring lock (another thread may have refreshed)
                current_time = time.time()
                if self.access_token and self.access_token != rejected_token and current_time < (self.token_expires_at - 300):
                    return self.access_token
                    
                logging.info("Fetching new access token")
                token_response = requests.post(self.token_url, timeout=15)
                token_response.raise_for_status()
                token_data = token_response.json()
                
                self.access_token = token_data["access_token"]
                expires_in = token_data.get("expires_in", 3600)
                self.token_expires_at = current_time + expires_in
                
                logging.info(f"Access token refreshed, expires in {expires_in} seconds")
                return self.access_token
        except Exception as e:
            logging.error(f"Failed to fetch access token: {e}")
            raise

    def run(self):
        """Main run loop with proper threading"""
        paths = self.create_directories()
        self._start_dashboard_status(paths)
        
        # Use ThreadPoolExecutor for concurrent recording checks
        self._executor = ThreadPoolExecutor(max_workers=min(len(self.usernames), 5))
        try:
            self._start_eventsub(paths)
            while not self._shutdown_event.is_set():
                self._prune_if_due(paths)
                # Submit tasks for each username
                future_to_username = {}
                current_time = time.time()
                for username in self.usernames:
                    if not self._should_poll_user(username):
                        continue
                    if not self._should_check_user_now(username, current_time):
                        continue
                    recorded_path, processed_path = paths[username]
                    future = self._executor.submit(self.check_and_record_user, username, recorded_path, processed_path)
                    future_to_username[future] = username
                
                # Wait for all tasks to complete with proper timeout handling
                completed_futures = set()
                try:
                    for future in as_completed(future_to_username, timeout=self.refresh):
                        username = future_to_username[future]
                        completed_futures.add(future)
                        try:
                            status = future.result()
                            self._update_user_check_schedule(username, status)
                        except Exception as e:
                            logging.error(f"Error in thread for {username}: {e}")
                except (TimeoutError, FuturesTimeoutError):
                    # Handle futures that didn't complete in time
                    incomplete = set(future_to_username.keys()) - completed_futures
                    for future in incomplete:
                        username = future_to_username[future]
                        logging.warning(f"Task for {username} did not complete in time")
                        future.cancel()  # Attempt to cancel
                
                # Process old recordings in the background so polling can continue.
                self._start_idle_processing_thread(paths)

                # Wait for next cycle
                if not self._shutdown_event.wait(timeout=self.refresh):
                    continue
                else:
                    break
        finally:
            self._shutdown_event.set()
            listener = getattr(self, '_eventsub_listener', None)
            if listener:
                listener.join(timeout=20)
            # Graceful shutdown of executor
            logging.info("Shutting down executor...")
            if self._executor:
                # Python 3.7 compatible shutdown (cancel_futures added in 3.9)
                self._executor.shutdown(wait=False)
            logging.info("Cleaning up processes...")
            self._cleanup_processes()
            telemetry = getattr(self, "_telemetry", None)
            if telemetry:
                telemetry.stop.set()
                telemetry.wake.set()
                telemetry.join(timeout=10)  # This already handles process termination properly

    def _start_eventsub(self, paths):
        if not getattr(self, 'eventsub_enabled', False):
            return
        from twitch_eventsub import EventSubListener

        def online(username):
            if self._shutdown_event.is_set() or username not in paths:
                return
            self._reset_user_backoff(username)
            with self._eventsub_recovery_lock:
                self._eventsub_recovery_users.add(username)
            # Run API checks outside the socket thread; existing reservations prevent duplicates.
            self._executor.submit(self._check_eventsub_user, username, *paths[username])

        self._eventsub_listener = EventSubListener(
            self.client_id, self.client_secret, self.eventsub_token_path,
            self.usernames, online, self._shutdown_event)
        self._eventsub_listener.start()

    def _check_eventsub_user(self, username, recorded_path, processed_path):
        status = self.check_and_record_user(username, recorded_path, processed_path)
        self._update_user_check_schedule(username, status)
        return status

    def _should_poll_user(self, username):
        """Healthy EventSub replaces routine polling; failed recordings still recover."""
        listener = getattr(self, '_eventsub_listener', None)
        if not listener or not listener.connected.is_set():
            return True
        with self._eventsub_recovery_lock:
            if username not in self._eventsub_recovery_users:
                return False
        if self._is_user_recording(username):
            with self._eventsub_recovery_lock:
                self._eventsub_recovery_users.discard(username)
            return False
        return True

    def check_and_record_user(self, username, recorded_path, processed_path):
        """Check and potentially record a single user"""
        try:
            if self._shutdown_event.is_set():
                return TwitchResponseStatus.ERROR
            logging.info(f"Checking {username}")
            status, info = self.check_user(username)
            
            if status == TwitchResponseStatus.NOT_FOUND:
                logging.error(f"{Fore.RED}Username {username} not found")
            elif status == TwitchResponseStatus.OFFLINE:
                logging.info(f"{Fore.YELLOW}{username} currently offline")
            elif status == TwitchResponseStatus.UNAUTHORIZED:
                logging.error(f"{Fore.RED}Unauthorized after token refresh; will retry next check")
            elif status == TwitchResponseStatus.ONLINE:
                if self._is_user_recording(username):
                    logging.info(
                        f"{Fore.YELLOW}{username} already has an active recording process; skipping duplicate start"
                    )
                    return status
                if self.can_start_new_recording():
                    self._start_recording_thread(username, info, recorded_path, processed_path)
                else:
                    logging.info(f"{Fore.YELLOW}Cannot start recording for {username} - resource limits")
            return status
        except Exception as e:
            logging.error(f"Error checking {username}: {e}")
            return TwitchResponseStatus.ERROR

    def _should_check_user_now(self, username, current_time):
        if not self.offline_backoff_enabled:
            return True

        with self._offline_backoff_lock:
            next_check_time = self._next_user_check_at.get(username, 0)
            return current_time >= next_check_time

    def _reset_user_backoff(self, username):
        with self._offline_backoff_lock:
            self._offline_check_counts[username] = 0
            self._next_user_check_at[username] = 0

    def _update_user_check_schedule(self, username, status):
        telemetry = getattr(self, "_telemetry", None)
        if telemetry:
            telemetry.note_channel(username, status.name)
        if status in (TwitchResponseStatus.OFFLINE, TwitchResponseStatus.NOT_FOUND):
            if hasattr(self, '_eventsub_recovery_lock'):
                with self._eventsub_recovery_lock:
                    self._eventsub_recovery_users.discard(username)
        if not self.offline_backoff_enabled:
            return

        if status == TwitchResponseStatus.OFFLINE:
            with self._offline_backoff_lock:
                offline_count = self._offline_check_counts.get(username, 0) + 1
                self._offline_check_counts[username] = offline_count

                delay_seconds = min(
                    self.offline_backoff_max_seconds,
                    self.offline_backoff_base_seconds * (2 ** (offline_count - 1))
                )
                self._next_user_check_at[username] = time.time() + delay_seconds

            logging.info(
                f"{username} offline backoff: next check in {int(delay_seconds)}s "
                f"(offline count: {offline_count})"
            )
            return

        self._reset_user_backoff(username)

    def create_directories(self):
        """Create necessary directories with proper error handling"""
        paths = {}
        try:
            for username in self.usernames:
                recorded_path = os.path.join(self.root_path, "recorded", username)
                processed_path = os.path.join(self.root_path, "processed", username)
                os.makedirs(recorded_path, exist_ok=True)
                os.makedirs(processed_path, exist_ok=True)
                paths[username] = (recorded_path, processed_path)
        except PermissionError:
            logging.error(f"Permission denied creating directories in {self.root_path}")
            sys.exit(1)
        except Exception as e:
            logging.error(f"Error creating directories: {e}")
            sys.exit(1)
        return paths

    def _prune_if_due(self, paths):
        now = time.monotonic()
        if now < self._next_prune_at:
            return
        # Processing can replace files and markers. Defer maintenance while it runs.
        with self._idle_processing_lock:
            if self._idle_processing_thread and self._idle_processing_thread.is_alive():
                return
            for username, (recorded_path, processed_path) in paths.items():
                if not self._is_user_recording(username):
                    self.prune_old_files(recorded_path)
                self.prune_old_files(processed_path)
                self.prune_old_files(os.path.join(processed_path, 'stitched'))
        self._next_prune_at = now + self.retention_check_interval

    def prune_old_files(self, path):
        """Prune old files with better error handling"""
        if self.prune_after_days <= 0:
            return
            
        try:
            now = datetime.datetime.now()
            for filepath in Path(path).glob('*.mp4'):
                if filepath.is_file() and not Path(str(filepath) + '.upload-pending').exists():
                    file_modified_time = datetime.datetime.fromtimestamp(filepath.stat().st_mtime)
                    age_in_days = (now - file_modified_time).days
                    if age_in_days > self.prune_after_days:
                        try:
                            filepath.unlink()
                            for suffix in ('.compressed', '.attempts', '.session.json', '.parts.json'):
                                sidecar = Path(str(filepath) + suffix)
                                if sidecar.exists():
                                    sidecar.unlink()
                            logging.info(f"Deleted old file: {filepath}")
                        except Exception as e:
                            logging.error(f"Failed to delete {filepath}: {e}")
        except Exception as e:
            logging.error(f"Error during pruning in {path}: {e}")

    def process_previous_recordings(self, recorded_path, processed_path):
        """Process ONE existing recording per call (called during idle time only)"""
        try:
            video_files = [f for f in os.listdir(recorded_path) 
                          if os.path.isfile(os.path.join(recorded_path, f)) and f.endswith('.mp4')]
            
            # Only process ONE file at a time to avoid blocking for too long
            if video_files:
                filename = video_files[0]  # Just process the first one
                recorded_filename = os.path.join(recorded_path, filename)
                processed_filename = os.path.join(processed_path, filename)
                logging.info(f"Processing {filename} ({len(video_files)-1} remaining)")
                self.process_recorded_file(recorded_filename, processed_filename)
        except Exception as e:
            logging.error(f"Error processing previous recordings: {e}")

    def process_recorded_file(self, recorded_filename, processed_filename):
        """Process a single recorded file with proper error handling"""
        try:
            # Wait a moment to ensure file is not being written to
            if self._shutdown_event.wait(3):
                return
            
            # Check if file is still being written (multiple size checks)
            sizes = []
            for i in range(3):
                sizes.append(os.path.getsize(recorded_filename))
                if i < 2:  # Don't sleep after last check
                    if self._shutdown_event.wait(2):
                        return
            
            # If file size is still changing, it's being written to
            if len(set(sizes)) > 1:
                logging.warning(f"File {recorded_filename} still being written (sizes: {sizes}), skipping")
                return
            
            # Additional check: ensure file isn't locked by another process
            try:
                with open(recorded_filename, 'rb') as f:
                    pass  # Just try to open, don't read
            except IOError as e:
                logging.warning(f"File {recorded_filename} is locked or inaccessible: {e}")
                return
            
            self._check_processing_idle()
            if self.disable_ffmpeg:
                if self.upload_to_network_drive_enabled:
                    Path(processed_filename + '.upload-pending').touch()
                logging.info(f"Moving: {recorded_filename}")
                self._move_recording(recorded_filename, processed_filename)
                self._transfer_recording_metadata(recorded_filename, processed_filename)
                self._clear_processing_attempts(recorded_filename)
            else:
                # Double check we're still idle before starting ffmpeg
                if self.active_recordings > 0:
                    logging.info(f"Stream recording started, postponing ffmpeg processing")
                    return
                    
                logging.info(f"Processing with ffmpeg: {recorded_filename}")
                if self.ffmpeg_copy_and_fix_errors(recorded_filename, processed_filename):
                    self._transfer_recording_metadata(recorded_filename, processed_filename)
                    self._clear_processing_attempts(recorded_filename)
                    try:
                        os.remove(recorded_filename)
                    except Exception as e:
                        logging.error(f"Failed to remove {recorded_filename} after processing: {e}")
                        # Continue anyway - processed file was created successfully
                else:
                    logging.error(f"FFmpeg processing failed for {recorded_filename}")
                    self._record_processing_failure(recorded_filename)
                    return
            
            if self.upload_to_network_drive_enabled:
                self.upload_to_network_drive(processed_filename)
                
        except ProcessingInterrupted:
            logging.info(f"Processing postponed: {recorded_filename}")
        except Exception as e:
            logging.error(f"Error processing file {recorded_filename}: {e}")
            if os.path.exists(recorded_filename):
                self._record_processing_failure(recorded_filename)

    def _transfer_recording_metadata(self, source, destination):
        metadata = Path(str(source) + '.session.json')
        if metadata.exists():
            shutil.copyfile(str(metadata), str(destination) + '.session.json')
            metadata.unlink()

    def _stitch_part(self, path):
        """Read broadcast identity and media format; fall back conservatively for old files."""
        match = re.fullmatch(r'(.+?) - (\d{4}-\d{2}-\d{2} \d{2}h\d{2}m\d{2}s) - (.+)\.mp4', path.name)
        if not match:
            return None
        telemetry = getattr(self, '_telemetry', None)
        cached = telemetry.journal.media(str(path)) if telemetry else None
        probe = cached.get('stitch_probe') if cached else None
        if not probe:
            result = subprocess.run([
                str(Path(self.ffmpeg_path).with_name('ffprobe')), '-v', 'error',
                '-show_streams', '-show_format', '-show_data_hash', 'sha256',
                '-of', 'json', str(path)
            ], capture_output=True, text=True, timeout=30)
            if result.returncode:
                return None
            probe = json.loads(result.stdout)
        duration = float(probe['format']['duration'])
        if not math.isfinite(duration) or duration <= 0:
            return None
        fields = ('codec_type', 'codec_name', 'width', 'height', 'pix_fmt',
                  'sample_rate', 'channels', 'time_base', 'extradata_hash')
        profile = [tuple(stream.get(key) for key in fields) for stream in probe['streams']]
        if not profile or not any(s[0] == 'video' for s in profile):
            return None
        start = datetime.datetime.strptime(match[2], '%Y-%m-%d %Hh%Mm%Ss').timestamp()
        end = start + duration
        stream_id = None
        metadata_path = Path(str(path) + '.session.json')
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text())
            # An unfinished recording must never enter a joined output.
            if not metadata.get('ended_at'):
                return None
            start, end = float(metadata['started_at']), float(metadata['ended_at'])
            stream_id = metadata.get('stream_id')
        telemetry = getattr(self, '_telemetry', None)
        if telemetry:
            video = next((s for s in probe['streams'] if s.get('codec_type') == 'video'), {})
            telemetry.cache_media(str(path), dict(duration=duration, width=video.get('width'),
                height=video.get('height'), codec=video.get('codec_name'),
                stitch_probe=dict(format=dict(duration=duration), streams=[{key:stream.get(key) for key in fields} for stream in probe['streams']])))
        return dict(path=path, start=start, end=end, duration=duration,
                    stream_id=stream_id, title=match[3], user=match[1], profile=profile)

    def stitch_processed_recordings(self, processed_path):
        """Publish verified joined copies, keeping all original parts for recovery."""
        parts = []
        for path in sorted(Path(processed_path).glob('*.mp4')):
            self._check_processing_idle()
            try:
                part = self._stitch_part(path)
                if part:
                    parts.append(part)
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                logging.warning(f'Skipping stitching candidate {path}: {exc}')
        parts.sort(key=lambda part: part['start'])
        groups = []
        max_gap = getattr(self, 'stitch_max_gap_seconds', 900)
        for part in parts:
            previous = groups[-1][-1] if groups else None
            same_session = False
            if previous and previous['user'] == part['user']:
                if previous['stream_id'] and part['stream_id']:
                    same_session = previous['stream_id'] == part['stream_id']
                elif not previous['stream_id'] and not part['stream_id']:
                    same_session = previous['title'] == part['title']
                gap = part['start'] - previous['end']
                same_session = same_session and -2 <= gap <= max_gap
            if same_session:
                groups[-1].append(part)
            else:
                groups.append([part])
        for group in groups:
            self._check_processing_idle()
            if len(group) < 2 or time.time() - group[-1]['end'] < max_gap:
                continue
            if any(part['profile'] != group[0]['profile'] for part in group):
                logging.warning(f'Cannot stitch incompatible media formats: {group[0]["path"]}')
                continue
            output_dir = Path(processed_path) / 'stitched'
            output_dir.mkdir(exist_ok=True)
            destination = output_dir / (group[0]['path'].stem + ' - stitched.mp4')
            manifest_path = Path(str(destination) + '.parts.json')
            names = [part['path'].name for part in group]
            if destination.exists() and manifest_path.exists():
                try:
                    if json.loads(manifest_path.read_text()) == names:
                        continue
                except ValueError:
                    pass
            temp_output = self._temporary_output(str(destination))
            try:
                with tempfile.TemporaryDirectory(prefix='twitch-stitch-') as workdir:
                    # Safe simple aliases avoid concat quoting issues with titles/paths.
                    entries = []
                    for index, part in enumerate(group):
                        alias = Path(workdir) / f'part-{index}.mp4'
                        alias.symlink_to(part['path'].resolve())
                        entries.append(f"file '{alias}'\n")
                        entries.append(f"duration {part['duration']}\n")
                    concat = Path(workdir) / 'parts.txt'
                    concat.write_text(''.join(entries))
                    expected = sum(part['duration'] for part in group)
                    result = self._run_background_ffmpeg([
                        self.ffmpeg_path, '-nostdin', '-nostats', '-loglevel', 'warning',
                        '-f', 'concat', '-safe', '0', '-i', str(concat),
                        '-map', '0', '-c', 'copy', '-movflags', '+faststart', '-y', temp_output
                    ], max(5400, int(expected * 2.5) + 900))
                    actual = self._probe_media_duration_seconds(temp_output)
                    if result.returncode or not actual or abs(actual - expected) > max(2, expected * .02):
                        logging.error(f'Stitch failed validation for {destination}: {result.stderr}')
                        continue
                    self._check_processing_idle()
                    if self.upload_to_network_drive_enabled:
                        Path(str(destination) + '.upload-pending').touch()
                    os.replace(temp_output, destination)
                    manifest_path.write_text(json.dumps(names, indent=2))
                    self._dashboard_event('broadcast-stitched', 'Published stitched broadcast with %s parts' % len(names), str(destination))
                    logging.info(f'Stitched {len(group)} recording parts into {destination}')
                    if self.upload_to_network_drive_enabled:
                        self.upload_to_network_drive(str(destination))
            finally:
                if os.path.exists(temp_output):
                    os.remove(temp_output)

    def _processing_attempts_file(self, recorded_filename):
        return f"{recorded_filename}.attempts"

    def _read_processing_attempts(self, recorded_filename):
        attempts_file = self._processing_attempts_file(recorded_filename)
        try:
            if not os.path.exists(attempts_file):
                return 0
            with open(attempts_file, "r") as file:
                return int(file.read().strip() or "0")
        except Exception:
            return 0

    def _write_processing_attempts(self, recorded_filename, attempts):
        attempts_file = self._processing_attempts_file(recorded_filename)
        with open(attempts_file, "w") as file:
            file.write(str(attempts))

    def _clear_processing_attempts(self, recorded_filename):
        attempts_file = self._processing_attempts_file(recorded_filename)
        try:
            if os.path.exists(attempts_file):
                os.remove(attempts_file)
        except Exception as e:
            logging.warning(f"Failed clearing attempts file {attempts_file}: {e}")

    def _quarantine_failed_recording(self, recorded_filename):
        username = Path(recorded_filename).parent.name
        failed_dir = os.path.join(self.root_path, "failed", username)
        os.makedirs(failed_dir, exist_ok=True)

        destination = os.path.join(failed_dir, os.path.basename(recorded_filename))
        if os.path.exists(destination):
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            stem, ext = os.path.splitext(os.path.basename(recorded_filename))
            destination = os.path.join(failed_dir, f"{stem}.failed-{timestamp}{ext}")

        attempts = self._read_processing_attempts(recorded_filename)
        shutil.move(recorded_filename, destination)
        self._transfer_recording_metadata(recorded_filename, destination)
        Path(str(destination)+'.failure.json').write_text(json.dumps(dict(attempts=attempts, failed_at=time.time(), reason='Processing retry limit reached')))
        self._clear_processing_attempts(recorded_filename)
        self._dashboard_event('recording-quarantined', 'Processing retry limit reached', str(destination))
        logging.error(
            f"Quarantined recording after {self.max_processing_attempts} failed processing attempts: {destination}"
        )

    def _record_processing_failure(self, recorded_filename):
        attempts = self._read_processing_attempts(recorded_filename) + 1
        self._write_processing_attempts(recorded_filename, attempts)

        logging.warning(
            f"Processing attempt {attempts}/{self.max_processing_attempts} failed for {recorded_filename}"
        )

        if attempts >= self.max_processing_attempts:
            try:
                self._quarantine_failed_recording(recorded_filename)
            except Exception as e:
                logging.error(f"Failed to quarantine recording {recorded_filename}: {e}")

    def _check_processing_idle(self):
        if self._shutdown_event.is_set() or self.active_recordings > 0:
            raise ProcessingInterrupted()

    def _move_recording(self, source, destination):
        try:
            os.replace(source, destination)
            return
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
        temp_output = self._temporary_output(destination)
        try:
            with open(source, 'rb') as src, open(temp_output, 'wb') as dst:
                while True:
                    self._check_processing_idle()
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if os.path.getsize(source) != os.path.getsize(temp_output):
                raise OSError('Recording copy size verification failed')
            shutil.copystat(source, temp_output)
            self._check_processing_idle()
            os.replace(temp_output, destination)
            os.remove(source)
        finally:
            if os.path.exists(temp_output):
                os.remove(temp_output)

    def _temporary_output(self, destination):
        fd, path = tempfile.mkstemp(prefix='.twitch-recorder-',
                                    suffix='.tmp.mp4', dir=str(Path(destination).parent))
        os.close(fd)
        return path

    def _background_pause_reason(self):
        if self.active_recordings:
            return 'recording'
        from benchmark_control import active_benchmark
        if active_benchmark(Path(__file__).resolve().parent):
            return 'benchmark'
        return None

    def _wait_for_compression_idle(self):
        while self._background_pause_reason() and not self._shutdown_event.is_set():
            self._shutdown_event.wait(0.5)
        if self._shutdown_event.is_set():
            raise ProcessingInterrupted()

    def _process_before_compression_resume(self):
        """Finish newly recorded footage while the encoder remains suspended."""
        for username, (recorded_path, processed_path) in getattr(self, '_idle_processing_paths', {}).items():
            processed_any = False
            while True:
                self._check_processing_idle()
                before = {str(path) for path in Path(recorded_path).glob('*.mp4')}
                if not before:
                    break
                self.process_previous_recordings(recorded_path, processed_path)
                self._check_processing_idle()
                after = {str(path) for path in Path(recorded_path).glob('*.mp4')}
                if after == before:
                    break  # A failed/busy source must not cause an endless retry loop.
                processed_any = True
            if processed_any and not list(Path(recorded_path).glob('*.mp4')) and getattr(self, 'stitch_recordings_enabled', True):
                self.stitch_processed_recordings(processed_path)

    def _run_background_ffmpeg(self, command, timeout, pause_for_recordings=False):
        """Pause compression for recordings; cancel other work; always reap children."""
        self._check_processing_idle()
        if pause_for_recordings:
            self._wait_for_compression_idle()
        process = None
        self._processing_active.set()
        telemetry = getattr(self, '_telemetry', None)
        capture = None
        interrupted = False
        suspended = False
        control = None
        try:
            # Keep verbose FFmpeg output off the heap for multi-hour recordings.
            with tempfile.TemporaryFile(mode='w+b') as stderr:
                if telemetry:
                    try:
                        capture = telemetry.progress(command)
                    except Exception:
                        capture = None
                if pause_for_recordings and capture is None:
                    from recorder_state import ProgressCapture
                    capture = ProgressCapture(None, command)
                process = subprocess.Popen(capture.command if capture else command,
                    stdout=capture.output if capture else subprocess.DEVNULL, stderr=stderr)
                if capture:
                    capture.attach(process)
                try:
                    if os.name == 'posix':
                        psutil.Process(process.pid).nice(10)
                except (psutil.Error, OSError):
                    pass
                deadline = time.monotonic() + timeout if timeout is not None else None
                try:
                    while process.poll() is None:
                        if capture:
                            capture.sample()
                        if self._shutdown_event.is_set():
                            raise ProcessingInterrupted()
                        if pause_for_recordings:
                            pause_reason = self._background_pause_reason()
                            if pause_reason:
                                if not suspended:
                                    try:
                                        control = psutil.Process(process.pid)
                                        control.suspend()
                                    except psutil.NoSuchProcess:
                                        if process.poll() is not None:
                                            break
                                        raise
                                    suspended = True
                                    logging.info('Idle compression paused for %s (PID %s); progress retained', pause_reason, process.pid)
                                    self._dashboard_event('compression-paused', 'Compression paused for %s; progress retained' % pause_reason, command[-1])
                                capture.set_paused(True, pause_reason)
                                self._shutdown_event.wait(0.5)
                                continue
                            if suspended:
                                try:
                                    self._process_before_compression_resume()
                                except ProcessingInterrupted:
                                    if self._shutdown_event.is_set():
                                        raise
                                    continue  # Another recording started; retain this encoder.
                                finally:
                                    self._processing_active.set()
                                control.resume()
                                suspended = False
                                capture.set_paused(False)
                                logging.info('Idle compression resumed (PID %s)', process.pid)
                                self._dashboard_event('compression-resumed', 'Compression resumed from saved in-memory progress', command[-1])
                            stall = getattr(self, 'idle_compress_stall_timeout', 1800)
                            if time.monotonic() - capture.last_advance_at >= stall:
                                raise subprocess.TimeoutExpired(command, stall)
                        else:
                            self._check_processing_idle()
                        if deadline is not None and time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(command, timeout)
                        self._shutdown_event.wait(0.5)
                    if pause_for_recordings:
                        pause_reason = self._background_pause_reason()
                        if pause_reason:
                            capture.set_paused(True, pause_reason)
                        self._wait_for_compression_idle()
                        capture.set_paused(False)
                    else:
                        self._check_processing_idle()
                    stderr.seek(0, os.SEEK_END)
                    stderr.seek(max(0, stderr.tell() - 16384))
                    return subprocess.CompletedProcess(command, process.returncode, '',
                                                       stderr.read().decode(errors='replace'))
                finally:
                    if process.poll() is None:
                        if suspended:
                            try:
                                control.resume()
                            except psutil.NoSuchProcess:
                                pass
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        except ProcessingInterrupted:
            interrupted = True
            raise
        finally:
            if capture:
                capture.finish(process.returncode if process else None, interrupted)
            self._processing_active.clear()

    def _valid_media_output(self, filename, source=None):
        if not os.path.isfile(filename) or os.path.getsize(filename) == 0:
            return False
        duration = self._probe_media_duration_seconds(filename)
        if not duration:
            return False
        source_duration = self._probe_media_duration_seconds(source) if source else None
        # Reject truncated but readable outputs before deleting/replacing the original.
        return not source_duration or duration >= source_duration - max(2, source_duration * 0.02)

    def ffmpeg_copy_and_fix_errors(self, recorded_filename, processed_filename):
        """Remux recorded stream into a proper MP4 container (stream copy, no re-encoding)."""
        temp_output = None
        try:
            file_size_gb = os.path.getsize(recorded_filename) / (1024**3)
            timeout_seconds = self._calculate_ffmpeg_timeout(recorded_filename, file_size_gb)
            
            logging.info(f"Processing {recorded_filename} ({file_size_gb:.2f}GB, timeout: {timeout_seconds}s)")
            
            # Stream copy: remux MPEG-TS into proper MP4 container without re-encoding
            # This is fast, preserves original quality, and produces player-compatible files
            temp_output = self._temporary_output(processed_filename)
            result = self._run_background_ffmpeg([
                self.ffmpeg_path, 
                "-nostdin", "-nostats", "-loglevel", "warning",
                "-err_detect", "ignore_err",
                "-i", recorded_filename,
                "-c", "copy",                # Copy all streams without re-encoding
                "-movflags", "+faststart",   # Optimize for streaming/seeking
                "-y",                        # Overwrite output file
                temp_output
            ], timeout_seconds)

            if result.returncode != 0:
                logging.error(f"FFmpeg failed for {recorded_filename}")
                logging.error(f"FFmpeg stderr: {result.stderr}")
                logging.error(f"FFmpeg stdout: {result.stdout}")
                return False
            if not self._valid_media_output(temp_output, recorded_filename):
                logging.error(f"Invalid or truncated remux output for {recorded_filename}")
                return False
            self._check_processing_idle()
            if self.upload_to_network_drive_enabled:
                Path(processed_filename + '.upload-pending').touch()
            os.replace(temp_output, processed_filename)
            self._dashboard_event('recording-processed', 'Verified remux published', processed_filename)
            logging.info(f"Successfully processed: {recorded_filename}")
            return True
        except subprocess.TimeoutExpired:
            logging.error(f"FFmpeg timeout ({timeout_seconds}s) processing {recorded_filename}")
            return False
        except ProcessingInterrupted:
            raise
        except Exception as e:
            logging.error(f"FFmpeg error: {e}")
            return False
        finally:
            if temp_output and os.path.exists(temp_output):
                os.remove(temp_output)

    def compress_processed_file(self, processed_path):
        """Compress ONE processed file per call using H.264 re-encoding to save space.
        
        Skips files that have already been compressed (marked with .compressed marker).
        Only runs when the system is idle (no active recordings).
        """
        temp_output = None
        try:
            video_files = [f for f in os.listdir(processed_path)
                          if os.path.isfile(os.path.join(processed_path, f))
                          and f.endswith('.mp4')
                          and not f.startswith('.')
                          and not os.path.exists(os.path.join(processed_path, f + '.upload-pending'))
                          and not os.path.exists(os.path.join(processed_path, f + '.compressed'))]
            
            if not video_files:
                return

            # Leave recent parts untouched until the stitching reconnect window closes.
            if getattr(self, 'stitch_recordings_enabled', True):
                eligible = []
                for filename in video_files:
                    path = Path(processed_path) / filename
                    try:
                        part = self._stitch_part(path)
                    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                        part = None
                    if not part or time.time() - part['end'] >= getattr(self, 'stitch_max_gap_seconds', 900):
                        eligible.append(filename)
                video_files = eligible
                if not video_files:
                    return
            
            filename = video_files[0]
            source = os.path.join(processed_path, filename)
            marker = source + '.compressed'
            
            file_size_gb = os.path.getsize(source) / (1024**3)
            self._probe_media_duration_seconds(source)  # Cache duration for long-running progress/ETA.
            
            logging.info(f"Idle compress: {filename} ({file_size_gb:.2f}GB)")
            
            # Bail out if a recording starts
            if self.active_recordings > 0:
                logging.info("Recording started, postponing idle compression")
                return
            
            temp_output = self._temporary_output(source)
            result = self._run_background_ffmpeg([
                self.ffmpeg_path,
                "-nostdin", "-nostats", "-loglevel", "warning",
                "-threads", str(self.processing_threads),
                "-i", source,
                "-c:v", "libx264",
                "-crf", str(self.idle_compress_crf),
                "-preset", self.idle_compress_preset,
                "-threads", str(self.processing_threads),
                "-filter_threads", "1",
                "-c:a", "aac",
                "-b:a", self.idle_compress_audio_bitrate,
                "-movflags", "+faststart",
                "-y",
                temp_output
            ], None, pause_for_recordings=True)
            
            if result.returncode != 0:
                logging.error(f"Idle compress failed for {filename}: {result.stderr}")
                if os.path.exists(temp_output):
                    os.remove(temp_output)
                return
            
            # Verify compressed file is valid and smaller
            if self._valid_media_output(temp_output, source):
                self._wait_for_compression_idle()
                original_size = os.path.getsize(source)
                compressed_size = os.path.getsize(temp_output)
                savings = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0
                
                if compressed_size < original_size:
                    os.replace(temp_output, source)
                    self._dashboard_event('recording-compressed', 'Verified smaller compressed output published', source)
                    # Mark as compressed so we don't re-process
                    with open(marker, 'w') as f:
                        f.write(f"compressed {datetime.datetime.now().isoformat()} savings={savings:.1f}%")
                    logging.info(f"Compressed {filename}: {original_size/(1024**2):.1f}MB -> {compressed_size/(1024**2):.1f}MB ({savings:.1f}% saved)")
                else:
                    os.remove(temp_output)
                    # Mark anyway so we skip it next time
                    with open(marker, 'w') as f:
                        f.write(f"skipped {datetime.datetime.now().isoformat()} already-small")
                    logging.info(f"Skipped compressing {filename} (already efficient)")
            else:
                if os.path.exists(temp_output):
                    os.remove(temp_output)
                    
        except ProcessingInterrupted:
            logging.info(f"Idle compression postponed for {processed_path}")
        except subprocess.TimeoutExpired:
            logging.error(f"Idle compression stalled for {processed_path}; no encoding progress for "
                          f"{getattr(self, 'idle_compress_stall_timeout', 1800)}s")
        except Exception as e:
            logging.error(f"Idle compress error: {e}")
        finally:
            if temp_output and os.path.exists(temp_output):
                os.remove(temp_output)

    def _probe_media_duration_seconds(self, filename):
        """Get media duration in seconds using ffprobe when available."""
        telemetry = getattr(self, '_telemetry', None)
        if telemetry:
            cached = telemetry.journal.media(filename)
            if cached and cached.get('duration'):
                return cached['duration']
        try:
            result = subprocess.run([
                str(Path(self.ffmpeg_path).with_name('ffprobe')),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filename
            ], capture_output=True, text=True, timeout=15)

            if result.returncode != 0:
                return None

            duration = float(result.stdout.strip())
            if duration > 0:
                if telemetry:
                    telemetry.cache_media(filename, dict(duration=duration))
                return duration
            return None
        except Exception:
            return None

    def _calculate_ffmpeg_timeout(self, recorded_filename, file_size_gb):
        """Calculate timeout based on duration when possible, with conservative fallback for slow CPUs."""
        duration_seconds = self._probe_media_duration_seconds(recorded_filename)
        if duration_seconds:
            timeout_seconds = max(5400, int(duration_seconds * 2.5) + 900)
            logging.info(
                f"Using duration-based timeout for {recorded_filename}: {timeout_seconds}s "
                f"(duration: {duration_seconds:.0f}s)"
            )
            return timeout_seconds

        timeout_seconds = max(5400, int(file_size_gb * 9000)) if file_size_gb > 0 else 5400
        logging.info(
            f"Using size-based timeout fallback for {recorded_filename}: {timeout_seconds}s "
            f"(size: {file_size_gb:.2f}GB)"
        )
        return timeout_seconds

    def check_user(self, username):
        """Check if user is streaming with token refresh"""
        # Proactively refresh token if needed
        self.fetch_access_token()
        
        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {self.access_token}"}
        try:
            response = requests.get(f"{self.url}?user_login={username}", headers=headers, timeout=15)
            
            if response.status_code == 401:
                # Try refreshing token once
                rejected_token = headers["Authorization"][7:]
                headers["Authorization"] = f"Bearer {self.fetch_access_token(rejected_token=rejected_token)}"
                response = requests.get(f"{self.url}?user_login={username}", headers=headers, timeout=15)
            
            response.raise_for_status()
            info = response.json()
            # Validate response has expected structure
            if "data" not in info:
                logging.error(f"Unexpected API response for {username}: missing 'data' key")
                return TwitchResponseStatus.ERROR, None
            return TwitchResponseStatus.ONLINE if info["data"] else TwitchResponseStatus.OFFLINE, info
            
        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking user {username}: {e}")
            if getattr(e, 'response', None) is not None:
                if e.response.status_code == 401:
                    return TwitchResponseStatus.UNAUTHORIZED, None
                elif e.response.status_code == 404:
                    return TwitchResponseStatus.NOT_FOUND, None
            return TwitchResponseStatus.ERROR, None

    def _retry_pending_uploads(self, processed_path):
        if not self.upload_to_network_drive_enabled:
            return
        for marker in sorted(Path(processed_path).glob('*.upload-pending')):
            if self._shutdown_event.is_set() or self.active_recordings > 0:
                return
            source = str(marker)[:-len('.upload-pending')]
            if os.path.isfile(source):
                self.upload_to_network_drive(source)

    def upload_to_network_drive(self, processed_filename):
        """Publish a complete copy atomically; retain a durable marker until successful."""
        temp_output = None
        marker = Path(processed_filename + '.upload-pending')
        try:
            marker.touch()
            if not self.network_drive_path:
                logging.error("Network upload enabled but network_drive_path is empty")
                return False
            self._check_processing_idle()
            os.makedirs(self.network_drive_path, exist_ok=True)
            destination = os.path.join(self.network_drive_path, os.path.basename(processed_filename))
            fd, temp_output = tempfile.mkstemp(prefix='.twitch-recorder-',
                                              suffix='.uploading', dir=self.network_drive_path)
            with os.fdopen(fd, 'wb') as target, open(processed_filename, 'rb') as source:
                while True:
                    self._check_processing_idle()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if os.path.getsize(temp_output) != os.path.getsize(processed_filename):
                raise OSError("Upload size verification failed")
            shutil.copystat(processed_filename, temp_output)
            self._check_processing_idle()
            os.replace(temp_output, destination)
            marker.unlink()
            logging.info(f"Successfully uploaded: {destination}")
            return True
        except ProcessingInterrupted:
            logging.info(f"Upload postponed: {processed_filename}")
            return False
        except Exception as e:
            logging.error(f"Failed to upload to network drive: {e}")
            return False
        finally:
            if temp_output and os.path.exists(temp_output):
                os.remove(temp_output)

    def record_stream(self, username, info, recorded_path, processed_path):
        """Record a stream with proper process management"""
        streamlink_process = None
        recorded_filename = None
        session = None
        try:
            channel = info["data"][0]
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %Hh%Mm%Ss')
            title = channel.get('title', 'Unknown')
            # Sanitize filename
            safe_title = "".join(c for c in title if c.isalnum() or c in [" ", "-", "_"])[:100].strip()
            # Ensure we have a valid title (fallback if all characters were stripped)
            if not safe_title:
                safe_title = "stream"
            filename = f"{username} - {timestamp} - {safe_title}.mp4"
            
            recorded_filename = os.path.join(recorded_path, filename)
            processed_filename = os.path.join(processed_path, filename)
            session = dict(stream_id=channel.get('id'), started_at=time.time(), ended_at=None)
            Path(recorded_filename + '.session.json').write_text(json.dumps(session))
            self._dashboard_event('recording-started', username+' recording started', recorded_filename)

            logging.info(f"{Fore.GREEN}{username} online, starting recording")

            # Start streamlink process
            streamlink_cmd = [
                "streamlink", "--twitch-disable-ads", "--retry-streams", "5",
                f"twitch.tv/{username}", self.quality, "-o", recorded_filename
            ]
            
            # Use DEVNULL to prevent buffer overflow from unread pipes
            streamlink_process = subprocess.Popen(
                streamlink_cmd, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Store process for cleanup (with proper lock)
            with self._recording_processes_lock:
                self._recording_processes[username] = streamlink_process
                self._pending_recordings.discard(username)

            # Monitor recording with improved progress tracking
            try:
                self._monitor_recording(streamlink_process, recorded_filename, filename)
            finally:
                # Always clean up process reference even if monitoring fails
                with self._recording_processes_lock:
                    self._recording_processes.pop(username, None)
                    self._pending_recordings.discard(username)
                
                # Ensure process is terminated if still running
                if streamlink_process.poll() is None:
                    logging.warning(f"Streamlink process still running for {username}, terminating")
                    try:
                        streamlink_process.terminate()
                        streamlink_process.wait(timeout=5)
                    except:
                        streamlink_process.kill()

            # Don't process immediately - let it happen during idle time in main loop
            if os.path.exists(recorded_filename) and os.path.getsize(recorded_filename) > 0:
                logging.info(f"Recording completed for {username}, will process when idle")
            else:
                logging.warning(f"Recording file for {username} not found or empty")

        except Exception as e:
            logging.error(f"Error recording {username}: {e}")
        finally:
            if session and recorded_filename:
                try:
                    session['ended_at'] = time.time()
                    Path(recorded_filename + '.session.json').write_text(json.dumps(session))
                except OSError as exc:
                    logging.warning(f'Could not save recording session metadata: {exc}')
            self._release_recording_reservation(username)
            self._dashboard_event('recording-ended', username+' recording ended', recorded_filename)

            # A Streamlink exit does not generate another Twitch online event.
            if getattr(self, 'eventsub_enabled', False) and not self._shutdown_event.is_set():
                with self._eventsub_recovery_lock:
                    self._eventsub_recovery_users.add(username)
                self._reset_user_backoff(username)

    def _monitor_recording(self, process, filename, display_name):
        """Monitor recording process with better progress display and timeout"""
        try:
            # Maximum recording time: 12 hours (configurable safety limit)
            max_recording_time = 12 * 3600
            start_time = time.monotonic()
            last_growth_at = start_time
            
            with tqdm(total=0, unit='B', unit_scale=True, desc=display_name[:50], ncols=100) as pbar:
                last_size = 0
                stalled_count = 0
                file_check_failures = 0
                
                while process.poll() is None:
                    # Check for overall timeout
                    if time.monotonic() - start_time > max_recording_time:
                        logging.warning(f"Recording timeout ({max_recording_time}s) reached for {display_name}")
                        process.terminate()
                        break
                    
                    if self._shutdown_event.is_set():
                        logging.info(f"Shutdown requested, stopping recording for {display_name}")
                        process.terminate()
                        break
                    
                    try:
                        if os.path.exists(filename):
                            current_size = os.path.getsize(filename)
                            if current_size > last_size:
                                pbar.total = current_size
                                pbar.n = current_size
                                pbar.refresh()
                                last_size = current_size
                                last_growth_at = time.monotonic()
                                stalled_count = 0
                                file_check_failures = 0
                            else:
                                stalled_count += 1
                                if stalled_count > 12:  # 1 minute of no growth
                                    logging.warning(f"Recording appears stalled for {display_name}")
                                    stalled_count = 0  # Reset to avoid spam
                        else:
                            file_check_failures += 1
                            if file_check_failures > 6:  # 30 seconds of missing file
                                logging.error(f"Recording file not found for {display_name}, may have failed")
                                file_check_failures = 0
                    except OSError as e:
                        logging.error(f"Error checking file size for {display_name}: {e}")
                    
                    if time.monotonic() - last_growth_at >= self.recording_stall_timeout:
                        logging.warning(f"Recording stalled for {display_name}; stopping so polling can retry")
                        process.terminate()
                        break
                    self._shutdown_event.wait(5)
                
                # Final update
                try:
                    if os.path.exists(filename):
                        final_size = os.path.getsize(filename)
                        pbar.total = final_size
                        pbar.n = final_size
                        pbar.refresh()
                        logging.info(f"Recording complete: {display_name} ({final_size / (1024**2):.1f}MB)")
                except OSError as e:
                    logging.error(f"Error getting final file size: {e}")
                    
        except Exception as e:
            logging.error(f"Error monitoring recording: {e}")

    def _start_dashboard_status(self, paths):
        try:
            from recorder_state import RecorderTelemetry
            self._telemetry = RecorderTelemetry(self, Path(__file__).resolve().parent, paths)
            self._telemetry.start()
        except Exception as exc:
            self._telemetry = None
            logging.warning('Dashboard status unavailable: %s', type(exc).__name__)

    def _dashboard_event(self, kind, message, path=None):
        telemetry = getattr(self, '_telemetry', None)
        if telemetry:
            telemetry.event(kind, message, path)
            telemetry.invalidate()


def setup_logging():
    """Setup logging with proper configuration"""
    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)
    
    # Setup file handler with rotation
    log_filename = f"logs/twitch-recorder-{datetime.datetime.now().strftime('%Y%m%d')}.log"
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler(sys.stdout)
        ]
    )

def main(argv):
    setup_logging()
    
    try:
        twitch_recorder = TwitchRecorder()
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        logging.error(f"Failed to initialize TwitchRecorder: {e}")
        return 1

    usage_message = "twitch-recorder.py -u <usernames> -q <quality>"

    try:
        opts, args = getopt.getopt(argv, "hu:q:l:", ["usernames=", "quality=", "log=", "logging=", "disable-ffmpeg"])
    except getopt.GetoptError:
        print(usage_message)
        sys.exit(2)
        
    for opt, arg in opts:
        if opt == "-h":
            print(usage_message)
            sys.exit()
        elif opt in ("-u", "--usernames"):
            twitch_recorder.usernames = [username.strip() for username in arg.split(",")]
            logging.info(f"Usernames set to: {twitch_recorder.usernames}")
        elif opt in ("-q", "--quality"):
            twitch_recorder.quality = arg
            logging.info(f"Quality set to: {arg}")
        elif opt in ("-l", "--log", "--logging"):
            logging_level = getattr(logging, arg.upper(), None)
            if not isinstance(logging_level, int):
                raise ValueError(f"Invalid log level: {arg.upper()}")
            logging.getLogger().setLevel(logging_level)
            logging.info(f"Logging level set to {arg.upper()}")
        elif opt == "--disable-ffmpeg":
            twitch_recorder.disable_ffmpeg = True
            logging.info("FFmpeg disabled")

    try:
        twitch_recorder.run()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return 1
    finally:
        logging.info("Application terminated")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
