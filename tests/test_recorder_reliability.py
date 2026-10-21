import errno
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_recorder_threading import make_recorder, recorder_module as module


def response(status, body):
    result = module.requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


@pytest.mark.parametrize('retry_status', [200, 401, 404])
def test_rejected_cached_token_refreshes_and_preserves_http_status(monkeypatch, retry_status):
    recorder = make_recorder()
    recorder.access_token = 'rejected'
    recorder.token_expires_at = time.time() + 86400
    recorder._token_refresh_lock = threading.Lock()
    recorder.client_id = 'test'
    recorder.token_url = 'https://example.invalid/token'
    recorder.url = 'https://example.invalid/streams'
    tokens = []

    def get(url, headers, timeout):
        tokens.append(headers['Authorization'])
        return response(401 if len(tokens) == 1 else retry_status, {'data': []})

    post = Mock(return_value=response(200, {'access_token': 'fresh', 'expires_in': 86400}))
    monkeypatch.setattr(module.requests, 'get', get)
    monkeypatch.setattr(module.requests, 'post', post)
    status, _ = recorder.check_user('alice')
    expected = {200: 'OFFLINE', 401: 'UNAUTHORIZED', 404: 'NOT_FOUND'}
    assert status.name == expected[retry_status]
    assert tokens == ['Bearer rejected', 'Bearer fresh']
    assert post.call_count == 1
    # A second checker rejecting the old token reuses the already refreshed one.
    assert recorder.fetch_access_token(rejected_token='rejected') == 'fresh'
    assert post.call_count == 1


@pytest.mark.parametrize('failure', [RuntimeError('startup failed'), SystemExit(1)])
def test_startup_failure_returns_nonzero(monkeypatch, failure):
    monkeypatch.setattr(module, 'setup_logging', lambda: None)
    monkeypatch.setattr(module, 'TwitchRecorder', Mock(side_effect=failure))
    assert module.main([]) == 1


def test_runtime_failure_returns_nonzero(monkeypatch):
    monkeypatch.setattr(module, 'setup_logging', lambda: None)
    monkeypatch.setattr(module, 'TwitchRecorder', lambda: SimpleNamespace(run=Mock(side_effect=OSError())))
    assert module.main([]) == 1


def test_missing_ffprobe_fails_before_processing(monkeypatch):
    recorder = make_recorder()
    recorder.ffmpeg_path = 'ffmpeg'

    def run(command, **kwargs):
        if command[0] == 'ffprobe':
            raise FileNotFoundError('ffprobe unavailable')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(SystemExit) as error:
        recorder._validate_dependencies()
    assert error.value.code == 1


def test_disabled_ffmpeg_still_finalizes_and_uploads(monkeypatch, tmp_path):
    recorder = make_recorder()
    recorder.disable_ffmpeg = True
    recorder.upload_to_network_drive_enabled = True
    recorder.network_drive_path = str(tmp_path / 'network')
    recorder.usernames = ['alice']
    recorded = tmp_path / 'recorded'
    processed = tmp_path / 'processed'
    recorded.mkdir()
    processed.mkdir()
    (recorded / 'stream.mp4').write_bytes(b'recorded bytes')
    monkeypatch.setattr(recorder._shutdown_event, 'wait', lambda timeout: False)
    recorder._start_idle_processing_thread({'alice': (str(recorded), str(processed))})
    recorder._idle_processing_thread.join(timeout=3)
    assert not recorder._idle_processing_thread.is_alive()
    assert not (recorded / 'stream.mp4').exists()
    assert (processed / 'stream.mp4').read_bytes() == b'recorded bytes'
    assert (tmp_path / 'network' / 'stream.mp4').read_bytes() == b'recorded bytes'
    assert not list(processed.glob('*.upload-pending'))


