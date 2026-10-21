import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from test_recorder_threading import make_recorder, recorder_module as module
from recorder_state import ProgressCapture, processing_queue


def wait_until(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Timed out waiting for encoder state')


def encoder(tmp_path, progressing=True):
    program = tmp_path / 'encoder'
    program.write_text('#!' + sys.executable + '\n' + (
        "import sys,time\nfrom pathlib import Path\n"
        "out=Path(sys.argv[-1]);frame=0\n"
        "while not out.with_suffix('.finish').exists():\n"
        " frame+=1\n"
        " out.write_text(str(frame))\n"
        " print('frame='+str(frame)+'\\nout_time=00:00:00.000000\\nprogress=continue',flush=True)\n"
        " time.sleep(.05)\n"
        if progressing else "import time\ntime.sleep(60)\n"))
    program.chmod(0o755)
    return [str(program), '-crf', '28', str(tmp_path / 'output.mp4')]


def start_worker(monkeypatch, recorder, command):
    children, results, errors = [], [], []
    popen = subprocess.Popen
    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    def run():
        try:
            results.append(recorder._run_background_ffmpeg(command, None, pause_for_recordings=True))
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    wait_until(lambda: bool(children))
    return worker, children[0], results, errors


@pytest.mark.skipif(os.name != 'posix', reason='Verifies OS process suspension')
@pytest.mark.parametrize('shutdown', [False, True])
def test_encoder_retains_progress_while_recording_and_is_reaped(monkeypatch, tmp_path, shutdown):
    recorder = make_recorder()
    recorder.idle_compress_stall_timeout = .75
    command = encoder(tmp_path)
    worker, child, results, errors = start_worker(monkeypatch, recorder, command)
    output = Path(command[-1])
    resumed_work = []
    def prepare():
        assert psutil.Process(child.pid).status() == psutil.STATUS_STOPPED
        resumed_work.append(child.pid)
    monkeypatch.setattr(recorder, '_process_before_compression_resume', prepare)
    try:
        wait_until(lambda: output.exists() and output.read_text() != '')
        recorder._reserve_recording('alice')
        wait_until(lambda: psutil.Process(child.pid).status() == psutil.STATUS_STOPPED)
        saved = output.read_text()
        time.sleep(1)  # Longer than the watchdog; paused time must not expire it.
        assert child.poll() is None and output.read_text() == saved
        if shutdown:
            recorder._shutdown_event.set()
        else:
            recorder._release_recording_reservation('alice')
            wait_until(lambda: output.read_text() not in ('', saved))
            output.with_suffix('.finish').touch()
        worker.join(5)
        assert not worker.is_alive() and child.poll() is not None
        assert not recorder._processing_active.is_set()
        if shutdown:
            assert len(errors) == 1 and isinstance(errors[0], module.ProcessingInterrupted)
        else:
            assert not errors and results[0].returncode == 0
            assert resumed_work == [child.pid]
    finally:
        recorder._shutdown_event.set()
        worker.join(6)
        if child.poll() is None:
            child.kill(); child.wait()


def test_encoder_without_progress_times_out_and_is_reaped(monkeypatch, tmp_path):
    recorder = make_recorder()
    recorder.idle_compress_stall_timeout = .1
    worker, child, results, errors = start_worker(monkeypatch, recorder, encoder(tmp_path, False))
    worker.join(5)
    try:
        assert not worker.is_alive() and child.poll() is not None
        assert not results and len(errors) == 1
        assert isinstance(errors[0], subprocess.TimeoutExpired)
    finally:
        recorder._shutdown_event.set(); worker.join(6)
        if child.poll() is None:
            child.kill(); child.wait()


def test_progress_watchdog_accepts_frames_before_first_muxed_timestamp(monkeypatch):
    clock = [0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    capture = ProgressCapture(None, ['ffmpeg', '-crf', '28', 'output.mp4'])
    clock[0] = 100
    capture.accept(b'frame=1\n'); capture.accept(b'total_size=N/A\n'); capture.accept(b'progress=continue\n')
    assert capture.last_advance_at == 100 and capture.job['media_seconds'] == 0
    capture.set_paused(True)
    clock[0] = 10000
    capture.accept(b'progress=continue\n')
    assert capture.job['phase'] == 'Paused for recording'
    capture.set_paused(False)
    assert capture.last_advance_at == 10000 and capture.paused_seconds == 9900


def test_queue_marks_retained_compression_as_paused():
    row = dict(path='source.mp4', stage='Processed', channel='pyka', title='test', size=123)
    queue = processing_queue([row], [], {}, True, [dict(inputs=['source.mp4'], paused=True)])
    assert queue[0]['state'] == 'Paused'
    assert 'progress retained' in queue[0]['reason']


def test_new_raw_recordings_are_processed_before_compression_resumes(monkeypatch, tmp_path):
    recorder = make_recorder()
    raw = tmp_path / 'raw'; raw.mkdir()
    processed = tmp_path / 'processed'; processed.mkdir()
    for name in ('part1.mp4', 'part2.mp4'):
        (raw / name).write_bytes(b'original')
    recorder._idle_processing_paths = {'pyka': (str(raw), str(processed))}
    steps = []
    def process(recorded, destination):
        source = next(Path(recorded).glob('*.mp4'))
        source.rename(Path(destination) / source.name)
        steps.append('remux')
    monkeypatch.setattr(recorder, 'process_previous_recordings', process)
    monkeypatch.setattr(recorder, 'stitch_processed_recordings', lambda path: steps.append('stitch'))
    recorder._process_before_compression_resume()
    assert steps == ['remux', 'remux', 'stitch']
    # A failing source returns to the next processing cycle without spinning.
    (raw / 'bad.mp4').write_bytes(b'bad')
    monkeypatch.setattr(recorder, 'process_previous_recordings', lambda *args: steps.append('failed'))
    recorder._process_before_compression_resume()
    assert steps[-1] == 'failed' and steps.count('failed') == 1
