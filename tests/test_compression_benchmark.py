import copy
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

import benchmark_control as control
import compression_benchmark as bench
from recorder_state import ProgressCapture, atomic_json, status_directory
from test_compression_pause import encoder, start_worker, wait_until
from test_recorder_threading import make_recorder


def report_fixture():
    return dict(source='example <video>.mp4', host='Pi', state='complete',
        profiles=[dict(id=p,preset='medium' if p=='current' else 'veryfast',crf=28,audio_bitrate='128k') for p in ('current','faster')],
        media=dict(duration=1000,size=100000000),sample_seconds=3,offsets=[300,600],min_ssim=.95,min_savings=.1,
        trials=[dict(profile=p,sample=i,ok=True,bytes=100000,encode_seconds=30 if p=='current' else 10,
                     ssim=.99 if i else .96,peak_rss=1024,clip=p+'.mp4') for p in ('current','faster') for i in range(2)])


def test_recommender_requires_complete_matching_quality_and_real_savings(tmp_path):
    report=report_fixture()
    assert bench.summarize(report)[1]['id']=='faster'
    report['trials'][-1]['ssim']=.97  # Global minimum still .96, but this sample degraded.
    assert bench.summarize(report)[1]['id']=='current'
    report['trials'][0]['ok']=False
    assert bench.summarize(report)[1] is None
    report=report_fixture(); report['source_changed']=True
    assert bench.summarize(report)[1] is None
    report=report_fixture(); report['media']['size']=100
    assert bench.summarize(report)[1] is None
    bench.save_report(tmp_path,report)
    assert '&lt;video&gt;' in (tmp_path/'report.html').read_text()
    assert json.loads((tmp_path/'report.json').read_text())['recommendation'] is None


def test_offsets_are_inside_recording_and_spread_out():
    assert bench.sample_offsets(63,3,2)==[20,40]
    with pytest.raises(ValueError):bench.sample_offsets(2,3,2)


def test_lease_lock_cleanup_staleness_and_pid_reuse(tmp_path):
    with control.BenchmarkLease(tmp_path) as lease:
        assert control.active_benchmark(tmp_path)
        with pytest.raises(RuntimeError):
            with control.BenchmarkLease(tmp_path):pass
        atomic_json(lease.path,dict(lease.identity,heartbeat_at=time.time()-20))
        assert not control.active_benchmark(tmp_path)
        atomic_json(lease.path,dict(pid=lease.identity['pid'],created_at=0,heartbeat_at=time.time()))
        assert not control.active_benchmark(tmp_path)
    assert not control.active_benchmark(tmp_path)


def test_runner_waits_for_recordings_and_stale_telemetry(tmp_path):
    runner=bench.Runner(tmp_path)
    with control.BenchmarkLease(tmp_path):
        path=status_directory(tmp_path)/'status.json'
        state=dict(pid=psutil.Process().pid,heartbeat_at=time.time(),benchmark_coordination=True,recording={},recording_pending=[])
        atomic_json(path,state)
        assert runner.blocked() is None
        state['recording_pending']=['pyka'];atomic_json(path,state)
        assert runner.blocked()=='paused for recording'
        state['heartbeat_at']=time.time()-30;atomic_json(path,state)
        assert 'fresh recorder status' in runner.blocked()


def test_runner_timeout_reaps_child(tmp_path):
    runner=bench.Runner(tmp_path,standalone=True)
    marker=tmp_path/'pid'
    command=[sys.executable,'-c','import os,time,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)',str(marker)]
    with pytest.raises(RuntimeError,match='Trial time limit'):
        runner.run(command,.4)
    assert not psutil.pid_exists(int(marker.read_text()))


def test_benchmark_lease_pauses_and_resumes_same_production_encoder(monkeypatch,tmp_path):
    recorder=make_recorder(); recorder.idle_compress_stall_timeout=.75
    monkeypatch.setattr(control,'active_benchmark',lambda base: lease_active[0])
    lease_active=[False]
    worker,child,results,errors=start_worker(monkeypatch,recorder,encoder(tmp_path))
    output=tmp_path/'output.mp4'
    try:
        wait_until(lambda:output.exists() and output.read_text()!='')
        lease_active[0]=True
        wait_until(lambda:psutil.Process(child.pid).status()==psutil.STATUS_STOPPED)
        saved=output.read_text();time.sleep(1)
        assert output.read_text()==saved
        lease_active[0]=False
        wait_until(lambda:output.read_text() not in ('',saved))
        output.with_suffix('.finish').touch();worker.join(5)
        assert not worker.is_alive() and not errors and results[0].returncode==0
    finally:
        recorder._shutdown_event.set();worker.join(6)
        if child.poll() is None:child.kill();child.wait()
    capture=ProgressCapture(None,['ffmpeg','-crf','28','output.mp4'])
    capture.set_paused(True,'benchmark');capture.accept(b'progress=continue\n')
    assert capture.job['phase']=='Paused for benchmark'


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg integration')
def test_real_ffmpeg_samples_with_audio_and_reports(tmp_path):
    source=tmp_path/'source.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','8','-c:v','libx264','-preset','ultrafast',
        '-crf','10','-c:a','aac','-y',str(source)],check=True)
    original=source.read_bytes()
    result=bench.main(['--base',str(tmp_path),'--source',str(source),'--standalone','--seconds','1',
                       '--samples','2','--presets','veryfast','--budget','60'])
    assert result==0 and source.read_bytes()==original
    report=json.loads(next((tmp_path/'.dashboard/benchmarks').glob('*/report.json')).read_text())
    assert len(report['trials'])==4 and all(t['ok'] for t in report['trials'])
    assert all(0 < t['ssim'] <= 1 for t in report['trials'])
