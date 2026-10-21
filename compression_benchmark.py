"""Terminal benchmark companion. Samples and reports only; never applies settings."""
import argparse
import datetime
import html
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil
from benchmark_control import BenchmarkLease, active_benchmark
from recorder_state import atomic_json, fingerprint, read_json, status_directory

PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow')


def sample_offsets(duration, seconds, count):
    if not math.isfinite(duration) or duration <= seconds:
        raise ValueError('Recording must be longer than the sample duration')
    # Centers spread over the recording; leave both edges out where possible.
    return [round((duration - seconds) * (i + 1) / (count + 1), 3) for i in range(count)]


def probe(ffmpeg, source):
    result = subprocess.run([str(Path(ffmpeg).with_name('ffprobe')), '-v', 'error',
        '-show_entries', 'format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate',
        '-of', 'json', str(source)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise ValueError('Cannot read media metadata: ' + result.stderr.decode(errors='replace')[-500:])
    data = json.loads(result.stdout)
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if not video:
        raise ValueError('Source has no video stream')
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid media duration')
    return dict(duration=duration, size=Path(source).stat().st_size,
                width=video['width'], height=video['height'], fps=video.get('avg_frame_rate'),
                audio=next((s['codec_name'] for s in data['streams'] if s['codec_type']=='audio'), None))


class Runner:
    def __init__(self, base, standalone=False, budget=1800):
        self.base = Path(base)
        self.standalone = standalone
        self.budget = budget
        self.used = 0
        self.phase = None

    def blocked(self):
        if self.standalone:
            return None
        if not active_benchmark(self.base):
            raise RuntimeError('Benchmark lease expired; stopping to release the recorder')
        state = read_json(status_directory(self.base) / 'status.json', {})
        if time.time() - state.get('heartbeat_at', 0) > 15 or state.get('stopping'):
            return 'waiting for fresh recorder status'
        try:
            recorder = psutil.Process(state['pid'])
            if recorder.create_time() > state['heartbeat_at']:
                return 'waiting for recorder startup'
            children = recorder.children()
        except (KeyError, psutil.Error):
            return 'waiting for recorder'
        if not state.get('benchmark_coordination'):
            raise RuntimeError('Recorder needs the benchmark coordination update first')
        if state.get('recording') or state.get('recording_pending'):
            return 'paused for recording'
        for child in children:
            try:
                args = child.cmdline()
                if any(Path(arg).name == 'streamlink' for arg in args[:3]) and '-o' in args:
                    return 'paused for recording'
                if args and Path(args[0]).name == 'ffmpeg' and child.status() != psutil.STATUS_STOPPED:
                    return 'waiting for recorder processing to yield'
            except psutil.Error:
                continue
        return None

    def announce(self, phase):
        if phase != self.phase:
            print('  ' + phase, flush=True)
            self.phase = phase

    def run(self, command, timeout):
        waiting = time.monotonic()
        while True:
            reason = self.blocked()
            if not reason:
                break
            self.announce(reason)
            if time.monotonic() - waiting > 86400:
                raise RuntimeError('Recorder stayed busy for a day; rerun when idle')
            time.sleep(.5)
        if self.used >= self.budget:
            raise RuntimeError('Benchmark active-time budget reached')
        elapsed = 0
        peak_rss = 0
        paused = False
        child = None
        with tempfile.TemporaryFile() as stderr:
            try:
                if sys.platform.startswith('linux'):
                    # Set a parent-death signal in a fresh interpreter, not preexec_fn
                    # (the lease heartbeat thread makes preexec_fn unsafe).
                    command = [sys.executable, '-c',
                        'import ctypes,os,signal,sys; '
                        'libc=ctypes.CDLL(None); '
                        'rc=libc.prctl(1,signal.SIGKILL); '
                        'sys.exit(125) if rc or os.getppid()!=int(sys.argv[1]) else None; '
                        'os.execvp(sys.argv[2],sys.argv[2:])', str(os.getpid())] + list(command)
                child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr)
                control = psutil.Process(child.pid)
                try:
                    control.nice(15)
                except psutil.Error:
                    pass
                last = time.monotonic()
                self.announce('encoding / measuring')
                while child.poll() is None:
                    now = time.monotonic()
                    if not paused:
                        delta = now-last; elapsed += delta; self.used += delta
                    last = now
                    reason = self.blocked()
                    try:
                        if reason and not paused:
                            control.suspend(); paused = True; waiting = now
                            self.announce(reason)
                        elif not reason and paused:
                            control.resume(); paused = False
                            self.announce('resumed')
                    except psutil.NoSuchProcess:
                        if child.poll() is None:
                            raise
                    if paused and now-waiting > 86400:
                        raise RuntimeError('Benchmark paused for a day')
                    if elapsed > timeout or self.used > self.budget:
                        raise RuntimeError('Trial time limit reached; try a shorter sample or faster preset')
                    try:
                        peak_rss = max(peak_rss, control.memory_info().rss)
                    except psutil.NoSuchProcess:
                        pass
                    time.sleep(.25)
                if not paused:
                    delta = time.monotonic()-last; elapsed += delta; self.used += delta
                stderr.seek(0, os.SEEK_END); stderr.seek(max(0, stderr.tell()-12000))
                log = stderr.read().decode(errors='replace')
                if child.returncode:
                    raise RuntimeError('FFmpeg failed: ' + log[-1200:])
                return dict(seconds=elapsed, peak_rss=peak_rss, log=log)
            finally:
                if child and child.poll() is None:
                    if paused:
                        try: psutil.Process(child.pid).resume()
                        except psutil.NoSuchProcess: pass
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill(); child.wait()


def encode_command(ffmpeg, source, output, offset, seconds, profile, threads):
    return [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'warning', '-threads', str(threads),
            '-ss', str(offset), '-i', str(source), '-t', str(seconds), '-map', '0:v:0', '-map', '0:a:0?',
            '-c:v', 'libx264', '-preset', profile['preset'], '-crf', str(profile['crf']),
            '-threads', str(threads), '-filter_threads', '1', '-c:a', 'aac', '-b:a', profile['audio_bitrate'],
            '-movflags', '+faststart', '-y', str(output)]


def quality_command(ffmpeg, source, output, offset, seconds, threads):
    return [ffmpeg, '-nostdin', '-hide_banner', '-threads', str(threads), '-ss', str(offset),
            '-t', str(seconds), '-i', str(source), '-threads', str(threads), '-i', str(output),
            '-filter_complex_threads', '1', '-filter_complex',
            '[0:v]setpts=PTS-STARTPTS[r];[1:v]setpts=PTS-STARTPTS[c];[r][c]ssim=shortest=1',
            '-an', '-f', 'null', '-']


def summarize(report):
    rows = []
    count = len(report['offsets'])
    for profile in report['profiles']:
        trials = [t for t in report['trials'] if t['profile']==profile['id'] and t.get('ok')]
        row = dict(profile, complete=len(trials)==count, samples=len(trials))
        if row['complete']:
            seconds = sum(t['encode_seconds'] for t in trials)
            sample_seconds = report['sample_seconds'] * count
            size = sum(t['bytes'] for t in trials) / sample_seconds * report['media']['duration']
            hours = seconds / sample_seconds * report['media']['duration'] / 3600
            row.update(estimated_bytes=size, savings=1-size/report['media']['size'], hours=hours,
                       min_ssim=min(t['ssim'] for t in trials), speed=sample_seconds/seconds,
                       peak_rss=max(t['peak_rss'] for t in trials),
                       score=max(0,report['media']['size']-size)/(1024**3)/max(hours,1e-9))
        rows.append(row)
    baseline = rows[0]
    reference = {t['sample']:t['ssim'] for t in report['trials']
                 if t['profile']==baseline['id'] and t.get('ok')}
    candidates = []
    for row in rows:
        matched_quality = all(t['ssim'] >= reference.get(t['sample'], 1)-.005
                              for t in report['trials'] if t['profile']==row['id'] and t.get('ok'))
        row['eligible'] = bool(not report.get('source_changed') and row['complete'] and baseline['complete'] and
            row['min_ssim'] >= report['min_ssim'] and row['savings'] >= report['min_savings'] and
            matched_quality)
        if row['eligible']: candidates.append(row)
    best = max(candidates, key=lambda r:r['score']) if candidates else None
    return rows, best


def save_report(directory, report):
    rows, best = summarize(report)
    report['summary'] = rows
    report['recommendation'] = best['id'] if best else None
    report['recommended_config'] = (dict(idle_compress_preset=best['preset'], idle_compress_crf=best['crf'],
        idle_compress_audio_bitrate=best['audio_bitrate']) if best else None)
    atomic_json(directory/'report.json', report)
    esc = html.escape
    body = ['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
        '<title>Pi compression benchmark</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;background:#101820;color:#e5edf3}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid #344653}a{color:#6cdbcb}code{white-space:pre-wrap}small{color:#bac8d2}</style>',
        '<h1>Compression benchmark</h1><p>'+esc(report['source'])+'</p>',
        '<p>Preliminary recommendation: <b>'+esc(best['id'] if best else 'No qualifying recommendation')+'</b></p>',
        '<p>Estimates from %s × %ss samples on %s. Short clips include startup/keyframe overhead; full-file results will differ. SSIM measures video similarity, not perceived quality or audio. Preview clips before changing settings.</p>' % (len(report['offsets']),report['sample_seconds'],esc(report['host'])),
        '<table><tr><th>Profile</th><th>Samples</th><th>Estimated hours</th><th>Estimated savings</th><th>Lowest SSIM</th><th>Peak RAM</th></tr>']
    for row in rows:
        values = [row['id'], '%s/%s'%(row['samples'],len(report['offsets'])),
            '%.1f'%row['hours'] if row['complete'] else '—',
            '%.1f%%'%(row['savings']*100) if row['complete'] else '—',
            '%.4f'%row['min_ssim'] if row['complete'] else '—',
            '%.0f MiB'%(row['peak_rss']/1024**2) if row['complete'] else '—']
        body.append('<tr>'+''.join('<td>'+esc(value)+'</td>' for value in values)+'</tr>')
    body += ['</table><h2>Suggested settings</h2><pre>'+esc(json.dumps(report['recommended_config'],indent=2))+'</pre>',
             '<p>Ranks estimated GiB saved per encoding hour, requiring at least %.0f%% savings and SSIM ≥ %.3f, within 0.005 of the baseline. Settings are never applied automatically.</p>'%(100*report['min_savings'],report['min_ssim']), '<h2>Preview samples and trial details</h2>']
    for trial in report['trials']:
        label=esc('%s — sample %s'%(trial['profile'],trial['sample']+1))
        body.append('<p>'+('<a href="'+esc(trial['clip'],quote=True)+'">'+label+'</a>' if trial.get('ok') else label+' — '+esc(trial.get('error','Incomplete')))+'</p>')
    body.append('<small>Run state: '+esc(report['state'])+'. Original recordings and recorder configuration are unchanged.</small>')
    (directory/'report.html').write_text('\n'.join(body))


def show(report):
    rows, best = summarize(report)
    print('\nPROFILE               SAMPLES    SPEED     EST.HOURS   EST.SAVED   MIN.SSIM')
    for row in rows:
        if row['complete']:
            print('%-21s %2s/%-3s %7.3fx %10.1f %10.1f%% %10.4f' % (row['id'],row['samples'],len(report['offsets']),row['speed'],row['hours'],100*row['savings'],row['min_ssim']))
        else: print('%-21s %2s/%-3s incomplete'%(row['id'],row['samples'],len(report['offsets'])))
    print('\nPreliminary recommendation: '+(best['id'] if best else 'none — insufficient results, quality or savings'))
    if best: print(json.dumps(report['recommended_config'],indent=2))
    print('Sample-based estimates; preview quality before changing settings.')


def recordings(base, config):
    root = Path(config.get('root_path', './recordings'))
    if not root.is_absolute(): root=base/root
    return sorted((p for p in (root/'processed').rglob('*.mp4') if not p.name.startswith('.')),
                  key=lambda p:p.stat().st_mtime, reverse=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--list',action='store_true')
    parser.add_argument('--report',nargs='?',const='latest')
    parser.add_argument('--seconds',type=float,default=3)
    parser.add_argument('--samples',type=int,default=2)
    parser.add_argument('--presets',default='fast,veryfast,superfast')
    parser.add_argument('--crfs',default=None)
    parser.add_argument('--trial-timeout',type=float,default=300)
    parser.add_argument('--budget',type=float,default=1800,help='Total active FFmpeg seconds, excluding recording pauses')
    parser.add_argument('--min-ssim',type=float,default=.95)
    parser.add_argument('--min-savings',type=float,default=.1)
    parser.add_argument('--standalone',action='store_true',help='For a separate host with no recorder process')
    args=parser.parse_args(argv);base=args.base.resolve()
    config=read_json(base/'config'/'config.json',None) or read_json(base/'config.json',{})
    runs=base/'.dashboard'/'benchmarks'
    if args.report:
        paths=sorted(runs.glob('*/report.json'))
        path=paths[-1] if args.report=='latest' and paths else Path(args.report)
        report=read_json(path)
        if not report: parser.error('No saved report found')
        show(report);print('Report:',path.with_suffix('.html'));return 0
    files=recordings(base,config)
    if args.list or (not args.source and sys.stdin.isatty()):
        for i,path in enumerate(files,1):print('%3d  %.2f GiB  %s'%(i,path.stat().st_size/1024**3,path.name))
        if args.list:return 0
        print('\nQuick comparison: two 3-second samples per profile; up to 30 active minutes.\nCurrent settings are included as the baseline. Ctrl+C stops and saves completed results.')
        try:
            choice=int(input('Recording number (0 to exit): ')) if files else 0
            if not 1 <= choice <= len(files):return 0
            args.source=files[choice-1]
        except (ValueError,IndexError):return 0
    if not args.source:parser.print_help();return 0
    presets=list(dict.fromkeys(p.strip() for p in args.presets.split(',')))
    try:crfs=[float(x) for x in args.crfs.split(',')] if args.crfs else [float(config.get('idle_compress_crf',28))]
    except ValueError:parser.error('CRFs must be comma-separated numbers')
    crfs=list(dict.fromkeys(crfs))
    if (not 0 < args.seconds <= 60 or not 1 <= args.samples <= 5 or not 0 < args.budget <= 86400 or
            not 0 < args.trial_timeout <= 7200 or not 0 <= args.min_ssim <= 1 or not 0 <= args.min_savings < 1 or
            len(presets)*len(crfs)>16 or any(p not in PRESETS for p in presets) or any(not 0<=c<=51 for c in crfs)):
        parser.error('Invalid sample limits, presets, CRFs or thresholds')
    source=args.source.resolve()
    if not source.is_file():parser.error('Source recording does not exist')
    ffmpeg=config.get('ffmpeg_path','ffmpeg')
    threads=max(1,int(config.get('processing_threads',1)))
    baseline=dict(id='current',preset=config.get('idle_compress_preset','medium'),crf=config.get('idle_compress_crf',28),audio_bitrate=config.get('idle_compress_audio_bitrate','128k'))
    profiles=[baseline]
    for preset in presets:
        for crf in crfs:
            if (preset,crf)==(baseline['preset'],baseline['crf']):continue
            profiles.append(dict(id='%s-crf%g'%(preset,crf),preset=preset,crf=crf,audio_bitrate=baseline['audio_bitrate']))
    if args.standalone:
        for p in psutil.process_iter(['cmdline']):
            if any(Path(x).name=='twitch-recorder.py' for x in (p.info['cmdline'] or [])[:3]):
                parser.error('A recorder is running on this host; use coordinated mode')
    directory=runs/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    directory.mkdir(parents=True,mode=0o700)
    runner=Runner(base,args.standalone,args.budget)
    report=None
    try:
        with BenchmarkLease(base):
            # Wait for production compression to pause before inspecting source media.
            until=time.monotonic()+60
            while True:
                reason=runner.blocked()
                if not reason:break
                runner.announce(reason)
                if time.monotonic()>until:raise RuntimeError('Recorder has not yielded; retry when idle')
                time.sleep(.5)
            media=probe(ffmpeg,source);signature=fingerprint(source)
            offsets=sample_offsets(media['duration'],args.seconds,args.samples)
            version=subprocess.check_output([ffmpeg,'-version'],timeout=15).decode().splitlines()[0]
            report=dict(schema=1,source=str(source),fingerprint=signature,media=media,host=platform.node(),
                        platform=platform.platform(),ffmpeg=version,threads=threads,profiles=profiles,
                        sample_seconds=args.seconds,offsets=offsets,min_ssim=args.min_ssim,min_savings=args.min_savings,
                        trials=[],state='running',started_at=time.time())
            save_report(directory,report)
            for profile in profiles:
                for index,offset in enumerate(offsets):
                    if runner.used>=args.budget:raise RuntimeError('Benchmark active-time budget reached')
                    if fingerprint(source)!=signature:
                        report['source_changed']=True
                        raise RuntimeError('Source changed during benchmark; results invalid')
                    if shutil.disk_usage(str(directory)).free < 1024**3:raise RuntimeError('Less than 1 GiB working space available')
                    clip=directory/('%s-%s.mp4'%(profile['id'],index+1))
                    trial=dict(profile=profile['id'],sample=index,offset=offset,clip=clip.name,ok=False)
                    print('\nTesting %s, sample %s/%s at %.1fs'%(profile['id'],index+1,len(offsets),offset),flush=True)
                    try:
                        encoded=runner.run(encode_command(ffmpeg,source,clip,offset,args.seconds,profile,threads),args.trial_timeout)
                        output=probe(ffmpeg,clip)
                        if abs(output['duration']-args.seconds)>max(.2,args.seconds*.05):raise RuntimeError('Sample duration mismatch')
                        quality=runner.run(quality_command(ffmpeg,source,clip,offset,args.seconds,threads),args.trial_timeout)
                        scores=re.findall(r'All:([0-9.]+)',quality['log'])
                        if not scores:raise RuntimeError('SSIM unavailable; cannot recommend this profile')
                        score=float(scores[-1])
                        if not math.isfinite(score) or not 0<=score<=1:raise RuntimeError('Invalid SSIM result')
                        trial.update(ok=True,encode_seconds=encoded['seconds'],peak_rss=encoded['peak_rss'],bytes=clip.stat().st_size,ssim=score)
                    except (RuntimeError,ValueError,subprocess.SubprocessError) as exc:
                        trial['error']=str(exc);print('  '+str(exc),flush=True)
                        if clip.exists():clip.unlink()
                    report['trials'].append(trial);report['active_seconds']=runner.used
                    if fingerprint(source)!=signature:
                        report['source_changed']=True
                        raise RuntimeError('Source changed during benchmark; results invalid')
                    save_report(directory,report)
            report['state']='complete'
    except KeyboardInterrupt:
        if report:report['state']='interrupted'
        print('\nStopped; completed results retained.',flush=True)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        if report:report['state']='stopped';report['error']=str(exc)
        print('Benchmark stopped: '+str(exc),file=sys.stderr)
    if report:
        save_report(directory,report);show(report);print('Report:',directory/'report.html',flush=True)
        return 0 if report['state']=='complete' else 1
    return 1


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(main())
