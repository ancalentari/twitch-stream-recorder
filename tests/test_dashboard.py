import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recorder_dashboard as dash


def collector(tmp_path, monkeypatch):
    config=tmp_path/'config.json'
    config.write_text(json.dumps(dict(root_path='./recording',usernames=['pyka'],refresh_interval=400,eventsub_enabled=True)))
    monkeypatch.setattr(dash.psutil,'cpu_percent',lambda **kwargs: 10)
    return dash.Collector(tmp_path)


def test_catalog_includes_all_stages_and_ignores_incomplete_outputs(tmp_path,monkeypatch):
    c=collector(tmp_path,monkeypatch)
    files=[('recorded/pyka','pyka - 2026-01-01 00h00m00s - same.mp4'),
           ('processed/pyka','pyka - 2026-01-01 00h00m00s - same.mp4'),
           ('processed/pyka/stitched','pyka - 2026-01-01 00h00m00s - same - stitched.mp4'),
           ('failed/pyka','broken.mp4'),('processed/pyka/stitched','.twitch-recorder-test.tmp.mp4')]
    for folder,name in files:
        directory=c.root/folder;directory.mkdir(parents=True,exist_ok=True)
        (directory/name).write_bytes(b'video')
    c.inventory(100,force=True)
    assert len(c.catalog)==4
    assert {row['stage'] for row in c.catalog}=={'Raw','Processed','Stitched','Failed'}
    joined=next(row for row in c.catalog if row['stage']=='Stitched')
    assert joined['title']=='same' and joined['channel']=='pyka'
    (c.root/'failed/pyka/new.mp4').write_bytes(b'new')
    c.inventory(105)
    assert len(c.catalog)==4
    c.inventory(131)
    assert len(c.catalog)==5


def test_log_tail_incremental_rotation_and_truncation(tmp_path):
    path=tmp_path/'current.log'
    path.write_text('2026-01-01 00:00:00,000 - INFO - EventSub connected\npartial')
    tail=dash.LogTail();tail.read(path)
    assert tail.eventsub[0]=='Connected'
    with path.open('a') as output:
        output.write(' line\nEventSub unavailable (connection lost)\n')
    tail.read(path)
    assert tail.lines[-2]=='partial line'
    assert tail.eventsub[0]=='Unavailable'
    tail.read(path)
    assert len(tail.lines)==3
    path.write_text('replacement\n')
    tail.read(path)
    assert list(tail.lines)==['replacement'] and tail.eventsub is None


def test_terminal_control_sequences_are_stripped():
    assert dash.clean('\x1b[31mred\x1b[0m\r\x07')=='red'


def test_explicit_timezone_does_not_change_log_timezone():
    import os
    before=os.environ.get('TZ')
    assert dash.display_time(0,'America/New_York')=='1969-12-31 19:00:00'
    assert os.environ.get('TZ')==before


def test_job_arguments_keep_paths_with_spaces_and_classify_stitching(tmp_path,monkeypatch):
    c=collector(tmp_path,monkeypatch)
    output=tmp_path/'output with spaces.mp4';output.write_bytes(b'out')
    concat=tmp_path/'parts.txt'
    alias=tmp_path/'part.mp4';alias.symlink_to(output)
    concat.write_text("file '%s'\nduration 20\n"%alias)
    class Process:
        def __init__(self,pid,args,ppid):
            self.info=dict(pid=pid,ppid=ppid,name='python',cmdline=args,create_time=10)
        def cwd(self):return str(tmp_path)
        def cpu_percent(self):return 1
        def status(self):return dash.psutil.STATUS_RUNNING
        def memory_info(self):return SimpleNamespace(rss=100)
    processes=[Process(1,['python3',str(tmp_path/'twitch-recorder.py')],0),
               Process(2,['ffmpeg','-f','concat','-i',str(concat),'-c','copy','-y',str(output)],1),
               Process(3,['python3','/other/twitch-recorder.py'],0)]
    monkeypatch.setattr(dash.psutil,'process_iter',lambda attrs:processes)
    records,jobs=c.processes(20)
    assert len(records)==1 and records[0]['pid']==1
    assert jobs[0]['kind']=='Stitching'
    assert jobs[0]['output']==str(output) and jobs[0]['inputs']==[str(output)]
    output.write_bytes(b'output growth')
    _,jobs=c.processes(22)
    assert jobs[0]['rate']==5


def test_filters_sorting_and_keyboard_navigation(tmp_path,monkeypatch):
    c=collector(tmp_path,monkeypatch)
    ui=dash.Dashboard(c,2)
    ui.data=dict(catalog=[dict(path='a',name='birthday.mp4',channel='pyka',stage='Raw',size=10,modified=20),
                          dict(path='b',name='house.mp4',channel='other',stage='Stitched',size=100,modified=10)])
    assert ui.rows()[0]['path']=='a'
    ui.key(ord('s'));assert ui.rows()[0]['path']=='b'
    ui.channel='pyka';assert len(ui.rows())==1
    ui.key(ord('/'))
    for char in 'birthday':ui.key(ord(char))
    ui.key(10);assert ui.query=='birthday' and len(ui.rows())==1
    ui.key(10);assert ui.detail
    ui.key(27);assert not ui.detail and ui.query==''
    assert not ui.key(ord('q'))


def test_render_handles_small_terminal_and_missing_files(tmp_path,monkeypatch):
    c=collector(tmp_path,monkeypatch)
    ui=dash.Dashboard(c,2)
    screen=Mock();screen.getmaxyx.return_value=(8,40)
    ui.render(screen)
    screen.refresh.assert_called_once()


def test_long_file_paths_wrap_instead_of_hiding_the_filename(tmp_path,monkeypatch):
    ui=dash.Dashboard(collector(tmp_path,monkeypatch),2)
    screen=Mock();screen.getmaxyx.return_value=(24,60)
    ui.draw_lines(screen,['/recordings/'+('directory/'*15)+'final.mp4'],1,20)
    rendered=''.join(call.args[2] for call in screen.addnstr.call_args_list)
    assert rendered.endswith('final.mp4')
    assert len(screen.addnstr.call_args_list)>1


@pytest.mark.parametrize('tab',range(6))
def test_all_views_render_at_standard_and_small_sizes(tmp_path,monkeypatch,tab):
    monkeypatch.setattr(dash.curses,'color_pair',lambda number: 0)
    c=collector(tmp_path,monkeypatch)
    ui=dash.Dashboard(c,2)
    ui.data=dict(hostname='pi',now=0,timezone='America/New_York',recorder=[],eventsub='Unavailable',jobs=[],
        disk_free=100,disk_total=200,cpu=10,memory_percent=20,memory_available=100,memory_total=200,
        disk_percent=50,uptime=100,counts={},catalog=[],catalog_at=0,logs=['a log line'],alerts=['Recorder stopped'],
        poll_interval=400,service={},temperature=None,load=[0,0,0],root='/recording',log_path=None,eventsub_reported=None)
    ui.tab=tab
    screen=Mock()
    for size in ((24,100),(12,60),(35,120)):
        screen.getmaxyx.return_value=size
        ui.render(screen)
