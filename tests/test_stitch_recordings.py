import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from test_recorder_threading import make_recorder, recorder_module as module


def candidates(tmp_path, monkeypatch, ids=('one', 'one'), titles=('same', 'same'), gap=120, incompatible=False):
    recorder = make_recorder()
    recorder.ffmpeg_path = 'ffmpeg'
    recorder.stitch_max_gap_seconds = 900
    parts = []
    for i in range(2):
        path = tmp_path / f"alice - 2026-01-01 0{i}h00m00s - {titles[i]}.mp4"
        path.write_bytes(b'original')
        start = 1000 + i * (600 + gap)
        parts.append(dict(path=path, start=start, end=start + 600, duration=600,
                          stream_id=ids[i], title=titles[i], user='alice',
                          profile=['different' if incompatible and i else 'same']))
    monkeypatch.setattr(recorder, '_stitch_part', lambda path: next(p for p in parts if p['path'] == path))
    return recorder, parts


@pytest.mark.parametrize('ids,titles,gap,incompatible,joined', [
    (('one', 'one'), ('old', 'new'), 120, False, True),
    (('one', 'two'), ('same', 'same'), 120, False, False),
    ((None, None), ('same', 'same'), 120, False, True),
    ((None, None), ('old', 'new'), 120, False, False),
    ((None, None), ('same', 'same'), 901, False, False),
    (('one', 'one'), ('same', 'same'), 120, True, False),
    (('one', None), ('same', 'same'), 120, False, False),
    (('one', 'one'), ('same', 'same'), -100, False, False),
])
def test_session_boundaries(monkeypatch, tmp_path, ids, titles, gap, incompatible, joined):
    recorder, parts = candidates(tmp_path, monkeypatch, ids, titles, gap, incompatible)
    calls = []
    def run(command, timeout):
        calls.append(command)
        Path(command[-1]).write_bytes(b'joined')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(recorder, '_run_background_ffmpeg', run)
    monkeypatch.setattr(recorder, '_probe_media_duration_seconds', lambda path: 1200)
    recorder.stitch_processed_recordings(str(tmp_path))
    assert bool(calls) == joined
    assert all(p['path'].read_bytes() == b'original' for p in parts)
    recorder.stitch_processed_recordings(str(tmp_path))
    assert len(calls) == int(joined)  # Completed groups do not run again.


@pytest.mark.parametrize('failure', ['truncated', 'ffmpeg', 'interrupted'])
def test_failed_join_keeps_originals_and_previous_output(monkeypatch, tmp_path, failure):
    recorder, parts = candidates(tmp_path, monkeypatch)
    output = tmp_path / 'stitched' / (parts[0]['path'].stem + ' - stitched.mp4')
    output.parent.mkdir()
    output.write_bytes(b'previous')
    def run(command, timeout):
        Path(command[-1]).write_bytes(b'bad')
        if failure == 'interrupted':
            raise module.ProcessingInterrupted()
        return subprocess.CompletedProcess(command, int(failure == 'ffmpeg'), '', 'failed')
    monkeypatch.setattr(recorder, '_run_background_ffmpeg', run)
    monkeypatch.setattr(recorder, '_probe_media_duration_seconds', lambda path: 600)
    if failure == 'interrupted':
        with pytest.raises(module.ProcessingInterrupted):
            recorder.stitch_processed_recordings(str(tmp_path))
    else:
        recorder.stitch_processed_recordings(str(tmp_path))
    assert output.read_bytes() == b'previous'
    assert not list(output.parent.glob('.*.tmp.mp4'))
    assert all(p['path'].exists() for p in parts)


def test_reconnect_window_delays_join(monkeypatch, tmp_path):
    recorder, parts = candidates(tmp_path, monkeypatch)
    parts[-1]['end'] = time.time()
    recorder._run_background_ffmpeg = lambda *args: pytest.fail('joined too early')
    recorder.stitch_processed_recordings(str(tmp_path))
    assert not (tmp_path / 'stitched').exists()


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_real_concat_handles_quotes_and_preserves_duration(tmp_path):
    recorder = make_recorder()
    recorder.ffmpeg_path = shutil.which('ffmpeg')
    recorder.stitch_max_gap_seconds = 900
    sample = tmp_path / 'sample.mp4'
    subprocess.run([recorder.ffmpeg_path, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=64x64:rate=10', '-t', '1', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(sample)], check=True)
    for i in range(2):
        path = tmp_path / f"alice - 2026-01-01 00h00m0{i}s - night's music.mp4"
        shutil.copyfile(sample, path)
    sample.unlink()
    recorder.stitch_processed_recordings(str(tmp_path))
    outputs = list((tmp_path / 'stitched').glob('*.mp4'))
    assert len(outputs) == 1
    assert recorder._probe_media_duration_seconds(str(outputs[0])) == pytest.approx(2, abs=.2)
    assert len(list(tmp_path.glob('*.mp4'))) == 2
    assert len(json.loads(Path(str(outputs[0]) + '.parts.json').read_text())) == 2


@pytest.mark.parametrize('ended_at', [None, 1300])
def test_session_metadata_overrides_filename_times(monkeypatch, tmp_path, ended_at):
    recorder = make_recorder()
    recorder.ffmpeg_path = 'ffmpeg'
    path = tmp_path / 'alice - 2026-01-01 00h00m00s - same.mp4'
    path.write_bytes(b'media')
    Path(str(path) + '.session.json').write_text(json.dumps(
        dict(stream_id='broadcast', started_at=1000, ended_at=ended_at)))
    probe = dict(format=dict(duration='20'), streams=[dict(codec_type='video', codec_name='h264')])
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps(probe), ''))
    part = recorder._stitch_part(path)
    if ended_at is None:
        assert part is None
    else:
        assert (part['start'], part['end'], part['stream_id']) == (1000, 1300, 'broadcast')
        assert part['duration'] == 20


def test_processing_preserves_session_metadata(tmp_path):
    recorder = make_recorder()
    source = tmp_path / 'source.mp4'
    destination = tmp_path / 'destination.mp4'
    metadata = Path(str(source) + '.session.json')
    metadata.write_text('{"stream_id":"broadcast","started_at":1000,"ended_at":1300}')
    recorder._transfer_recording_metadata(str(source), str(destination))
    assert not metadata.exists()
    assert json.loads(Path(str(destination) + '.session.json').read_text())['stream_id'] == 'broadcast'