@pytest.mark.parametrize('reason', ['recording', 'shutdown', 'timeout'])
def test_background_child_is_stopped_and_reaped(monkeypatch, reason):
    recorder = make_recorder()
    started = threading.Event()
    children = []
    errors = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(module.subprocess, 'Popen', popen)

    def run():
        try:
            recorder._run_background_ffmpeg(
                [sys.executable, '-c', 'import time; time.sleep(30)'],
                0.05 if reason == 'timeout' else 30,
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(3)
        if reason == 'recording':
            recorder._reserve_recording('alice')
        elif reason == 'shutdown':
            recorder._shutdown_event.set()
        worker.join(3)
        assert not worker.is_alive()
        assert children[0].poll() is not None
        assert len(errors) == 1
        expected = subprocess.TimeoutExpired if reason == 'timeout' else module.ProcessingInterrupted
        assert isinstance(errors[0], expected)
        assert not recorder._processing_active.is_set()
    finally:
        recorder._shutdown_event.set()
        worker.join(6)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize('result', ['failed', 'invalid', 'interrupted', 'success'])
def test_remux_only_publishes_valid_complete_output(monkeypatch, tmp_path, result):
    recorder = make_recorder()
    recorder.ffmpeg_path = 'ffmpeg'
    source = tmp_path / 'source.mp4'
    destination = tmp_path / 'destination.mp4'
    source.write_bytes(b'original recording')
    destination.write_bytes(b'previous good output')
    monkeypatch.setattr(recorder, '_calculate_ffmpeg_timeout', lambda *args: 10)
    monkeypatch.setattr(recorder, '_valid_media_output', lambda *args: result != 'invalid')

    def run(command, timeout):
        Path(command[-1]).write_bytes(b'new complete output')
        assert destination.read_bytes() == b'previous good output'
        if result == 'interrupted':
            raise module.ProcessingInterrupted()
        return subprocess.CompletedProcess(command, 1 if result == 'failed' else 0, '', 'test')

    monkeypatch.setattr(recorder, '_run_background_ffmpeg', run)
    if result == 'interrupted':
        with pytest.raises(module.ProcessingInterrupted):
            recorder.ffmpeg_copy_and_fix_errors(str(source), str(destination))
    else:
        assert recorder.ffmpeg_copy_and_fix_errors(str(source), str(destination)) == (result == 'success')
    assert source.read_bytes() == b'original recording'
    assert destination.read_bytes() == (b'new complete output' if result == 'success' else b'previous good output')
    assert not list(tmp_path.glob('.*.tmp.mp4'))


def test_interrupted_processing_does_not_consume_retry(monkeypatch, tmp_path):
    recorder = make_recorder()
    source = tmp_path / 'recorded.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(recorder._shutdown_event, 'wait', lambda timeout: False)
    monkeypatch.setattr(recorder, 'ffmpeg_copy_and_fix_errors', Mock(side_effect=module.ProcessingInterrupted))
    recorder.process_recorded_file(str(source), str(tmp_path / 'processed.mp4'))
    assert source.exists()
    assert not Path(str(source) + '.attempts').exists()


def test_truncated_output_is_rejected(monkeypatch, tmp_path):
    recorder = make_recorder()
    output = tmp_path / 'output.mp4'
    output.write_bytes(b'playable but truncated')
    monkeypatch.setattr(recorder, '_probe_media_duration_seconds', lambda name: 10 if name == str(output) else 100)
    assert not recorder._valid_media_output(str(output), 'source.mp4')


@pytest.mark.parametrize('publish_fails', [False, True])
def test_cross_filesystem_move_preserves_original_until_publication(monkeypatch, tmp_path, publish_fails):
    recorder = make_recorder()
    source = tmp_path / 'source.mp4'
    destination = tmp_path / 'destination.mp4'
    source.write_bytes(b'original recording')
    destination.write_bytes(b'previous output')
    real_replace = os.replace

    def replace(src, dst):
        if src == str(source):
            raise OSError(errno.EXDEV, 'different filesystems')
        assert source.read_bytes() == b'original recording'
        assert destination.read_bytes() == b'previous output'
        if publish_fails:
            raise OSError('publication failed')
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, 'replace', replace)
    if publish_fails:
        with pytest.raises(OSError):
            recorder._move_recording(str(source), str(destination))
        assert source.exists()
        assert destination.read_bytes() == b'previous output'
    else:
        recorder._move_recording(str(source), str(destination))
        assert not source.exists()
        assert destination.read_bytes() == b'original recording'
    assert not list(tmp_path.glob('.*.tmp.mp4'))


def test_failed_upload_retains_previous_copy_and_retries_after_restart(monkeypatch, tmp_path):
    recorder = make_recorder()
    recorder.network_drive_path = str(tmp_path / 'network')
    source = tmp_path / 'stream.mp4'
    source.write_bytes(b'new complete recording')
    network = tmp_path / 'network'
    network.mkdir()
    destination = network / source.name
    destination.write_bytes(b'previous good copy')
    real_replace = os.replace

    def fail_publish(src, dst):
        assert destination.read_bytes() == b'previous good copy'
        raise OSError('network unavailable')

    monkeypatch.setattr(module.os, 'replace', fail_publish)
    assert not recorder.upload_to_network_drive(str(source))
    assert destination.read_bytes() == b'previous good copy'
    assert Path(str(source) + '.upload-pending').exists()
    assert not list(network.glob('*.uploading'))
    monkeypatch.setattr(module.os, 'replace', real_replace)
    restarted = make_recorder()
    restarted.upload_to_network_drive_enabled = True
    restarted.network_drive_path = str(network)
    restarted._retry_pending_uploads(str(tmp_path))
    assert destination.read_bytes() == source.read_bytes()
    assert not Path(str(source) + '.upload-pending').exists()


