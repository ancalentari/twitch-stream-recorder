import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recorder_state as state
import recorder_dashboard as dash
from test_recorder_threading import make_recorder,recorder_module
from test_dashboard import collector


def row(name,start,end,stream=None,stage='Processed',title='same'):
    return dict(path='/recordings/'+stage+'/'+name,name=name,title=title,channel='pyka',stage=stage,
        size=10,modified=start,started_at=start,ended_at=end,stream_id=stream,session_open=False,
        media={},parts=[],compressed=True,upload_pending=False,attempts=0)


def test_broadcasts_use_manifest_ids_and_measured_gaps():
    parts=[row('a.mp4',100,200),row('b.mp4',210,300),row('c.mp4',2000,2100),
           row('d.mp4',2200,2300,stream='different')]
    groups=state.broadcast_groups(parts,gap=30)
    assert sorted(g['part_count'] for g in groups)==[1,1,2]
    # Explicit manifest links retained originals even without duration metadata.
    joined=row('joined.mp4',100,None,stage='Stitched');joined['parts']=['a.mp4','b.mp4']
    parts[0]['ended_at']=None;parts[1]['ended_at']=None
    groups=state.broadcast_groups(parts+[joined],gap=30)
    stitched=next(g for g in groups if g['state']=='Stitched')
    assert stitched['part_count']==2 and len(stitched['files'])==3


def test_identical_titles_with_different_broadcast_ids_stay_separate():
    groups=state.broadcast_groups([row('a',100,200,'one'),row('b',205,300,'two')])
    assert len(groups)==2
    groups=state.broadcast_groups([row('a',100,200,'one'),row('b',10000,10100,'one',title='new title')])
    assert len(groups)==1


def test_queue_running_reconnect_window_and_completed_stitch():
    parts=[row('a',100,200),row('b',205,300)]
    for part in parts:part['compressed']=False
    groups=state.broadcast_groups(parts)
    queue=state.processing_queue(parts,groups,{},jobs=[dict(inputs=[parts[0]['path'],parts[1]['path']])],now=400)
    stitch=next(q for q in queue if q['kind']=='Stitch')
    assert stitch['state']=='Running' and stitch['required_bytes']==20
    queue=state.processing_queue(parts,groups,{},now=400)
    assert 'reconnect window' in next(q for q in queue if q['kind']=='Stitch')['reason']
    joined=row('joined',100,300,stage='Stitched');joined['parts']=['a','b']
    assert not any(q['kind']=='Stitch' for q in state.processing_queue(parts+[joined],state.broadcast_groups(parts+[joined]),{},now=1400))
    assert not state.processing_queue(parts,groups,dict(disable_ffmpeg=True),now=400)


def test_history_survives_restart_and_media_cache_invalidates(tmp_path):
    journal=state.Journal(tmp_path)
    path=tmp_path/'video.mp4';path.write_bytes(b'video')
    journal.event('warning','Bearer private-token client_secret=abc',path)
    journal.cache(path,dict(duration=120,width=720))
    journal.cache(path,dict(height=480))
    second=state.Journal(tmp_path)
    assert second.media(path)==dict(duration=120,width=720,height=480)
    assert 'private-token' not in second.recent()[0]['message']
    path.write_bytes(b'changed video')
    assert second.media(path) is None


def telemetry(tmp_path,monkeypatch):
    recorder=make_recorder();recorder.root_path=str(tmp_path/'recording');recorder.usernames=['pyka']
    recorder.ffmpeg_path='ffmpeg'
    monkeypatch.setattr(state,'status_directory',lambda base:tmp_path/'runtime')
    instance=state.RecorderTelemetry(recorder,tmp_path,{'pyka':(str(tmp_path/'recording/recorded/pyka'),str(tmp_path/'recording/processed/pyka'))})
    # This fixture tests publisher methods without starting a daemon.
    import logging
    logging.getLogger().removeHandler(instance.handler)
    return instance


