# Compression benchmark companion

Run on the Pi as the same user as the recorder (`pi`):

```bash
cd /home/pi/twitch-recoder/twitch-stream-recorder
./scripts/benchmark.sh
```

Select a processed recording from the numbered list. The quick comparison tests
current settings plus `fast`, `veryfast` and `superfast`, using two three-second
samples spread through the recording. CRF and audio bitrate initially match the
recorder configuration. Results are saved after every trial. Ctrl+C stops safely.

```bash
./scripts/benchmark.sh --list
./scripts/benchmark.sh --source '/full/path/to/recording.mp4' --presets fast,veryfast --crfs 26,28 --seconds 3 --samples 2
./scripts/benchmark.sh --report
```

The default budget is 30 minutes of active FFmpeg work, including quality checks;
each encode or quality check has a five-minute limit. Recording pauses do not
consume those limits. Use `--budget` and `--trial-timeout` (seconds) to adjust them.
Shorter samples help on a Pi Zero; use longer samples and several different
broadcasts before adopting a setting. Saved reports are in
`.dashboard/benchmarks/<run>/report.json` and `report.html`; keep the adjacent MP4
samples with the HTML when copying it elsewhere. Old runs can be removed manually
once their results are no longer needed. No recordings or config values are changed.

## What recommendations mean

The app compares encode time, sample output size, peak encoder RAM, and FFmpeg
SSIM video similarity against the source at the same timestamps. It estimates
whole-recording size and active encoding hours, then ranks estimated GiB saved per
encoding hour. A recommendation requires:

- Every requested sample completed for both that profile and the current baseline.
- At least 10% estimated storage savings (`--min-savings 0.1`).
- SSIM at least 0.95 for every sample (`--min-ssim 0.95`).
- No sample more than 0.005 below the corresponding baseline sample's SSIM.
- An unchanged source file throughout the comparison.

These are configurable heuristics, not a perceptual quality guarantee. SSIM does
not measure audio quality. Preview the generated clips, especially motion, faces,
text and dark scenes. Startup/keyframe/container overhead makes very short tests
noisy; estimates are preliminary. An already compressed source tests an additional
generation of compression. Prefer representative originals awaiting compression.
The default objective is storage savings per unit of encoding time; it does not
optimize for the smallest possible archive or promise a completion deadline.

## Recorder coordination

A heartbeat lease in RAM temporarily pauses production compression. The dashboard
shows **Paused for benchmark**. The companion waits for recorder processing to
yield, and pauses its own FFmpeg process when recording starts or recorder health
becomes uncertain. It checks local status and child processes; it makes no Twitch
requests. Exiting releases the lease so production compression resumes from its
existing process. If the companion crashes, the lease expires within 15 seconds;
on Linux its active FFmpeg child also receives a parent-death kill signal.

The recorder needs the coordination update. Installing it requires one service
restart; an existing compression job restarts from the beginning on that update.
Subsequent benchmark sessions preserve the encoder's in-memory progress.
A benchmark initially waits up to one minute for an idle recorder. Mid-run recording
pauses can last up to one day. A second benchmark is refused while one holds the
lease. A stopped recorder needs to be restarted before coordinated mode proceeds.

`--standalone` is for a separate computer without a running recorder. Measurements
on a Mac do not predict Pi performance. Match CPU quotas and cooling when comparing
runs; the regular launcher does not inherit the recorder's systemd CPU cap.
For this Pi's 80% cap, an optional equivalent launch is:

```bash
sudo systemd-run --scope -p CPUQuota=80% sudo -u pi \
  ./scripts/benchmark.sh --source '/full/path/to/recording.mp4'
```

This temporarily creates a systemd scope, without changing recorder settings.
Run outside other CPU-heavy work and watch the dashboard's temperature/health.
A report reflects the measured host and conditions, not a universal preset ranking.

FFmpeg documents the [SSIM filter](https://ffmpeg.org/ffmpeg-filters.html#ssim).
