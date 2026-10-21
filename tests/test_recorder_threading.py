import importlib.util
import sys
import threading
import time
import types
from pathlib import Path


def load_recorder_module():
    if "tqdm" not in sys.modules:
        tqdm_module = types.ModuleType("tqdm")

        class DummyTqdm:
            def __init__(self, *args, **kwargs):
                self.total = 0
                self.n = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def refresh(self):
                pass

        tqdm_module.tqdm = DummyTqdm
        sys.modules["tqdm"] = tqdm_module

    if "colorama" not in sys.modules:
        colorama_module = types.ModuleType("colorama")
        colorama_module.init = lambda *args, **kwargs: None
        colorama_module.Fore = types.SimpleNamespace(YELLOW="", RED="", GREEN="")
        colorama_module.Style = types.SimpleNamespace()
        sys.modules["colorama"] = colorama_module

    module_path = Path(__file__).resolve().parents[1] / "twitch-recorder.py"
    spec = importlib.util.spec_from_file_location("twitch_recorder_module", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recorder_module = load_recorder_module()


def make_recorder(max_concurrent_recordings=2):
    recorder = recorder_module.TwitchRecorder.__new__(recorder_module.TwitchRecorder)
    recorder.max_concurrent_recordings = max_concurrent_recordings
    recorder._active_recordings_lock = threading.Lock()
    recorder._active_recordings = 0
    recorder._recording_processes = {}
    recorder._pending_recordings = set()
    recorder._recording_threads = {}
    recorder._recording_stream_ids = {}
    recorder._recording_processes_lock = threading.Lock()
    recorder._recording_slots = threading.BoundedSemaphore(max_concurrent_recordings)
    recorder._idle_processing_thread = None
    recorder._idle_processing_lock = threading.Lock()
    recorder._processing_active = threading.Event()
    recorder._shutdown_event = threading.Event()
    recorder.disable_ffmpeg = False
    recorder.idle_compress_enabled = True
    recorder.processing_threads = 1
    recorder.recording_stall_timeout = 300
    recorder.retention_check_interval = 3600
    recorder._next_prune_at = 0
    recorder.upload_to_network_drive_enabled = False
    recorder.usernames = []
    recorder._eventsub_recovery_users = set()
    recorder._eventsub_recovery_lock = threading.Lock()
    return recorder


def online_info(stream_id="stream-1", title="Test Stream"):
    return {"data": [{"id": stream_id, "title": title}]}


def test_duplicate_stream_reservation_is_rejected_until_released():
    recorder = make_recorder(max_concurrent_recordings=2)

    assert recorder._reserve_recording("alice", "stream-1") == (True, None)

    reserved, reason = recorder._reserve_recording("alice", "stream-1")
    assert reserved is False
    assert "already" in reason or "pending" in reason
    assert recorder.active_recordings == 1

    recorder._release_recording_reservation("alice")

    assert recorder.active_recordings == 0
    assert recorder._reserve_recording("alice", "stream-1") == (True, None)
    recorder._release_recording_reservation("alice")


def test_max_concurrent_recording_slots_are_enforced_atomically():
    recorder = make_recorder(max_concurrent_recordings=1)

    assert recorder._reserve_recording("alice", "stream-1") == (True, None)

    reserved, reason = recorder._reserve_recording("bob", "stream-2")
    assert reserved is False
    assert reason == "maximum concurrent recordings reached"
    assert recorder.active_recordings == 1

    recorder._release_recording_reservation("alice")


def test_start_recording_thread_returns_without_blocking_checker_pool():
    recorder = make_recorder(max_concurrent_recordings=1)
    started = threading.Event()
    finish = threading.Event()
    threads = []

    def fake_record_stream(username, info, recorded_path, processed_path):
        threads.append(threading.current_thread())
        try:
            started.set()
            finish.wait(timeout=2)
        finally:
            recorder._release_recording_reservation(username)

    recorder.record_stream = fake_record_stream

    start = time.monotonic()
    assert recorder._start_recording_thread(
        "alice",
        online_info("stream-1"),
        "/tmp/recorded",
        "/tmp/processed",
    )
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert started.wait(timeout=1)
    assert recorder.active_recordings == 1
    assert recorder._is_user_recording("alice") is True

    reserved, reason = recorder._reserve_recording("alice", "stream-1")
    assert reserved is False
    assert "already" in reason

    finish.set()
    threads[0].join(timeout=1)
    assert recorder.active_recordings == 0


def test_check_and_record_user_starts_background_recording_for_online_stream():
    recorder = make_recorder(max_concurrent_recordings=1)
    calls = []

    recorder.check_user = lambda username: (
        recorder_module.TwitchResponseStatus.ONLINE,
        online_info("stream-1"),
    )
    recorder.can_start_new_recording = lambda: True
    recorder._start_recording_thread = (
        lambda username, info, recorded_path, processed_path: calls.append(
            (username, info, recorded_path, processed_path)
        )
    )

    status = recorder.check_and_record_user("alice", "/tmp/recorded", "/tmp/processed")

    assert status == recorder_module.TwitchResponseStatus.ONLINE
    assert calls == [("alice", online_info("stream-1"), "/tmp/recorded", "/tmp/processed")]


def test_check_and_record_user_skips_existing_recording():
    recorder = make_recorder(max_concurrent_recordings=1)
    calls = []

    recorder.check_user = lambda username: (
        recorder_module.TwitchResponseStatus.ONLINE,
        online_info("stream-1"),
    )
    recorder._start_recording_thread = (
        lambda username, info, recorded_path, processed_path: calls.append(username)
    )
    assert recorder._reserve_recording("alice", "stream-1") == (True, None)

    status = recorder.check_and_record_user("alice", "/tmp/recorded", "/tmp/processed")

    assert status == recorder_module.TwitchResponseStatus.ONLINE
    assert calls == []
    recorder._release_recording_reservation("alice")