def test_retry_preserves_metadata_and_rejects_collisions_or_escape(tmp_path,monkeypatch):
    t=telemetry(tmp_path,monkeypatch)
    failed=t.root/'failed/pyka/a.mp4';failed.parent.mkdir(parents=True);failed.write_bytes(b'original')
    Path(str(failed)+'.session.json').write_text(json.dumps(dict(stream_id='one',started_at=100,ended_at=200)))
    target=t.root/'recorded/pyka/a.mp4';target.parent.mkdir(parents=True);target.write_bytes(b'existing')
    with pytest.raises(FileExistsError):t.retry(failed)
    assert failed.read_bytes()==b'original' and target.read_bytes()==b'existing'
    target.unlink();t.retry(failed)
    assert not failed.exists() and target.read_bytes()==b'original'
    assert state.read_json(str(target)+'.session.json')['stream_id']=='one'
    outside=tmp_path/'outside.mp4';outside.write_bytes(b'safe')
    with pytest.raises(ValueError):t.retry(outside)
    alias=failed.parent/'alias.mp4';alias.symlink_to(outside)
    with pytest.raises(ValueError):t.retry(alias)
    assert outside.read_bytes()==b'safe'


def test_retry_request_waits_for_idle_and_is_durable(tmp_path,monkeypatch):
    t=telemetry(tmp_path,monkeypatch)
    failed=t.root/'failed/pyka/a.mp4';failed.parent.mkdir(parents=True);failed.write_bytes(b'original')
    request=state.request_retry(tmp_path,failed)
    t.recorder._start_idle_processing_thread=Mock()
    t.recorder._active_recordings=1
    t.commands()
    assert failed.exists() and (tmp_path/'.dashboard/commands'/ (request+'.json')).exists()
    t.recorder._active_recordings=0;t.commands()
    assert not failed.exists()
    t.recorder._start_idle_processing_thread.assert_called_once()


def test_publisher_writes_fresh_heartbeat_and_safe_config(tmp_path,monkeypatch):
    t=telemetry(tmp_path,monkeypatch)
    t.note_channel('pyka','OFFLINE');t.publish()
    status=state.read_json(t.runtime/'status.json')
    assert status['pid']==os.getpid() and abs(status['heartbeat_at']-time.time())<3
    assert status['channels']['pyka']['state']=='OFFLINE'
    assert 'client_secret' not in json.dumps(status)
    assert status['catalog_version']==1


def test_progress_pipe_drains_and_handles_na_speed(tmp_path,monkeypatch):
    t=telemetry(tmp_path,monkeypatch)
    source=tmp_path/'video.mp4';source.write_bytes(b'video')
    t.journal.cache(source,dict(duration=120))
    capture=t.progress(['ffmpeg','-i',str(source),'-crf','28','out.mp4'])
    script="import sys;sys.stdout.write('out_time=00:01:00.000000\\nspeed=N/A\\nprogress=continue\\n');sys.stdout.flush()"
    process=subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.PIPE)
    capture.attach(process);process.wait();capture.thread.join(timeout=2)
    assert t.job['percent']==50 and t.job['media_seconds']==60 and t.job['speed'] is None
    capture.finish(0)
    assert t.job is None and t.journal.recent()[0]['kind']=='ffmpeg-finished'


def test_recorder_ffmpeg_progress_integration(tmp_path,monkeypatch):
    t=telemetry(tmp_path,monkeypatch);t.recorder._telemetry=t
    source=tmp_path/'source.mp4';source.write_bytes(b'video')
    # Synthetic executable follows FFmpeg's -progress contract and tests real Popen cleanup.
    program=tmp_path/'ffmpeg-test'
    program.write_text('#!'+sys.executable+"\nimport sys\nsys.stdout.write('out_time=00:00:05.000000\\nspeed=2x\\nprogress=end\\n')\n")
    program.chmod(0o755)
    result=t.recorder._run_background_ffmpeg([str(program),'-i',str(source),str(tmp_path/'out.mp4')],10)
    assert result.returncode==0 and not t.recorder._processing_active.is_set() and t.job is None
    assert any(event['kind']=='ffmpeg-finished' for event in t.journal.recent())