def test_retention_repeats_and_protects_active_and_pending_files(monkeypatch, tmp_path):
    recorder = make_recorder()
    recorder.prune_after_days = 1
    recorded = tmp_path / 'recorded'
    processed = tmp_path / 'processed'
    recorded.mkdir()
    processed.mkdir()
    old = time.time() - 10 * 86400

    def old_file(directory, name):
        path = directory / name
        path.write_bytes(b'data')
        os.utime(path, (old, old))
        return path

    active = old_file(recorded, 'active.mp4')
    pending = old_file(processed, 'pending.mp4')
    Path(str(pending) + '.upload-pending').touch()
    expired = old_file(processed, 'expired.mp4')
    marker = Path(str(expired) + '.compressed')
    marker.touch()
    recorder._reserve_recording('alice')
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    paths = {'alice': (str(recorded), str(processed))}
    recorder._prune_if_due(paths)
    assert active.exists() and pending.exists()
    assert not expired.exists() and not marker.exists()
    recorder._release_recording_reservation('alice')
    recorder._prune_if_due(paths)
    assert active.exists()
    now[0] += recorder.retention_check_interval
    recorder._prune_if_due(paths)
    assert not active.exists() and pending.exists()


@pytest.mark.parametrize('file_exists', [False, True])
def test_stalled_recording_is_terminated(monkeypatch, tmp_path, file_exists):
    recorder = make_recorder()
    recorder.recording_stall_timeout = 3
    filename = tmp_path / 'recording.mp4'
    if file_exists:
        filename.write_bytes(b'unchanging recording')
    ticks = iter(range(100))
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(recorder._shutdown_event, 'wait', lambda timeout: False)
    process = SimpleNamespace(poll=lambda: None, terminate=Mock())
    recorder._monitor_recording(process, str(filename), 'test')
    process.terminate.assert_called_once()


def test_high_cpu_does_not_disable_polling(monkeypatch):
    recorder = make_recorder()
    recorder.usernames = ['alice']
    recorder.refresh = 1
    monkeypatch.setattr(recorder, 'create_directories', lambda: {'alice': ('recorded', 'processed')})
    monkeypatch.setattr(recorder, '_prune_if_due', lambda paths: None)
    monkeypatch.setattr(recorder, '_should_check_user_now', lambda *args: True)
    monkeypatch.setattr(recorder, '_update_user_check_schedule', lambda *args: None)
    monkeypatch.setattr(recorder, '_start_idle_processing_thread', lambda paths: None)
    monkeypatch.setattr(module.psutil, 'cpu_percent', lambda **kwargs: 100)
    calls = []

    def check(*args):
        calls.append(args)
        recorder._shutdown_event.set()
        return module.TwitchResponseStatus.OFFLINE

    monkeypatch.setattr(recorder, 'check_and_record_user', check)
    recorder.run()
    assert len(calls) == 1


@pytest.mark.parametrize('processing,memory,expected', [(False, 20, False), (True, 20, True), (True, 95, False)])
def test_background_cpu_yields_but_memory_limit_remains(monkeypatch, processing, memory, expected):
    recorder = make_recorder()
    recorder.cpu_threshold = 80
    recorder.memory_threshold = 80
    recorder.root_path = '.'
    if processing:
        recorder._processing_active.set()
    monkeypatch.setattr(module.psutil, 'cpu_percent', lambda **kwargs: 95)
    monkeypatch.setattr(module.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=memory))
    monkeypatch.setattr(module.psutil, 'disk_usage', lambda path: SimpleNamespace(free=2 * 1024**3))
    assert recorder.can_start_new_recording() == expected


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg tools required')
def test_real_ffmpeg_remux_and_compression(tmp_path):
    recorder = make_recorder()
    recorder.ffmpeg_path = shutil.which('ffmpeg')
    recorder.idle_compress_crf = 28
    recorder.idle_compress_preset = 'ultrafast'
    recorder.idle_compress_audio_bitrate = '128k'
    source = tmp_path / 'source.mp4'
    processed = tmp_path / 'processed'
    processed.mkdir()
    destination = processed / 'stream.mp4'
    subprocess.run([
        recorder.ffmpeg_path, '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=128x72:rate=10:duration=1', '-c:v', 'libx264', '-f', 'mpegts', str(source),
    ], check=True, capture_output=True, timeout=15)
    assert recorder.ffmpeg_copy_and_fix_errors(str(source), str(destination))
    recorder.compress_processed_file(str(processed))
    assert Path(str(destination) + '.compressed').exists()
    assert recorder._valid_media_output(str(destination), str(source))
    assert not list(processed.glob('.*.tmp.mp4'))
