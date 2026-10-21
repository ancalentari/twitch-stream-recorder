import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('mode', ['failure', 'invalid', 'success'])
def test_batch_checks_ffmpeg_status_and_only_publishes_valid_output(tmp_path, mode):
    root = tmp_path / 'recordings'
    recorded = root / 'recorded' / 'alice'
    processed = root / 'processed' / 'alice'
    recorded.mkdir(parents=True)
    processed.mkdir(parents=True)
    (recorded / 'stream.mp4').write_bytes(b'source')
    destination = processed / 'stream.mp4'
    destination.write_bytes(b'previous output')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    docker = bin_dir / 'docker'
    docker.write_text(f'#!{sys.executable}\n' + '''import os
import sys
from pathlib import Path

mode = os.environ['TEST_FFMPEG_MODE']
filename = Path(os.environ['TWITCH_RECORDER_RECORDING']) / sys.argv[-1][len('/recordings/'):]
if 'ffprobe' in sys.argv:
    if filename.read_bytes().startswith(b'valid'):
        print('12.000000')
        sys.exit(0)
    sys.exit(1)
filename.write_bytes((b'invalid' if mode == 'invalid' else b'valid') * 300000)
if mode == 'failure':
    print('Error: simulated FFmpeg failure', file=sys.stderr)
    sys.exit(1)
# Successful encoding can produce no lines matching the progress grep.
sys.exit(0)
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
               TWITCH_RECORDER_BASE=str(tmp_path), TWITCH_RECORDER_RECORDING=str(root),
               TEST_FFMPEG_MODE=mode)
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'process-recordings.sh'
    result = subprocess.run(['bash', str(script)], env=env, text=True,
                            capture_output=True, timeout=10)
    if mode == 'success':
        assert result.returncode == 0, result.stdout + result.stderr
        assert destination.read_bytes().startswith(b'valid')
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert destination.read_bytes() == b'previous output'
    assert (recorded / 'stream.mp4').read_bytes() == b'source'
    assert not list(processed.glob('.processing.*'))