def test_live_status_is_preferred_but_stale_or_wrong_pid_falls_back(tmp_path,monkeypatch):
    c=collector(tmp_path,monkeypatch)
    runtime=tmp_path/'runtime';runtime.mkdir()
    monkeypatch.setattr(dash,'status_directory',lambda base:runtime)
    monkeypatch.setattr(c,'processes',lambda now:([dict(pid=123,started=0,memory=10)],[]))
    monkeypatch.setattr(c,'service_status',lambda now:None)
    monkeypatch.setattr(dash.psutil,'virtual_memory',lambda:SimpleNamespace(percent=10,available=100,total=200))
    monkeypatch.setattr(dash.psutil,'disk_usage',lambda path:SimpleNamespace(percent=10,free=10**10,total=2*10**10))
    monkeypatch.setattr(dash.psutil,'boot_time',lambda:0)
    data=dict(pid=123,heartbeat_at=time.time(),notifications=dict(connected=True,last_message_at=time.time()),
        catalog=[row('a',100,200)],catalog_at=time.time(),queue=[],history=[],channels={})
    state.atomic_json(runtime/'status.json',data)
    snapshot=c.snapshot()
    assert snapshot['status_live'] and snapshot['eventsub']=='Connected' and len(snapshot['catalog'])==1
    data['heartbeat_at']=time.time()-60;state.atomic_json(runtime/'status.json',data)
    snapshot=c.snapshot(force=True)
    assert not snapshot['status_live'] and any('heartbeat is stale' in alert for alert in snapshot['alerts'])
    data['pid']=999;data['heartbeat_at']=time.time();state.atomic_json(runtime/'status.json',data)
    assert not c.snapshot()['status_live']


def test_diagnostic_report_does_not_export_config_credentials(tmp_path):
    path=dash.export_report(tmp_path,dict(logs=['Bearer secret access_token=private'],history=[]))
    text=path.read_text()
    assert 'Bearer secret' not in text and 'access_token=private' not in text
    assert path.stat().st_mode&0o777==0o600


def test_queue_marks_incompatible_or_distant_parts_blocked():
    parts=[row('a',100,200,'one'),row('b',5000,5100,'one')]
    queue=state.processing_queue(parts,state.broadcast_groups(parts),{},now=20000)
    assert next(q for q in queue if q['kind']=='Stitch')['state']=='Blocked'
    parts[1]['started_at']=210;parts[1]['ended_at']=300
    for index,part in enumerate(parts):part['media']=dict(stitch_probe=dict(streams=[dict(codec_name=str(index))]))
    queue=state.processing_queue(parts,state.broadcast_groups(parts),{},now=20000)
    stitch=next(q for q in queue if q['kind']=='Stitch')
    assert stitch['state']=='Blocked' and 'Incompatible' in stitch['reason']


def test_relative_media_paths_use_the_same_cache_and_queue_identity(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    journal=state.Journal(tmp_path)
    path=Path('source.mp4');path.write_bytes(b'video')
    journal.cache(path,dict(duration=10))
    assert journal.media(path.resolve())['duration']==10
    t=telemetry(tmp_path,monkeypatch);t.journal=journal
    capture=t.progress(['ffmpeg','-i','source.mp4','out.mp4'])
    assert capture.inputs==[str(path.resolve())] and capture.job['expected_seconds']==10
    capture.finish(None)


def test_history_ring_keeps_only_the_latest_events(tmp_path):
    journal=state.Journal(tmp_path)
    with journal.connection() as db:
        db.executemany('INSERT INTO events(at,kind,message) VALUES(?,?,?)',[(1,'old',str(i)) for i in range(1001)])
    journal.event('new','latest')
    with journal.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1000
    assert journal.recent()[0]['message']=='latest'


def test_broadcast_view_sorts_by_stream_start_and_keeps_filtered_session_intact(tmp_path,monkeypatch):
    c=collector(tmp_path,monkeypatch);ui=dash.Dashboard(c,2);ui.broadcasts=True
    old=row('older.mp4',100,200);old['modified']=5000
    newer=row('newer.mp4',1000,1200,title='newer broadcast')
    joined=row('joined.mp4',100,200,stage='Stitched');joined['parts']=['older.mp4']
    catalog=[old,newer,joined]
    ui.data=dict(catalog=catalog,broadcasts=state.broadcast_groups(catalog))
    assert ui.rows()[0]['title']==newer['title'] and ui.rows()[0]['modified']==1000
    ui.stage='Stitched'
    assert len(ui.rows())==1 and len(ui.rows()[0]['group']['files'])==2


def test_retry_shortcut_requires_live_heartbeat_and_failed_file(tmp_path,monkeypatch):
    c=collector(tmp_path,monkeypatch);ui=dash.Dashboard(c,2);ui.tab=1
    failed=row('bad.mp4',100,200,stage='Failed')
    ui.data=dict(catalog=[failed],status_live=False)
    ui.key(ord('R'))
    assert 'heartbeat' in ui.problem and not (tmp_path/'.dashboard/commands').exists()
    ui.data['status_live']=True
    ui.key(ord('R'))
    assert len(list((tmp_path/'.dashboard/commands').glob('*.json')))==1
