# Recorder TUI

SSH into the Pi and launch the consolidated dashboard:

```bash
ssh pi@raspberrypi.local
dashboard
```

The repository launcher is `./scripts/dashboard.sh`. The former live and watch
launchers open the same TUI; `dashboard-watch.sh 10` still sets a ten-second refresh.

| View | What it shows |
| --- | --- |
| 1 Overview | Recorder and EventSub status, active Streamlink/FFmpeg jobs, actual output growth, recent files and events |
| 2 Recordings | Broadcast sessions and individual files, searchable titles, channel and stage filters, full paths and joined parts |
| 3 Activity | Latest dated recorder log, newest entries first |
| 4 Health | CPU, RAM, disk, temperature, uptime, heartbeat age, EventSub message age, reconnect failures and active fallback |
| 5 Queue | Active and waiting tasks, reasons, retry attempts and temporary space requirements |
| 6 History | Persistent recorder outcomes, reconnects, failures and command results |

Use **1–6** or **Tab** to change views; **Up/Down** or **j/k** to select and
scroll; **PgUp/PgDn** to move a page. In Recordings, **/** searches,
**c** cycles channels, **g** cycles stages, **s** switches newest/largest sorting,
and **Enter** opens file details. **Esc** closes details or clears search.
**b** toggles broadcast sessions and individual files. In individual-file mode,
**p** requests a path copy using terminal clipboard support, and **R** queues a
retry for the selected quarantined file. The recorder accepts retries only from
its configured failed folders, preserves session metadata, and refuses existing
destination files. Requests wait while recording or background processing is
active, expire after 24 hours, and appear in history when handled.
**D** exports a diagnostic JSON report to `.dashboard/reports/`.

**r** refreshes immediately, **Space** pauses the display, **?** shows help,
and **q** exits.

Status refreshes every two seconds, and the display redraws only for new data,
keys or terminal resizing. The recorder publishes an atomic heartbeat every five
seconds in `/dev/shm` on the Pi; catalog scans happen every thirty seconds or after
recording/processing transitions. The TUI uses that catalog and heartbeat when
fresh and matching the current recorder PID. It falls back to processes, logs and
cached scans if telemetry is missing, stale or from a previous service instance.
This dashboard makes no Twitch API requests.

EventSub allows five seconds of arrival grace beyond Twitch's negotiated keepalive
timeout (normally 30 seconds), to avoid reconnecting for minor network or Pi
scheduling delays. Longer silence triggers a reconnect and enables fallback
polling at the configured interval. Timeout logs include the measured silence and
deadline. A successful reconnect checks for broadcasts missed during the outage;
routine polling stays suspended while EventSub is connected.

FFmpeg progress is read continuously from its progress pipe with bounded memory.
Percent and approximate ETA use actual media time and cached source duration;
unknown durations show bytes written without an invented percentage. Media
progress can reach 100% before MP4 finalization and validation finish. Working
space estimates are conservative and show the next task's temporary output needs.
Alerts highlight stale heartbeats, stalled output/media progress, notification
reconnect failures, low disk/RAM and quarantined recordings.

Idle compression pauses the existing FFmpeg process when any recording reserves
a slot, preserving its current encoding progress and temporary output. Once
recordings finish, newly captured footage is remuxed and eligible parts are
stitched before that same encoder resumes. The Overview phase and Queue show
paused compression; expected pauses do not raise stalled-output alerts. ETA uses
active encoding time, excluding recording pauses.

Compression has no fixed total runtime limit: it can use idle periods lasting
days. `idle_compress_stall_timeout_seconds` defaults to 1800 (30 minutes) and
stops an encoder only when frames, media time and output bytes all stop advancing
while it is running. Paused time is excluded, and resuming starts a fresh watchdog
window. Remuxing and stitching retain their existing timeouts.

Pause/resume keeps the encoder's state in memory and retains its RAM allocation.
A recorder service restart, crash or Pi reboot restarts the unfinished file from
the beginning. The original recording is replaced only after the complete output
passes duration validation and is smaller; incomplete output is never published.

Broadcasts group by session ID, explicit stitched-part manifests, or matching
legacy titles and measured time gaps. Grouping is for browsing; the recorder still
checks timing and codec compatibility before stitching. Queue entries describe
candidate work; they are not a guaranteed execution order. Originals and stitched
outputs are retained separately and their storage is counted separately.

History and media metadata are stored in `.dashboard/history.sqlite`. History
keeps the latest 1,000 events (the TUI displays the latest 100). Media cache entries
invalidate when a file's inode, size or modification time changes. Legacy
processed files are probed one at a time, at least ten seconds apart, while idle.
Warnings and errors are retained along with recording/processing outcomes; history
begins when this telemetry version is installed. Diagnostic reports omit recorder
credentials and redact token values in log text.

Displayed times default to America/New_York and can be changed with
`dashboard_timezone` in recorder config. File dates are filesystem modification
times, labeled Updated. Source log timestamps retain the Pi's log timezone.

For a one-time snapshot or scriptable JSON:

```bash
./scripts/dashboard.sh --once
./scripts/dashboard.sh --json
./scripts/dashboard.sh --refresh 5
./scripts/dashboard.sh --export
```

A noninteractive invocation automatically prints a snapshot. The interactive TUI
requires a terminal at least 60 columns by 12 rows and uses Python's standard
`curses` module plus the project's existing `psutil` dependency. If launching
remotely in one command, allocate a terminal:

```bash
ssh -t pi@raspberrypi.local /home/pi/twitch-recoder/twitch-stream-recorder/scripts/dashboard.sh
```

The [benchmark companion](COMPRESSION-BENCHMARK.md) compares compression settings
on short samples. While it runs, the dashboard shows **Paused for benchmark** for
production compression. Recordings retain priority over both jobs.
