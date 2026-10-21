"""Recorder telemetry, catalog and history shared by the service and TUI (Python 3.7+)."""
import contextlib
import datetime
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

NAME = re.compile(r'^(.+?) - (\d{4}-\d{2}-\d{2} \d{2}h\d{2}m\d{2}s) - (.+)\.mp4$')


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as target:
            json.dump(value, target)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def status_directory(base):
    base = Path(base).resolve()
    if os.name == 'posix' and Path('/dev/shm').is_dir():
        key = hashlib.sha256(str(base).encode()).hexdigest()[:12]
        return Path('/dev/shm') / ('twitch-recorder-%s-%s' % (os.getuid(), key))
    return base / '.dashboard' / 'runtime'


def fingerprint(path):
    stat = Path(path).stat()
    return '%s:%s:%s' % (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def redact(message):
    message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(message))
    message = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', message)
    message = re.sub(r'(?i)((?:client_secret|access_token|refresh_token|device_code)\s*[=:]\s*)[^\s&,]+', r'\1[redacted]', message)
    return message[:2000]


class Journal:
    def __init__(self, base):
        self.path = Path(base) / '.dashboard' / 'history.sqlite'
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL, kind TEXT, message TEXT, path TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS media (path TEXT PRIMARY KEY, fingerprint TEXT, data TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS events_at ON events(at)')
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, kind, message, path=None):
        with self.connection() as db:
            db.execute('INSERT INTO events(at,kind,message,path) VALUES(?,?,?,?)',
                       (time.time(), kind, redact(message), str(Path(path).resolve()) if path else None))
            db.execute('DELETE FROM events WHERE id <= (SELECT MAX(id)-1000 FROM events)')

    def recent(self, limit=100):
        with self.connection() as db:
            rows = db.execute('SELECT at,kind,message,path FROM events ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [dict(at=row[0], kind=row[1], message=row[2], path=row[3]) for row in rows]

    def media(self, path):
        try:
            path = str(Path(path).resolve())
            with self.connection() as db:
                row = db.execute('SELECT fingerprint,data FROM media WHERE path=?', (str(path),)).fetchone()
            return json.loads(row[1]) if row and row[0] == fingerprint(path) else None
        except (OSError,sqlite3.Error,ValueError):
            return None

    def cache(self, path, data):
        path = str(Path(path).resolve())
        if Path(path).name.startswith('.'):
            return  # Temporary outputs must be verified before becoming catalog entries.
        signature = fingerprint(path)
        with self.connection() as db:
            old = db.execute('SELECT fingerprint,data FROM media WHERE path=?', (str(path),)).fetchone()
            merged = json.loads(old[1]) if old and old[0] == signature else {}
            merged.update(data)
            if old and old[0] == signature and merged == json.loads(old[1]):
                return
            db.execute('INSERT OR REPLACE INTO media VALUES(?,?,?)', (str(path), signature, json.dumps(merged)))

    def all_media(self):
        with self.connection() as db:
            return {row[0]: (row[1], json.loads(row[2])) for row in db.execute('SELECT path,fingerprint,data FROM media')}


def scan_catalog(root, media=None):
    rows, errors = [], []
    media = media or {}
    for folder, stage in (('recorded', 'Raw'), ('processed', 'Processed'), ('failed', 'Failed')):
        directory = Path(root) / folder
        def scan_error(exc):
            errors.append('Cannot scan %s' % exc.filename)
        if not directory.exists():
            continue
        for parent, dirs, files in os.walk(str(directory), onerror=scan_error):
            dirs[:] = [name for name in dirs if not name.startswith('.')]
            for name in files:
                if name.startswith('.') or not name.endswith('.mp4'):
                    continue
                path = Path(parent) / name
                try:
                    stat = path.stat()
                    relative = path.relative_to(directory)
                    channel = relative.parts[0] if len(relative.parts) > 1 else 'unknown'
                    kind = 'Stitched' if 'stitched' in relative.parts[:-1] else stage
                    match = NAME.match(name)
                    title = match[3] if match else path.stem
                    if title.endswith(' - stitched'):
                        title = title[:-11]
                    session = read_json(str(path)+'.session.json', {})
                    session = session if isinstance(session, dict) else {}
                    start = session.get('started_at')
                    if start is None and match:
                        start = datetime.datetime.strptime(match[2], '%Y-%m-%d %Hh%Mm%Ss').timestamp()
                    cached = media.get(str(path))
                    probe = cached[1] if cached and cached[0] == fingerprint(path) else {}
                    attempts = 0
                    try:
                        attempts = int(Path(str(path)+'.attempts').read_text())
                    except (OSError, ValueError):
                        pass
                    failure = read_json(str(path)+'.failure.json', {})
                    if isinstance(failure,dict) and kind=='Failed':
                        attempts = failure.get('attempts',attempts)
                    parts = read_json(str(path)+'.parts.json', [])
                    rows.append(dict(path=str(path), name=name, channel=channel, stage=kind,
                        size=stat.st_size, modified=stat.st_mtime, title=title,
                        compressed=Path(str(path)+'.compressed').exists(),
                        upload_pending=Path(str(path)+'.upload-pending').exists(),
                        attempts=attempts, stream_id=session.get('stream_id'), started_at=start,
                        ended_at=session.get('ended_at') or (start+probe['duration'] if start is not None and probe.get('duration') else None),
                        session_open=bool(session) and not session.get('ended_at'),
                        media=probe, failure=failure, parts=parts if isinstance(parts, list) else []))
                except (OSError, ValueError, TypeError):
                    continue
    return sorted(rows, key=lambda row: row['modified'], reverse=True), errors


def broadcast_groups(catalog, gap=900):
    """Group by explicit manifests/stream IDs or conservative title + measured timing."""
    parents = list(range(len(catalog)))
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    def join(left, right):
        parents[find(right)] = find(left)
    names = {}
    streams = {}
    for index, row in enumerate(catalog):
        key = (row['channel'], row['name'])
        if key in names:
            join(index, names[key])  # Raw/processed versions of the same part.
        names[key] = index
        if row.get('stream_id'):
            key = (row['channel'], row['stream_id'])
            if key in streams:
                join(index, streams[key])
            streams[key] = index
    for index, row in enumerate(catalog):
        for name in row.get('parts', []):
            other = names.get((row['channel'], name))
            if other is not None:
                join(index, other)
    parts = sorted((i for i, row in enumerate(catalog) if row['stage'] != 'Stitched' and row.get('started_at') is not None),
                   key=lambda i: catalog[i]['started_at'])
    previous_by_channel = {}
    for index in parts:
        row = catalog[index]
        previous = previous_by_channel.get(row['channel'])
        if previous is not None:
            before = catalog[previous]
            end = before.get('ended_at')
            same = row['title'] == before['title'] and not row.get('session_open') and not before.get('session_open')
            if row.get('stream_id') and before.get('stream_id'):
                same = row['stream_id'] == before['stream_id']
            if same and end is not None and -2 <= row['started_at']-end <= gap:
                join(index, previous)
        previous_by_channel[row['channel']] = index
    grouped = {}
    for index, row in enumerate(catalog):
        grouped.setdefault(find(index), []).append(row)
    result = []
    for rows in grouped.values():
        rows.sort(key=lambda row: row.get('started_at') or row['modified'])
        first = next((row for row in rows if row['stage'] != 'Stitched'), rows[0])
        parts = {row['name'] for row in rows if row['stage'] != 'Stitched'}
        joined = any(row['stage'] == 'Stitched' for row in rows)
        raw = any(row['stage'] == 'Raw' for row in rows)
        failed = any(row['stage'] == 'Failed' for row in rows)
        state = 'Failed' if failed else 'Open session' if any(row.get('session_open') for row in rows) else (
            'Awaiting processing' if raw else 'Stitched' if joined else 'Parts ready' if len(parts)>1 else 'Processed')
        result.append(dict(id=first.get('stream_id') or first['path'], channel=first['channel'], title=first['title'],
            started_at=first.get('started_at') or first['modified'], modified=max(row['modified'] for row in rows),
            size=sum(row['size'] for row in rows), part_count=len(parts) or len(rows), state=state, files=rows))
    return sorted(result, key=lambda group: group['started_at'], reverse=True)


def processing_queue(catalog, groups, config, busy=False, jobs=None, now=None):
    now = now or time.time()
    active = {path for job in jobs or [] for path in job.get('inputs', [])}
    rows = []
    def append(row, kind, reason, required=0):
        paths = row.get('inputs', [row.get('path')])
        paused = next((job for job in jobs or [] if job.get('paused') and any(path in job.get('inputs', []) for path in paths)), None)
        state = 'Paused' if paused else 'Running' if any(path in active for path in paths) else 'Blocked' if row.get('blocked') else 'Waiting'
        rows.append(dict(kind=kind, channel=row['channel'], title=row['title'], path=paths[0], inputs=paths,
                         state=state, reason=paused.get('phase', 'Paused for recording')+'; progress retained' if paused else 'Active job' if state=='Running' else reason,
                         attempts=row.get('attempts',0), required_bytes=required))
    for row in catalog:
        reason = 'Recording has priority' if busy else 'Next idle processing cycle'
        if row['stage'] == 'Failed':
            append(row, 'Retry needed', 'Quarantined; select file and press R to request a retry')
        elif row['stage'] == 'Raw':
            if row.get('session_open'):
                reason = 'Still recording or session did not close cleanly'
            append(row, 'Move' if config.get('disable_ffmpeg') else 'Remux', reason, row['size'])
        elif row.get('upload_pending') and config.get('upload_to_network_drive'):
            append(row, 'Upload', reason)
        elif row['stage']=='Processed' and not row.get('compressed') and config.get('idle_compress_enabled',True) and not config.get('disable_ffmpeg'):
            if row.get('ended_at') and now-row['ended_at'] < config.get('stitch_max_gap_seconds',900):
                reason = 'Waiting for broadcast reconnect window to close'
            append(row, 'Compress', reason, row['size'])
    if config.get('stitch_recordings_enabled',True) and not config.get('disable_ffmpeg'):
        for group in groups:
            parts = [row for row in group['files'] if row['stage']=='Processed']
            if len(parts) < 2:
                continue
            complete = any(row['stage']=='Stitched' and set(row.get('parts',[])) == {part['name'] for part in parts} for row in group['files'])
            if complete:
                continue
            reason = 'Recording has priority' if busy else 'Next idle stitching pass; codec compatibility will be checked'
            blocked = False
            if any(row['stage']=='Raw' for row in group['files']):
                reason = 'Waiting for raw parts to finish processing'
            elif any(not part.get('ended_at') for part in parts):
                reason = 'Waiting for media metadata / closed session'
            elif now-max(part['ended_at'] for part in parts) < config.get('stitch_max_gap_seconds',900):
                reason = 'Waiting for broadcast reconnect window to close'
            else:
                ordered=sorted(parts,key=lambda part:part.get('started_at') or 0)
                if any(part['started_at']-before['ended_at'] > config.get('stitch_max_gap_seconds',900) or
                       part['started_at']-before['ended_at'] < -2 for before,part in zip(ordered,ordered[1:])):
                    reason='Parts exceed the configured stitch gap or overlap';blocked=True
                profiles=[part.get('media',{}).get('stitch_probe',{}).get('streams') for part in parts]
                if all(profiles) and any(profile!=profiles[0] for profile in profiles):
                    reason='Incompatible media formats; automatic stitching is blocked';blocked=True
            append(dict(channel=group['channel'],title=group['title'],blocked=blocked,inputs=[part['path'] for part in parts]),
                   'Stitch',reason,sum(part['size'] for part in parts))
    return sorted(rows,key=lambda row: ({'Running':0,'Paused':0}.get(row['state'],1), {'Upload':0,'Remux':1,'Move':1,'Stitch':2,'Compress':3}.get(row['kind'],4)))


class ProgressCapture:
    def __init__(self, telemetry, command):
        self.telemetry = telemetry
        self.output = subprocess.PIPE
        self.reader = None
        self.thread = None
        self.command = [command[0], '-progress', 'pipe:1']+list(command[1:])
        self.partial = b''
        self.values = {}
        self.lock = threading.RLock()
        self.paused = False
        self.pause_reason = 'recording'
        self.pause_started = None
        self.paused_seconds = 0
        self.started_monotonic = time.monotonic()
        self.last_advance_at = self.started_monotonic
        self.last_frame = 0
        self.last_bytes = 0
        args = list(command)
        source = str(Path(args[args.index('-i')+1]).resolve()) if '-i' in args else None
        self.kind = 'Stitching' if 'concat' in args else 'Compressing' if '-crf' in args else 'Remuxing'
        self.inputs = [source] if source else []
        duration = None
        if self.kind == 'Stitching':
            self.inputs = []
            duration = 0
            for line in Path(source).read_text().splitlines():
                if line.startswith("file '"):
                    self.inputs.append(str(Path(line[6:-1]).resolve()))
                elif line.startswith('duration '):
                    duration += float(line[9:])
        elif source:
            data = (telemetry.journal.media(source) or {}) if telemetry else {}
            duration = data.get('duration')
        self.job = dict(kind=self.kind, inputs=self.inputs, source=source, output=str(Path(args[-1]).resolve()),
                        expected_seconds=duration, media_seconds=0, percent=None, speed=None, pid=None,
                        started_at=time.time(), progress_at=time.time(), phase='Starting')

    def attach(self, process):
        self.job['pid'] = process.pid
        self.reader = process.stdout
        if self.telemetry:
            self.telemetry.set_job(self.job)
        self.thread=threading.Thread(target=self.drain,name='ffmpeg-progress',daemon=True)
        self.thread.start()
        if self.telemetry:
            self.telemetry.event('processing-started',self.kind+': '+', '.join(self.inputs),self.inputs[0] if self.inputs else None)

    def drain(self):
        try:
            for raw in iter(self.reader.readline,b''):
                self.accept(raw)
        except (OSError,ValueError):
            pass

    def accept(self, raw):
        with self.lock:
            self._accept(raw)

    def _accept(self, raw):
        key, separator, value = raw.decode(errors='replace').strip().partition('=')
        if separator and len(key)<40 and len(value)<200:
            self.values[key] = value
        if key!='progress':
            return
        try:
            stamp = self.values.get('out_time','0:0:0').split(':')
            seconds = max(0,float(stamp[0])*3600+float(stamp[1])*60+float(stamp[2]))
            if not math.isfinite(seconds):
                return
        except (ValueError,IndexError):
            seconds = self.job['media_seconds']
        expected = self.job['expected_seconds']
        percent = min(100,seconds/expected*100) if expected else None
        try:
            speed = float(self.values.get('speed','').rstrip('x'))
            if not math.isfinite(speed):
                speed=None
        except ValueError:
            speed=None
        changed = seconds > self.job['media_seconds']
        now = time.monotonic()
        try:
            frames = int(self.values.get('frame', '0'))
        except ValueError:
            frames = self.last_frame
        try:
            size = int(self.values.get('total_size', '0'))
        except ValueError:
            size = self.last_bytes
        advancing = changed or frames > self.last_frame or size > self.last_bytes
        if advancing:
            self.last_advance_at = now
        self.last_frame = max(self.last_frame, frames)
        self.last_bytes = max(self.last_bytes, size)
        active_seconds = now - self.started_monotonic - self.paused_seconds
        if self.pause_started is not None:
            active_seconds -= now - self.pause_started
        if self.kind == 'Compressing' and active_seconds > 0 and speed is not None:
            speed = seconds / active_seconds if seconds > 0 else None
        self.job.update(media_seconds=seconds,percent=percent,speed=speed,
                        active_seconds=max(0, active_seconds), paused=self.paused,
                        phase='Paused for '+self.pause_reason if self.paused else 'Finalizing / validating' if value=='end' else 'Processing')
        if advancing:
            self.job['progress_at']=time.time()
        if self.telemetry:
            self.telemetry.set_job(self.job)

    def set_paused(self, paused, reason='recording'):
        with self.lock:
            self.pause_reason = reason
            self._set_paused(paused)

    def _set_paused(self, paused):
        now = time.monotonic()
        self.paused = paused
        if paused and self.pause_started is None:
            self.pause_started = now
        elif not paused and self.pause_started is not None:
            self.paused_seconds += now - self.pause_started
            self.pause_started = None
            self.last_advance_at = now
            self.job['progress_at'] = time.time()
        self.job.update(paused=paused, paused_seconds=self.paused_seconds,
                        phase='Paused for '+self.pause_reason if paused else 'Finalizing / validating' if self.values.get('progress')=='end' else 'Processing')
        if self.telemetry:
            self.telemetry.set_job(self.job)

    def sample(self):
        pass  # A dedicated reader drains the pipe continuously using bounded memory.

    def finish(self, returncode, interrupted=False):
        try:
            if self.thread:
                self.thread.join(timeout=2)
            kind = 'processing-yielded' if interrupted else 'ffmpeg-finished' if returncode==0 else 'processing-failed'
            if self.telemetry:
                self.telemetry.event(kind,'%s: FFmpeg exit %s%s' % (self.kind,returncode,'; yielded to recording/shutdown' if interrupted else ''),
                                     self.inputs[0] if self.inputs else None)
        finally:
            if self.telemetry:
                self.telemetry.set_job(None)
            if self.reader:
                self.reader.close()
            if self.telemetry:
                self.telemetry.invalidate()


class HistoryHandler(logging.Handler):
    def __init__(self, telemetry):
        super().__init__(logging.WARNING)
        self.telemetry=telemetry

    def emit(self, record):
        self.telemetry.event(record.levelname.lower(),record.getMessage())


class RecorderTelemetry(threading.Thread):
    def __init__(self, recorder, base, paths):
        super().__init__(name='recorder-status',daemon=True)
        self.recorder=recorder
        self.base=Path(base).resolve()
        self.root=Path(recorder.root_path).resolve()
        self.paths=paths
        self.runtime=status_directory(self.base)
        self.runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.journal=Journal(self.base)
        self.lock=threading.Lock()
        self.stop=threading.Event()
        self.wake=threading.Event()
        self.job=None
        self.channels={name:dict(state='Unknown',checked_at=None) for name in recorder.usernames}
        self.catalog=[];self.errors=[];self.catalog_at=0;self.version=0
        self.probe_at=0;self.probe_failures={};self.last_notification_state=None
        self.config={key:getattr(recorder,key,default) for key,default in (
            ('idle_compress_enabled',True),('disable_ffmpeg',False),('upload_to_network_drive_enabled',False),
            ('stitch_recordings_enabled',True),('stitch_max_gap_seconds',900),('max_processing_attempts',3),
            ('recording_stall_timeout',300))}
        self.config['upload_to_network_drive']=self.config.pop('upload_to_network_drive_enabled')
        self.handler=HistoryHandler(self)
        logging.getLogger().addHandler(self.handler)
        self.event('recorder-started','Recorder started (PID %s)'%os.getpid())

    def event(self, kind, message, path=None):
        try:
            self.journal.event(kind,message,path)
        except (OSError,sqlite3.Error):
            pass  # Telemetry never blocks recording on storage failure.

    def invalidate(self):
        with self.lock:
            self.catalog_at=0
        self.wake.set()

    def note_channel(self, username, state):
        with self.lock:
            self.channels[username]=dict(state=state,checked_at=time.time())

    def set_job(self, job):
        with self.lock:
            self.job=dict(job) if job else None

    def cache_media(self, path, data):
        try:
            self.journal.cache(path,data)
        except (OSError,sqlite3.Error):
            pass

    def progress(self, command):
        return ProgressCapture(self,command)

    def retry(self, path):
        source=Path(path)
        root=self.root/'failed'
        if source.is_symlink() or not source.is_file() or source.suffix!='.mp4':
            raise ValueError('Retry requires an existing quarantined MP4 file')
        relative=source.resolve().relative_to(root.resolve())
        if len(relative.parts)!=2 or relative.parts[0] not in self.recorder.usernames:
            raise ValueError('Retry path must belong to a configured failed channel')
        destination=self.root/'recorded'/relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        # Link fails safely if the destination already exists; source remains intact.
        os.link(str(source),str(destination))
        try:
            metadata=Path(str(source)+'.session.json')
            if metadata.exists():
                atomic_json(str(destination)+'.session.json',read_json(metadata,{}))
            source.unlink()
        except Exception:
            destination.unlink()
            raise
        for suffix in ('.attempts','.session.json','.failure.json'):
            try:
                Path(str(source)+suffix).unlink()
            except FileNotFoundError:
                pass
        self.recorder._clear_processing_attempts(str(destination))
        self.event('retry-requested','Returned quarantined recording to the processing queue',destination)
        self.invalidate()

    def commands(self):
        if self.recorder.active_recordings or self.recorder._processing_active.is_set():
            return
        thread=getattr(self.recorder,'_idle_processing_thread',None)
        if thread and thread.is_alive():
            return
        directory=self.base/'.dashboard'/'commands'
        processed=False
        for path in sorted(directory.glob('*.json'))[:10]:
            command=read_json(path,{})
            try:
                if not isinstance(command,dict) or command.get('action')!='retry':
                    raise ValueError('Unsupported dashboard command')
                if time.time()-command.get('created_at',0)>86400:
                    raise ValueError('Retry request expired')
                self.retry(command.get('path',''))
                processed=True
            except (OSError,ValueError,TypeError) as exc:
                self.event('command-rejected',str(exc))
            path.unlink()
        if processed:
            self.recorder._start_idle_processing_thread(self.paths)

    def backfill(self, now):
        if self.recorder.active_recordings or self.recorder._processing_active.is_set() or now-self.probe_at<10:
            return
        for row in self.catalog:
            if row['stage'] in ('Raw','Failed') or row.get('media') or self.probe_failures.get(row['path'],0)>now:
                continue
            self.probe_at=now
            try:
                result=subprocess.run([str(Path(self.recorder.ffmpeg_path).with_name('ffprobe')),'-v','error',
                    '-show_entries','format=duration:stream=codec_type,codec_name,width,height','-of','json',row['path']],
                    capture_output=True,text=True,timeout=8)
                probe=json.loads(result.stdout)
                duration=float(probe['format']['duration'])
                if result.returncode or not math.isfinite(duration) or duration<=0:
                    raise ValueError('Invalid media metadata')
                video=next((s for s in probe.get('streams',[]) if s.get('codec_type')=='video'),{})
                self.cache_media(row['path'],dict(duration=duration,width=video.get('width'),height=video.get('height'),codec=video.get('codec_name')))
                self.invalidate()
            except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):
                self.probe_failures[row['path']]=now+3600
            break

    def publish(self):
        now=time.time()
        with self.lock:
            catalog_at=self.catalog_at
            job=dict(self.job) if self.job else None
            channels={name:dict(state) for name,state in self.channels.items()}
        if now-catalog_at>=30:
            self.catalog,self.errors=scan_catalog(self.root,self.journal.all_media())
            with self.lock:
                self.catalog_at=now
            self.version+=1
        listener=getattr(self.recorder,'_eventsub_listener',None)
        notifications=listener.health_snapshot() if listener and hasattr(listener,'health_snapshot') else dict(connected=False)
        marker=(notifications.get('connected'),notifications.get('failure_count',0))
        if marker!=self.last_notification_state:
            self.event('notifications','EventSub connected=%s; failures=%s'%marker)
            self.last_notification_state=marker
        with self.recorder._recording_processes_lock:
            recording={name:process.pid for name,process in self.recorder._recording_processes.items() if process and process.poll() is None}
            recording_pending=list(getattr(self.recorder, '_pending_recordings', []))
        with self.recorder._eventsub_recovery_lock:
            recovery=list(self.recorder._eventsub_recovery_users)
        groups=broadcast_groups(self.catalog,getattr(self.recorder,'stitch_max_gap_seconds',900))
        queue=processing_queue(self.catalog,groups,self.config,bool(recording),[job] if job else [],now)
        pending=[dict(read_json(path,{}),request_id=path.stem) for path in (self.base/'.dashboard'/'commands').glob('*.json') if isinstance(read_json(path,{}),dict)]
        atomic_json(self.runtime/'status.json',dict(schema=1,pid=os.getpid(),heartbeat_at=now,stopping=self.stop.is_set(),
            catalog_version=self.version,catalog_at=self.catalog_at,catalog=self.catalog,catalog_errors=self.errors,
            jobs=[job] if job else [],channels=channels,recording=recording,recording_pending=recording_pending,
            benchmark_coordination=True,notifications=notifications,
            eventsub_enabled=getattr(self.recorder,'eventsub_enabled',False),
            fallback_active=bool(getattr(self.recorder,'eventsub_enabled',False) and not notifications.get('connected')),
            recovery_users=recovery,queue=queue,config=self.config,history=self.journal.recent(),commands=pending))

    def run(self):
        try:
            while not self.stop.is_set() and not self.recorder._shutdown_event.is_set():
                try:
                    self.commands();self.publish();self.backfill(time.time())
                except Exception as exc:
                    self.event('telemetry-error',type(exc).__name__+': '+str(exc))
                self.wake.wait(5);self.wake.clear()
        finally:
            logging.getLogger().removeHandler(self.handler)
            self.event('recorder-stopped','Recorder status publisher stopped')
            try:
                self.stop.set();self.publish()
            except Exception:
                pass


def request_retry(base, path):
    directory=Path(base)/'.dashboard'/'commands'
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    request_id=uuid.uuid4().hex
    atomic_json(directory/(request_id+'.json'),dict(action='retry',path=str(path),created_at=time.time()))
    return request_id
